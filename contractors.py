"""
contractors.py — Defender Octa "Contractor Passes" module (factory feature #1)
==============================================================================
Drop into your Flask backend folder, next to site_profile.py.

WIRE IT (2 lines in your main app file):
    from contractors import contractors_bp
    app.register_blueprint(contractors_bp)

Every route here is protected by @feature_required("contractor_passes"),
so society deployments answer 403 automatically. Your existing JWT
before_request guard applies on top, same as your other routes.

WHAT A "CONTRACTOR PASS" IS (the model)
---------------------------------------
A contractor is different from a visitor: they come repeatedly for a
period (an electrician for 3 days, a construction crew for 2 months).
So a pass has:
  - who: name, phone, company, optional vehicle number
  - why: purpose (e.g. "AC maintenance")
  - where: allowed areas (free-text list the guard can read out)
  - when: valid_from / valid_to dates  -> outside this window the pass
    is automatically INVALID, no manual expiry needed
  - a short pass code like CP-4F7K the guard can type at the gate
Gate events (check-in / check-out) are logged against the pass, giving
the owner a per-contractor attendance & on-site history for free.

STORAGE
-------
Own SQLite file "contractors.db" in the folder set by OCTA_DATA_DIR
(default: same folder as this file). Kept separate from your incident DB
on purpose: zero risk of touching existing tables, easy per-client backup.
Tables are created automatically on first use.

STATUSES
--------
  active   -> within validity window and not revoked
  expired  -> computed automatically when now > valid_to
  revoked  -> manually blocked (misconduct etc.); revoked wins over dates
"""

import os
import re
import secrets
import sqlite3
from datetime import datetime, date

from flask import Blueprint, jsonify, request

from site_profile import feature_required, is_enabled

# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get(
    "OCTA_DATA_DIR", os.path.dirname(os.path.abspath(__file__))
)
DB_PATH = os.path.join(DATA_DIR, "contractors.db")

PASS_PREFIX = "CP"  # printed on the pass: CP-4F7K
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L confusion


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init_db():
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS contractor_passes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                pass_code     TEXT UNIQUE NOT NULL,
                name          TEXT NOT NULL,
                phone         TEXT NOT NULL,
                company       TEXT DEFAULT '',
                vehicle_no    TEXT DEFAULT '',
                purpose       TEXT DEFAULT '',
                allowed_areas TEXT DEFAULT '',
                valid_from    TEXT NOT NULL,   -- YYYY-MM-DD
                valid_to      TEXT NOT NULL,   -- YYYY-MM-DD
                revoked       INTEGER DEFAULT 0,
                revoke_reason TEXT DEFAULT '',
                created_at    TEXT NOT NULL,
                notes         TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS contractor_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pass_id    INTEGER NOT NULL REFERENCES contractor_passes(id),
                event_type TEXT NOT NULL CHECK (event_type IN ('in','out')),
                event_time TEXT NOT NULL,
                gate       TEXT DEFAULT 'Main Gate',
                by_guard   TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_events_pass
                ON contractor_events(pass_id, event_time);
            """
        )


_init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _new_pass_code(conn) -> str:
    for _ in range(20):
        code = PASS_PREFIX + "-" + "".join(
            secrets.choice(_CODE_ALPHABET) for _ in range(4)
        )
        row = conn.execute(
            "SELECT 1 FROM contractor_passes WHERE pass_code = ?", (code,)
        ).fetchone()
        if not row:
            return code
    # Practically unreachable; widen the code if a site ever gets that big.
    return PASS_PREFIX + "-" + "".join(
        secrets.choice(_CODE_ALPHABET) for _ in range(6)
    )


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_date(s) -> bool:
    if not isinstance(s, str) or not _DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _status(row) -> str:
    if row["revoked"]:
        return "revoked"
    today = date.today().isoformat()
    if today < row["valid_from"]:
        return "not_started"
    if today > row["valid_to"]:
        return "expired"
    return "active"


def _pass_dict(row, conn=None) -> dict:
    d = dict(row)
    d["status"] = _status(row)
    d["revoked"] = bool(row["revoked"])
    if conn is not None:
        last = conn.execute(
            "SELECT event_type, event_time, gate FROM contractor_events "
            "WHERE pass_id = ? ORDER BY event_time DESC, id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        d["on_site"] = bool(last and last["event_type"] == "in")
        d["last_event"] = dict(last) if last else None
    return d


def _clean(s, limit=200) -> str:
    return str(s or "").strip()[:limit]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
contractors_bp = Blueprint("contractors", __name__)


@contractors_bp.route("/api/contractors", methods=["POST"])
@feature_required("contractor_passes")
def create_pass():
    """Issue a new contractor pass."""
    data = request.get_json(silent=True) or {}
    name = _clean(data.get("name"))
    phone = _clean(data.get("phone"), 20)
    valid_from = _clean(data.get("valid_from"), 10)
    valid_to = _clean(data.get("valid_to"), 10)

    if not name or not phone:
        return jsonify({"error": "name and phone are required"}), 400
    if not (_valid_date(valid_from) and _valid_date(valid_to)):
        return jsonify({"error": "valid_from and valid_to must be YYYY-MM-DD"}), 400
    if valid_to < valid_from:
        return jsonify({"error": "valid_to cannot be before valid_from"}), 400

    with _db() as conn:
        code = _new_pass_code(conn)
        cur = conn.execute(
            """INSERT INTO contractor_passes
               (pass_code, name, phone, company, vehicle_no, purpose,
                allowed_areas, valid_from, valid_to, created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                code, name, phone,
                _clean(data.get("company")),
                _clean(data.get("vehicle_no"), 20).upper(),
                _clean(data.get("purpose")),
                _clean(data.get("allowed_areas"), 400),
                valid_from, valid_to,
                datetime.now().isoformat(timespec="seconds"),
                _clean(data.get("notes"), 500),
            ),
        )
        row = conn.execute(
            "SELECT * FROM contractor_passes WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return jsonify(_pass_dict(row, conn)), 201


@contractors_bp.route("/api/contractors", methods=["GET"])
@feature_required("contractor_passes")
def list_passes():
    """List passes. Optional ?q=search and ?status=active|expired|revoked."""
    q = _clean(request.args.get("q"), 60)
    want_status = _clean(request.args.get("status"), 20)
    with _db() as conn:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                """SELECT * FROM contractor_passes
                   WHERE name LIKE ? OR phone LIKE ? OR company LIKE ?
                      OR pass_code LIKE ? OR vehicle_no LIKE ?
                   ORDER BY id DESC LIMIT 300""",
                (like, like, like, like, like),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM contractor_passes ORDER BY id DESC LIMIT 300"
            ).fetchall()
        out = [_pass_dict(r, conn) for r in rows]
    if want_status:
        out = [p for p in out if p["status"] == want_status]
    return jsonify({"passes": out, "count": len(out)})


@contractors_bp.route("/api/contractors/validate/<pass_code>", methods=["GET"])
@feature_required("contractor_passes")
def validate_pass(pass_code):
    """Gate lookup: guard types the code, gets a clear GO / NO-GO answer."""
    code = _clean(pass_code, 12).upper()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM contractor_passes WHERE pass_code = ?", (code,)
        ).fetchone()
        if not row:
            return jsonify({"found": False, "allow": False,
                            "message": "Pass not found"}), 404
        d = _pass_dict(row, conn)
        allow = d["status"] == "active"
        reasons = {
            "active": "Pass valid — allow entry",
            "expired": "Pass EXPIRED — do not allow",
            "revoked": "Pass REVOKED — do not allow, inform supervisor",
            "not_started": "Pass not yet valid — starts " + row["valid_from"],
        }
        return jsonify({"found": True, "allow": allow,
                        "message": reasons[d["status"]], "pass": d})


@contractors_bp.route("/api/contractors/<int:pass_id>/event", methods=["POST"])
@feature_required("contractor_passes")
def gate_event(pass_id):
    """Record check-in or check-out. Body: {"event_type": "in"|"out", ...}"""
    data = request.get_json(silent=True) or {}
    event_type = _clean(data.get("event_type"), 3).lower()
    if event_type not in ("in", "out"):
        return jsonify({"error": "event_type must be 'in' or 'out'"}), 400

    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM contractor_passes WHERE id = ?", (pass_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "pass not found"}), 404
        status = _status(row)
        if event_type == "in" and status != "active":
            return jsonify({"error": f"cannot check in: pass is {status}"}), 409
        # Check-OUT is always allowed (someone already inside must be able
        # to leave even if the pass expired at midnight).
        conn.execute(
            """INSERT INTO contractor_events
               (pass_id, event_type, event_time, gate, by_guard)
               VALUES (?,?,?,?,?)""",
            (
                pass_id, event_type,
                datetime.now().isoformat(timespec="seconds"),
                _clean(data.get("gate"), 60) or "Main Gate",
                _clean(data.get("by_guard"), 60),
            ),
        )
        d = _pass_dict(
            conn.execute(
                "SELECT * FROM contractor_passes WHERE id = ?", (pass_id,)
            ).fetchone(),
            conn,
        )
    # Optional WhatsApp hook — only if the site has alerts on. Kept as a
    # stub so this module works standalone; wire to your Twilio sender.
    if is_enabled("whatsapp_alerts"):
        _notify_whatsapp_stub(d, event_type)
    return jsonify({"ok": True, "pass": d})


@contractors_bp.route("/api/contractors/<int:pass_id>/revoke", methods=["POST"])
@feature_required("contractor_passes")
def revoke_pass(pass_id):
    data = request.get_json(silent=True) or {}
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM contractor_passes WHERE id = ?", (pass_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "pass not found"}), 404
        conn.execute(
            "UPDATE contractor_passes SET revoked = 1, revoke_reason = ? "
            "WHERE id = ?",
            (_clean(data.get("reason"), 300), pass_id),
        )
        d = _pass_dict(
            conn.execute(
                "SELECT * FROM contractor_passes WHERE id = ?", (pass_id,)
            ).fetchone(),
            conn,
        )
    return jsonify({"ok": True, "pass": d})


@contractors_bp.route("/api/contractors/<int:pass_id>/events", methods=["GET"])
@feature_required("contractor_passes")
def pass_events(pass_id):
    """Attendance history for one contractor."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM contractor_events WHERE pass_id = ? "
            "ORDER BY event_time DESC, id DESC LIMIT 500",
            (pass_id,),
        ).fetchall()
    return jsonify({"events": [dict(r) for r in rows]})


@contractors_bp.route("/api/contractors/onsite", methods=["GET"])
@feature_required("contractor_passes")
def onsite_now():
    """Who is inside the factory RIGHT NOW — the owner's favourite screen,
    and the muster list during a fire drill / emergency."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM contractor_passes ORDER BY id DESC LIMIT 1000"
        ).fetchall()
        inside = [
            _pass_dict(r, conn) for r in rows
        ]
    inside = [p for p in inside if p.get("on_site")]
    return jsonify({"onsite": inside, "count": len(inside)})


# ---------------------------------------------------------------------------
def _notify_whatsapp_stub(pass_dict, event_type):
    """Replace this body with a call to your existing Twilio sender
    (the same one behind flat visitor notifications). Message idea:
      'Contractor {name} ({company}) checked {IN/OUT} at {gate}, {time}'.
    Left as a print so the module never crashes without Twilio wiring."""
    print(f"[contractors] WhatsApp stub: {pass_dict['name']} "
          f"checked {event_type.upper()}")
