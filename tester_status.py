"""
tester_status.py -- who is actually in the closed test, and still using it.

    sudo docker exec octa-demo python /app/tester_status.py

Google needs 12 testers OPTED IN CONTINUOUSLY for the 14 days before you
apply, and weighs whether they actually used the app. Those are two
different questions and this prints both:

  OPTED IN    first successful login. Proof, not a guess: the app cannot
              be installed from a closed track without opting in first,
              so a login means they opted in on or before that date.
  LAST OPENED last authenticated request (OCT-132). last_login alone
              cannot answer this -- the token lasts 30 days, so somebody
              who logged in once and never returned looks identical to
              somebody who opens it daily.

The apply date is set by the 12TH-EARLIEST opt-in, never the latest. With
exactly twelve testers your slowest friend is your launch date; with
fifteen, the three slowest stop mattering. That is the argument for
recruiting above twelve, and this prints the date either way.

Rows in flat_pins that are not in the roster (test flats, the reviewer's
flat) are listed separately and never counted.
"""

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "/app")
import resident_app as ra                                   # noqa: E402

NEEDED = 12
TEST_DAYS = 14
STALE_HOURS = 48          # beyond this, "still using it" is doing no work

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(HERE, "closed_test_roster.json"), encoding="utf-8") as f:
        ROSTER = json.load(f)
except (OSError, ValueError) as e:
    sys.exit("Cannot read closed_test_roster.json: %s" % e)

TESTERS = [t for t in ROSTER.get("testers", [])
           if t.get("email", "").strip() and t.get("flat", "").strip()]
if not TESTERS:
    sys.exit("No testers in closed_test_roster.json.")


def _parse(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _ago(dt, now):
    if dt is None:
        return ""
    h = (now - dt).total_seconds() / 3600.0
    if h < 1:
        return "just now"
    if h < 48:
        return "%dh ago" % int(h)
    return "%dd ago" % int(h / 24)


def _parse_day(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except ValueError:
        return None


def resolve(testers, rows):
    """Who is genuinely opted in, counted ONCE per person.

    The trap this exists for: a login on a SHARED flat proves that one of
    the two people opted in, and cannot say which. Crediting both doubles
    the count. Six would have been reported for three real opt-ins, and
    the cost of believing it is applying with fewer than twelve and being
    refused -- after fourteen days.

    So: an explicit opted_on always counts. A login counts for a flat only
    one person uses. On a shared flat with a login and nobody confirmed,
    exactly ONE person is credited and the flat is flagged as needing a
    name.
    """
    by_flat = {}
    for t in testers:
        by_flat.setdefault(ra._norm_flat(t["flat"]), []).append(t)

    status, ambiguous = {}, []
    for flat, group in by_flat.items():
        row = rows.get(flat)
        login = _parse(row["last_login"]) if row else None
        named = [t for t in group if _parse_day(t.get("opted_on"))]

        for t in named:
            status[id(t)] = ("in", _parse_day(t["opted_on"]), "confirmed")

        if login and not named:
            # One person on this flat is demonstrably in. Credit one.
            if len(group) == 1:
                status[id(group[0])] = ("in", login, "")
            else:
                status[id(group[0])] = ("in", login, "one of %d on this flat"
                                        % len(group))
                ambiguous.append(flat)
    return status, ambiguous


def main():
    now = datetime.now()
    con = ra._con()
    rows = {}
    for r in con.execute("SELECT flat_no, last_login, last_seen FROM flat_pins"):
        rows[ra._norm_flat(r["flat_no"])] = r
    con.close()

    shared = {}
    for t in TESTERS:
        shared.setdefault(ra._norm_flat(t["flat"]), []).append(t)
    status, ambiguous = resolve(TESTERS, rows)

    print()
    print("  OCTA RESIDENT -- CLOSED TEST   (%s)" % now.strftime("%d %b %Y %H:%M"))
    print("  " + "-" * 88)
    print("  %-26s %-7s %-14s %-12s %s"
          % ("TESTER", "FLAT", "OPTED IN", "LAST OPENED", "NOTE"))
    print("  " + "-" * 88)

    opted_dates, active = [], 0
    for t in TESTERS:
        flat = ra._norm_flat(t["flat"])
        row = rows.get(flat)
        seen = _parse(row["last_seen"]) if row else None
        who = t.get("name") or t["email"].split("@")[0]
        st = status.get(id(t))

        if st:
            opted_dates.append(st[1])
        if seen and (now - seen) <= timedelta(hours=STALE_HOURS):
            active += 1

        note = st[2] if st else ""
        if not note and len(shared.get(flat, [])) > 1:
            note = "shared flat"
        print("  %-26s %-7s %-14s %-12s %s"
              % (who[:26], flat,
                 st[1].strftime("%d %b") if st else "-- not yet --",
                 _ago(seen, now) or "--", note))

    print("  " + "-" * 88)
    extra = sorted(set(rows) - {ra._norm_flat(t["flat"]) for t in TESTERS})
    if extra:
        print("  not testers (ignored): %s" % ", ".join(extra))
    if ambiguous:
        print("  %s: somebody logged in but the flat is shared -- ask who, and"
              % ", ".join(ambiguous))
        print("  put the date in closed_test_roster.json under opted_on.")

    n = len(opted_dates)
    print()
    print("  OPTED IN        %d of %d" % (n, NEEDED))
    print("  USED IT IN %dh  %d" % (STALE_HOURS, active))

    if n < NEEDED:
        print("  APPLY ON        -- need %d more --" % (NEEDED - n))
        print()
        print("  Chase the %d who have not opted in. Every day one of them"
              % (NEEDED - n))
        print("  waits is a day added to your launch date.")
    else:
        opted_dates.sort()
        gating = opted_dates[NEEDED - 1]
        apply_on = gating + timedelta(days=TEST_DAYS)
        left = (apply_on.date() - now.date()).days
        print("  APPLY ON        %s%s"
              % (apply_on.strftime("%d %b %Y"),
                 "  (today or later -- go)" if left <= 0 else "  (%d days)" % left))
        print("  SET BY          the 12th opt-in, %s" % gating.strftime("%d %b"))
        if active < NEEDED:
            print()
            print("  WARNING: %d of %d have not opened the app in %dh. Google"
                  % (NEEDED - active, NEEDED, STALE_HOURS))
            print("  weighs USE, not just opt-in. Nudge the quiet ones.")
    print()


if __name__ == "__main__":
    main()
