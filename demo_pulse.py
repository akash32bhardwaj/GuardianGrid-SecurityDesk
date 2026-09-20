#!/usr/bin/env python3
"""
demo_pulse.py — keep the demo site alive between nightly reseeds.  OCT-107
===========================================================================

THE PROBLEM THIS SOLVES

`reset_demo.sh` runs at 03:00 and writes a whole day's narrative in one go.
After that, nothing happens. Measured on 20 Sep at 22:54 IST: the newest
vehicle event was 08:28 and the entire day held 11 events — fourteen hours
of stillness on a product sold on live monitoring, on the one site every
prospect is sent to.

That is the third recurrence of this shape (OCT-46, OCT-59, OCT-107). The
first two were the reseed FAILING. This one is the reseed succeeding and
then the site sitting still until the next one. A prospect who opens the
link you sent at breakfast, at four in the afternoon, sees a dead system.

It also produced a client-facing number: with no traffic in the last twelve
hours `/api/score/live` reported `vehicles_total: 0`, which combined with a
handful of test incidents to drive the security score to 15 and fire a
watchdog alarm that spoke aloud. A stale demo does not fail quietly.

WHAT THIS DOES

Adds two to four plausible vehicle events per hour, spread through the hour
that has just passed. Most are plates from the resident registry, so a gate
lookup resolves to a real name and flat; some are unknown, so the Access Mix
stays interesting rather than uniformly green.

FOUR PROPERTIES THAT ARE NOT OPTIONAL, each earned from a finding in the
register rather than chosen for tidiness:

  1. It writes through `db.record_event()`, never a raw INSERT.
     Six things already write to `vehicle_events` and only record_event
     canonicalises the timestamp (OCT-64). Two of the other five are demo
     seeders that put the column back into mixed format every night
     (OCT-106). Adding a seventh bypassing writer while that finding is
     open would be absurd.

  2. It never writes a future timestamp (OCT-65).
     A demo showing a detection that has not happened yet is the most
     obvious tell there is.

  3. ENTRY and EXIT are balanced against what is actually on site.
     `rebuild_today_state` replays these to decide who is still inside
     (OCT-64). Writing unmatched ENTRYs would fill the "on site" count with
     cars that never leave, which is worse than an empty screen because it
     looks like data.

  4. It says what it did, every run (OCT-52).
     A silent job that stops is precisely how we arrived here three times.
     The summary line is the thing that makes a fourth time noticeable.

USAGE

    python3 /app/demo_pulse.py                 # write the pulse
    python3 /app/demo_pulse.py --dry-run       # show what it would write
    python3 /app/demo_pulse.py --count 6       # override the event count

CRON (through run_job.sh, so a failure reaches a human):

    7 6-23 * * * /opt/octa/run_job.sh demo-pulse octa-demo \
        docker exec octa-demo python3 /app/demo_pulse.py \
        >> /var/log/octa-jobs.log 2>&1

Minute 7 rather than 0 so it never collides with the reseed or the audit.
Hours 6–23 because the quiet-hours rule below already thins overnight
traffic, and a job that does nothing is better not run at all.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

import db as gg_db  # noqa: E402  — resolves its own path (OCT-66)


# ── Vocabulary ────────────────────────────────────────────────────────
# Taken from the live column values on 20 Sep, NOT invented. OCT-61 was an
# entire finding about `Car` and `car` being counted as two vehicle types;
# a pulse job writing slightly-wrong values would seed that back nightly.

CAMERAS = [
    ("Basement Entry", 45),
    ("Garden Gate", 25),
    ("Visitor Parking", 15),
    ("Main Gate", 13),
    ("Back Gate", 2),
]

VTYPES = [("Car", 80), ("Motorcycle", 15), ("Truck", 4), ("Bus", 1)]

# Access values the registry can produce, mapped to what a gate records.
REGISTRY_ACCESS = {"KNOWN", "APPROVED", "VISITOR", "BLACKLISTED", "RESIDENT"}

# A barred vehicle turning up is realistic and shows the product working,
# but each one costs 10 points on the security score (OCT-108), so it stays
# rare rather than becoming a nightly feature of the demo.
BLACKLIST_CHANCE = 0.05

# Share of events that use a plate nobody has registered. Keeps the Access
# Mix from reading 100% verified, which would look staged.
UNKNOWN_CHANCE = 0.22

# Visitors get a generated plate and VISITOR access.
VISITOR_CHANCE = 0.12

# Events land inside the window that has just passed, never ahead of now.
WINDOW_MINUTES = 58

# If the table already has something this recent, assume a pulse has just
# run and do nothing. Protects against a double cron or a manual re-run.
SKIP_IF_NEWER_THAN_MINUTES = 20

# Overnight is quiet on a real site, and a demo that shows rush-hour
# traffic at 3am is its own kind of tell.
QUIET_START_HOUR = 0
QUIET_END_HOUR = 6
QUIET_MAX_EVENTS = 1

STATE_REGISTERED = "REGISTERED"
STATE_UNKNOWN = "UNKNOWN"


def _weighted(pairs):
    names = [p[0] for p in pairs]
    weights = [p[1] for p in pairs]
    return random.choices(names, weights=weights, k=1)[0]


def _random_plate() -> str:
    """A plate that looks Indian and is not in the registry."""
    series = random.choice(["PB", "PB", "PB", "HR", "CH", "DL", "HP"])
    return (f"{series}{random.randint(10, 99):02d}"
            f"{random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
            f"{random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
            f"{random.randint(1000, 9999)}")


def _registry():
    """Registered vehicles, as dicts. Empty list rather than an exception:
    a pulse with no registry still beats a dead site."""
    try:
        from resident_db import db as rdb
        return [r for r in rdb.get_all() if r.get("plate_number")]
    except Exception as exc:
        print(f"[PULSE] registry unavailable, using unknown plates only: {exc}",
              file=sys.stderr)
        return []


def _on_site(con, today: str) -> dict:
    """plate -> net ENTRY count for today, so EXITs can be matched.

    Uses the normalised-timestamp comparison from OCT-64 rather than raw
    text, because the demo database currently holds both formats (OCT-106)
    and a plain date() on the raw column would miss rows.
    """
    rows = con.execute(
        "SELECT plate, "
        "  SUM(CASE WHEN event='ENTRY' THEN 1 ELSE 0 END) - "
        "  SUM(CASE WHEN event='EXIT'  THEN 1 ELSE 0 END) AS net "
        "FROM vehicle_events "
        "WHERE date(REPLACE(timestamp,'T',' ')) = ? "
        "GROUP BY plate HAVING net > 0",
        (today,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _newest_age_minutes(con) -> float | None:
    row = con.execute(
        "SELECT MAX(datetime(REPLACE(timestamp,'T',' '))) FROM vehicle_events"
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        newest = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.now() - newest).total_seconds() / 60.0


def build_events(count: int) -> list[dict]:
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    registry = _registry()

    known = [r for r in registry
             if (r.get("status") or "").upper() not in ("BLACKLISTED",)]
    barred = [r for r in registry
              if (r.get("status") or "").upper() == "BLACKLISTED"]

    con = sqlite3.connect(gg_db.DB_PATH, timeout=10)
    try:
        inside = _on_site(con, today)
    finally:
        con.close()

    events: list[dict] = []
    for _ in range(count):
        # Prefer taking a car OUT that is currently in, so the on-site
        # count stays believable instead of climbing all day.
        exiting = [p for p in inside if inside[p] > 0]
        do_exit = bool(exiting) and random.random() < 0.45

        if do_exit:
            plate = random.choice(exiting)
            inside[plate] -= 1
            match = next((r for r in registry
                          if r.get("plate_number") == plate), None)
            event = "EXIT"
        else:
            roll = random.random()
            if barred and roll < BLACKLIST_CHANCE:
                match = random.choice(barred)
            elif roll < BLACKLIST_CHANCE + UNKNOWN_CHANCE or not known:
                match = None
            else:
                match = random.choice(known)
            plate = match["plate_number"] if match else _random_plate()
            event = "ENTRY"
            inside[plate] = inside.get(plate, 0) + 1

        if match:
            status = (match.get("status") or "KNOWN").upper()
            access = status if status in REGISTRY_ACCESS else "KNOWN"
            if access == "RESIDENT":
                access = "KNOWN"          # OCT-61: one vocabulary
            vtype = match.get("vehicle_type") or _weighted(VTYPES)
            state = STATE_REGISTERED
        else:
            access = "VISITOR" if random.random() < VISITOR_CHANCE else "UNKNOWN"
            vtype = _weighted(VTYPES)
            state = STATE_UNKNOWN

        # Never ahead of now (OCT-65). Two seconds of margin so a slow run
        # cannot drift past the clock between building and writing.
        offset = random.randint(2, WINDOW_MINUTES * 60)
        stamp = now - timedelta(seconds=offset)

        events.append({
            "plate": plate,
            "type": vtype if vtype in dict(VTYPES) else "Car",
            "state": state,
            "event": event,
            "confidence": round(random.uniform(0.82, 0.99), 2),
            "image": "",
            "timestamp": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            "access": access,
            "camera": _weighted(CAMERAS),
        })

    events.sort(key=lambda e: e["timestamp"])
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description="Keep the demo site alive (OCT-107)")
    ap.add_argument("--count", type=int, default=None,
                    help="number of events to write (default 2-4, fewer overnight)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written and change nothing")
    ap.add_argument("--force", action="store_true",
                    help="write even if a recent event suggests a pulse just ran")
    args = ap.parse_args()

    now = datetime.now()

    if args.count is not None:
        count = max(0, args.count)
    elif QUIET_START_HOUR <= now.hour < QUIET_END_HOUR:
        count = random.randint(0, QUIET_MAX_EVENTS)
    else:
        count = random.randint(2, 4)

    if count == 0:
        print(f"[PULSE] {now:%Y-%m-%d %H:%M} quiet hours — no events written")
        return 0

    con = sqlite3.connect(gg_db.DB_PATH, timeout=10)
    try:
        age = _newest_age_minutes(con)
    finally:
        con.close()

    if (age is not None and age < SKIP_IF_NEWER_THAN_MINUTES
            and not args.force and args.count is None):
        print(f"[PULSE] newest event is {age:.0f} min old "
              f"(< {SKIP_IF_NEWER_THAN_MINUTES}) — skipping, use --force to override")
        return 0

    events = build_events(count)

    if args.dry_run:
        print(f"[PULSE] DRY RUN — would write {len(events)} events to {gg_db.DB_PATH}")
        for e in events:
            print(f"    {e['timestamp']}  {e['event']:5}  {e['plate']:12} "
                  f"{e['access']:12} {e['camera']}")
        return 0

    written = 0
    for e in events:
        try:
            gg_db.record_event(e)
            written += 1
        except Exception as exc:
            print(f"[PULSE] FAILED to write {e['plate']}: {exc}", file=sys.stderr)

    # The summary is the point of the job existing, not decoration. This
    # line is what makes a fourth stalled demo noticeable (OCT-52).
    con = sqlite3.connect(gg_db.DB_PATH, timeout=10)
    try:
        age = _newest_age_minutes(con)
        today_total = con.execute(
            "SELECT COUNT(*) FROM vehicle_events "
            "WHERE date(REPLACE(timestamp,'T',' ')) = ?",
            (now.strftime("%Y-%m-%d"),)).fetchone()[0]
    finally:
        con.close()

    kinds = {}
    for e in events:
        kinds[e["access"]] = kinds.get(e["access"], 0) + 1
    mix = " ".join(f"{k}:{v}" for k, v in sorted(kinds.items()))

    print(f"[PULSE] {now:%Y-%m-%d %H:%M}  wrote {written}/{len(events)}  "
          f"{mix}  today={today_total}  newest={age:.0f}min ago  db={gg_db.DB_PATH}")

    if written < len(events):
        return 1        # run_job.sh turns a non-zero exit into a WhatsApp
    return 0


if __name__ == "__main__":
    sys.exit(main())
