"""服务层：理赔流程编排、状态守卫、审计与查询。

数据落库走 data.Database，证据核验的判定逻辑走 rules.py 纯函数；
本层只负责事务边界、角色校验、状态机与待核验/锁定流程。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from data import DEFAULT_DB, Database, DomainError, rows_to_dicts
from rules import (
    check_geo_evidence,
    coordinate,
    find_cross_device,
    haversine_km,
    normalize_time,
    parse_time,
    radius_km,
)

TERMINAL = {"duplicate", "approved", "rejected", "closed"}
TRANSITIONS = {
    "received": {"triaged"},
    "triaged": {"assigned", "escalated"},
    "assigned": {"survey", "escalated"},
    "survey": {"review", "escalated"},
    "review": {"approved", "rejected", "escalated"},
    "escalated": {"assigned", "review", "rejected"},
}
EVIDENCE_PENDING = "pending"
EVIDENCE_VERIFIED = "verified"
EVIDENCE_REJECTED = "rejected"
# 对外接口使用的核验决定：确认通过 / 驳回
VERIFY_DECISION_MAP = {"confirmed": EVIDENCE_VERIFIED, "rejected": EVIDENCE_REJECTED}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def actor_id(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


class CatastropheClaimService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db = Database(db_path)

    @property
    def db_path(self) -> str:
        return self.db.db_path

    def connect(self):
        return self.db.connect()

    def _audit(self, conn, claim_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(claim_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (claim_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utc_now()),
        )

    def _claim(self, conn, claim_id: int):
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise DomainError("理赔案件不存在", 404)
        return row

    def _zone_for(self, conn, event_id: str):
        return conn.execute("SELECT * FROM disaster_zones WHERE event_id=?", (event_id,)).fetchone()

    # ------------------------------------------------------------------ 分级/灾区登记

    def register_zone(self, actor: str, role: str, event_id: str, name: str,
                      latitude: float, longitude: float, radius: float,
                      disaster_time: str) -> dict[str, Any]:
        """主管登记（或更新）灾情的灾区中心、灾情时刻与有效半径。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "登记灾区")
        event_id = str(event_id or "").strip()
        name = str(name or "").strip()
        if not event_id or not name:
            raise DomainError("灾情编号和灾区名称不能为空")
        lat = coordinate(latitude, "灾区中心纬度", -90, 90)
        lon = coordinate(longitude, "灾区中心经度", -180, 180)
        radius = radius_km(radius)
        disaster_ts = normalize_time(disaster_time, "灾情时刻")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            conn.execute(
                """INSERT INTO disaster_zones(event_id,name,latitude,longitude,radius_km,disaster_time,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET
                     name=excluded.name, latitude=excluded.latitude, longitude=excluded.longitude,
                     radius_km=excluded.radius_km, disaster_time=excluded.disaster_time, updated_at=excluded.updated_at""",
                (event_id, name, lat, lon, radius, disaster_ts, actor, now, now),
            )
            row = conn.execute("SELECT * FROM disaster_zones WHERE event_id=?", (event_id,)).fetchone()
            self._audit(conn, None, actor, "zone.registered", {
                "event_id": event_id, "name": name, "latitude": lat, "longitude": lon,
                "radius_km": radius, "disaster_time": disaster_ts,
            })
            return dict(row)

    def create_claim(self, actor: str, role: str, claim_no: str, event_id: str,
                     region: str, peril_type: str, policy_no: str, claimant_ref: str,
                     latitude: float, longitude: float, estimated_loss: float,
                     urgent_need: bool = False, lodging_required: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "supervisor"}, "创建报案")
        values = [claim_no, event_id, region, peril_type, policy_no, claimant_ref]
        if not all(str(v).strip() for v in values):
            raise DomainError("案件必需字段不能为空")
        lat = coordinate(latitude, "纬度", -90, 90)
        lon = coordinate(longitude, "经度", -180, 180)
        try:
            estimated_loss = float(estimated_loss)
        except (TypeError, ValueError) as exc:
            raise DomainError("预估损失必须是数值") from exc
        if estimated_loss < 0:
            raise DomainError("预估损失不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            duplicate_of = None
            candidates = conn.execute(
                """SELECT * FROM claims WHERE event_id=? AND policy_no=? AND status<>'duplicate'
                   ORDER BY id DESC LIMIT 50""",
                (event_id.strip(), policy_no.strip()),
            ).fetchall()
            for row in candidates:
                within_time = abs((datetime.fromisoformat(now) - datetime.fromisoformat(row["created_at"])).total_seconds()) <= 172800
                loss_close = abs(row["estimated_loss"] - estimated_loss) <= max(1000.0, row["estimated_loss"] * 0.1)
                if within_time and loss_close and haversine_km(lat, lon, row["latitude"], row["longitude"]) <= 3.0:
                    duplicate_of = row["id"]
                    break
            status = "duplicate" if duplicate_of else "received"
            try:
                cur = conn.execute(
                    """INSERT INTO claims(claim_no,event_id,region,peril_type,policy_no,claimant_ref,latitude,longitude,
                       estimated_loss,urgent_need,lodging_required,status,duplicate_of,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_no.strip(), event_id.strip(), region.strip(), peril_type.strip(), policy_no.strip(),
                     claimant_ref.strip(), lat, lon, estimated_loss, int(bool(urgent_need)), int(bool(lodging_required)),
                     status, duplicate_of, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("报案编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "claim.created", {"duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "claim.duplicate_detected", {"new_claim": claim_no.strip()})
            return dict(self._claim(conn, cur.lastrowid))

    def triage_claim(self, actor: str, role: str, claim_id: int, expected_version: int,
                     fraud_score: float = 0.0, remote_review: bool = False,
                     zone_latitude: float | None = None, zone_longitude: float | None = None,
                     zone_radius_km: float | None = None, disaster_time: str | None = None,
                     zone_name: str | None = None) -> dict[str, Any]:
        """主管分级。同时传齐四个灾区参数时，顺带登记/更新该灾情的灾区与有效期。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "案件分级")
        try:
            fraud_score = float(fraud_score)
        except (TypeError, ValueError) as exc:
            raise DomainError("欺诈评分必须是数值") from exc
        if not 0 <= fraud_score <= 1:
            raise DomainError("欺诈评分应在 0 到 1 之间")
        zone_fields = (zone_latitude, zone_longitude, zone_radius_km, disaster_time)
        zone_provided = [v is not None and str(v) != "" for v in zone_fields]
        if any(zone_provided) and not all(zone_provided):
            raise DomainError("登记灾区需同时提供灾区中心经纬度、半径和灾情时刻")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "received":
                raise DomainError("只有待分级案件可以分级", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if all(zone_provided):
                self._upsert_zone(conn, claim["event_id"], zone_name or claim["region"],
                                  zone_latitude, zone_longitude, zone_radius_km, disaster_time, actor)
            priority = min(100.0, claim["estimated_loss"] / 100000.0 * 25 + (40 if claim["urgent_need"] else 0) + fraud_score * 20 + (10 if claim["lodging_required"] else 0))
            new_status = "escalated" if fraud_score >= 0.8 else "triaged"
            conn.execute(
                "UPDATE claims SET fraud_score=?,priority_score=?,remote_review=?,status=?,version=version+1,updated_at=? WHERE id=?",
                (fraud_score, priority, int(bool(remote_review)), new_status, utc_now(), claim_id),
            )
            self._audit(conn, claim_id, actor, "claim.triaged", {"priority": priority, "status": new_status,
                                                                  "zone_registered": all(zone_provided)})
            return dict(self._claim(conn, claim_id))

    def _upsert_zone(self, conn, event_id: str, name: str, latitude: Any, longitude: Any,
                     radius: Any, disaster_time: Any, actor: str) -> dict[str, Any]:
        lat = coordinate(latitude, "灾区中心纬度", -90, 90)
        lon = coordinate(longitude, "灾区中心经度", -180, 180)
        km = radius_km(radius)
        disaster_ts = normalize_time(disaster_time, "灾情时刻")
        now = utc_now()
        conn.execute(
            """INSERT INTO disaster_zones(event_id,name,latitude,longitude,radius_km,disaster_time,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 name=excluded.name, latitude=excluded.latitude, longitude=excluded.longitude,
                 radius_km=excluded.radius_km, disaster_time=excluded.disaster_time, updated_at=excluded.updated_at""",
            (event_id, str(name or event_id).strip(), lat, lon, km, disaster_ts, actor, now, now),
        )
        return dict(conn.execute("SELECT * FROM disaster_zones WHERE event_id=?", (event_id,)).fetchone())

    def assign_claim(self, actor: str, role: str, claim_id: int, assignee: str,
                     expected_version: int, surveyor: str | None = None) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "分配案件")
        assignee = assignee.strip()
        if not assignee:
            raise DomainError("查勘负责人不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"triaged", "escalated", "assigned"}:
                raise DomainError("当前状态不能分配", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            conn.execute(
                """UPDATE claims SET assignee=?,surveyor=?,status='assigned',version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (assignee, surveyor.strip() if surveyor else None, utc_now(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.assigned", {"assignee": assignee, "surveyor": surveyor})
            return dict(self._claim(conn, claim_id))

    # ------------------------------------------------------------------ 现场证据补录

    def add_evidence(self, actor: str, role: str, claim_id: int, sha256: str,
                     filename: str, source: str, latitude: float | None = None,
                     longitude: float | None = None, captured_at: str | None = None,
                     device_id: str | None = None) -> dict[str, Any]:
        """补录证据。

        补录现场证据需同时给齐经纬度、拍摄时间和设备号；四项缺一（只给一部分）按参数错误拒绝。
        仅给哈希（历史离线补传）时保持旧行为，默认直接视为已核验。
        带定位的证据命中任一规则（灾区外 / 早于灾情 / 同设备 60 分钟内 100 公里以上跳变）
        即进入待核验，主管确认前案件无法提交核损。
        """
        actor = actor_id(actor)
        require_role(role, {"intake", "adjuster", "surveyor", "supervisor"}, "添加损失证据")
        sha256 = sha256.strip().lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise DomainError("证据哈希必须是 64 位 SHA-256 十六进制")
        if not filename.strip() or not source.strip():
            raise DomainError("证据文件名和来源不能为空")
        geo_fields = (latitude, longitude, captured_at, device_id)
        geo_present = [v is not None and str(v) != "" for v in geo_fields]
        if any(geo_present) and not all(geo_present):
            raise DomainError("现场证据需同时补录经纬度、拍摄时间和设备号")
        lat = lon = None
        captured_ts = None
        device = None
        captured_dt = None
        if all(geo_present):
            lat = coordinate(latitude, "证据纬度", -90, 90)
            lon = coordinate(longitude, "证据经度", -180, 180)
            captured_dt = parse_time(captured_at, "拍摄时间")
            captured_ts = captured_dt.isoformat(timespec="seconds")
            device = str(device_id).strip()
            if not device:
                raise DomainError("设备号不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] in TERMINAL:
                raise DomainError("已结束案件不能添加证据", 409)
            existing = conn.execute("SELECT * FROM evidence WHERE claim_id=? AND sha256=?", (claim_id, sha256)).fetchone()
            if existing:
                return dict(existing)
            status = EVIDENCE_VERIFIED
            locked = 1
            reasons: list[str] = []
            cross_hits: list[dict[str, Any]] = []
            if all(geo_present):
                # 默认先待核验，核验通过才锁定，避免漏过任何规则
                status, locked = EVIDENCE_PENDING, 0
                zone = self._zone_for(conn, claim["event_id"])
                reasons = check_geo_evidence(lat, lon, captured_dt, zone)
                prior = conn.execute(
                    """SELECT id,claim_id,device_id,captured_at,latitude,longitude FROM evidence
                       WHERE device_id=? AND captured_at IS NOT NULL
                         AND latitude IS NOT NULL AND longitude IS NOT NULL
                         AND status<>'rejected'
                       ORDER BY captured_at""",
                    (device,),
                ).fetchall()
                cross_hits = find_cross_device(prior, device, captured_dt, lat, lon)
                if cross_hits:
                    reasons.append(
                        "同一设备 %s 在 %d 分钟内于相距 %.1f 公里（≥100 公里）的其他现场出现"
                        % (device, cross_hits[0]["gap_minutes"], cross_hits[0]["distance_km"])
                    )
                if not reasons:
                    status, locked = EVIDENCE_VERIFIED, 1
            cur = conn.execute(
                """INSERT INTO evidence(claim_id,sha256,filename,source,submitter,status,
                   latitude,longitude,captured_at,device_id,locked,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (claim_id, sha256, filename.strip(), source.strip(), actor, status,
                 lat, lon, captured_ts, device, locked, utc_now()),
            )
            evidence_id = cur.lastrowid
            # 跨案件同哈希批量复用（原有规则，与地理核验并存）
            hash_claims = [r["claim_id"] for r in conn.execute(
                "SELECT DISTINCT claim_id FROM evidence WHERE sha256=?", (sha256,)
            ).fetchall()]
            suspicious = len(hash_claims) >= 3
            if suspicious:
                for cid in hash_claims:
                    conn.execute(
                        "UPDATE claims SET fraud_score=MAX(fraud_score,0.95),status='escalated',version=version+1,updated_at=? WHERE id=? AND status<>'duplicate'",
                        (utc_now(), cid),
                    )
                self._audit(conn, claim_id, actor, "evidence.bulk_reuse_detected", {"sha256": sha256, "claim_ids": hash_claims})
            self._audit(conn, claim_id, actor, "evidence.added", {
                "evidence_id": evidence_id, "suspicious": suspicious,
                "geo_verified": all(geo_present), "status": status, "reasons": reasons,
            })
            if status == EVIDENCE_PENDING:
                self._audit(conn, claim_id, actor, "evidence.pending_review", {
                    "evidence_id": evidence_id, "reasons": reasons, "cross_device_hits": cross_hits,
                })
            row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            return {"evidence": dict(row), "bulk_reuse": suspicious, "affected_claims": hash_claims,
                    "pending": status == EVIDENCE_PENDING, "reasons": reasons, "cross_device_hits": cross_hits}

    # ------------------------------------------------------------------ 主管核验

    def verify_evidence(self, actor: str, role: str, evidence_id: int, decision: str,
                        reason: str = "") -> dict[str, Any]:
        """主管确认/驳回待核验证据。确认通过即锁定；驳回必须写明原因，原始证据保留。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "核验现场证据")
        decision = (decision or "").strip()
        if decision not in VERIFY_DECISION_MAP:
            raise DomainError("核验决定必须是 confirmed（确认通过）或 rejected（驳回）")
        new_status = VERIFY_DECISION_MAP[decision]
        reason = (reason or "").strip()
        if decision == "rejected" and not reason:
            raise DomainError("驳回证据必须写明原因")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if not row:
                raise DomainError("证据不存在", 404)
            if row["status"] != EVIDENCE_PENDING:
                raise DomainError("只有待核验证据可以确认或驳回", 409)
            conn.execute(
                "UPDATE evidence SET status=?,review_reason=?,reviewed_by=?,reviewed_at=?,locked=1 WHERE id=?",
                (new_status, reason or None, actor, utc_now(), evidence_id),
            )
            self._audit(conn, row["claim_id"], actor, "evidence.verified" if new_status == EVIDENCE_VERIFIED else "evidence.rejected", {
                "evidence_id": evidence_id, "reason": reason,
            })
            return dict(conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone())

    def pending_evidence(self, role: str) -> list[dict[str, Any]]:
        """主管待核验清单（含案件与灾区信息，供页面展示）。"""
        require_role(role, {"supervisor", "auditor"}, "查看待核验清单")
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT e.*, c.claim_no, c.event_id, c.region,
                          z.latitude AS zone_latitude, z.longitude AS zone_longitude,
                          z.radius_km, z.disaster_time
                   FROM evidence e
                   JOIN claims c ON c.id = e.claim_id
                   LEFT JOIN disaster_zones z ON z.event_id = c.event_id
                   WHERE e.status=?
                   ORDER BY e.created_at""",
                (EVIDENCE_PENDING,),
            ).fetchall()
            result = rows_to_dicts(rows)
        for item in result:
            if item.get("zone_latitude") is not None:
                item["distance_km"] = round(
                    haversine_km(item["latitude"], item["longitude"],
                                 item["zone_latitude"], item["zone_longitude"]), 1)
            else:
                item["distance_km"] = None
        return result

    # ------------------------------------------------------------------ 查勘与核损

    def record_survey(self, actor: str, role: str, claim_id: int, damage_ratio: float,
                      findings: str, recommendation: str, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "录入查勘结果")
        try:
            damage_ratio = float(damage_ratio)
        except (TypeError, ValueError) as exc:
            raise DomainError("损失比例必须是数值") from exc
        if not 0 <= damage_ratio <= 1 or not findings.strip() or not recommendation.strip():
            raise DomainError("损失比例或查勘内容无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"assigned", "escalated"}:
                raise DomainError("当前状态不能录入查勘", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["assignee"] != actor and claim["surveyor"] != actor:
                raise DomainError("只有被分配的查勘人员可以录入结果", 403)
            if claim["status"] == "escalated" and claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件须先完成复核降险，不能直接提交查勘", 409)
            conn.execute(
                "INSERT INTO survey_notes(claim_id,surveyor,damage_ratio,findings,recommendation,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, actor, damage_ratio, findings.strip(), recommendation.strip(), utc_now()),
            )
            conn.execute("UPDATE claims SET status='survey',version=version+1,updated_at=? WHERE id=?", (utc_now(), claim_id))
            self._audit(conn, claim_id, actor, "survey.recorded", {"damage_ratio": damage_ratio, "recommendation": recommendation})
            return dict(self._claim(conn, claim_id))

    def submit_review(self, actor: str, role: str, claim_id: int, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "提交核损")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "survey":
                raise DomainError("只有已查勘案件可以提交核损", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            # 主管确认前案件不能提交核损：存在待核验证据直接拦截
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence WHERE claim_id=? AND status=?",
                (claim_id, EVIDENCE_PENDING),
            ).fetchone()["c"]
            if pending:
                raise DomainError("有 %d 条现场证据待主管核验，确认或驳回后才能提交核损" % pending, 409)
            if not conn.execute("SELECT 1 FROM survey_notes WHERE claim_id=?", (claim_id,)).fetchone():
                raise DomainError("缺少查勘记录", 409)
            conn.execute("UPDATE claims SET status='review',version=version+1,updated_at=? WHERE id=?", (utc_now(), claim_id))
            self._audit(conn, claim_id, actor, "claim.review_submitted", {"blocked_pending": pending})
            return dict(self._claim(conn, claim_id))

    def emergency_advance(self, actor: str, role: str, claim_id: int, amount: float,
                          expected_version: int, reference: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "批准紧急预付")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise DomainError("预付金额必须是数值") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not claim["urgent_need"]:
                raise DomainError("非紧急案件不能预付", 409)
            if claim["status"] in {"duplicate", "approved", "rejected", "closed"}:
                raise DomainError("当前案件状态不能预付", 409)
            if claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件不能预付", 409)
            limit = claim["estimated_loss"] * 0.2
            if amount <= 0 or amount > limit:
                raise DomainError("预付金额必须大于0且不超过预估损失的20%", 409)
            if claim["emergency_advance"] + amount > limit:
                raise DomainError("累计预付超过上限", 409)
            try:
                conn.execute(
                    "INSERT INTO payments(claim_id,kind,amount,approved_by,reference,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, "emergency_advance", amount, actor, reference.strip(), utc_now()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("付款参考号已存在", 409) from exc
            conn.execute("UPDATE claims SET emergency_advance=emergency_advance+?,version=version+1,updated_at=? WHERE id=?", (amount, utc_now(), claim_id))
            self._audit(conn, claim_id, actor, "payment.emergency_advance", {"amount": amount, "reference": reference})
            return dict(self._claim(conn, claim_id))

    def finalize_claim(self, actor: str, role: str, claim_id: int, decision: str,
                       payout: float, expected_version: int, reason: str = "") -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "最终核定")
        if decision not in {"approve", "reject"}:
            raise DomainError("核定决定无效")
        try:
            payout = float(payout)
        except (TypeError, ValueError) as exc:
            raise DomainError("核定金额必须是数值") from exc
        if payout < 0:
            raise DomainError("核定金额不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "review":
                raise DomainError("只有待复核案件可以最终核定", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["duplicate_of"]:
                raise DomainError("重复报案不能核定赔付", 409)
            if claim["fraud_score"] >= 0.8 and decision == "approve":
                raise DomainError("高风险案件未解除风险标记，不能赔付", 409)
            if decision == "approve" and payout > claim["estimated_loss"]:
                raise DomainError("核定金额不能超过预估损失", 409)
            if decision == "reject" and not reason.strip():
                raise DomainError("拒赔必须填写理由", 409)
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE claims SET status=?,final_payout=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (status, payout if decision == "approve" else 0, utc_now(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.finalized", {"decision": decision, "payout": payout, "reason": reason.strip()})
            return dict(self._claim(conn, claim_id))

    # ------------------------------------------------------------------ 查询

    def _evidence_summary(self, conn, claim_ids: list[int]) -> dict[int, dict[str, int]]:
        if not claim_ids:
            return {}
        marks = ",".join("?" for _ in claim_ids)
        rows = conn.execute(
            "SELECT claim_id,status,COUNT(*) AS c FROM evidence WHERE claim_id IN (%s) GROUP BY claim_id,status" % marks,
            claim_ids,
        ).fetchall()
        summary: dict[int, dict[str, int]] = {cid: {"pending": 0, "verified": 0, "rejected": 0, "total": 0} for cid in claim_ids}
        for r in rows:
            bucket = summary.setdefault(r["claim_id"], {"pending": 0, "verified": 0, "rejected": 0, "total": 0})
            bucket[r["status"]] = r["c"]
            bucket["total"] += r["c"]
        return summary

    def queue(self, role: str = "viewer", actor: str = "") -> list[dict[str, Any]]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看理赔队列", 403)
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE (assignee=? OR surveyor=?) AND status NOT IN ('approved','rejected','duplicate') ORDER BY priority_score DESC,created_at",
                    (actor, actor),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,created_at").fetchall()
            claims = rows_to_dicts(rows)
            summary = self._evidence_summary(conn, [c["id"] for c in claims])
        for c in claims:
            c["evidence_summary"] = summary.get(c["id"], {"pending": 0, "verified": 0, "rejected": 0, "total": 0})
        return claims

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        allowed = role in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}
        if not allowed:
            return {"claims": [], "evidence": [], "payments": [], "timeline": [], "zones": [], "access_limited": True}
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                claims = rows_to_dicts(conn.execute(
                    "SELECT * FROM claims WHERE assignee=? OR surveyor=? ORDER BY priority_score DESC,id DESC", (actor, actor)
                ).fetchall())
            else:
                claims = rows_to_dicts(conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,id DESC").fetchall())
            ids = [c["id"] for c in claims]
            if ids:
                marks = ",".join("?" for _ in ids)
                evidence = rows_to_dicts(conn.execute("SELECT * FROM evidence WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall())
                payments = rows_to_dicts(conn.execute("SELECT * FROM payments WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall())
                timeline = rows_to_dicts(conn.execute("SELECT * FROM timeline WHERE claim_id IN (%s) ORDER BY id DESC LIMIT 300" % marks, ids).fetchall())
            else:
                evidence, payments, timeline = [], [], []
            zones = rows_to_dicts(conn.execute("SELECT * FROM disaster_zones ORDER BY event_id").fetchall())
            summary = self._evidence_summary(conn, ids)
        for c in claims:
            c["evidence_summary"] = summary.get(c["id"], {"pending": 0, "verified": 0, "rejected": 0, "total": 0})
        return {"claims": claims, "evidence": evidence, "payments": payments,
                "timeline": timeline, "zones": zones, "access_limited": False}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM claims").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        c1 = self.create_claim("intake-demo", "intake", "CLM-DEMO-001", "TY2026", "沿海A区", "洪水", "P-1001", "R-01", 30.1, 121.2, 500000, True, True)
        self.create_claim("intake-demo", "intake", "CLM-DEMO-002", "TY2026", "沿海A区", "洪水", "P-1002", "R-02", 30.2, 121.3, 240000, False, False)
        return {"seeded": True, "first_claim_id": c1["id"]}
