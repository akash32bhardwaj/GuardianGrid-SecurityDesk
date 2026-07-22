"""
seed_demo.py — populate the database with demonstration gate traffic
---------------------------------------------------------------------
Creates realistic multi-gate activity so the camera heatmap, KPI cards
and access-mix donut look like a busy society during demos.

All seeded rows use plates prefixed "DEMO" so they can be identified,
filtered, or removed at any time.

Run from the backend folder (next to api_server.py):

    python seed_demo.py            # seed today
    python seed_demo.py --days 7   # seed the last 7 days
    python seed_demo.py --clear    # remove all demo rows

IMPORTANT: demo rows are only *counted* when "demo_mode": true is set
in site_config.json. Leaving them in the database with demo_mode off
is harmless — every query filters them out.
"""

import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

DB = "guardiangrid.db"

# Gate name -> (first hour, last hour, max events per hour)
GATES = {
    "Garden Gate":     (7, 20, 2),
    "Basement Entry":  (8, 22, 3),
    "Visitor Parking": (9, 18, 1),
}

# Access mix for seeded traffic — weighted to look like a real society
ACCESS_WEIGHTS = [
    ("KNOWN", 70),       # residents
    ("VISITOR", 22),     # approved visitors
    ("UNKNOWN", 7),      # unregistered
    ("BLACKLISTED", 1),  # rare
]

VTYPES = [("Car", 70), ("Motorcycle", 25), ("Truck", 3), ("Bus", 2)]


def _weighted(pairs):
    population = [p[0] for p in pairs]
    weights = [p[1] for p in pairs]
    return random.choices(population, weights=weights, k=1)[0]


def _require_db():
    if not os.path.exists(DB):
        sys.exit(
            f"ERROR: {DB} not found in this folder.\n"
            "       cd to the backend folder (next to api_server.py) first."
        )


def clear_demo():
    _require_db()
    c = sqlite3.connect(DB)
    n = c.execute("DELETE FROM vehicle_events WHERE plate LIKE 'DEMO%'").rowcount
    c.commit()
    c.close()
    print(f"Removed {n} demo row(s).")


def seed(days: int = 1):
    _require_db()
    c = sqlite3.connect(DB)

    existing = c.execute(
        "SELECT COUNT(*) FROM vehicle_events WHERE plate LIKE 'DEMO%'"
    ).fetchone()[0]
    if existing:
        print(f"Note: {existing} demo row(s) already present. Clearing them first.")
        c.execute("DELETE FROM vehicle_events WHERE plate LIKE 'DEMO%'")

    rows = []
    for day_offset in range(days):
        base = (datetime.now() - timedelta(days=day_offset)).replace(
            minute=0, second=0, microsecond=0
        )
        for gate, (start, end, per_hour) in GATES.items():
            for hour in range(start, end):
                for _ in range(random.randint(0, per_hour)):
                    ts = base.replace(hour=hour) + timedelta(
                        minutes=random.randint(0, 59),
                        seconds=random.randint(0, 59),
                    )
                    # don't seed into the future
                    if ts > datetime.now():
                        continue
                    plate = f"DEMO{random.randint(1000, 9999)}"
                    rows.append((
                        plate,
                        _weighted(VTYPES),
                        "",
                        "ENTRY",
                        round(random.uniform(88.0, 99.0), 1),
                        "",
                        ts.isoformat(),
                        _weighted(ACCESS_WEIGHTS),
                        gate,
                    ))
                    # ~70% of entries also exit later the same day
                    if random.random() < 0.70:
                        out = ts + timedelta(minutes=random.randint(20, 240))
                        if out < datetime.now():
                            rows.append((
                                plate, "Car", "", "EXIT", 100.0, "",
                                out.isoformat(), "KNOWN", gate,
                            ))

    c.executemany(
        """INSERT INTO vehicle_events
           (plate, vtype, state, event, confidence, image, timestamp, access, camera)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    c.commit()
    c.close()

    print(f"Seeded {len(rows)} demo event(s) across {len(GATES)} gate(s), {days} day(s).")
    print()
    print("To make them visible on the dashboard, set this in site_config.json:")
    print('    "demo": { "demo_mode": true }')
    print("Then restart Flask.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed or clear Defender Octa demo data")
    ap.add_argument("--days", type=int, default=1, help="how many days back to seed")
    ap.add_argument("--clear", action="store_true", help="remove all demo rows")
    args = ap.parse_args()

    if args.clear:
        clear_demo()
    else:
        seed(args.days)
