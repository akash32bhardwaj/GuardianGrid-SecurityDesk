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

--------------------------------------------------------------------------
OCT-66 — WHERE THE DATABASE IS

`DB_PATH = Path("guardiangrid.db")` resolved against the process working
directory. It worked only because the container is launched with its
working directory at /data. Anything started from elsewhere — a cron
script, a `docker exec` that lands in /app, a developer running a helper
from the repo root — opened a DIFFERENT file, and sqlite3 CREATES a
missing database rather than failing, so the symptom was "my data is
gone", never "wrong path".

Measured on demo, 18 Sep: `docker exec` lands in /app while /proc/1/cwd is
/data. Same relative name, two different files, depending on who ran it.

Now resolved once, absolutely, at import: GG_DB_PATH if set, then the
/data mount, then beside this file for a laptop checkout. Same order as
api_server.py's resolver, deliberately — two resolvers that disagree is
the bug one level up.

--------------------------------------------------------------------------
OCT-64 — TWO TIMESTAMP FORMATS IN ONE COLUMN

`timestamp` holds both "2026-09-14T12:10:00" and "2026-09-14 12:10:00".
What that does and does not break, measured in sqlite rather than assumed:

    value                    date()       strftime('%H')
    '2026-09-14T12:10:00'    2026-09-14   12
    '2026-09-14 12:10:00'    2026-09-14   12

So `date()` and `strftime()` parse BOTH. Every grouping query in this file
— hourly_stats, daily_summary, access_mix, camera_heat — was always fine,
and the earlier version of this note overstated the damage.

Text COMPARISON is where it breaks, because "T" is 0x54 and a space is
0x20. Measured on a table holding both formats plus one malformed row:

    MAX(ts)                              -> 'garbage'
    MAX(datetime(REPLACE(ts,'T',' ')))   -> '2026-09-14 12:10:00'

    ORDER BY ts DESC     -> garbage, T12:10, T00:09, ' 12:10', ''
    ORDER BY normalised  -> T12:10, ' 12:10', T00:09, garbage, ''

A single malformed row wins MAX outright. Three places in this file
compared the column as text, and each had a visible consequence:

  events_for_date       ORDER BY timestamp DESC LIMIT — the drill-down
                        table for a day was not in time order, and the
                        LIMIT kept the wrong rows.

  vehicle_summary       MAX(timestamp) AS last_seen, then ORDER BY it.
                        One bad row and a vehicle's "last seen" reads as
                        literal garbage — and sorts to the top of the
                        registry, because garbage sorts high.

  rebuild_today_state   ORDER BY timestamp, replaying ENTRY/EXIT in order
                        to work out who is still inside. Out of order, an
                        EXIT can be applied before its ENTRY, so a car
                        that has left shows as inside. This is the one
                        that matters: it is a guard-facing answer.

Fixed two ways, because one alone is not enough:

  1. On READ, every comparison goes through TS_ORDER, which normalises
     the separator. This works on the data as it is today, with no
     migration and no downtime.

  2. On WRITE, record_event() canonicalises to "YYYY-MM-DD HH:MM:SS"
     whatever the caller passes, so the column stops accumulating a
     second format. api_server.py builds the record with
     `now.isoformat()`; rather than chase every caller, the storage
     boundary is the one place that can guarantee the invariant.

  3. `normalise_timestamps()` rewrites the rows already stored. It is
     idempotent, runs in a transaction, and defaults to a dry run:

         docker exec -w /data octa-demo python /app/db.py --check
         docker exec -w /data octa-demo python /app/db.py --migrate

     Read-path fixes mean the migration is housekeeping, not a
     prerequisite. Nothing breaks if it never runs.
--------------------------------------------------------------------------
"""

import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime


def _resolve_db_path() -> Path:
    """The one answer to "where is guardiangrid.db" — see OCT-66 above."""
    override = os.environ.get("GG_DB_PATH")
    if override:
        return Path(override)
    docker_db = Path("/data/guardiangrid.db")
    if docker_db.exists():
        return docker_db
    return Path(__file__).resolve().parent / "guardiangrid.db"


DB_PATH = _resolve_db_path()

# Canonical stored form. Space-separated because that is what SQLite's own
# datetime() emits, so a normalised column compares correctly as plain text
# too — belt and braces once the migration has run.
TS_FMT = "%Y-%m-%d %H:%M:%S"

# Use this ANYWHERE the column is ordered, compared or aggregated. Never
# compare `timestamp` as raw text. Unparseable values become NULL, and
# SQLite sorts NULL lowest, so a malformed row sinks instead of winning.
TS_ORDER = "datetime(REPLACE(timestamp,'T',' '))"


def canon_ts(value=None) -> str:
    """Whatever a caller hands us -> 'YYYY-MM-DD HH:MM:SS'.

    Accepts a datetime, an ISO string with either separator, or None for
    "now". Anything unparseable is returned unchanged rather than dropped:
    losing the only record of when something happened is worse than
    storing it in a shape that sorts last.
    """
    if value is None:
        return datetime.now().strftime(TS_FMT)
    if isinstance(value, datetime):
        return value.strftime(TS_FMT)
    text = str(value).strip()
    if not text:
        return datetime.now().strftime(TS_FMT)
    try:
        return datetime.fromisoformat(text.replace("T", " ")).strftime(TS_FMT)
    except ValueError:
        return text


def parse_ts(value):
    """Stored string -> datetime, or None. Never raises."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("T", " "))
    except ValueError:
        return None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plate       TEXT NOT NULL,
    vtype       TEXT,
    state       TEXT,
    event       TEXT,              -- ENTRY / EXIT
    confidence  REAL,
    image       TEXT,              -- snapshot filename
    access      TEXT,              -- KNOWN / VISITOR / UNKNOWN / BLACKLISTED
    camera      TEXT,              -- which camera triggered
    timestamp   TEXT NOT NULL      -- 'YYYY-MM-DD HH:MM:SS' (see OCT-64)
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
    print(f"[DB] Event store ready -> {DB_PATH}", flush=True)
    if not DB_PATH.exists():
        print(f"[DB] WARNING: {DB_PATH} did not exist and was created empty. "
              f"If this site has history, GG_DB_PATH is pointing at the wrong "
              f"file.", file=sys.stderr, flush=True)


def record_event(record: dict):
    """Call with the same `record` dict built in process_entry_exit().

    OCT-64: the timestamp is canonicalised here rather than trusted from
    the caller. api_server.py passes `datetime.now().isoformat()`, which
    is the "T" form; normalising at the storage boundary is the only way
    to guarantee the column stops gaining a second format, whatever any
    future caller does.
    """
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
                canon_ts(record.get("timestamp")),
                record.get("access", "UNKNOWN"),
                record.get("camera", "Main Gate"),
            ),
        )


def hourly_stats(date_str: str | None = None):
    """Hourly ENTRY/EXIT buckets for one date (default: today).
    date_str format: 'YYYY-MM-DD'. Returns list for the chart.

    date()/strftime() parse both stored formats — verified — so this
    query needed no change.
    """
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
        if h is None:          # malformed row: no hour to bucket it into
            continue
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
    """Full event list for one date — powers the drill-down table.

    OCT-64: ordered on TS_ORDER, not raw text. With a LIMIT, a text sort
    did not merely shuffle the rows — it kept the wrong ones.
    """
    with _conn() as c:
        rows = c.execute(
            f"""SELECT plate, vtype AS type, state, event, confidence,
                       image, timestamp
                FROM vehicle_events
                WHERE date(timestamp) = ?
                ORDER BY {TS_ORDER} DESC, id DESC LIMIT ?""",
            (date_str, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def vehicle_summary(limit: int = 100):
    """Per-vehicle registry with real visit counts — upgrades the
    Vehicles page from an event log to a true registry.

    OCT-64: `MAX(timestamp)` was a TEXT max. Measured: with one malformed
    row present it returns that row, so a vehicle's last_seen read as
    literal garbage AND sorted to the top of the registry, because
    garbage sorts high. Now MAX over the normalised expression.
    """
    with _conn() as c:
        rows = c.execute(
            f"""SELECT plate,
                       MAX(vtype)  AS type,
                       MAX(state)  AS state,
                       COUNT(*)    AS visits,
                       MAX({TS_ORDER}) AS last_seen
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
            (name, flat, phone, purpose, canon_ts()),
        )
        return cur.lastrowid


def visitors_today():
    day = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            """SELECT id, name, flat, phone, purpose, in_time, out_time
               FROM visitors WHERE date(in_time) = ?
               ORDER BY datetime(REPLACE(in_time,'T',' ')) DESC""",
            (day,),
        ).fetchall()
    return [dict(r) for r in rows]


def visitor_exit(visitor_id):
    with _conn() as c:
        cur = c.execute(
            "UPDATE visitors SET out_time = ? WHERE id = ? AND out_time IS NULL",
            (canon_ts(), visitor_id),
        )
        return cur.rowcount > 0


def _configured_cameras():
    """Camera names from site_config.json's rtsp_cameras — so widgets can show
    every monitored camera permanently, even with zero activity.

    Honours OCTA_SITE_CONFIG, the same variable site_profile.py reads. This
    used to look only beside THIS file, so a site whose config was mounted
    elsewhere silently showed no configured cameras. It also swallowed every
    exception in a bare `except` (the OCT-83 family), so a malformed config
    was indistinguishable from a site with no cameras.
    """
    import json
    path = os.environ.get("OCTA_SITE_CONFIG") or str(
        Path(__file__).with_name("site_config.json"))
    try:
        with open(path, encoding="utf-8") as f:
            cams = json.load(f).get("rtsp_cameras", []) or []
        return [c["name"] for c in cams if c.get("name")]
    except FileNotFoundError:
        return []
    except Exception as exc:
        print(f"[DB] camera list unavailable from {path}: {exc}",
              file=sys.stderr, flush=True)
        return []


def camera_heat(date_str: str | None = None):
    """Trigger counts per camera per hour — powers the heatmap.
    Always includes every configured camera (zero-filled if quiet), so the
    widget permanently shows the full monitored set, not just active ones."""
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
    out = [{"camera": r["camera"], "hour": r["hour"], "n": r["n"]}
           for r in rows if r["hour"] is not None]
    seen = {o["camera"] for o in out}
    for name in _configured_cameras():
        if name not in seen:
            out.append({"camera": name, "hour": 0, "n": 0})  # zero-filled row
    return out


def rebuild_today_state():
    """Recompute today's stats and who's inside from stored events.
    Returns (stats_dict, entry_times_dict).

    OCT-64, and the one that mattered most in this file. This replays
    ENTRY/EXIT in order to decide who is still inside. Ordered as raw
    text, an EXIT could be applied before its own ENTRY — so a car that
    had already left showed as inside, on a screen a guard reads.

    Also hardened: a malformed timestamp used to raise straight out of
    datetime.fromisoformat and take the whole rebuild with it. One bad
    row should cost that row, not the entire "who is on site" answer.
    """
    day = datetime.now().strftime("%Y-%m-%d")
    stats = {"entries": 0, "exits": 0, "cars": 0, "motorcycles": 0,
             "buses": 0, "trucks": 0, "total": 0}
    with _conn() as c:
        rows = c.execute(
            f"""SELECT plate, vtype, event, timestamp FROM vehicle_events
                WHERE date(timestamp) = ? ORDER BY {TS_ORDER}, id""",
            (day,),
        ).fetchall()
    inside = {}
    skipped = 0
    for r in rows:
        if r["event"] == "ENTRY":
            when = parse_ts(r["timestamp"])
            if when is None:
                skipped += 1
                continue
            stats["entries"] += 1
            stats["total"] += 1
            t = (r["vtype"] or "").lower()
            if t == "car": stats["cars"] += 1
            elif t in ("motorcycle", "bike", "scooter"): stats["motorcycles"] += 1
            elif t == "bus": stats["buses"] += 1
            elif t == "truck": stats["trucks"] += 1
            inside[r["plate"]] = when
        elif r["event"] == "EXIT":
            stats["exits"] += 1
            inside.pop(r["plate"], None)
    if skipped:
        print(f"[DB] rebuild_today_state skipped {skipped} row(s) with an "
              f"unreadable timestamp", file=sys.stderr, flush=True)
    return stats, inside


# ── OCT-64 migration ──────────────────────────────────────────────

def timestamp_report():
    """How many rows are in which format. Safe, read-only."""
    with _conn() as c:
        def n(sql, *a):
            return c.execute(sql, a).fetchone()[0]
        return {
            "total": n("SELECT COUNT(*) FROM vehicle_events"),
            "iso_T": n("SELECT COUNT(*) FROM vehicle_events "
                       "WHERE timestamp LIKE '____-__-__T%'"),
            "space": n("SELECT COUNT(*) FROM vehicle_events "
                       "WHERE timestamp LIKE '____-__-__ %'"),
            "unparseable": n(f"SELECT COUNT(*) FROM vehicle_events "
                             f"WHERE {TS_ORDER} IS NULL"),
        }


def normalise_timestamps(apply: bool = False):
    """Rewrite 'T'-separated timestamps to the canonical space form.

    Idempotent, transactional, and a no-op unless apply=True. Rows that
    cannot be parsed are LEFT ALONE — rewriting a value we do not
    understand is how data gets quietly destroyed, and the read path
    already sorts them last rather than first.
    """
    before = timestamp_report()
    if not apply:
        return {"applied": False, "before": before,
                "would_change": before["iso_T"]}
    with _conn() as c:
        cur = c.execute(
            f"""UPDATE vehicle_events
                SET timestamp = {TS_ORDER}
                WHERE timestamp LIKE '____-__-__T%'
                  AND {TS_ORDER} IS NOT NULL""")
        changed = cur.rowcount
    return {"applied": True, "changed": changed,
            "before": before, "after": timestamp_report()}


if __name__ == "__main__":
    print(f"database: {DB_PATH}")
    if not DB_PATH.exists():
        sys.exit(f"nothing at {DB_PATH} — set GG_DB_PATH or run from /data")
    if "--migrate" in sys.argv:
        result = normalise_timestamps(apply=True)
        print(f"rewrote {result['changed']} row(s)")
        print(f"before: {result['before']}")
        print(f"after : {result['after']}")
        if result["after"]["unparseable"]:
            print(f"\n{result['after']['unparseable']} row(s) left alone as "
                  f"unparseable — they sort last on read and are not lost.")
    else:
        print(timestamp_report())
        print("\nread-only. add --migrate to rewrite the 'T' rows.")
