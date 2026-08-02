"""
seed_incidents.py — populate demo incidents for sales demos
------------------------------------------------------------
Same philosophy as seed_demo.py / seed_briefs.py:
  * every seeded row is identifiable (incident_id starts with "DEMO-",
    operator = "DEMO-SEED") and one command purges them all
  * randomized per run, so each society gets a different-looking week

Creates 4–6 incidents spread over the last N days:
  mostly MEDIUM (loitering, unknown vehicle), one resolved HIGH for
  drama, exactly ONE left OPEN so prospects see a live workflow.
Also drops matching rows into the escalations table so escalation
metrics have demo data to chew on.

Run from the data folder (inside a container: docker exec -w /data ...):
    python /app/seed_incidents.py             # last 7 days
    python /app/seed_incidents.py --days 14
    python /app/seed_incidents.py --purge     # remove ONLY demo incidents
"""
import argparse
import json
import random
import sqlite3
import uuid
from datetime import datetime, timedelta

DB = "guardiangrid.db"

CAMERAS = ["Main Gate", "Basement Entry", "Visitor Parking", "Garden Gate"]

SCENARIOS = [
    # (title, description, severity, hour_range, plate?)
    ("Loitering detected",
     "Person lingering near {cam} beyond the configured threshold. "
     "AI flagged repeated presence over 4 minutes.",
     "MEDIUM", (22, 2), False),
    ("Unknown vehicle at gate",
     "Unregistered vehicle {plate} attempted entry at {cam}. "
     "Guard verification requested.",
     "MEDIUM", (10, 21), True),
    ("Repeat unregistered plate",
     "Vehicle {plate} observed at {cam} for the third consecutive day "
     "without registration. Pattern flagged by AI.",
     "MEDIUM", (9, 19), True),
    ("Restricted zone entry",
     "Movement detected in restricted area near {cam} outside "
     "permitted hours.",
     "HIGH", (23, 4), False),
    ("Tailgating at gate",
     "Second vehicle followed a resident entry through {cam} without "
     "separate verification.",
     "LOW", (8, 20), True),
    ("Camera obstruction",
     "View from {cam} partially blocked for over 10 minutes. "
     "Possible tampering or parked obstruction.",
     "LOW", (7, 18), False),
]

STATES = ["PB", "RJ", "HR", "DL", "CH"]


def _plate():
    return (f"{random.choice(STATES)}{random.randint(1, 65):02d}"
            f"{random.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}"
            f"{random.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}"
            f"{random.randint(1000, 9999)}")


def _ts(days_ago, hour_range):
    h1, h2 = hour_range
    day = datetime.now() - timedelta(days=days_ago)
    if h1 <= h2:
        hour = random.randint(h1, h2)
    else:  # wraps midnight, e.g. (22, 2)
        hour = random.choice(list(range(h1, 24)) + list(range(0, h2 + 1)))
    return day.replace(hour=hour, minute=random.randint(0, 59),
                       second=random.randint(0, 59), microsecond=0)


def seed(days):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    n = random.randint(4, 6)
    picks = random.sample(SCENARIOS, min(n, len(SCENARIOS)))
    # one incident per distinct day; force the newest slot to be today
    day_slots = sorted(random.sample(range(0, days), len(picks)))
    day_slots[0] = 0
    made = 0

    for idx, (scn, days_ago) in enumerate(zip(picks, day_slots)):
        title, desc_tpl, severity, hours, wants_plate = scn
        cam = random.choice(CAMERAS)
        plate = _plate() if wants_plate else None
        created = _ts(days_ago, hours)
        desc = desc_tpl.format(cam=cam, plate=plate or "")
        inc_id = f"DEMO-{uuid.uuid4().hex[:8].upper()}"

        is_open = (idx == 0)  # today's incident stays open
        status = "OPEN" if is_open else "RESOLVED"
        resolve_minutes = random.randint(12, 95)
        resolved_at = (None if is_open else
                       (created + timedelta(minutes=resolve_minutes))
                       .isoformat(sep=" ", timespec="seconds"))
        notes = [] if is_open else [{
            "operator": "GuardianGrid Ops",
            "message": random.choice([
                "Verified with on-site guard. No threat — visitor expected.",
                "Guard dispatched, area checked and cleared.",
                "Owner contacted; vehicle registered on the spot.",
                "Reviewed footage — false alarm, resolved.",
            ]),
            "time": resolved_at,
        }]

        cur.execute(
            """INSERT INTO incidents
               (incident_id, title, description, severity, camera_name,
                evidence_image, operator, status, created_at, updated_at,
                resolved_at, notes, plate_number, resident_name,
                flat_number, confidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (inc_id, title, desc, severity, cam, None, "DEMO-SEED",
             status,
             created.isoformat(sep=" ", timespec="seconds"),
             (resolved_at or created.isoformat(sep=" ", timespec="seconds")),
             resolved_at, json.dumps(notes), plate, None, None,
             round(random.uniform(0.62, 0.94), 2)),
        )

        # matching escalation row (tier by severity)
        tier = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}[severity]
        ack_latency = None if is_open else float(random.randint(35, 400))
        cur.execute(
            """INSERT INTO escalations
               (incident_id, escalated_at, tier, trigger_type, camera,
                zone, hour_of_day, channel, subject, verdict, verdict_at,
                verdict_by, verdict_note, acknowledged_at,
                ack_latency_seconds, auto_closed)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (inc_id,
             created.isoformat(sep=" ", timespec="seconds"),
             tier, title.lower().replace(" ", "_"), cam, "perimeter",
             created.hour,
             "whatsapp" if tier < 3 else "voice+whatsapp",
             title,
             None if is_open else random.choice(
                 ["true_positive", "false_positive"]),
             resolved_at, None if is_open else "DEMO-SEED",
             None,
             None if is_open else
             (created + timedelta(seconds=ack_latency))
             .isoformat(sep=" ", timespec="seconds"),
             ack_latency, 0),
        )
        made += 1

    con.commit()
    con.close()
    print(f"Seeded {made} demo incident(s) over {days} day(s) "
          f"(1 open, {made - 1} resolved).")


def purge():
    con = sqlite3.connect(DB)
    a = con.execute(
        "DELETE FROM incidents WHERE incident_id LIKE 'DEMO-%'").rowcount
    b = con.execute(
        "DELETE FROM escalations WHERE incident_id LIKE 'DEMO-%'").rowcount
    con.commit()
    con.close()
    print(f"Purged {a} demo incident(s) and {b} escalation row(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--purge", action="store_true")
    args = ap.parse_args()
    purge() if args.purge else seed(args.days)
