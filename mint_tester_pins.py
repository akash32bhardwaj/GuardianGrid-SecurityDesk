import os, sys
sys.path.insert(0, "/app")
import resident_app as ra

LINK = os.environ.get("OPTIN_LINK", "").strip()
if not LINK:
    sys.exit("OPTIN_LINK is empty. Set it, then run this again.")

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

TESTERS = [
    ("akash08bhardwaj@gmail.com",         "A-101"),
    ("aakash.aakashb.bhardwaj@gmail.com", "A-204"),
    ("saroj08bhardwaj@gmail.com",         "B-302"),
    ("brijb424@gmail.com",                "B-405"),
    ("ritesh.chander02@gmail.com",        "C-108"),
    ("neeru012125@gmail.com",             "C-210"),
    ("nancyluthra16@gmail.com",           "D-112"),
    ("sahilsharma2471@gmail.com",         "D-306"),
    ("singh.amandeep1989@gmail.com",      "A-101"),
    ("prajwalbhushan.pb@gmail.com",       "A-204"),
    ("amandeep2020@gmail.com",            "B-302"),
    ("hbembey18@gmail.com",               "B-405"),
]
# D-404 is deliberately NOT in this list. It is Google's reviewer's flat,
# its PIN goes into App Access before the release is rolled out, and this
# script runs after rollout. Minting it here would replace the PIN already
# submitted and hand the reviewer a login that fails.
REVIEWER_FLAT = "D-404"
flats = sorted({f for _, f in TESTERS})
assert REVIEWER_FLAT not in flats, "reviewer flat must not be re-minted here"

con = ra._con()
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

for i, (email, flat) in enumerate(TESTERS, 1):
    print("=" * W)
    print("  MESSAGE %2d of %d   ->   %s" % (i, len(TESTERS), email))
    print("=" * W)
    print(MSG.format(link=LINK, flat=flat, pin=pins[flat]))
    print()

print("=" * W)
print("  Done. Record today's date against each tester in the roster")
print("  once they confirm they have installed it.")
print("=" * W)
