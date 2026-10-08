"""
migrate_state_column.py — repair the `state` column in vehicle_events
=====================================================================
WHY THIS EXISTS

`vehicle_events.state` holds the number plate's ISSUING STATE -- Punjab,
Haryana -- which is what core/anpr_engine.py produces via STATE_CODES.

Two writers put a RESIDENT STATUS there instead:

  gate_capture.py   wrote KNOWN / UNKNOWN / BLACKLISTED  (OCT-144)
  demo_pulse.py     wrote REGISTERED / UNKNOWN           (OCT-145)

Both are fixed. This repairs what they already wrote.

WHY `state` AND NOT `access` OR `vtype`

Those two have read-side canonicalisers -- canon_access() and canon_vtype()
-- so a stale spelling is folded back to one vocabulary every time it is
read. There is NO canon_state(). Nothing anywhere maps "REGISTERED" to a
place. So this column is the only one of the three whose history stays
wrong until something rewrites it.

WHAT IT DOES

For every row whose `state` is neither empty nor a real Indian state name,
it DERIVES the state from the plate prefix using the same STATE_CODES table
the camera uses, and falls back to "" when the prefix is unrecognised.

Deriving rather than blanking is not a guess: the prefix IS the issuing
state, and that is precisely how the ANPR arrives at state_name. It also
leaves the column consistent with what demo_pulse now writes, instead of
adding 247 more empties to a column that is already mostly empty.

USAGE

    python migrate_state_column.py                    # dry run, changes nothing
    python migrate_state_column.py --apply            # back up, then migrate
    python migrate_state_column.py --db /data/guardiangrid.db --apply

Idempotent: a second run finds nothing to do.
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime


def resolve_db(explicit=None):
    for c in (explicit, os.environ.get("GG_DB"),
              "/data/guardiangrid.db", "guardiangrid.db"):
        if c and os.path.exists(c):
            return os.path.abspath(c)
    sys.exit("ERROR: no guardiangrid.db found. Pass --db /path/to/guardiangrid.db")


def state_table():
    """The one table of Indian state codes. Imported, never copied -- a
    second copy here would drift from the engine's, which is OCT-24's
    shape and the reason OCT-145 imports it too."""
    try:
        from core.anpr_engine import STATE_CODES
        return dict(STATE_CODES)
    except Exception as e:
        sys.exit("ERROR: cannot import core.anpr_engine.STATE_CODES (%s).\n"
                 "Run this inside the container, where cv2 is available.\n"
                 "Refusing to proceed with a local copy of the table." % e)


def main():
    ap = argparse.ArgumentParser(description="Repair vehicle_events.state")
    ap.add_argument("--db", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    args = ap.parse_args()

    path = resolve_db(args.db)
    codes = state_table()
    valid = set(codes.values())
    print("database : %s" % path)
    print("known states: %d" % len(valid))
    print()

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row

    before = dict(con.execute(
        "SELECT state, COUNT(*) FROM vehicle_events GROUP BY state"))
    bad_values = sorted(k for k in before
                        if k is not None and k != "" and k not in valid)
    if not bad_values:
        print("Nothing to do: every non-empty state is a real place name.")
        con.close()
        return 0

    print("values that are not places:")
    for v in bad_values:
        print("  %-16r %d row(s)" % (v, before[v]))
    print()

    # `rowid AS _rid`, not a bare `rowid`. vehicle_events declares
    # `id INTEGER PRIMARY KEY`, which makes rowid an ALIAS of it, and
    # SQLite then returns the column under the name `id` -- so r["rowid"]
    # raises IndexError on the real table while working perfectly on a
    # fixture that has no integer primary key. An explicit alias is the
    # same name either way.
    rows = con.execute(
        "SELECT rowid AS _rid, plate, state FROM vehicle_events "
        "WHERE state IS NOT NULL AND state <> ''").fetchall()
    plan = []
    for r in rows:
        if r["state"] in valid:
            continue
        new = codes.get(str(r["plate"] or "")[:2].upper(), "")
        plan.append((r["_rid"], r["plate"], r["state"], new))

    derived = sum(1 for p in plan if p[3])
    blanked = len(plan) - derived
    print("plan: %d row(s) -- %d derived from the plate, %d blanked"
          % (len(plan), derived, blanked))
    for rowid, plate, old, new in plan[:10]:
        print("  %-12s %-14r -> %r" % (plate, old, new))
    if len(plan) > 10:
        print("  ... and %d more" % (len(plan) - 10))
    print()

    if not args.apply:
        print("DRY RUN -- nothing written. Re-run with --apply.")
        con.close()
        return 0

    # The timestamp goes BEFORE the extension so the backup still ends in
    # .db and the repo's existing `*.db` rule covers it. Named the other way
    # round -- guardiangrid.db.pre-state-migration-… -- it is not ignored,
    # and a database copy shows up as an untracked file waiting to be
    # committed. A naming choice that cannot be forgotten beats a .gitignore
    # line that can (OCT-17).
    root, ext = os.path.splitext(path)
    backup = "%s.pre-state-migration-%s%s" % (
        root, datetime.now().strftime("%Y%m%d-%H%M%S"), ext or ".db")
    con.close()
    shutil.copy2(path, backup)
    print("backup   : %s" % backup)

    con = sqlite3.connect(path)
    con.executemany("UPDATE vehicle_events SET state=? WHERE rowid=?",
                    [(new, rowid) for rowid, _p, _o, new in plan])
    con.commit()

    after = dict(con.execute(
        "SELECT state, COUNT(*) FROM vehicle_events GROUP BY state"))
    still = sorted(k for k in after
                   if k is not None and k != "" and k not in valid)
    con.close()

    print()
    print("after:")
    for k in sorted(after, key=lambda x: -after[x]):
        print("  %-20r %d" % (k, after[k]))
    print()
    if still:
        print("FAILED: these are still not places: %s" % still)
        print("        the backup above is the database as it was.")
        return 1
    print("OK: every non-empty state is now a real place name.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
