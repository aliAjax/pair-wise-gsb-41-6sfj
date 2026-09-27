"""规则层：现场证据核验的纯业务规则。

所有函数都是无副作用的纯函数，不读写数据库，便于单独测试：
- 灾区位置 / 有效期：haversine 距离判断是否落在灾区半径内，拍摄时间不得早于灾情时刻；
- 同设备跨区：同一设备号 60 分钟内在相距 100 公里以上的两个现场出现即判异常。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from data import DomainError

CROSS_DEVICE_WINDOW_SECONDS = 3600   # 同一设备两次拍摄的时间窗：60 分钟
CROSS_DEVICE_MIN_DISTANCE_KM = 100.0  # 两次现场相距阈值：100 公里

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def coordinate(value: Any, label: str, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("%s必须是数值" % label) from exc
    if not low <= value <= high:
        raise DomainError("%s超出有效范围" % label)
    return value


def radius_km(value: Any) -> float:
    value = coordinate(value, "灾区半径", 0.01, 20000.0)
    return value


def parse_time(value: Any, label: str = "时间") -> datetime:
    """解析 ISO 8601 时间；不带时区的时间戳按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            raise DomainError("%s不能为空" % label)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainError("%s必须是 ISO 8601 格式（如 2026-09-27T08:30:00+08:00）" % label) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_time(value: Any, label: str = "时间") -> str:
    return parse_time(value, label).isoformat(timespec="seconds")


def check_geo_evidence(latitude: float, longitude: float, captured_at: datetime,
                       zone: sqlite3_Row_like) -> list[str]:
    """对照灾区登记信息给出命中的核验原因，空列表表示通过。

    zone 需含 latitude/longitude/radius_km/disaster_time 字段；
    案件所属灾情未登记灾区时返回空列表（无规则可比对）。
    """
    reasons: list[str] = []
    if zone is None:
        return reasons
    distance = haversine_km(latitude, longitude, zone["latitude"], zone["longitude"])
    if distance > float(zone["radius_km"]):
        reasons.append(
            "证据位置（%.5f,%.5f）距灾区中心 %.1f 公里，超出灾区半径 %s 公里"
            % (latitude, longitude, distance, zone["radius_km"])
        )
    disaster_time = parse_time(zone["disaster_time"], "灾情时刻")
    if captured_at < disaster_time:
        delta = int((disaster_time - captured_at).total_seconds())
        reasons.append(
            "拍摄时间 %s 早于灾情时刻 %s（早 %d 分钟）"
            % (captured_at.isoformat(timespec="seconds"), disaster_time.isoformat(timespec="seconds"),
               max(1, delta // 60))
        )
    return reasons


def find_cross_device(candidates: Iterable[Any], device_id: str, captured_at: datetime,
                      latitude: float, longitude: float) -> list[dict[str, Any]]:
    """在候选证据中寻找同设备跨区冲突。

    candidates 为含 device_id/captured_at/latitude/longitude/id 的记录（任意来源），
    只有四项齐备的记录参与计算。返回命中的冲突点列表（去重、最多 5 个）。
    """
    hits: list[dict[str, Any]] = []
    for row in candidates:
        other_device = _field(row, "device_id")
        other_time = _field(row, "captured_at")
        other_lat = _field(row, "latitude")
        other_lon = _field(row, "longitude")
        if not other_device or other_time is None or other_lat is None or other_lon is None:
            continue
        if str(other_device) != str(device_id):
            continue
        other_dt = parse_time(other_time, "拍摄时间")
        if abs((captured_at - other_dt).total_seconds()) > CROSS_DEVICE_WINDOW_SECONDS:
            continue
        distance = haversine_km(latitude, longitude, float(other_lat), float(other_lon))
        if distance >= CROSS_DEVICE_MIN_DISTANCE_KM:
            hits.append({
                "other_evidence_id": _field(row, "id"),
                "other_claim_id": _field(row, "claim_id"),
                "distance_km": round(distance, 1),
                "gap_minutes": int(abs((captured_at - other_dt).total_seconds()) // 60),
                "other_captured_at": other_dt.isoformat(timespec="seconds"),
                "other_latitude": float(other_lat),
                "other_longitude": float(other_lon),
            })
    # 去重并限制数量，避免异常设备在大量现场间跳变时刷爆审计记录
    unique: dict[Any, dict[str, Any]] = {}
    for hit in hits:
        unique.setdefault(hit["other_evidence_id"], hit)
    return list(unique.values())[:5]


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return row[name] if hasattr(row, "keys") else getattr(row, name, None)


# 类型别名仅用于注解可读性：sqlite3.Row / dict 均可
sqlite3_Row_like = Any
