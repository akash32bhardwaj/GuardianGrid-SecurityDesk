r"""
seed_test_events.py — DEFENDER OCTA demo data seeder
-----------------------------------------------------
Inserts realistic vehicle events, incidents and visitors spread across
"last night" and "today", so every wow-demo search query returns hits.

Run from the folder that holds guardiangrid.db:

    python seed_test_events.py           -> insert demo rows
    python seed_test_events.py --remove  -> delete ONLY rows this script added
    (container: docker exec -w /data octa-demo python /app/seed_test_events.py)

Safe by design:
  * Every seeded row is tagged (image = 'SEED.jpg' for vehicles,
    incident_id prefix 'SEED-', visitor purpose prefix '[SEED]'),
    so --remove never touches your real data.
  * Uses the same guardiangrid.db your api_server.py uses.

--------------------------------------------------------------------------
OCT-65 — SEEDED EVENTS DATED LATER TODAY

`ts(0, 12, 10)` means "today at 12:10" whatever time the script runs. The
nightly reseed runs at 03:00 IST, so every morning the demo database held
detections timestamped 09:20, 10:05, 11:30 and 12:10 — hours that had not
happened yet. A prospect opening the demo at 08:00 saw vehicles arriving
after lunch.

On a product whose whole pitch is live monitoring, a future-dated
detection is the most obvious tell there is, and it was the morning state
every single day.

Fixed by scaling today's events into the part of today that has actually
elapsed, using ONE factor for all of them. A single linear scale is
monotonic by construction, so ordering and relative spacing survive at
every hour; nothing is ever in the future; and at 03:00 the day's story is
told across 00:00-03:00 instead of being invented. Yesterday's events are
left exactly as they are: yesterday is over, all of it is in the past.

When the elapsed window is short the events compress, and the script says
so rather than quietly producing nine detections inside four minutes.

--------------------------------------------------------------------------
OCT-62 — OUTPUT THAT READS AS TABLE TOTALS

This script printed:

    vehicle_events  : 9 rows

which is what IT inserted. reset_demo.sh runs the big seeder first (~1,800
events) and this one last, so the job appeared to seed 1,798 events and
finish with 9 in the table. It caused a real false alarm during the EVT
pass — on the one job you would be reading at 2am precisely because
something else had already gone wrong.

Now it prints both, labelled, and the totals come from an actual
SELECT COUNT(*) rather than from len() of a list in this file.

--------------------------------------------------------------------------
OCT-61 / OCT-66 — two smaller things fixed in passing

The seeder wrote `RESIDENT` and `APPROVED` for access and `car` / `bike`
for vtype, bypassing the canonical vocabulary in api_server.py because it
INSERTs straight into SQLite. It was a source of the mixed vocabulary, not
just a victim of it. It now writes what the application writes.

The database path came from os.getcwd(). It now honours GG_DB_PATH and the
/data mount first, matching api_server.py and db.py, and still falls back
to the working directory.
--------------------------------------------------------------------------
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta


def _resolve_db():
    """Same order as api_server.py and db.py. See OCT-66."""
    override = os.environ.get("GG_DB_PATH")
    if override:
        return override
    docker_db = "/data/guardiangrid.db"
    if os.path.exists(docker_db):
        return docker_db
    return os.path.join(os.getcwd(), "guardiangrid.db")


DB = _resolve_db()

TAG_IMG = "SEED.jpg"          # vehicle_events marker
TAG_INC = "SEED-"             # incidents marker (incident_id prefix)
TAG_VIS = "[SEED] "           # visitors marker (purpose prefix)

# How much of today has elapsed, computed once so every row in a run maps
# consistently. Two minutes of headroom keeps the newest seeded event just
# behind the clock rather than exactly on it.
_NOW = datetime.now()
_MIDNIGHT = _NOW.replace(hour=0, minute=0, second=0, microsecond=0)
_ELAPSED = (_NOW - _MIDNIGHT).total_seconds() - 120

# The latest "today" time the narrative below uses (12:10). Every today
# timestamp is scaled by ONE factor derived from this, which is what makes
# the mapping order-preserving.
#
# My first attempt at this passed a time through unchanged when it was
# already in the past and compressed only the ones that were not. That is
# wrong, and the test output showed it: at 03:00, 02:15 passed through as
# 02:15 while 03:05 was compressed to 00:22 — so an event that happens
# LATER in the story got an EARLIER timestamp. A demo timeline in the
# wrong order is a worse bug than the one being fixed.
#
# One linear scale applied to everything is monotonic by construction: if
# a < b then a*k < b*k for any k > 0. Order and relative spacing hold at
# every hour of the day.
_LATEST_TODAY = 12 * 3600 + 10 * 60          # keep >= the latest ts(0, ...)
_SCALE = 1.0 if _ELAPSED >= _LATEST_TODAY else max(_ELAPSED, 0) / _LATEST_TODAY
_COMPRESSED = _SCALE < 1.0


def ts(days_ago=0, hour=0, minute=0):
    """A timestamp for the demo narrative that is never in the future.

    Yesterday and earlier are returned as asked — those hours have all
    happened. Today's are scaled into the elapsed part of the day by a
    single factor, so ordering survives (OCT-65).
    """
    if days_ago > 0:
        d = _NOW - timedelta(days=days_ago)
        return d.replace(hour=hour, minute=minute, second=0,
                         microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    wanted = hour * 3600 + minute * 60
    if wanted > _LATEST_TODAY:
        # A caller added a later event than _LATEST_TODAY allows for. Say
        # so rather than silently dating it into the future.
        raise ValueError(
            f"ts(0, {hour}, {minute}) is later than _LATEST_TODAY "
            f"({_LATEST_TODAY // 3600:02d}:{_LATEST_TODAY % 3600 // 60:02d}); "
            f"raise that constant to keep the clamp order-preserving.")
    if _ELAPSED <= 0:
        # Run within two minutes of midnight. Nothing today is safely in
        # the past; put it just before midnight instead of in the future.
        return (_MIDNIGHT - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    return (_MIDNIGHT + timedelta(seconds=int(wanted * _SCALE))
            ).strftime("%Y-%m-%d %H:%M:%S")


def _counts(cur):
    """Real table totals, not this script's insert counts. See OCT-62."""
    out = {}
    for table in ("vehicle_events", "incidents", "visitors"):
        try:
            out[table] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = None
    return out


def seed(con):
    cur = con.cursor()

    # ── vehicle_events ────────────────────────────────────────────
    # (plate, vtype, state, event, confidence, access, camera, timestamp)
    #
    # vtype and access are the canonical spellings api_server.py writes
    # (canon_vtype / canon_access) — this seeder used to introduce the
    # variants that OCT-61 was about.
    vehicles = [
        # last night (yesterday 20:00 -> today 06:00)
        ("PB08AB4521", "Car",        "REGISTERED", "ENTRY", 0.94, "KNOWN",    "Main Gate", ts(1, 21, 12)),
        ("PB10ZZ7788", "Car",        "UNKNOWN",    "ENTRY", 0.83, "UNKNOWN",  "Main Gate", ts(1, 23, 41)),
        ("PB65QK3344", "Motorcycle", "UNKNOWN",    "ENTRY", 0.79, "UNKNOWN",  "Back Gate", ts(0, 2, 15)),
        ("PB10ZZ7788", "Car",        "UNKNOWN",    "EXIT",  0.81, "UNKNOWN",  "Main Gate", ts(0, 3, 5)),
        ("PB08AB4521", "Car",        "REGISTERED", "EXIT",  0.92, "KNOWN",    "Main Gate", ts(0, 7, 45)),
        # today
        ("PB11CD9012", "Truck",      "REGISTERED", "ENTRY", 0.88, "APPROVED", "Back Gate", ts(0, 9, 20)),
        ("PB08MN5566", "Motorcycle", "REGISTERED", "ENTRY", 0.91, "KNOWN",    "Main Gate", ts(0, 10, 5)),
        ("HR26TT0001", "Car",        "UNKNOWN",    "ENTRY", 0.85, "UNKNOWN",  "Main Gate", ts(0, 11, 30)),
        ("PB11CD9012", "Truck",      "REGISTERED", "EXIT",  0.87, "APPROVED", "Back Gate", ts(0, 12, 10)),
    ]
    cur.executemany(
        "INSERT INTO vehicle_events "
        "(plate, vtype, state, event, confidence, image, access, camera, timestamp) "
        "VALUES (?,?,?,?,?,'" + TAG_IMG + "',?,?,?)",
        vehicles,
    )

    # ── incidents ─────────────────────────────────────────────────
    incidents = [
        (TAG_INC + "9001", "Unknown vehicle loitering near Main Gate",
         "HIGH", "OPEN", "Main Gate",
         "Unregistered car PB10ZZ7788 waited 6 minutes before entry.",
         ts(1, 23, 39)),
        (TAG_INC + "9002", "Unknown two-wheeler at Back Gate after midnight",
         "MEDIUM", "OPEN", "Back Gate",
         "Bike PB65QK3344 entered at 02:15, no resident match.",
         ts(0, 2, 16)),
        (TAG_INC + "9003", "Unknown face detected near parking",
         "HIGH", "REVIEWED", "Parking Cam",
         "Face not in resident database; snapshot stored.",
         ts(0, 11, 32)),
    ]
    cur.executemany(
        "INSERT INTO incidents "
        "(incident_id, title, severity, status, camera_name, description, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        incidents,
    )

    # ── visitors (best-effort — skipped if column names differ) ───
    n_visitors = 0
    try:
        visitors = [
            ("Ramesh Kumar",  "B-204", TAG_VIS + "Amazon delivery", ts(0, 9, 40),  ts(0, 9, 52)),
            ("Sunita Devi",   "A-101", TAG_VIS + "House help",      ts(0, 8, 0),   None),
            ("Vikram Singh",  "C-302", TAG_VIS + "Guest",           ts(1, 21, 30), ts(1, 23, 10)),
        ]
        cur.executemany(
            "INSERT INTO visitors (name, flat, purpose, in_time, out_time) "
            "VALUES (?,?,?,?,?)",
            visitors,
        )
        n_visitors = len(visitors)
    except sqlite3.Error as e:
        print(f"  visitors        : skipped ({e})")

    con.commit()

    # OCT-62: inserted-by-this-run and table-totals are different numbers
    # and are now labelled as such. The totals are a real COUNT(*).
    totals = _counts(cur)
    newest = cur.execute(
        "SELECT MAX(datetime(REPLACE(timestamp,'T',' '))) FROM vehicle_events"
    ).fetchone()[0]

    print(f"\n  inserted by THIS script:")
    print(f"    vehicle_events : {len(vehicles)}")
    print(f"    incidents      : {len(incidents)}")
    print(f"    visitors       : {n_visitors}")
    print(f"  TABLE TOTALS now ({DB}):")
    for table, n in totals.items():
        print(f"    {table:<15}: {'n/a' if n is None else n}")
    print(f"    newest event   : {newest}")

    if _COMPRESSED:
        elapsed_h = max(_ELAPSED, 0) / 3600.0
        print(f"\n  NOTE: only {elapsed_h:.1f}h of today had elapsed, so today's"
              f" events were compressed into it rather than dated into the"
              f" future (OCT-65). Order and spacing are preserved.")

    print("\nSeeded. Try these searches:")
    print('   unknown vehicles last night')
    print('   kal raat kaun aaya tha?')
    print('   PB10 gaadi kab aayi thi')
    print('   high severity alerts today')
    print('   all trucks today')
    print('   visitors today')


def remove(con):
    cur = con.cursor()
    v = cur.execute("DELETE FROM vehicle_events WHERE image = ?",
                    (TAG_IMG,)).rowcount
    i = cur.execute("DELETE FROM incidents WHERE incident_id LIKE ?",
                    (TAG_INC + "%",)).rowcount
    try:
        p = cur.execute("DELETE FROM visitors WHERE purpose LIKE ?",
                        (TAG_VIS + "%",)).rowcount
    except sqlite3.Error:
        p = 0
    con.commit()
    print(f"Removed {v} vehicle events, {i} incidents, {p} visitors (seed rows only).")
    totals = _counts(cur)
    print(f"  TABLE TOTALS now: " +
          ", ".join(f"{k} {'n/a' if n is None else n}" for k, n in totals.items()))


if __name__ == "__main__":
    if not os.path.exists(DB):
        sys.exit(f"DB not found: {DB} — set GG_DB_PATH, or cd to the folder "
                 f"holding guardiangrid.db first.")
    con = sqlite3.connect(DB)
    if "--remove" in sys.argv:
        remove(con)
    else:
        seed(con)
    con.close()
