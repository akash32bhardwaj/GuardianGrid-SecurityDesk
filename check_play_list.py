"""OCT-147 -- compare the roster against the Play Console tester list.

    python check_play_list.py play_testers.txt

Three lists govern a closed test and nothing has ever compared them:

  1. the Play Console email list  -> decides who CAN OPT IN
  2. closed_test_roster.json      -> decides who GETS A PIN
  3. an Android phone             -> decides who can INSTALL

The absence of any one is SILENT. A tester missing from (1) receives the
link, presses nothing that works, and reports "App not available" -- while
every list you own says they are a tester. That cost three people in
September, and Neha on 13 Oct, found only because she sent a screenshot.

Feed it whatever Play Console gives you: the downloaded CSV, or a text
file you pasted the addresses into. Anything that looks like an email is
read; everything else is ignored.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROSTER = os.path.join(HERE, "closed_test_roster.json")
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def norm(e):
    return e.strip().lower()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    path = sys.argv[1]
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError as e:
        sys.exit("Cannot read %s: %s" % (path, e))

    play = {norm(m) for m in EMAIL.findall(raw)}
    if not play:
        sys.exit("No email addresses found in %s -- is it the right file?" % path)

    d = json.load(open(ROSTER, encoding="utf-8"))
    rows = d.get("testers", [])
    roster = {norm(t["email"]): t for t in rows if t.get("email")}
    dropped = {norm(x["email"]) for x in d.get("_dropped", []) if x.get("email")}
    # Second addresses of real testers, on the Play list on purpose and
    # deliberately without a flat. Expected, so not reported as strays --
    # a check that cries wolf stops being read.
    alt = {norm(x["email"]) for x in d.get("_alt_addresses", []) if x.get("email")}

    missing = sorted(set(roster) - play)          # cannot opt in
    extra = sorted(play - set(roster) - dropped - alt)  # cannot log in
    both = sorted(set(roster) & play)
    readded = sorted(play & dropped)              # deliberately removed

    print()
    print("  ROSTER %d  |  PLAY LIST %d  |  MATCHED %d"
          % (len(roster), len(play), len(both)))
    print("  " + "-" * 72)

    if missing:
        print("  NOT ON THE PLAY LIST -- they can never opt in, however often")
        print("  you chase them. Add them in Play Console, Testers tab.")
        for e in missing:
            t = roster[e]
            print("    %-38s flat %-7s %s"
                  % (e, t.get("flat", "-"), t.get("name", "")))
        print()

    if extra:
        print("  ON THE PLAY LIST BUT NOT IN THE ROSTER -- they can opt in and")
        print("  install, then have no flat or PIN and see an empty app.")
        for e in extra:
            print("    %s" % e)
        print()

    if readded:
        print("  ON THE PLAY LIST BUT DELIBERATELY DROPPED -- check _dropped")
        print("  before re-adding; these were removed for a reason.")
        for e in readded:
            print("    %s" % e)
        print()

    known_alt = sorted(play & alt)
    if known_alt:
        print("  second addresses, expected: %s" % ", ".join(known_alt))
        print()

    if not (missing or extra or readded):
        print("  The two lists agree. Everyone with a PIN can opt in, and")
        print("  nobody can opt in who would then find an empty app.")
    print()
    return 1 if (missing or extra) else 0


if __name__ == "__main__":
    sys.exit(main())
