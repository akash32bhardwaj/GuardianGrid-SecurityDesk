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

# Same database file as db.py. In Docker the live DB is volume-mounted at
# /data/guardiangrid.db; locally it sits next to api_server.py. This used
# to be the bare relative path "guardiangrid.db", which happened to work
# only because the container entrypoint does `cd /data` first — a process
# started from anywhere else would silently create a second, empty
# database and report no incidents at all.
DB_PATH = (Path("/data/guardiangrid.db")
           if Path("/data/guardiangrid.db").exists()
           else Path("guardiangrid.db"))

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


# ── Disposition: genuine incident, or false alarm? ────────────────────
#
# The Command Canvas records that judgement in its own table,
# canvas_resolutions, which nothing outside the Canvas ever read. So the
# case file — the screen you would actually show a client to prove an
# alert was worth acting on — could tell you an incident was RESOLVED but
# not whether it had turned out to be real. The distinction survived only
# in a toast that vanished on refresh.
#
# Rather than duplicate the column, the case file now reads the table the
# Canvas already writes. A missing table is normal on a fresh install and
# means exactly what it says: nothing has been dispositioned yet.

_DISPO_FIELDS = ("resolution", "note", "resolved_by", "resolved_at")


def _dispositions(c, incident_ids):
    """{incident_id: {resolution, note, by, at}} for the ids given."""
    if not incident_ids:
        return {}
    try:
        marks = ",".join("?" * len(incident_ids))
        rows = c.execute(
            f"SELECT incident_id, resolution, note, resolved_by, resolved_at"
            f"  FROM canvas_resolutions WHERE incident_id IN ({marks})",
            tuple(incident_ids),
        ).fetchall()
    except sqlite3.Error:
        return {}            # table not created yet: nothing dispositioned
    return {r["incident_id"]: {
        "resolution": r["resolution"],
        "note": r["note"],
        "by": r["resolved_by"],
        "at": r["resolved_at"],
    } for r in rows}


def _attach_disposition(c, incidents):
    """Fold the Canvas verdict into each incident dict, in place."""
    found = _dispositions(c, [i["incident_id"] for i in incidents])
    for inc in incidents:
        inc["disposition"] = found.get(inc["incident_id"])
    return incidents


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
        return _attach_disposition(c, [_row_to_dict(r) for r in rows])


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
        if not row:
            return None
        return _attach_disposition(c, [_row_to_dict(row)])[0]
