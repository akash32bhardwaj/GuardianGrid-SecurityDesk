"""
flat_directory.py — GuardianGrid flat/owner directory  (Visitor Notify v1)
--------------------------------------------------------------------------
Stores which WhatsApp number belongs to which flat, and logs every
notification attempt so the dashboard can show "notified / failed".

This module deliberately does NOT touch db.py — it owns two tables of
its own inside the same guardiangrid.db file:

  flats(flat_no PRIMARY KEY, owner_name, whatsapp, added_at)
  visitor_notifications(id, visitor_id, flat_no, status, detail, ts)

CLI usage (run from the backend folder):

  python flat_directory.py add B-302 "Rahul Sharma" +919876543210
  python flat_directory.py list
  python flat_directory.py remove B-302

WhatsApp numbers must be in international format: +91XXXXXXXXXX
"""

import os
import re
import sys
import sqlite3
from datetime import datetime

DB_FILE = "guardiangrid.db"

# +, then 10-15 digits, nothing else (e.g. +919876543210)
PHONE_RE = re.compile(r"^\+\d{10,15}$")


# ── Wrong-directory guard (same idea as migrate.py) ──────────────
def _guard_cwd():
    if not (os.path.exists("api_server.py") or os.path.exists(DB_FILE)):
        print("[FLATS] ERROR: run this from the GuardianGrid backend folder")
        print(f"[FLATS] current folder: {os.getcwd()}")
        sys.exit(1)


def _con():
    return sqlite3.connect(DB_FILE)


def _norm_flat(flat_no: str) -> str:
    return (flat_no or "").strip().upper()


# ── Schema ───────────────────────────────────────────────────────
def init_flats():
    con = _con()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS flats (
            flat_no    TEXT PRIMARY KEY,
            owner_name TEXT NOT NULL,
            whatsapp   TEXT NOT NULL,
            added_at   TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS visitor_notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id INTEGER,
            flat_no    TEXT,
            status     TEXT,      -- sent | failed | skipped
            detail     TEXT,
            ts         TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()


# ── CRUD ─────────────────────────────────────────────────────────
def set_flat(flat_no: str, owner_name: str, whatsapp: str) -> bool:
    flat_no = _norm_flat(flat_no)
    whatsapp = (whatsapp or "").strip().replace(" ", "").replace("-", "")
    if not flat_no or not owner_name:
        return False
    if not PHONE_RE.match(whatsapp):
        print(f"[FLATS] ERROR: '{whatsapp}' is not a valid number. "
              "Use + followed by digits only, e.g. +919876543210 "
              "(replace every X with a real digit!)")
        return False
    con = _con()
    con.execute(
        "INSERT INTO flats (flat_no, owner_name, whatsapp, added_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(flat_no) DO UPDATE SET "
        "owner_name = excluded.owner_name, whatsapp = excluded.whatsapp",
        (flat_no, owner_name.strip(), whatsapp, datetime.now().isoformat()),
    )
    con.commit()
    con.close()
    return True


def get_flat(flat_no: str):
    """Return {flat_no, owner_name, whatsapp} or None."""
    flat_no = _norm_flat(flat_no)
    if not flat_no:
        return None
    con = _con()
    row = con.execute(
        "SELECT flat_no, owner_name, whatsapp FROM flats WHERE flat_no = ?",
        (flat_no,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"flat_no": row[0], "owner_name": row[1], "whatsapp": row[2]}


def all_flats():
    con = _con()
    rows = con.execute(
        "SELECT flat_no, owner_name, whatsapp FROM flats ORDER BY flat_no"
    ).fetchall()
    con.close()
    return [{"flat_no": r[0], "owner_name": r[1], "whatsapp": r[2]} for r in rows]


def remove_flat(flat_no: str) -> bool:
    flat_no = _norm_flat(flat_no)
    con = _con()
    cur = con.execute("DELETE FROM flats WHERE flat_no = ?", (flat_no,))
    con.commit()
    changed = cur.rowcount > 0
    con.close()
    return changed


# ── Notification log ─────────────────────────────────────────────
def record_notification(visitor_id, flat_no, status, detail=""):
    con = _con()
    con.execute(
        "INSERT INTO visitor_notifications (visitor_id, flat_no, status, detail, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (visitor_id, _norm_flat(flat_no), status, (detail or "")[:300],
         datetime.now().isoformat()),
    )
    con.commit()
    con.close()


def notification_for_visitor(visitor_id):
    """Latest notification row for a visitor entry, or None."""
    con = _con()
    row = con.execute(
        "SELECT status, detail, ts FROM visitor_notifications "
        "WHERE visitor_id = ? ORDER BY id DESC LIMIT 1",
        (visitor_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"status": row[0], "detail": row[1], "ts": row[2]}


# ── CLI ──────────────────────────────────────────────────────────
def _cli():
    _guard_cwd()
    init_flats()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0].lower()

    if cmd == "add" and len(args) >= 4:
        flat, name, number = args[1], args[2], args[3]
        if set_flat(flat, name, number):
            print(f"[FLATS] saved: {_norm_flat(flat)} → {name} ({number})")
        else:
            print("[FLATS] ERROR: check inputs. Number must start with + "
                  "(example: +919876543210)")
    elif cmd == "list":
        rows = all_flats()
        if not rows:
            print("[FLATS] directory is empty")
        for r in rows:
            print(f"  {r['flat_no']:<10} {r['owner_name']:<25} {r['whatsapp']}")
        print(f"[FLATS] total: {len(rows)}")
    elif cmd == "remove" and len(args) >= 2:
        ok = remove_flat(args[1])
        print(f"[FLATS] {'removed' if ok else 'not found'}: {_norm_flat(args[1])}")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
