"""
db.py — GuardianGrid event storage (SQLite)
--------------------------------------------
Persists every vehicle event so history survives restarts and
powers the calendar, trends, and reports.

Place this file next to api_server.py. No installation needed —
sqlite3 ships with Python. Creates guardiangrid.db automatically.

Thread-safe: api_server runs the camera in a background thread,
so each call opens its own short-lived connection (safe + simple
at gate-traffic volumes).
"""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path("guardiangrid.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plate       TEXT NOT NULL,
    vtype       TEXT,
    state       TEXT,
    event       TEXT,              -- ENTRY / EXIT
    confidence  REAL,
    image       TEXT,              -- snapshot filename
    access      TEXT,              -- RESIDENT / VISITOR / UNKNOWN / BLACKLISTED
    camera      TEXT,              -- which camera triggered
    timestamp   TEXT NOT NULL      -- ISO format
);
CREATE INDEX IF NOT EXISTS idx_events_time  ON vehicle_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_plate ON vehicle_events(plate);
"""


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.executescript(_SCHEMA)
    print(f"[DB] Event store ready → {DB_PATH.resolve()}")


def record_event(record: dict):
    """Call with the same `record` dict built in process_entry_exit()."""
    with _conn() as c:
        c.execute(
            """INSERT INTO vehicle_events
               (plate, vtype, state, event, confidence, image, timestamp, access, camera)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.get("plate"),
                record.get("type"),
                record.get("state"),
                record.get("event"),
                record.get("confidence"),
                record.get("image"),
                record.get("timestamp") or datetime.now().isoformat(),
                record.get("access", "UNKNOWN"),
                record.get("camera", "Main Gate"),
            ),
        )


def hourly_stats(date_str: str | None = None):
    """Hourly ENTRY/EXIT buckets for one date (default: today).
    date_str format: 'YYYY-MM-DD'. Returns list for the chart."""
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            """SELECT CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
                      SUM(event = 'ENTRY') AS entered,
                      SUM(event = 'EXIT')  AS exited
               FROM vehicle_events
               WHERE date(timestamp) = ?
               GROUP BY hour ORDER BY hour""",
            (day,),
        ).fetchall()
    out = []
    for r in rows:
        h = r["hour"]
        label = datetime(2000, 1, 1, h).strftime("%I%p").lstrip("0")  # "9AM"
        out.append({"hour": h, "h": label,
                    "entered": r["entered"] or 0, "exited": r["exited"] or 0})
    return out


def daily_summary(year: int, month: int):
    """Per-day totals for one month — powers the calendar view.
    Returns: [{"date": "2026-07-01", "entered": 42, "exited": 40}, ...]"""
    ym = f"{year:04d}-{month:02d}"
    with _conn() as c:
        rows = c.execute(
            """SELECT date(timestamp) AS d,
                      SUM(event = 'ENTRY') AS entered,
                      SUM(event = 'EXIT')  AS exited
               FROM vehicle_events
               WHERE strftime('%Y-%m', timestamp) = ?
               GROUP BY d ORDER BY d""",
            (ym,),
        ).fetchall()
    return [{"date": r["d"], "entered": r["entered"] or 0,
             "exited": r["exited"] or 0} for r in rows]


def events_for_date(date_str: str, limit: int = 200):
    """Full event list for one date — powers the drill-down table."""
    with _conn() as c:
        rows = c.execute(
            """SELECT plate, vtype AS type, state, event, confidence,
                      image, timestamp
               FROM vehicle_events
               WHERE date(timestamp) = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (date_str, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def vehicle_summary(limit: int = 100):
    """Per-vehicle registry with real visit counts — upgrades the
    Vehicles page from an event log to a true registry."""
    with _conn() as c:
        rows = c.execute(
            """SELECT plate,
                      MAX(vtype)  AS type,
                      MAX(state)  AS state,
                      COUNT(*)    AS visits,
                      MAX(timestamp) AS last_seen
               FROM vehicle_events
               GROUP BY plate
               ORDER BY last_seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

def access_mix(date_str: str | None = None):
    """RESIDENT/VISITOR/UNKNOWN/BLACKLISTED counts for one date."""
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            """SELECT COALESCE(access, 'UNKNOWN') AS status, COUNT(*) AS n
               FROM vehicle_events
               WHERE date(timestamp) = ?
               GROUP BY status""",
            (day,),
        ).fetchall()
    return [{"status": r["status"], "count": r["n"]} for r in rows]

# ── Visitors ──────────────────────────────────────────────────────

_VISITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    flat     TEXT,
    phone    TEXT,
    purpose  TEXT,
    in_time  TEXT NOT NULL,
    out_time TEXT
);
"""

def init_visitors():
    with _conn() as c:
        c.executescript(_VISITOR_SCHEMA)

def add_visitor(name, flat, phone, purpose=""):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO visitors (name, flat, phone, purpose, in_time) VALUES (?, ?, ?, ?, ?)",
            (name, flat, phone, purpose, datetime.now().isoformat()),
        )
        return cur.lastrowid

def visitors_today():
    day = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            """SELECT id, name, flat, phone, purpose, in_time, out_time
               FROM visitors WHERE date(in_time) = ?
               ORDER BY in_time DESC""",
            (day,),
        ).fetchall()
    return [dict(r) for r in rows]

def visitor_exit(visitor_id):
    with _conn() as c:
        cur = c.execute(
            "UPDATE visitors SET out_time = ? WHERE id = ? AND out_time IS NULL",
            (datetime.now().isoformat(), visitor_id),
        )
        return cur.rowcount > 0

def camera_heat(date_str: str | None = None):
    """Trigger counts per camera per hour — powers the heatmap."""
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            """SELECT COALESCE(camera, 'Main Gate') AS camera,
                      CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
                      COUNT(*) AS n
               FROM vehicle_events
               WHERE date(timestamp) = ?
               GROUP BY camera, hour""",
            (day,),
        ).fetchall()
    return [{"camera": r["camera"], "hour": r["hour"], "n": r["n"]} for r in rows]
def rebuild_today_state():
    """Recompute today's stats and who's inside from stored events.
    Returns (stats_dict, entry_times_dict)."""
    day = datetime.now().strftime("%Y-%m-%d")
    stats = {"entries": 0, "exits": 0, "cars": 0, "motorcycles": 0,
             "buses": 0, "trucks": 0, "total": 0}
    with _conn() as c:
        rows = c.execute(
            """SELECT plate, vtype, event, timestamp FROM vehicle_events
               WHERE date(timestamp) = ? ORDER BY timestamp""",
            (day,),
        ).fetchall()
    inside = {}
    for r in rows:
        if r["event"] == "ENTRY":
            stats["entries"] += 1
            stats["total"] += 1
            t = (r["vtype"] or "").lower()
            if t == "car": stats["cars"] += 1
            elif t == "motorcycle": stats["motorcycles"] += 1
            elif t == "bus": stats["buses"] += 1
            elif t == "truck": stats["trucks"] += 1
            inside[r["plate"]] = datetime.fromisoformat(r["timestamp"])
        elif r["event"] == "EXIT":
            stats["exits"] += 1
            inside.pop(r["plate"], None)
    return stats, inside