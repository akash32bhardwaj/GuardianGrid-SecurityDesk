"""
db.py — GuardianGrid event storage (PostgreSQL)
--------------------------------------------
Persists every vehicle event so history survives restarts and
powers the calendar, trends, and reports.

Uses psycopg2 and DATABASE_URL for Render deployment.
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set. Please set it to your Render PostgreSQL connection string.")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_events (
    id          SERIAL PRIMARY KEY,
    plate       TEXT NOT NULL,
    vtype       TEXT,
    state       TEXT,
    event       TEXT,              -- ENTRY / EXIT
    confidence  REAL,
    image       TEXT,              -- snapshot filename
    access      TEXT,              -- RESIDENT / VISITOR / UNKNOWN / BLACKLISTED
    camera      TEXT,              -- which camera triggered
    timestamp   TIMESTAMP NOT NULL -- ISO format
);
CREATE INDEX IF NOT EXISTS idx_events_time  ON vehicle_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_plate ON vehicle_events(plate);
"""

def _conn():
    c = psycopg2.connect(DATABASE_URL)
    c.autocommit = True 
    return c

def init_db():
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
    print(f"[DB] Event store ready -> PostgreSQL")

def record_event(record: dict):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO vehicle_events
                   (plate, vtype, state, event, confidence, image, timestamp, access, camera)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT EXTRACT(HOUR FROM timestamp) AS hour,
                          SUM(CASE WHEN event = 'ENTRY' THEN 1 ELSE 0 END) AS entered,
                          SUM(CASE WHEN event = 'EXIT' THEN 1 ELSE 0 END)  AS exited
                   FROM vehicle_events
                   WHERE DATE(timestamp) = %s
                   GROUP BY hour ORDER BY hour""",
                (day,),
            )
            rows = cur.fetchall()
    out = []
    for r in rows:
        h = int(r["hour"])
        label = datetime(2000, 1, 1, h).strftime("%I%p").lstrip("0")
        out.append({"hour": h, "h": label,
                    "entered": r["entered"] or 0, "exited": r["exited"] or 0})
    return out

def daily_summary(year: int, month: int):
    ym = f"{year:04d}-{month:02d}"
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT DATE(timestamp) AS d,
                          SUM(CASE WHEN event = 'ENTRY' THEN 1 ELSE 0 END) AS entered,
                          SUM(CASE WHEN event = 'EXIT' THEN 1 ELSE 0 END)  AS exited
                   FROM vehicle_events
                   WHERE TO_CHAR(timestamp, 'YYYY-MM') = %s
                   GROUP BY d ORDER BY d""",
                (ym,),
            )
            rows = cur.fetchall()
    return [{"date": r["d"].strftime("%Y-%m-%d") if hasattr(r["d"], 'strftime') else r["d"], "entered": r["entered"] or 0,
             "exited": r["exited"] or 0} for r in rows]

def events_for_date(date_str: str, limit: int = 200):
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT plate, vtype AS type, state, event, confidence,
                          image, timestamp
                   FROM vehicle_events
                   WHERE DATE(timestamp) = %s
                   ORDER BY timestamp DESC LIMIT %s""",
                (date_str, limit),
            )
            rows = cur.fetchall()
    for r in rows:
        if hasattr(r['timestamp'], 'isoformat'):
            r['timestamp'] = r['timestamp'].isoformat()
    return [dict(r) for r in rows]

def vehicle_summary(limit: int = 100):
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT plate,
                          MAX(vtype)  AS type,
                          MAX(state)  AS state,
                          COUNT(*)    AS visits,
                          MAX(timestamp) AS last_seen
                   FROM vehicle_events
                   GROUP BY plate
                   ORDER BY last_seen DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    for r in rows:
        if hasattr(r['last_seen'], 'isoformat'):
            r['last_seen'] = r['last_seen'].isoformat()
    return [dict(r) for r in rows]

def access_mix(date_str: str | None = None):
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT COALESCE(access, 'UNKNOWN') AS status, COUNT(*) AS n
                   FROM vehicle_events
                   WHERE DATE(timestamp) = %s
                   GROUP BY status""",
                (day,),
            )
            rows = cur.fetchall()
    return [{"status": r["status"], "count": r["n"]} for r in rows]

# ── Visitors ──────────────────────────────────────────────────────

_VISITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    flat     TEXT,
    phone    TEXT,
    purpose  TEXT,
    in_time  TIMESTAMP NOT NULL,
    out_time TIMESTAMP
);
"""

def init_visitors():
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(_VISITOR_SCHEMA)

def add_visitor(name, flat, phone, purpose=""):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO visitors (name, flat, phone, purpose, in_time) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (name, flat, phone, purpose, datetime.now().isoformat()),
            )
            return cur.fetchone()[0]

def visitors_today():
    day = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT id, name, flat, phone, purpose, in_time, out_time
                   FROM visitors WHERE DATE(in_time) = %s
                   ORDER BY in_time DESC""",
                (day,),
            )
            rows = cur.fetchall()
    for r in rows:
        if hasattr(r['in_time'], 'isoformat'): r['in_time'] = r['in_time'].isoformat()
        if r['out_time'] and hasattr(r['out_time'], 'isoformat'): r['out_time'] = r['out_time'].isoformat()
    return [dict(r) for r in rows]

def visitor_exit(visitor_id):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE visitors SET out_time = %s WHERE id = %s AND out_time IS NULL",
                (datetime.now().isoformat(), visitor_id),
            )
            return cur.rowcount > 0

def camera_heat(date_str: str | None = None):
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT COALESCE(camera, 'Main Gate') AS camera,
                          EXTRACT(HOUR FROM timestamp) AS hour,
                          COUNT(*) AS n
                   FROM vehicle_events
                   WHERE DATE(timestamp) = %s
                   GROUP BY camera, hour""",
                (day,),
            )
            rows = cur.fetchall()
    return [{"camera": r["camera"], "hour": int(r["hour"]), "n": r["n"]} for r in rows]

def rebuild_today_state():
    day = datetime.now().strftime("%Y-%m-%d")
    stats = {"entries": 0, "exits": 0, "cars": 0, "motorcycles": 0,
             "buses": 0, "trucks": 0, "total": 0}
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT plate, vtype, event, timestamp FROM vehicle_events
                   WHERE DATE(timestamp) = %s ORDER BY timestamp""",
                (day,),
            )
            rows = cur.fetchall()
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
            
            ts = r["timestamp"]
            if hasattr(ts, 'isoformat'):
                inside[r["plate"]] = ts
            else:
                inside[r["plate"]] = datetime.fromisoformat(ts)
        elif r["event"] == "EXIT":
            stats["exits"] += 1
            inside.pop(r["plate"], None)
    return stats, inside