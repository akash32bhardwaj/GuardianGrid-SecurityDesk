r"""
seed_test_events.py — DEFENDER OCTA demo data seeder
-----------------------------------------------------
Inserts realistic vehicle events, incidents and visitors spread across
"last night" and "today", so every wow-demo search query returns hits.

Run from C:\GuardianGrid\GuardianGrid-SecurityDesk :

    python seed_test_events.py           -> insert demo rows
    python seed_test_events.py --remove  -> delete ONLY rows this script added

Safe by design:
  * Every seeded row is tagged (image = 'SEED.jpg' for vehicles,
    incident_id prefix 'SEED-', visitor purpose prefix '[SEED]'),
    so --remove never touches your real data.
  * Uses the same guardiangrid.db your api_server.py uses.
"""

import sqlite3
import sys
import os
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "guardiangrid.db")

TAG_IMG = "SEED.jpg"          # vehicle_events marker
TAG_INC = "SEED-"             # incidents marker (incident_id prefix)
TAG_VIS = "[SEED] "           # visitors marker (purpose prefix)


def ts(days_ago=0, hour=0, minute=0):
    d = datetime.now() - timedelta(days=days_ago)
    return d.replace(hour=hour, minute=minute, second=0,
                     microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def seed(con):
    cur = con.cursor()

    # ── vehicle_events ────────────────────────────────────────────
    # (plate, vtype, state, event, confidence, access, camera, timestamp)
    vehicles = [
        # last night (yesterday 20:00 → today 06:00)
        ("PB08AB4521", "car",   "REGISTERED", "ENTRY", 0.94, "RESIDENT",  "Main Gate", ts(1, 21, 12)),
        ("PB10ZZ7788", "car",   "UNKNOWN",    "ENTRY", 0.83, "UNKNOWN",   "Main Gate", ts(1, 23, 41)),
        ("PB65QK3344", "bike",  "UNKNOWN",    "ENTRY", 0.79, "UNKNOWN",   "Back Gate", ts(0, 2, 15)),
        ("PB10ZZ7788", "car",   "UNKNOWN",    "EXIT",  0.81, "UNKNOWN",   "Main Gate", ts(0, 3, 5)),
        ("PB08AB4521", "car",   "REGISTERED", "EXIT",  0.92, "RESIDENT",  "Main Gate", ts(0, 7, 45)),
        # today
        ("PB11CD9012", "truck", "REGISTERED", "ENTRY", 0.88, "APPROVED",  "Back Gate", ts(0, 9, 20)),
        ("PB08MN5566", "bike",  "REGISTERED", "ENTRY", 0.91, "RESIDENT",  "Main Gate", ts(0, 10, 5)),
        ("HR26TT0001", "car",   "UNKNOWN",    "ENTRY", 0.85, "UNKNOWN",   "Main Gate", ts(0, 11, 30)),
        ("PB11CD9012", "truck", "REGISTERED", "EXIT",  0.87, "APPROVED",  "Back Gate", ts(0, 12, 10)),
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
        print(f"  visitors        : {len(visitors)} rows")
    except sqlite3.Error as e:
        print(f"  visitors        : skipped ({e})")

    con.commit()
    print(f"  vehicle_events  : {len(vehicles)} rows")
    print(f"  incidents       : {len(incidents)} rows")
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


if __name__ == "__main__":
    if not os.path.exists(DB):
        sys.exit(f"DB not found: {DB} — run this from the SecurityDesk folder.")
    con = sqlite3.connect(DB)
    if "--remove" in sys.argv:
        remove(con)
    else:
        seed(con)
    con.close()
