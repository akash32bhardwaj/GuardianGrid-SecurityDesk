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

import logging
import os
import re
import secrets
import sqlite3
from datetime import datetime, date

from flask import Blueprint, jsonify, request

from site_profile import feature_required, is_enabled

# This module had no logger at all -- its one diagnostic was a print.
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OCT-96. This defaulted to the folder holding this file. In the container
# that is /app — part of the image, replaced wholesale by every deploy. So
# every contractor pass and every check-in was written to storage the next
# release would delete. Measured 20 Sep: /app/contractors.db, modified at
# the moment of the last container recreate, 0 rows — a trap that had not
# yet sprung, because no site had issued a pass. It springs the first time
# a factory uses the feature in a week that a release goes out.
#
# Persistent storage in the container is /data. OCTA_DATA_DIR still wins
# when set; the laptop, with no /data, keeps the old behaviour.
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get(
    "OCTA_DATA_DIR",
    "/data" if os.path.isdir("/data") else _CODE_DIR,
)
DB_PATH = os.path.join(DATA_DIR, "contractors.db")


def _adopt_legacy_db():
    """If the old in-image file has data and the persistent one does not
    exist yet, carry it across once instead of silently starting empty."""
    legacy = os.path.join(_CODE_DIR, "contractors.db")
    if legacy == DB_PATH or os.path.exists(DB_PATH) or not os.path.exists(legacy):
        return
    try:
        con = sqlite3.connect(legacy)
        n = con.execute("SELECT COUNT(*) FROM contractor_passes").fetchone()[0]
        con.close()
    except sqlite3.Error:
        return
    if n:
        import shutil
        shutil.copy2(legacy, DB_PATH)
        logger.info("[contractors] carried %d pass(es) from %s to %s",
                    n, legacy, DB_PATH)


_adopt_legacy_db()
# These two were prints, so they went to stdout and not to the log that
# 98ed487 configured a level for. A boot line that cannot be filtered,
# levelled or timestamped alongside everything else is a boot line nobody
# correlates with anything.
logger.info("[contractors] storage -> %s", DB_PATH)

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
    # Only if the site has alerts on. This used to call a stub that printed
    # to stdout, so the flag promised a notification and delivered nothing.
    if is_enabled("whatsapp_alerts"):
        _notify_whatsapp(d, event_type)
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
def _notify_whatsapp(pass_dict, event_type):
    """Tell the security head that a contractor came in or went out.

    This was a print statement for as long as the module existed, sitting
    behind `if is_enabled("whatsapp_alerts")`. A site with alerts switched
    ON believed contractor check-in and check-out were being notified, and
    the entire implementation wrote a line to stdout. The feature flag was
    the lie: it did not gate a send, it gated a print.

    Three rules this follows, each learned elsewhere in this codebase:

    - It goes through send_alert(), so it uses a TEMPLATE when one is
      configured and says which path it took. A freeform WhatsApp body
      only arrives inside a 24-hour window (OCT-129), and a contractor
      arriving at the gate is exactly the message that has to reach a cold
      phone.
    - It NEVER raises into the request. A guard scanning a pass at the
      gate must not see an error because Twilio is slow.
    - A send that does not happen says WHY, at warning level, naming the
      missing piece. Silence is what this function used to do.
    """
    name = pass_dict.get("name") or "A contractor"
    company = pass_dict.get("company") or ""
    gate = (pass_dict.get("last_event") or {}).get("gate") or "the gate"
    when = (pass_dict.get("last_event") or {}).get("event_time") or ""
    when = str(when).replace("T", " ")[:16]
    direction = "IN" if str(event_type).lower() == "in" else "OUT"
    who = f"{name} ({company})" if company else name

    body = (f"\U0001f477 *Contractor {direction}*\n"
            f"{who}\n"
            f"{gate} — {when}")

    try:
        import whatsapp_alerts as wa
    except Exception as e:
        logger.warning("[contractors] WhatsApp not notified for %s (%s): "
                       "whatsapp_alerts unavailable: %s", name, direction, e)
        return

    try:
        to = wa._security_number()
        if not to:
            logger.warning("[contractors] WhatsApp not notified for %s (%s): "
                           "no security number configured (dashboard setting "
                           "'security_whatsapp', or SECURITY_WHATSAPP)",
                           name, direction)
            return
        r = wa.send_alert(to, "contractor",
                          [who, direction, gate, when or "\u2014"],
                          fallback_body=body)
        if r.get("success"):
            logger.info("[contractors] %s checked %s — WhatsApp sent (%s)",
                        name, direction, r.get("path", "?"))
        else:
            logger.warning("[contractors] %s checked %s — WhatsApp NOT sent: %s",
                           name, direction, r.get("error") or r)
    except Exception as e:
        # Deliberately swallowed: the gate scan must succeed regardless.
        logger.warning("[contractors] WhatsApp notify failed for %s (%s): %s",
                       name, direction, e)
