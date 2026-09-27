"""核验数据层：灾区登记表与证据核验字段的建表、迁移和读写。"""
from __future__ import annotations

import sqlite3

ZONE_SCHEMA = """
CREATE TABLE IF NOT EXISTS disaster_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL UNIQUE REFERENCES claims(id),
    center_lat REAL NOT NULL,
    center_lon REAL NOT NULL,
    disaster_time TEXT NOT NULL,
    radius_km REAL NOT NULL,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

EVIDENCE_COLUMNS = [
    ("latitude", "REAL"),
    ("longitude", "REAL"),
    ("captured_at", "TEXT"),
    ("device_id", "TEXT"),
    ("verify_flags", "TEXT NOT NULL DEFAULT '[]'"),
    ("verified_by", "TEXT"),
    ("verified_at", "TEXT"),
    ("reject_reason", "TEXT"),
]


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ZONE_SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(evidence)")}
    for name, ddl in EVIDENCE_COLUMNS:
        if name not in existing:
            conn.execute("ALTER TABLE evidence ADD COLUMN %s %s" % (name, ddl))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_device ON evidence(device_id, captured_at)")


def upsert_zone(conn: sqlite3.Connection, claim_id: int, center_lat: float, center_lon: float,
                disaster_time: str, radius_km: float, actor: str, now: str) -> None:
    conn.execute(
        """INSERT INTO disaster_zones(claim_id,center_lat,center_lon,disaster_time,radius_km,registered_by,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(claim_id) DO UPDATE SET center_lat=excluded.center_lat,
               center_lon=excluded.center_lon, disaster_time=excluded.disaster_time,
               radius_km=excluded.radius_km, registered_by=excluded.registered_by,
               updated_at=excluded.updated_at""",
        (claim_id, center_lat, center_lon, disaster_time, radius_km, actor, now, now),
    )


def get_zone(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM disaster_zones WHERE claim_id=?", (claim_id,)).fetchone()


def device_sightings(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, claim_id, latitude, longitude, captured_at FROM evidence
           WHERE device_id=? AND captured_at IS NOT NULL AND latitude IS NOT NULL AND longitude IS NOT NULL""",
        (device_id,),
    ).fetchall()
