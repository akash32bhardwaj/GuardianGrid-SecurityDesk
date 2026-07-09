"""
incident_models.py — SQLite-backed incident storage
-----------------------------------------------------
Drop-in replacement for the in-memory version. Same five functions,
same signatures, same return shapes — incident_service.py and
incident_routes.py need NO changes.

Incidents now persist in guardiangrid.db (same file as vehicle
events), so case files survive server restarts.

Notes are stored as a JSON array in a TEXT column — simple and
sufficient at gate-security volumes.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime

# Same database file as db.py. Path is relative to the folder
# api_server.py runs from (the project root).
DB_PATH = Path("guardiangrid.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT UNIQUE NOT NULL,
    title          TEXT,
    description    TEXT,
    severity       TEXT,
    camera_name    TEXT,
    evidence_image TEXT,
    operator       TEXT,
    status         TEXT DEFAULT 'OPEN',
    created_at     TEXT,
    updated_at     TEXT,
    resolved_at    TEXT,
    notes          TEXT DEFAULT '[]',   -- JSON array
    plate_number   TEXT,
    resident_name  TEXT,
    flat_number    TEXT,
    confidence     REAL
);
"""

_FIELDS = [
    "incident_id", "title", "description", "severity", "camera_name",
    "evidence_image", "operator", "status", "created_at", "updated_at",
    "resolved_at", "notes", "plate_number", "resident_name",
    "flat_number", "confidence",
]

# Columns an update is allowed to change (protects id/created_at)
_UPDATABLE = {
    "title", "description", "severity", "camera_name", "evidence_image",
    "operator", "status", "resolved_at", "plate_number",
    "resident_name", "flat_number", "confidence",
}


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)   # ensure table exists on first touch
    return c


def _row_to_dict(row):
    d = {k: row[k] for k in _FIELDS}
    try:
        d["notes"] = json.loads(d["notes"] or "[]")
    except (TypeError, ValueError):
        d["notes"] = []
    return d


def _next_incident_id(c) -> str:
    row = c.execute("SELECT MAX(id) AS m FROM incidents").fetchone()
    return f"GG-{(row['m'] or 0) + 1:04d}"


def create_incident(
    title,
    description,
    severity,
    camera_name,
    operator=None,
    evidence_image=None,
    plate_number=None,
    resident_name=None,
    flat_number=None,
    confidence=None,
):
    now = datetime.now().isoformat()
    with _conn() as c:
        incident_id = _next_incident_id(c)
        c.execute(
            """INSERT INTO incidents
               (incident_id, title, description, severity, camera_name,
                evidence_image, operator, status, created_at, updated_at,
                resolved_at, notes, plate_number, resident_name,
                flat_number, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, NULL, '[]', ?, ?, ?, ?)""",
            (incident_id, title, description, severity, camera_name,
             evidence_image, operator, now, now,
             plate_number, resident_name, flat_number, confidence),
        )
        row = c.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_all_incidents():
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM incidents ORDER BY id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_incident(incident_id, updates):
    safe = {k: v for k, v in (updates or {}).items() if k in _UPDATABLE}
    now = datetime.now().isoformat()
    safe["updated_at"] = now
    if (updates or {}).get("status") == "RESOLVED":
        safe["resolved_at"] = now

    sets = ", ".join(f"{k} = ?" for k in safe)
    with _conn() as c:
        cur = c.execute(
            f"UPDATE incidents SET {sets} WHERE incident_id = ?",
            (*safe.values(), incident_id),
        )
        if cur.rowcount == 0:
            return None
        row = c.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
    return _row_to_dict(row)


def add_note(incident_id, operator, message):
    now = datetime.now().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT notes FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            notes = json.loads(row["notes"] or "[]")
        except (TypeError, ValueError):
            notes = []
        notes.append({"operator": operator, "message": message, "timestamp": now})
        c.execute(
            "UPDATE incidents SET notes = ?, updated_at = ? WHERE incident_id = ?",
            (json.dumps(notes), now, incident_id),
        )
        row = c.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
    return _row_to_dict(row)


def get_incident_by_id(incident_id):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None
