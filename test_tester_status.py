"""The two counting bugs tester_status.py had on 13 Oct, pinned.

Both were double-counts on SHARED FLATS and SHARED PEOPLE -- the exact
failure resolve()'s own docstring describes, and the cost of believing
either is applying to Google with fewer than twelve testers and being
refused after fourteen days.

resident_app is stubbed so this runs anywhere; everything else is the real
module, imported from disk.
"""
import os
import sys
import types
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

# stub resident_app: tester_status only uses _norm_flat and _con
stub = types.ModuleType("resident_app")
stub._norm_flat = lambda f: str(f).strip().upper()
stub._con = lambda: (_ for _ in ()).throw(AssertionError("DB must not be touched"))
sys.modules["resident_app"] = stub
sys.path.insert(0, HERE)

import tester_status as ts  # noqa: E402

NOW = datetime(2026, 10, 13, 11, 31, 0)
fails = []


def row(last_login=None, last_seen=None):
    return {"last_login": last_login, "last_seen": last_seen}


# ── 1. a shared flat must count ONCE as active ──────────────────────
testers = [
    {"email": "a@x.com", "flat": "A-101", "name": "Akash"},
    {"email": "b@x.com", "flat": "A-101", "name": "Amandeep S"},
]
rows = {"A-101": row(last_seen="2026-10-13 11:00:00")}
got = ts.count_active(testers, rows, NOW, stub._norm_flat)
if got != 1:
    fails.append(f"shared flat, one session -> active={got}, want 1 "
                 f"(this is the bug: one last_seen credited to two rows)")
else:
    print("  shared flat, one fresh session      -> active = 1")

# ── 2. two separate flats, both fresh, count twice ──────────────────
testers2 = [
    {"email": "a@x.com", "flat": "A-101"},
    {"email": "b@x.com", "flat": "B-302"},
]
rows2 = {"A-101": row(last_seen="2026-10-13 11:00:00"),
         "B-302": row(last_seen="2026-10-13 10:00:00")}
got = ts.count_active(testers2, rows2, NOW, stub._norm_flat)
if got != 2:
    fails.append(f"two flats both fresh -> active={got}, want 2")
else:
    print("  two distinct flats, both fresh      -> active = 2")

# ── 3. stale sessions do not count ──────────────────────────────────
rows3 = {"A-101": row(last_seen="2026-10-09 11:00:00")}
got = ts.count_active([{"email": "a@x.com", "flat": "A-101"}], rows3, NOW, stub._norm_flat)
if got != 0:
    fails.append(f"4-day-old session -> active={got}, want 0")
else:
    print("  session older than 48h              -> active = 0")

# ── 4. one person, two addresses, two flats = ONE tester ────────────
same = [
    {"email": "akash08@x.com", "flat": "A-101", "person": "akash"},
    {"email": "aakash.b@x.com", "flat": "A-204", "person": "akash"},
]
if len({ts._person(t) for t in same}) != 1:
    fails.append("two rows with the same `person` resolved to two people")
else:
    print("  two addresses, one `person` key     -> 1 person")

# a roster with no `person` key must behave exactly as before
nokey = [{"email": "x@x.com", "flat": "A-1"}, {"email": "y@x.com", "flat": "A-2"}]
if len({ts._person(t) for t in nokey}) != 2:
    fails.append("rows without `person` collapsed together -- backwards incompatible")
else:
    print("  rows with no `person` key           -> unchanged, 2 people")

# ── self-check: the PRE-FIX logic must give the wrong answer ────────
# The old count lived inline in main() and incremented once per ROW:
#     for t in TESTERS:
#         seen = rows[flat]["last_seen"]
#         if seen and fresh: active += 1
# Reconstructed here, because a test that has never been seen to fail has
# not been shown to test anything.
pre = 0
for t in testers:                       # the shared-flat case from test 1
    r = rows.get(stub._norm_flat(t["flat"]))
    seen = ts._parse(r["last_seen"]) if r else None
    if seen and (NOW - seen) <= timedelta(hours=ts.STALE_HOURS):
        pre += 1
if pre != 2:
    fails.append(f"reconstruction of the old loop gave {pre}, expected it to "
                 f"double-count to 2 -- the reconstruction is wrong, so the "
                 f"pass above proves nothing")
else:
    print(f"  self-check: old per-row loop gave {pre} for one session  <- the bug")

print()
if fails:
    print("FAIL")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print("PASS - a shared flat and a shared person each count once")
