#!/usr/bin/env python3
"""OCT-169 — pin the plate validator against real misreads.

Why this does not simply `import core.anpr_engine`: that module imports cv2
and numpy at the top, so the test would need the whole ML stack installed to
check twenty lines of string logic. It tries the import first and falls back
to lifting the validator's own source out of the file, so the check runs on a
bare machine as well as inside the container. Either way it exercises the
shipped code, not a copy of it.

Run:  python tests/test_plate_validation.py
"""
import io
import os
import re
import sys

ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "core", "anpr_engine.py")


def load_validator():
    """Return (plate_reject_reason, how_it_was_loaded)."""
    sys.path.insert(0, os.path.join(os.path.dirname(ENGINE), ".."))
    try:
        from core.anpr_engine import plate_reject_reason   # noqa
        return plate_reject_reason, "imported"
    except Exception:
        pass

    src = io.open(ENGINE, encoding="utf-8").read()
    ns = {"re": re}

    def grab(pattern, label, dotall=False):
        flags = re.M | (re.S if dotall else 0)
        m = re.search(pattern, src, flags)
        if not m:
            sys.exit(f"FAIL: cannot find {label} in core/anpr_engine.py. "
                     "If it was renamed, update this test deliberately.")
        return m.group(0)

    for chunk in (
        grab(r"^STATE_CODES = \{.*?^\}", "STATE_CODES", dotall=True),
        grab(r"^VALID_STATE_CODES = .*$", "VALID_STATE_CODES"),
        grab(r"^BH_PATTERN\s+= .*$", "BH_PATTERN"),
        grab(r"^OLD_PATTERN = .*$", "OLD_PATTERN"),
        grab(r"^LEGACY_PATTERN = .*$", "LEGACY_PATTERN"),
        grab(r"^def plate_reject_reason\(text\):.*?(?=^def is_valid_indian_plate)",
             "plate_reject_reason", dotall=True),
    ):
        exec(chunk, ns)
    return ns["plate_reject_reason"], "extracted from source"


# Every one of these was read by this engine from a real vehicle and written
# to vehicle_events. They are in output/webcam as filenames.
MUST_REJECT = {
    "ZZ8HG5174":   "ZZ is not a state code",
    "LZ8HG5174":   "LZ is not a state code",
    "OF53OV9006":  "OF is not a state code",
    "RP8AA7777":   "RP is not a state code",
    "MHR26DO5554": "eleven characters, no plate shape",
    "RJ11CYUUU2":  "000 read as UUU",
    "RJ16CYUUU2":  "000 read as UUU",
    "RJ16CTUUU1":  "000 read as UUU",
}

# Rejecting any of these would be worse than the bug being fixed. The last
# four are the awkward ones: the BH series, an old plate with no letter
# series, a rural plate with three digits, and a three-letter series.
MUST_ACCEPT = [
    "HR98AA7777", "MH12BP8265", "DL3CAB5678", "RJ11CV0002", "UP53OV9006",
    "PB65AB1234", "PB10AB1234", "CH01AB1234", "HR26DO5551", "JK01AB1234",
    "22BH1234AB", "PB101234", "HR26A123", "MH12ABC1234",
]

# Punjab is the pilot market; a regression here is the expensive one.
PUNJAB_REGION = ["PB01AB1234", "PB65AB1234", "CH01AA0001", "HR26AB1234",
                 "HP12AB1234", "JK02AB1234", "DL8CAB1234", "RJ14AB1234"]


def main():
    reject, how = load_validator()
    print(f"validator {how}\n")
    failures = []

    for plate, why in MUST_REJECT.items():
        r = reject(plate)
        print(f"  {'ok  ' if r else 'FAIL'}  reject {plate:<12} {r or '*** ACCEPTED ***'}")
        if not r:
            failures.append(f"{plate} accepted; it is {why}")

    print()
    for plate in MUST_ACCEPT + PUNJAB_REGION:
        r = reject(plate)
        print(f"  {'ok  ' if not r else 'FAIL'}  accept {plate:<12} {r or ''}")
        if r:
            failures.append(f"{plate} rejected ({r}) — a real plate would be refused")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print(f"PASSED — {len(MUST_REJECT)} misreads refused, "
          f"{len(MUST_ACCEPT) + len(PUNJAB_REGION)} real plates accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
