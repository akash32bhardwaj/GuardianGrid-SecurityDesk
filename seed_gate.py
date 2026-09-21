"""
seed_gate.py — populate the guard-side tables for demonstrations
---------------------------------------------------------------------
WHY THIS EXISTS

reset_demo.sh seeds vehicle_events, incidents, briefs and visitors, so
the dashboard looks like a busy society. It seeds nothing the Security
Gate screen reads. On 17 Sep 2026 the demo site held:

    arrival_requests     0 rows
    gate_passes          0 rows
    household_requests   0 rows
    household_members    0 rows
    resident_notices     0 rows
    flats                2 rows

So the screen showing the guard decision loop — the part of the product
that is not just another camera feed — was blank next to a dashboard
carrying 1,797 events. The demo showed the half every competitor has and
hid the half they don't.

WHAT IT DELIBERATELY DOES NOT SEED

Pending arrivals. resident_app.py runs a background thread that flips an
unanswered hold to EXPIRED after ARRIVAL_WINDOW_SECONDS (3 minutes), so a
seeded queue of waiting visitors would empty itself before anyone looked
at it. This lays down decided history instead — admitted, declined,
expired — and leaves the live hold for you to create in front of the
client, which is the moment worth showing anyway.

VOCABULARY

Every status here is copied from the INSERT and UPDATE statements in
resident_app.py rather than guessed, because OCT-50 was exactly that
mistake: the incident seeder wrote "true_positive" into a system whose
only counted values were genuine/false/ambiguous, and the demo reported a
0% false-escalation rate as a result.

    arrival_requests    PENDING -> APPROVED | DECLINED | EXPIRED
                        decision ALLOW | WAIT | DENY
    gate_passes         ACTIVE | CANCELLED | USED  (EXPIRED is derived
                        at read time from valid_to, never stored)
    household_requests  PENDING -> APPROVED | REJECTED

Timestamps are "%Y-%m-%d %H:%M:%S" with a space, matching what the app
writes. The arrivals query compares this column as a string, so a row
written with an ISO "T" separator sorts differently and can silently drop
out of a range — see OCT-64.

USAGE

    python seed_gate.py                # seed
    python seed_gate.py --clear        # remove everything it created
    python seed_gate.py --db /data/guardiangrid.db

Everything it writes is tagged so --clear can find it again: pass codes
start with DM, seeded arrivals carry guard "demo-seed", notices are
authored by "Committee (demo)", and flats/requests use the DEMO_FLATS
list below.
"""

import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

# ── Where the database is ────────────────────────────────────────────
# seed_demo.py hardcodes a relative "guardiangrid.db", which is why it
# must be run from exactly one directory and fails confusingly anywhere
# else (OCT-66, and the cause of OCT-46). Resolve it properly and say
# out loud which file is being written.
def resolve_db(explicit=None):
    for candidate in (explicit, os.environ.get("GG_DB"),
                      "/data/guardiangrid.db", "guardiangrid.db"):
        if candidate and os.path.exists(candidate):
            return os.path.abspath(candidate)
    sys.exit("ERROR: no guardiangrid.db found. Pass --db /path/to/guardiangrid.db")


# One definition of the canonical timestamp, used by everything this file
# writes — including seed_registry_events, which used to disagree with the
# docstring above. See OCT-106.
TS_FMT = "%Y-%m-%d %H:%M:%S"

NOW = datetime.now()
def ts(delta_minutes=0):
    return (NOW + timedelta(minutes=delta_minutes)).strftime(TS_FMT)


MARK_GUARD  = "demo-seed"
MARK_AUTHOR = "Committee (demo)"
MARK_CODE   = "DM"

DEMO_FLATS = [
    ("A-101", "Rajinder Singh",   "+919876500101"),
    ("A-204", "Simran Kaur",      "+919876500204"),
    ("B-302", "Harpreet Gill",    "+919876500302"),
    ("B-405", "Manjit Sandhu",    "+919876500405"),
    ("C-108", "Neha Sharma",      "+919876500108"),
    ("C-210", "Gurpreet Bajwa",   "+919876500210"),
    ("D-112", "Amandeep Dhillon", "+919876500112"),
    ("D-306", "Kiran Malhotra",   "+919876500306"),
]

PURPOSES = ["Delivery", "Guest", "Maid", "Cab", "Maintenance", "Courier"]


def table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def clear(con):
    removed = {}
    plans = [
        ("arrival_requests",   "DELETE FROM arrival_requests WHERE guard=?", (MARK_GUARD,)),
        ("gate_passes",        "DELETE FROM gate_passes WHERE code LIKE ?", (MARK_CODE + "%",)),
        ("resident_notices",   "DELETE FROM resident_notices WHERE author=?", (MARK_AUTHOR,)),
    ]
    flats = [f[0] for f in DEMO_FLATS]
    marks = ",".join("?" * len(flats))
    plans += [
        ("household_requests", f"DELETE FROM household_requests WHERE flat_no IN ({marks})", flats),
        ("household_members",  f"DELETE FROM household_members WHERE flat_no IN ({marks})", flats),
        ("flats",              f"DELETE FROM flats WHERE flat_no IN ({marks})", flats),
    ]
    plates = [r[0] for r in REGISTERED]
    pmarks = ",".join("?" * len(plates))
    plans += [("vehicle_events",
               f"DELETE FROM vehicle_events WHERE plate IN ({pmarks})", plates)]
    for name, sql, args in plans:
        if not table_exists(con, name):
            continue
        removed[name] = con.execute(sql, args).rowcount
    con.commit()
    for k, v in removed.items():
        print(f"  removed {v:>3} from {k}")


def seed_flats(con):
    if not table_exists(con, "flats"):
        print("  flats: table missing, skipped")
        return
    n = 0
    for flat, owner, phone in DEMO_FLATS:
        cur = con.execute(
            "INSERT OR IGNORE INTO flats (flat_no, owner_name, whatsapp, added_at) "
            "VALUES (?,?,?,?)", (flat, owner, phone, ts(-60 * 24 * 30)))
        n += cur.rowcount
    print(f"  flats: {n} added ({len(DEMO_FLATS) - n} already present)")


def seed_members(con):
    if not table_exists(con, "household_members"):
        print("  household_members: table missing, skipped")
        return
    people = [
        ("A-101", "family", "Jaspreet Singh",  "+919876511101", "Son"),
        ("A-101", "staff",  "Lakshmi",         "+919876511102", "Househelp, mornings"),
        ("A-204", "family", "Arjun Kaur",      "+919876511204", "Brother"),
        ("B-302", "driver", "Sukhwinder",      "+919876511302", "Family driver"),
        ("C-108", "staff",  "Pooja",           "+919876511108", "Cook, evenings"),
        ("D-306", "family", "Ishaan Malhotra", "+919876511306", "Son, college"),
    ]
    for flat, kind, name, phone, note in people:
        con.execute(
            "INSERT INTO household_members (flat_no, kind, name, phone, note, added_at, added_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (flat, kind, name, phone, note, ts(-60 * 24 * random.randint(5, 40)), "resident"))
    print(f"  household_members: {len(people)} added")


def seed_requests(con):
    """Two waiting on the guard, one already approved, one rejected —
    so the approval queue has something in it and the history shows the
    decision actually being recorded."""
    if not table_exists(con, "household_requests"):
        print("  household_requests: table missing, skipped")
        return
    rows = [
        # flat,   kind,      name,             plate,        phone,            note,                      status,     mins_ago
        # OCT-105: a vehicle request's note is the resident form's "Make &
        # colour" field, and approval stores it as vehicle_model. Seeding a
        # sentence here put sentences in the model column, which read as a
        # product bug. Seed what a resident would actually type.
        ("A-204", "vehicle", "PB65AK2210",     "PB65AK2210", "+919876500204", "Maruti Baleno, red",       "PENDING",  -35),
        ("C-210", "family",  "Ravneet Bajwa",  "",           "+919876512210", "Daughter, moving in",      "PENDING",  -110),
        ("B-405", "vehicle", "PB10DR4417",     "PB10DR4417", "+919876500405", "Mahindra XUV700, black",   "APPROVED", -60 * 30),
        ("D-112", "staff",   "Unverified help", "",          "+919876512112", "No ID provided",           "REJECTED", -60 * 50),
    ]
    for flat, kind, name, plate, phone, note, status, mins in rows:
        decided_by = None if status == "PENDING" else "admin-demo"
        decided_at = None if status == "PENDING" else ts(mins + 45)
        con.execute(
            "INSERT INTO household_requests (flat_no, resident_phone, resident_name, kind, name, "
            "plate, phone, note, status, created_at, decided_by, decided_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (flat, phone, dict((f[0], f[1]) for f in DEMO_FLATS).get(flat, ""),
             kind, name, plate, phone, note, status, ts(mins), decided_by, decided_at))
    print(f"  household_requests: {len(rows)} added (2 pending)")


def seed_passes(con):
    """A pass a guard can actually type in during a walkthrough, one
    already used, one past its window. EXPIRED is never stored — the app
    derives it from valid_to at read time — so the expired one is just an
    ACTIVE row whose window has closed."""
    if not table_exists(con, "gate_passes"):
        print("  gate_passes: table missing, skipped")
        return
    rows = [
        # code,        flat,    resident,           visitor,           type,        plate,        from,   to,     multi, uses, status,      last_used
        (f"{MARK_CODE}4417", "A-101", "Rajinder Singh",  "Amazon Delivery", "Delivery",  "",           -30,    +240,   0,     0,    "ACTIVE",    None),
        (f"{MARK_CODE}8802", "B-302", "Harpreet Gill",   "Sunita (Maid)",   "Staff",     "",           -60*8,  +60*8,  1,     3,    "ACTIVE",    -45),
        (f"{MARK_CODE}2193", "C-108", "Neha Sharma",     "Rohit Verma",     "Guest",     "PB11XY7788", -60*26, -60*20, 0,     1,    "USED",      -60*21),
        (f"{MARK_CODE}5560", "D-306", "Kiran Malhotra",  "Blue Dart",       "Courier",   "",           -60*50, -60*48, 0,     0,    "ACTIVE",    None),
        (f"{MARK_CODE}7731", "A-204", "Simran Kaur",     "Ola Cab",         "Cab",       "PB08QQ1122", -20,    +120,   0,     0,    "CANCELLED", None),
    ]
    phones = dict((f[0], f[2]) for f in DEMO_FLATS)
    for code, flat, res, vis, vtype, plate, frm, to, multi, uses, status, used in rows:
        con.execute(
            "INSERT INTO gate_passes (code, flat_no, resident_name, resident_phone, "
            "visitor_name, visitor_type, vehicle_plate, valid_from, valid_to, "
            "multi_entry, uses, status, created_at, last_used_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, flat, res, phones.get(flat, ""), vis, vtype, plate,
             ts(frm), ts(to), multi, uses, status, ts(frm - 10),
             ts(used) if used is not None else None))
    print(f"  gate_passes: {len(rows)} added — {MARK_CODE}4417 is live and typeable")


def seed_arrivals(con):
    """Decided history only. A PENDING row would be flipped to EXPIRED by
    the background thread within three minutes, so the live hold is left
    for you to create in front of the client."""
    if not table_exists(con, "arrival_requests"):
        print("  arrival_requests: table missing, skipped")
        return
    rows = [
        # flat,  visitor,          purpose,       status,     decision, mins_ago, admitted
        ("A-101", "Swiggy rider",   "Delivery",    "APPROVED", "ALLOW",  -25,  True),
        ("B-302", "Sunita",         "Maid",        "APPROVED", "ALLOW",  -95,  True),
        ("C-108", "Unknown caller", "Guest",       "DECLINED", "DENY",   -150, False),
        ("D-306", "Plumber",        "Maintenance", "APPROVED", "ALLOW",  -210, True),
        ("A-204", "Courier",        "Courier",     "EXPIRED",  None,     -300, False),
        ("B-405", "Cab driver",     "Cab",         "APPROVED", "ALLOW",  -420, True),
    ]
    for flat, visitor, purpose, status, decision, mins, admitted in rows:
        con.execute(
            "INSERT INTO arrival_requests (flat_no, visitor_name, purpose, guard, status, "
            "decision, decided_by, decided_at, created_at, expires_at, admitted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (flat, visitor, purpose, MARK_GUARD, status, decision,
             "resident" if decision else None,
             ts(mins + 1) if decision else None,
             ts(mins), ts(mins + 3),
             ts(mins + 2) if admitted else None))
    print(f"  arrival_requests: {len(rows)} added (decided history, none pending)")


def seed_notices(con):
    if not table_exists(con, "resident_notices"):
        print("  resident_notices: table missing, skipped")
        return
    rows = [
        ("Water tanker timing changed",
         "From Monday the tanker will arrive at 6:30 AM instead of 8 AM. "
         "Please move vehicles from the basement ramp before then.", -60 * 20, 60 * 24 * 10),
        ("Visitor parking full on Sundays",
         "Please ask guests to use the Garden Gate overflow bay until the "
         "resurfacing work finishes.", -60 * 70, 60 * 24 * 5),
        ("Diwali security roster",
         "Two additional guards will be on duty at the Main Gate from 6 PM "
         "to 2 AM for the festival week.", -60 * 100, 60 * 24 * 20),
    ]
    for title, body, mins, life in rows:
        con.execute(
            "INSERT INTO resident_notices (title, body, author, created_at, expires_at) "
            "VALUES (?,?,?,?,?)", (title, body, MARK_AUTHOR, ts(mins), ts(mins + life)))
    print(f"  resident_notices: {len(rows)} added")


# ── Registered vehicles ──────────────────────────────────────────────
# The gate's KNOWN / UNKNOWN / BLACKLISTED word does not come from SQLite
# at all — resident_db.py keeps it in a JSON registry (OCT-76). Without
# that file every plate a guard checks answers UNKNOWN, including a
# resident's own car, so the screen cannot demonstrate the one decision it
# exists to support.
#
# Plates here are deliberately realistic Punjab registrations rather than
# DEMO####, because these are meant to read as residents' own cars. They
# also get their own events in vehicle_events, so a guard can look one up,
# see KNOWN with a name and flat, and find the same car in the log.

REGISTERED = [
    # plate,        owner,              flat,    block, model,               colour,   status
    ("PB10AB2025", "Rajinder Singh",   "A-101", "A", "Maruti Swift",       "Silver", "KNOWN"),
    ("PB65QK3344", "Simran Kaur",      "A-204", "A", "Hyundai i20",        "White",  "KNOWN"),
    ("PB08MN5566", "Harpreet Gill",    "B-302", "B", "Honda City",         "Grey",   "KNOWN"),
    ("PB10DR4417", "Manjit Sandhu",    "B-405", "B", "Mahindra XUV700",    "Black",  "KNOWN"),
    ("PB11XY7788", "Neha Sharma",      "C-108", "C", "Tata Nexon",         "Blue",   "KNOWN"),
    ("PB65AK2210", "Gurpreet Bajwa",   "C-210", "C", "Maruti Baleno",      "Red",    "KNOWN"),
    ("PB08QQ1122", "Amandeep Dhillon", "D-112", "D", "Hyundai Creta",      "White",  "KNOWN"),
    ("PB13LK9090", "Kiran Malhotra",   "D-306", "D", "Honda Activa",       "Grey",   "KNOWN"),
    ("HR26TT0099", "Vikram Chadha",    "B-302", "B", "Toyota Innova",      "Silver", "VISITOR"),
    ("PB07ZZ6611", "Former tenant",    "C-108", "C", "Maruti Alto",        "White",  "BLACKLISTED"),
]


def seed_registry():
    """Write the registry through resident_db when it can be imported, so
    the schema and the file location come from the module that owns them
    rather than from assumptions made here. Fall back to writing the JSON
    directly only if that import fails."""
    records = {}
    for plate, owner, flat, block, model, colour, status in REGISTERED:
        records[plate] = dict(
            plate_number=plate, resident_name=owner, flat_number=flat,
            block=block, phone="", vehicle_type=(
                "Motorcycle" if "Activa" in model else "Car"),
            vehicle_model=model, vehicle_color=colour, status=status,
            notes=("Vehicle barred by the committee"
                   if status == "BLACKLISTED" else ""),
            added_on=NOW.strftime("%Y-%m-%d"))

    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import resident_db as rdb
    except Exception as exc:
        target = os.environ.get("GG_RESIDENT_DB") or "/data/residents.json"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        import json
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, ensure_ascii=False)
        print(f"  registry: {len(records)} vehicles written directly to "
              f"{target} (resident_db import failed: {exc})")
        return

    for rec in records.values():
        rdb.db.add(rdb.Resident(**rec))
    print(f"  registry: {len(records)} vehicles via resident_db -> {rdb.DB_FILE}")


def seed_registry_events(con):
    """A few sightings per registered car, so a plate the guard looks up
    also appears in the vehicle log.

    OCT-106: this used to write the ISO 'T' separator, reasoning that it
    should match the bulk of the table. The bulk was wrong — matching it
    propagated the error, and between this file and seed_demo.py the two
    seeders put 1,782 of 1,789 rows back into T format on the night after
    OCT-64's migration ran. The canonical form is the space separator that
    `record_event()` writes, and it is what this file's own docstring said
    all along."""
    rows = []
    for plate, _o, _f, _b, model, _c, status in REGISTERED:
        vtype = "Motorcycle" if "Activa" in model else "Car"
        access = "BLACKLISTED" if status == "BLACKLISTED" else (
            "VISITOR" if status == "VISITOR" else "KNOWN")
        for day in range(3):
            for hour in random.sample(range(7, 22), random.randint(1, 2)):
                when = (NOW - timedelta(days=day)).replace(
                    hour=hour, minute=random.randint(0, 59),
                    second=random.randint(0, 59), microsecond=0)
                if when > NOW:
                    continue
                rows.append((plate, vtype, "", "ENTRY",
                             round(random.uniform(91.0, 99.0), 1), "",
                             when.strftime(TS_FMT), access, "Main Gate"))
                out = when + timedelta(minutes=random.randint(25, 300))
                if out < NOW:
                    rows.append((plate, vtype, "", "EXIT", 100.0, "",
                                 out.strftime(TS_FMT), access, "Main Gate"))
    con.executemany(
        "INSERT INTO vehicle_events (plate, vtype, state, event, confidence, "
        "image, timestamp, access, camera) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    print(f"  vehicle_events: {len(rows)} sightings for registered cars")


def main():
    ap = argparse.ArgumentParser(description="Seed the guard-side demo tables")
    ap.add_argument("--db", default=None, help="path to guardiangrid.db")
    ap.add_argument("--clear", action="store_true", help="remove seeded rows and exit")
    args = ap.parse_args()

    path = resolve_db(args.db)
    print(f"database: {path}\n")
    con = sqlite3.connect(path)

    print("clearing any previous gate seed:")
    clear(con)

    if args.clear:
        con.close()
        print("\nCleared. Nothing seeded.")
        return

    print("\nseeding:")
    seed_flats(con)
    seed_members(con)
    seed_requests(con)
    seed_passes(con)
    seed_arrivals(con)
    seed_notices(con)
    seed_registry()
    seed_registry_events(con)
    con.commit()
    con.close()

    print("\nThe Security Gate screen now has: two approvals waiting, a live "
          f"pass code ({MARK_CODE}4417), several decided arrivals and three "
          "notices.")
    print("To demo a live hold, use Manual Capture on the gate screen and "
          "create one in front of the client — it appears on the resident's "
          "phone and expires after three minutes if nobody answers.")


if __name__ == "__main__":
    main()
