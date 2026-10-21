"""The notice composer's limits must match what the server actually stores.

resident_app.py does `title[:80]` and `body[:500]` SILENTLY. Anything typed
past those disappears on save with nothing said -- which looks like the app
losing your text, not like a limit. CommitteeNotices.jsx therefore stops the
input at the same numbers and shows the count.

Two numbers in two repositories is exactly how that agreement rots. This
pins them together, reading both from source rather than restating them.

The frontend lives in a different repository, so it is located by
OCTA_FRONTEND or a few usual places, and the test SKIPS when it cannot be
found. A test that fails because a sibling checkout is absent teaches
people to ignore it.

    python test_notice_limits.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "resident_app.py")

CANDIDATES = [
    os.environ.get("OCTA_FRONTEND", ""),
    os.path.join(os.path.expanduser("~"), "Desktop", "GuardianGrid",
                 "guardiangrid-command-center"),
]
# Deliberately NOT searched: <repo>/../guardiangrid-command-center. On this
# machine that is C:\GuardianGrid\guardiangrid-command-center -- the July
# tree OCT-80 is about, two months stale and not what ships. A path that
# resolves to the wrong checkout is worse than no path.

fails = []

# ── the server's real limits, lifted from the handler ───────────────
src = io.open(SERVER, encoding="utf-8").read()
block = re.search(r"def notices_add\(\):(.*?)\n@", src, re.S)
if not block:
    sys.exit("could not find notices_add() in resident_app.py")
block = block.group(1)

def grab(pattern, what):
    m = re.search(pattern, block)
    if not m:
        fails.append("could not read %s from notices_add()" % what)
        return None
    return int(m.group(1))

s_title = grab(r'data\.get\("title"\).*?\[:(\d+)\]', "the title limit")
s_body = grab(r'data\.get\("body"\).*?\[:(\d+)\]', "the body limit")
s_days = grab(r'data\.get\("days"\)\s*or\s*(\d+)', "the default expiry")
s_max_days = grab(r"min\(days,\s*(\d+)\)", "the maximum expiry")

print("  server:  title %s  body %s  default %s days  max %s days"
      % (s_title, s_body, s_days, s_max_days))

# ── the composer ────────────────────────────────────────────────────
front = next((p for p in CANDIDATES if p and os.path.isdir(p)), None)
if not front:
    print("\n  SKIP -- frontend repo not found. Set OCTA_FRONTEND to the")
    print("  guardiangrid-command-center checkout to include this check.")
    raise SystemExit(0)

comp = os.path.join(front, "src", "v2", "CommitteeNotices.jsx")
if not os.path.isfile(comp):
    print("\n  SKIP -- %s not found." % comp)
    raise SystemExit(0)

jsx = io.open(comp, encoding="utf-8").read()


def grab_js(name):
    m = re.search(r"const %s = (\d+);" % name, jsx)
    if not m:
        fails.append("could not read %s from CommitteeNotices.jsx" % name)
        return None
    return int(m.group(1))


c_title = grab_js("TITLE_MAX")
c_body = grab_js("BODY_MAX")
c_days = grab_js("DEFAULT_DAYS")
_m = re.search(r"const SPANS = \[(.*?)\]", jsx)
spans = [int(x) for x in re.findall(r"\d+", _m.group(1))] if _m else []
if not spans:
    fails.append("could not read SPANS from CommitteeNotices.jsx")

print("  client:  title %s  body %s  default %s days  spans %s"
      % (c_title, c_body, c_days, spans))
print()

if None not in (s_title, c_title) and s_title != c_title:
    fails.append("title: server truncates at %d, composer stops at %d -- text "
                 "would vanish on save" % (s_title, c_title))
if None not in (s_body, c_body) and s_body != c_body:
    fails.append("body: server truncates at %d, composer stops at %d -- text "
                 "would vanish on save" % (s_body, c_body))
if None not in (s_days, c_days) and s_days != c_days:
    fails.append("default expiry: server uses %d days, composer preselects %d "
                 "-- the chip would lie about what happens" % (s_days, c_days))
if c_days is not None and spans and c_days not in spans:
    fails.append("the default of %d days is not one of the chips %s, so "
                 "nothing is selected when the form opens" % (c_days, spans))
if s_max_days is not None and spans and max(spans) > s_max_days:
    fails.append("a chip offers %d days but the server clamps to %d"
                 % (max(spans), s_max_days))

# ── the notify flag: OFF by default, same name on both sides ────────
# The regression that matters is somebody changing useState(false) to true,
# or "remembering" the last choice. Then "notify when it matters" quietly
# becomes "notify always", residents mute the app, and they stop seeing the
# gate arrival ask and the panic acknowledgement with it.
if "data.get(\"notify\")" not in block:
    fails.append("the server no longer reads a `notify` flag in notices_add()")
if "notify" not in (re.search(r"body: JSON\.stringify\(\{(.*?)\}\)", jsx) or
                    type("", (), {"group": lambda s, n: ""})()).group(1):
    fails.append("the composer does not send `notify` in the POST body")

m_def = re.search(r"const \[notify, setNotify\] = useState\((\w+)\)", jsx)
if not m_def:
    fails.append("could not find the notify state in CommitteeNotices.jsx")
elif m_def.group(1) != "false":
    fails.append("notify defaults to %s -- it must default to false, or every "
                 "notice buzzes every phone by accident" % m_def.group(1))
else:
    print("  notify:  server reads it, composer sends it, default false")

if "setNotify(false)" not in jsx:
    fails.append("notify is not reset after a post -- the checkbox would stay "
                 "armed and the next notice would notify without being asked")
else:
    print("  notify:  reset after each post, so it cannot stay armed")

print()
if fails:
    print("FAIL")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print("PASS - the composer's limits are the server's limits")
