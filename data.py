"""数据层：SQLite 连接、表结构与轻量迁移。

本模块不包含任何业务规则，只负责表的创建、列的增量迁移和连接管理，
业务逻辑见 service.py，纯规则见 rules.py。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "catastrophe_claims.db"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# 旧版 evidence 表没有核验相关列，按此清单增量补齐（列名 -> ALTER 定义）。
EVIDENCE_MIGRATION_COLUMNS: dict[str, str] = {
    "latitude": "REAL",
    "longitude": "REAL",
    "captured_at": "TEXT",
    "device_id": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'verified'",
    "review_reason": "TEXT",
    "reviewed_by": "TEXT",
    "reviewed_at": "TEXT",
    "locked": "INTEGER NOT NULL DEFAULT 1",
}


class Database:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_no TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL,
                    region TEXT NOT NULL,
                    peril_type TEXT NOT NULL,
                    policy_no TEXT NOT NULL,
                    claimant_ref TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    estimated_loss REAL NOT NULL,
                    urgent_need INTEGER NOT NULL DEFAULT 0,
                    fraud_score REAL NOT NULL DEFAULT 0,
                    priority_score REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'received',
                    assignee TEXT,
                    surveyor TEXT,
                    lodging_required INTEGER NOT NULL DEFAULT 0,
                    remote_review INTEGER NOT NULL DEFAULT 0,
                    emergency_advance REAL NOT NULL DEFAULT 0,
                    final_payout REAL,
                    duplicate_of INTEGER REFERENCES claims(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS disaster_zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    disaster_time TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL,
                    submitter TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'verified',
                    latitude REAL,
                    longitude REAL,
                    captured_at TEXT,
                    device_id TEXT,
                    review_reason TEXT,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id,sha256)
                );
                CREATE TABLE IF NOT EXISTS survey_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    surveyor TEXT NOT NULL,
                    damage_ratio REAL NOT NULL,
                    findings TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    approved_by TEXT NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER REFERENCES claims(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, priority_score DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(sha256);
                CREATE INDEX IF NOT EXISTS idx_zone_event ON disaster_zones(event_id);
                """
            )
            self._migrate_evidence(conn)
            # 核验相关列在迁移后才可能存在，索引放到迁移完成后创建
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_evidence_status ON evidence(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_device ON evidence(device_id, captured_at);
                """
            )

    def _migrate_evidence(self, conn: sqlite3.Connection) -> None:
        """对旧库增量补列；补列前的历史证据默认视为已核验、已锁定。"""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(evidence)").fetchall()}
        if not existing:
            return
        for column, decl in EVIDENCE_MIGRATION_COLUMNS.items():
            if column not in existing:
                conn.execute("ALTER TABLE evidence ADD COLUMN %s %s" % (column, decl))
        # 历史证据（旧版只有哈希、无定位）的状态为 received 等旧值，
        # 统一视为已核验并锁定，避免污染每案核验统计与待核验清单
        conn.execute(
            "UPDATE evidence SET status='verified', locked=1 WHERE status NOT IN ('pending','verified','rejected')"
        )


def rows_to_dicts(rows: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
