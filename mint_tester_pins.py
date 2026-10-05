import json
import os, sys
sys.path.insert(0, "/app")
import resident_app as ra

LINK = os.environ.get("OPTIN_LINK", "").strip()
if not LINK:
    sys.exit("OPTIN_LINK is empty. Set it, then run this again.")

# The link is checked, not just required. Both of these happened on 2 Oct
# and each one burned a full run of eight PINs:
#
#   1. The command was pasted with a PLACEHOLDER still in it, and twelve
#      messages were printed saying "PASTE_THE_LINK_HERE". Non-empty is not
#      the same as real. whatsapp_alerts.py already learned this -- it has
#      a "PASTE_YOUR" check and a Content-SID shape guard for exactly this.
#
#   2. The STORE LISTING url was used instead of the opt-in link. They look
#      equally plausible and only one works: a closed-test app is not
#      published publicly, so a tester who opens
#      play.google.com/store/apps/details?id=... before opting in gets
#      "Item not found". The opt-in link is play.google.com/apps/testing/...
#      and Play Console labels it "Join on the web".
#
# A PIN is shown once. A bad link means every PIN printed in that run has
# to be thrown away and re-minted, so the link is worth more scrutiny than
# it looks.
_low = LINK.lower()
if "paste" in _low or "<" in LINK or "your_" in _low or " " in LINK:
    sys.exit("OPTIN_LINK looks like a placeholder (%r).\n"
             "Paste the real URL from Play Console: Testers tab -> "
             "'Join on the web'." % LINK)
if "/store/apps/details" in _low:
    sys.exit("OPTIN_LINK is the public STORE LISTING, not the opt-in link.\n"
             "A closed-test app is not published publicly, so testers who "
             "open that url before opting in get 'Item not found'.\n"
             "Use the one Play Console calls 'Join on the web' -- "
             "Testers tab, scroll down. It looks like\n"
             "  https://play.google.com/apps/testing/<package name>")
if not _low.startswith("https://"):
    sys.exit("OPTIN_LINK must start with https:// (got %r)." % LINK)
if "play.google.com/apps/testing/" not in _low:
    # Not fatal: Play Console has changed this url before and may again.
    # But say so loudly, because the cost of being wrong is eight PINs.
    print("!! WARNING: OPTIN_LINK is not the play.google.com/apps/testing/")
    print("!! form this script expects. If Play Console gave you this url "
          "under")
    print("!! 'Join on the web', it is probably fine. If you typed it from "
          "memory,")
    print("!! stop now -- every PIN below would have to be re-minted.")
    print("!! Link: %s" % LINK)
    print()

# The key every PIN is hashed with.
#
# Importing this module does NOT load it: _SECRET is b"" at module level
# and only init_resident_app() fills it in, which a docker exec never
# calls. OCT-95 fixed exactly this for _DB_PATH and left the variable
# three lines below it alone. Hashing with b"" would write a valid-looking
# hash that can never match, and the resident would be told their PIN was
# wrong -- OCT-94's failure through OCT-95's door. Load it the way init
# does, then prove it is the same key the running app recorded.
ra._SECRET = ra._load_or_create_secret()
if ra._SECRET_SOURCE != "file":
    sys.exit("Key source is %r, expected 'file'. Refusing to mint PINs."
             % ra._SECRET_SOURCE)
if len(ra._SECRET) < 32:
    sys.exit("Key is %d bytes, expected at least 32. Refusing."
             % len(ra._SECRET))
stored = ra._state_get("pin_key_fp")
if stored and stored != ra._secret_fp():
    sys.exit("Key fingerprint %s does not match the stored %s. Refusing."
             % (ra._secret_fp(), stored))

# The roster lives in closed_test_roster.json, which tester_status.py also
# reads. One list, not two: a second copy of the same twelve addresses is
# how OCT-24, OCT-48, OCT-53 and OCT-80 all started, and adding a tester
# should be editing a data file rather than editing Python.
_ROSTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "closed_test_roster.json")
try:
    with open(_ROSTER_PATH, encoding="utf-8") as _f:
        _ROSTER = json.load(_f)
except (OSError, ValueError) as e:
    sys.exit("Cannot read %s: %s" % (_ROSTER_PATH, e))

TESTERS = [(t["email"].strip(), ra._norm_flat(t["flat"]))
           for t in _ROSTER.get("testers", [])
           if t.get("email", "").strip() and t.get("flat", "").strip()]
if not TESTERS:
    sys.exit("No testers in %s -- nothing to mint." % _ROSTER_PATH)

# D-404 is deliberately NOT in this list. It is Google's reviewer's flat,
# its PIN goes into App Access before the release is rolled out, and this
# script runs after rollout. Minting it here would replace the PIN already
# submitted and hand the reviewer a login that fails.
REVIEWER_FLAT = ra._norm_flat(_ROSTER.get("reviewer_flat", "D-404"))
flats = sorted({f for _, f in TESTERS})
assert REVIEWER_FLAT not in flats, (
    "reviewer flat %s must not be re-minted here -- remove it from "
    "closed_test_roster.json" % REVIEWER_FLAT)

# A flat that is not in seed_gate.DEMO_FLATS has no owner, no household
# members, no registered car and no gate history, so a tester who logs
# into it sees an empty app -- which reads as a broken app, and is worse
# for engagement than sharing an active flat with someone else.
#
# This has now happened twice. D-404 was added by hand during testing and
# the seeder wrote sightings for eight other flats and none for the one
# every screenshot was taken from. The comment recording that is in
# seed_gate.py, directly above the line that fixed it -- and on 4 Oct
# C-101 was created by hand anyway and handed to a tester.
#
# A comment is not a check. This is the check.
try:
    import seed_gate
    _SEEDED = {ra._norm_flat(f[0]) for f in seed_gate.DEMO_FLATS}
except Exception as _e:
    _SEEDED = None
    print("!! WARNING: cannot read seed_gate.DEMO_FLATS (%s)." % _e)
    print("!! Skipping the empty-flat check. If a flat below is not in the")
    print("!! seeder, its tester will open the app to a blank screen.")
    print()

if _SEEDED is not None:
    _unseeded = sorted(f for f in flats if f not in _SEEDED)
    if _unseeded and os.environ.get("OCTA_ALLOW_UNSEEDED", "").strip() != "1":
        sys.exit(
            "These flats are not in seed_gate.DEMO_FLATS: %s\n"
            "\n"
            "A flat the seeder does not know about has no owner, no members,\n"
            "no registered vehicle and no gate history. The tester would log\n"
            "in successfully and see an empty app, then stop opening it.\n"
            "\n"
            "Add them to DEMO_FLATS in seed_gate.py -- with a vehicle in\n"
            "REGISTERED and a row in seed_arrivals, or the flat is still\n"
            "blank where it matters -- then reseed, then run this again.\n"
            "\n"
            "To mint anyway: OCTA_ALLOW_UNSEEDED=1 (the tester gets a\n"
            "working login to an empty society)."
            % ", ".join(_unseeded))
    if _unseeded:
        print("!! OCTA_ALLOW_UNSEEDED=1 -- minting %d flat(s) the seeder does"
              % len(_unseeded))
        print("!! not populate: %s" % ", ".join(_unseeded))
        print("!! Those testers will open the app to a blank screen.")
        print()

con = ra._con()

# Only mint flats that do NOT already have a PIN.
#
# The whole roster is in this list, and most of those PINs are already in
# somebody's WhatsApp. Re-minting them would invalidate every message
# already sent -- and the failure is silent and delayed: the tester types
# the PIN they were given, is told it is wrong, and neither of you knows
# why. Nine people were one command away from exactly that when two
# testers were added on 4 Oct.
#
# So adding a tester is now a safe operation: it mints only the new
# flats. Re-minting an existing one is deliberate and loud.
_existing = {ra._norm_flat(r[0]) for r in
             con.execute("SELECT flat_no FROM flat_pins WHERE pin_hash IS NOT NULL")}
_REMINT = os.environ.get("OCTA_REMINT", "").strip() == "1"
_skipped = [f for f in flats if f in _existing] if not _REMINT else []
if _skipped and not _REMINT:
    flats = [f for f in flats if f not in _existing]
    print("Skipping %d flat(s) that already have a PIN: %s"
          % (len(_skipped), ", ".join(_skipped)))
    print("Their PINs are unchanged, so messages already sent still work.")
    print("To regenerate them anyway: OCTA_REMINT=1 (this invalidates every")
    print("PIN already given out for those flats).")
    print()
if _REMINT:
    print("!! OCTA_REMINT=1 -- regenerating PINs for ALL %d flats." % len(flats))
    print("!! Every PIN already sent for these flats stops working.")
    print()
if not flats:
    con.close()
    sys.exit("Every flat in the roster already has a PIN. Nothing to mint.\n"
             "Add a tester to closed_test_roster.json with a NEW flat, or set "
             "OCTA_REMINT=1 to regenerate.")

pins = {}
for f in flats:
    p = ra._gen_pin()
    con.execute(
        "INSERT OR REPLACE INTO flat_pins "
        "(flat_no, pin_hash, created_at, created_by, attempts, locked_until, "
        " last_login) VALUES (?,?,?,?,0,0,NULL)",
        (f, ra._pin_hash(f, p), ra._now_str(), "closed-test"))
    pins[f] = p
con.commit()

# Read back and re-derive. A PIN that does not verify is worse than no PIN:
# the tester is told they typed it wrong.
bad = []
for f in flats:
    row = con.execute("SELECT pin_hash FROM flat_pins WHERE flat_no=?",
                      (f,)).fetchone()
    if not row or row[0] != ra._pin_hash(f, pins[f]):
        bad.append(f)
con.close()
if bad:
    sys.exit("Hash did not verify for: %s. Nothing printed." % ", ".join(bad))

MSG = """Hi — the Octa Resident test is live. Three things, five minutes.

1) Tap this and accept, then install the app:
{link}

2) Open Octa Resident, tap "Just looking? Try the demo society",
   and enter society code DEMO.

3) Log in with:
   Flat: {flat}
   PIN:  {pin}

Please open it every couple of days for the next two weeks — Google
checks that testers actually used it, and that is the whole point of
this. Tell me anything that looks wrong or confusing.

Thanks. This is the last thing standing between me and launch."""

W = 68
print("=" * W)
print("  %d flats, PINs minted %s" % (len(flats), ra._now_str()))
print("  key fingerprint %s (source: %s)" % (ra._secret_fp(), ra._SECRET_SOURCE))
print("  All hashes verified. These PINs are shown ONCE.")
print("=" * W)
print()
print("-" * W)
print("  %s was NOT touched. Google's reviewer keeps the PIN already" % REVIEWER_FLAT)
print("  submitted in App Access. Do not give that flat to a tester.")
print("-" * W)
print()

_SEND = [(e, f) for e, f in TESTERS if f in pins]
print("  %d message(s) to send, for %d newly minted flat(s)."
      % (len(_SEND), len(pins)))
print()
for i, (email, flat) in enumerate(_SEND, 1):
    print("=" * W)
    print("  MESSAGE %2d of %d   ->   %s" % (i, len(_SEND), email))
    print("=" * W)
    print(MSG.format(link=LINK, flat=flat, pin=pins[flat]))
    print()

print("=" * W)
print("  Done. Record today's date against each tester in the roster")
print("  once they confirm they have installed it.")
print("=" * W)
