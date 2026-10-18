#!/usr/bin/env python3
"""OCT-171 — the Gate Console's resident lookup, and the near-miss suggestion.

Uses GG_RESIDENT_DB to point resident_db at a temp file, so this never reads
or writes the real registry. The plates below are the actual misreads this
engine produced, taken from the filenames in output/webcam (OCT-169).

Run:  python tests/test_gate_lookup.py
"""
import ast
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, REPO)

_tmp = tempfile.mkdtemp(prefix="gate_lookup_test_")
os.environ["GG_RESIDENT_DB"] = os.path.join(_tmp, "residents.json")

import resident_db                                            # noqa: E402
from resident_db import Resident                              # noqa: E402
import gate_capture                                           # noqa: E402

gate_capture.init_gate_capture(base_dir=_tmp)

# Controlled registry. Nothing is saved to disk by add().
resident_db.db._db.clear()
for r in [
    Resident("MH12BP8265", "Priya Sharma", "A-204", status="KNOWN"),
    Resident("DL3CAB5678", "Rohit Verma",  "B-101", status="KNOWN"),
    Resident("HR26XY9999", "flagged",      "",      status="BLACKLISTED"),
    # Two registered plates one edit apart: a read between them is ambiguous
    # and must not be guessed at.
    Resident("PB10AB1234", "Amrit Singh",  "C-301", status="KNOWN"),
    Resident("PB10AB1239", "Simran Kaur",  "C-302", status="KNOWN"),
]:
    resident_db.db.add(r)

fails = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"          got  {got!r}")
        print(f"          want {want!r}")
        fails.append(label)


print("_lookup — this is what was broken\n")

hit = gate_capture._lookup("MH12BP8265")
check("registered plate is found", hit.get("found"), True)
check("  carries its status", hit.get("status"), "KNOWN")
check("  carries the resident", hit.get("resident_name"), "Priya Sharma")
check("  carries the flat", hit.get("flat_number"), "A-204")

bl = gate_capture._lookup("HR26XY9999")
check("BLACKLISTED is found AND flagged",
      (bl.get("found"), bl.get("status")), (True, "BLACKLISTED"))

miss = gate_capture._lookup("ZZ99ZZ9999")
check("unregistered plate is not found", miss.get("found"), False)
check("  and reports UNKNOWN", miss.get("status"), "UNKNOWN")

print("\n_near_miss — suggested, never applied\n")

check("MH62BP8265 (real misread of MH12BP8265) suggests it",
      gate_capture._near_miss("MH62BP8265"),
      {"plate": "MH12BP8265", "resident": "Priya Sharma · Flat A-204",
       "status": "KNOWN", "distance": 1})

bn = gate_capture._near_miss("HR26XY9998")
check("a near miss on a BLACKLISTED plate returns its status",
      (bn or {}).get("status"), "BLACKLISTED")
check("  and names the plate to re-check",
      (bn or {}).get("plate"), "HR26XY9999")

check("an exact match suggests nothing",
      gate_capture._near_miss("MH12BP8265"), None)
check("a plate nothing like a resident's suggests nothing",
      gate_capture._near_miss("TN01ZZ0001"), None)
check("two registered plates equally close -> no guess",
      gate_capture._near_miss("PB10AB1230"), None)

print("\nknown limits of fuzzy_lookup, pinned so they stay known\n")

check("two-character misread is NOT caught (DL5CAB5078 is 2 edits "
      "from DL3CAB5678)",
      gate_capture._near_miss("DL5CAB5078"), None)
check("different-length misread is NOT caught (fuzzy_lookup compares "
      "same-length plates only)",
      gate_capture._near_miss("HR9BA4777"), None)

print("\nregression guard\n")

# Checked with ast rather than a text search, because the docstring on
# _lookup QUOTES the broken import as documentation -- and a text search
# found it there, which is this guard catching its own first draft.
src = io.open(os.path.join(REPO, "gate_capture.py"), encoding="utf-8").read()
bad = [
    n for n in ast.walk(ast.parse(src))
    if isinstance(n, ast.ImportFrom)
    and n.module == "resident_db"
    and any(a.name == "lookup_plate" for a in n.names)
]
check("no executable import of lookup_plate", bad, [])
check("resident_db still exposes no lookup_plate",
      hasattr(resident_db, "lookup_plate"), False)

print()
if fails:
    print(f"FAILED ({len(fails)}):")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("PASSED")
