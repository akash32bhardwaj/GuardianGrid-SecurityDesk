#!/usr/bin/env python3
"""
check_dbpath.py — proves OCT-81 before and after the fix.

Run it INSIDE the container so it sees the same filesystem the app sees:

    sudo docker exec octa-demo python /tmp/check_dbpath.py

(copy it in first:  sudo docker cp /tmp/check_dbpath.py octa-demo:/tmp/ )

BEFORE the fix it should report a stray, empty /app/guardiangrid.db with 0
vehicle_events next to a real /data/guardiangrid.db with thousands.
AFTER the fix, /app/guardiangrid.db should not be recreated once deleted,
and /api/forecast should be reading the populated one.
"""
import os
import sqlite3

CANDIDATES = [
    "/data/guardiangrid.db",          # the container mount — the real one
    "/app/guardiangrid.db",           # BASE_DIR — what /api/forecast used
    "guardiangrid.db",                # relative — what /api/day used
]


def describe(path):
    real = os.path.abspath(path)
    if not os.path.exists(real):
        return f"  {path:<32} -> {real}\n      ABSENT"
    size = os.path.getsize(real)
    line = f"  {path:<32} -> {real}\n      {size:,} bytes"
    try:
        # read-only URI so this script cannot create what it is looking for
        con = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM vehicle_events").fetchone()[0]
            newest = con.execute(
                "SELECT MAX(datetime(REPLACE(timestamp,'T',' ')))"
                " FROM vehicle_events").fetchone()[0]
            line += f"\n      vehicle_events: {n:,} rows, newest {newest}"
        except sqlite3.Error as e:
            line += f"\n      no usable vehicle_events table ({e})"
        con.close()
    except sqlite3.Error as e:
        line += f"\n      cannot open read-only: {e}"
    return line


print(f"cwd of this script : {os.getcwd()}")
try:
    print(f"cwd of the app (pid 1): {os.readlink('/proc/1/cwd')}")
except OSError as e:
    print(f"cwd of the app (pid 1): unavailable ({e})")
print()
for c in CANDIDATES:
    print(describe(c))
print()
print("A populated /data copy next to an empty /app copy is OCT-81: sqlite3")
print("creates a missing file instead of failing, so /api/forecast opened a")
print("database it had just made and computed a threat forecast from zero rows.")
