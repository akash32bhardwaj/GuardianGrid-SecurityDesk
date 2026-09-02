r"""
resident_app.py — DEFENDER OCTA Resident App (v1 backend)
-----------------------------------------------------------
The flat-owner's side of Defender Octa. One blueprint, same server,
works per-site automatically.

WHO CALLS WHAT
  Resident phone (PWA at /resident)  ->  /api/resident/*   (resident token)
  Guard console (existing dashboard)  ->  /api/gate/pass/* (guard JWT)
                                          /api/gate/notices (guard JWT)

RESIDENT LOGIN — two doors, resident picks either:
  A) phone number + WhatsApp OTP (default, zero setup for the resident)
  B) flat number + PIN — for residents who don't want to share a number.
     The committee generates PIN slips from the dashboard; no phone is
     stored, WhatsApp alerts stay off, push notifications still work.
  POST /api/resident/pin/login        {flat, pin} -> {token, resident}
  Admin: GET  /api/admin/flats/pins           status per flat
         POST /api/admin/flats/pins/generate  {only_missing?} -> one-time PIN list
         POST /api/admin/flats/pins/reset     {flat} -> new PIN (one-time view)

RESIDENT LOGIN A  (phone number + WhatsApp OTP)
  POST /api/resident/otp/request   {phone}        -> OTP sent on WhatsApp
  POST /api/resident/otp/verify    {phone, otp}   -> {token, resident}
  The phone must already be on file: flat_directory (whatsapp) or the
  resident vehicle DB (residents.json phone). Unknown numbers get a
  polite "not registered — contact your committee". No self-signup, so
  a stranger can never see a flat's gate activity.

RESIDENT ROUTES  (Authorization: Bearer <resident token>)
  GET  /api/resident/me
  GET  /api/resident/home              this flat's day + protection state
  GET  /api/resident/passes            my gate passes (active + recent)
  POST /api/resident/passes            create {visitor_name, visitor_type,
                                       vehicle_plate?, valid: today|allday|
                                       weekend|custom, valid_to?, multi_entry?}
  POST /api/resident/passes/<code>/cancel
  POST /api/resident/ask               {q} Hinglish/English, scoped to flat
  GET  /api/resident/pulse             morning brief, score trend, notices
  POST /api/resident/sos               {note?} -> CRITICAL incident + WhatsApp
  GET  /api/resident/image/<filename>  event photo (only if it's this flat's)
  GET  /api/resident/activity?date=    this flat's day, any date (Activity tab)
  GET  /api/resident/arrivals/pending  visitor holding at the gate for me?
  POST /api/resident/arrivals/<id>/decide {decision: ALLOW|WAIT|DENY}
  GET  /api/resident/household         my vehicles, help, family (+ pending)
  POST /api/resident/household         {kind: vehicle|help|family, name, plate?, phone?}
  DELETE /api/resident/household/<id>  withdraw a pending request
  GET/POST /api/resident/prefs         {vehicle_alerts, daily_brief}
  GET  /api/resident/manifest.webmanifest, /api/resident/icon-192.png|512.png

GUARD / ADMIN ROUTES  (normal dashboard JWT — the global guard applies)
  GET  /api/gate/pass/<code>           look a pass up at the gate
  POST /api/gate/pass/<code>/use       admit -> logs visitor + notifies flat
  GET  /api/gate/notices               committee notices (admin sees all)
  POST /api/gate/notices               {title, body, days?}
  DELETE /api/gate/notices/<id>
  POST /api/gate/arrival               {name, flat, purpose, photo(file)} -> hold + ask resident
  GET  /api/gate/arrivals              live status of today's holds
  POST /api/gate/arrivals/<id>/admit   log the visitor in (after ALLOW)
  GET  /api/gate/household/pending     residents' add-vehicle/help/family requests
  POST /api/gate/household/<id>/approve | /reject

BACKGROUND (started by init, daemon threads):
  vehicle-alert poller  every 20s: new vehicle_events for plates whose
                        resident opted in -> WhatsApp "your car left/entered"
  daily-brief sender    07:35 local: society brief to residents who opted in
  arrival expiry        holds unanswered for 3 min -> EXPIRED, guard falls
                        back to the normal visitor log + WhatsApp

SECURITY MODEL
  Resident tokens are NOT the dashboard JWT. They are signed with a
  separate per-site secret (data/resident_secret.key, auto-created), so a
  resident token is useless against every guard/admin route, and the
  dashboard JWT is never handed to a resident phone. /api/resident/ is
  therefore listed in AUTH_EXEMPT_PREFIXES and this module enforces its
  own auth on every resident route.

Integration in api_server.py:
    from resident_app import resident_app_bp, init_resident_app
    init_resident_app(base_dir=BASE_DIR)
    app.register_blueprint(resident_app_bp)
  and add "/api/resident/" to AUTH_EXEMPT_PREFIXES.

whatsapp_config.py (optional new key):
    COMMITTEE_WHATSAPP = "+91XXXXXXXXXX"   # SOS also goes here
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, Response, jsonify, request, send_from_directory

logger = logging.getLogger(__name__)
resident_app_bp = Blueprint("resident_app", __name__)

# ── Tunables ─────────────────────────────────────────────────────
OTP_TTL_SECONDS       = 5 * 60      # OTP valid for 5 minutes
OTP_MAX_ATTEMPTS      = 5           # wrong guesses before the OTP dies
OTP_REQUESTS_PER_HOUR = 5           # per phone number
TOKEN_TTL_DAYS        = 30          # resident stays logged in
SOS_COOLDOWN_SECONDS  = 60          # ignore double-taps from the same flat
PASS_CODE_PREFIX      = "OCTA"
HOME_LOOKBACK_HOURS   = 24
PULSE_CACHE_SECONDS   = 60
ARRIVAL_WINDOW_SECONDS = 180        # resident has 3 min to answer a hold
ALERT_POLL_SECONDS    = 20          # vehicle-alert poller cadence
DAILY_BRIEF_TIME      = "07:35"     # resident brief, local time

# ── State set by init ────────────────────────────────────────────
_BASE_DIR = "."
_DB_PATH  = "guardiangrid.db"
_SECRET   = b""
_pulse_cache = {"at": 0, "payload": None}
_sos_last = {}          # flat_no -> epoch of last SOS
_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════
# Init
# ════════════════════════════════════════════════════════════════════

def init_resident_app(base_dir: str, start_threads: bool = True):
    global _BASE_DIR, _DB_PATH, _SECRET
    _BASE_DIR = base_dir
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")
    _SECRET = _load_or_create_secret()
    _ensure_tables()
    os.makedirs(os.path.join(_data_dir(), "arrivals"), exist_ok=True)
    if start_threads:
        threading.Thread(target=_background_loop, daemon=True,
                         name="resident-bg").start()
    logger.info(f"[RESIDENT] app armed (db={_DB_PATH})")


def _data_dir():
    """Per-site writable folder: /data in Docker, ./data on the laptop."""
    if os.path.isdir("/data"):
        return "/data"
    d = os.path.join(_BASE_DIR, "data")
    os.makedirs(d, exist_ok=True)
    return d


def _load_or_create_secret() -> bytes:
    env = os.environ.get("RESIDENT_JWT_SECRET", "").strip()
    if env:
        return env.encode()
    path = os.path.join(_data_dir(), "resident_secret.key")
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                s = f.read().strip()
                if len(s) >= 32:
                    return s
        s = secrets.token_urlsafe(48).encode()
        with open(path, "wb") as f:
            f.write(s)
        return s
    except OSError as e:
        logger.error(f"[RESIDENT] secret file error: {e} — using process secret")
        return secrets.token_urlsafe(48).encode()


def _con():
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _ensure_tables():
    try:
        con = _con()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS resident_otp (
            phone TEXT PRIMARY KEY,
            otp_hash TEXT,
            expires_at REAL,
            attempts INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS resident_otp_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT, requested_at REAL
        );
        CREATE TABLE IF NOT EXISTS gate_passes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            flat_no TEXT,
            resident_name TEXT,
            resident_phone TEXT,
            visitor_name TEXT,
            visitor_type TEXT,
            vehicle_plate TEXT,
            valid_from TEXT,
            valid_to TEXT,
            multi_entry INTEGER DEFAULT 0,
            uses INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ACTIVE',
            created_at TEXT,
            last_used_at TEXT,
            used_by TEXT
        );
        CREATE TABLE IF NOT EXISTS resident_sos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flat_no TEXT, phone TEXT, resident_name TEXT,
            note TEXT, created_at TEXT, incident_id TEXT, notified TEXT
        );
        CREATE TABLE IF NOT EXISTS resident_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, body TEXT, author TEXT,
            created_at TEXT, expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS arrival_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flat_no TEXT, visitor_name TEXT, purpose TEXT, photo TEXT,
            guard TEXT, status TEXT DEFAULT 'PENDING',
            decision TEXT, decided_by TEXT, decided_at TEXT,
            created_at TEXT, expires_at TEXT, admitted_at TEXT, visitor_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS household_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flat_no TEXT, resident_phone TEXT, resident_name TEXT,
            kind TEXT, name TEXT, plate TEXT, phone TEXT, note TEXT,
            status TEXT DEFAULT 'PENDING', created_at TEXT,
            decided_by TEXT, decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS household_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flat_no TEXT, kind TEXT, name TEXT, phone TEXT, note TEXT,
            added_at TEXT, added_by TEXT
        );
        CREATE TABLE IF NOT EXISTS resident_prefs (
            phone TEXT PRIMARY KEY,
            vehicle_alerts INTEGER DEFAULT 0,
            daily_brief INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS resident_state (
            key TEXT PRIMARY KEY, value TEXT
        );
        CREATE TABLE IF NOT EXISTS flat_pins (
            flat_no TEXT PRIMARY KEY,
            pin_hash TEXT,
            created_at TEXT, created_by TEXT,
            attempts INTEGER DEFAULT 0, locked_until REAL DEFAULT 0,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS resident_logins (
            phone TEXT PRIMARY KEY, flat_no TEXT, first_login TEXT, last_login TEXT, logins INTEGER DEFAULT 0
        );
        """)
        con.commit()
        con.close()
    except sqlite3.Error as e:
        logger.error(f"[RESIDENT] table init failed: {e}")


# ════════════════════════════════════════════════════════════════════
# Helpers — phones, plates, flats
# ════════════════════════════════════════════════════════════════════

def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _norm_phone(s: str) -> str:
    """Normalise to E.164 (+91XXXXXXXXXX). 10-digit → assume India."""
    d = _digits(s)
    if len(d) == 10:
        return "+91" + d
    if len(d) == 12 and d.startswith("91"):
        return "+" + d
    if len(d) == 11 and d.startswith("0"):
        return "+91" + d[1:]
    return "+" + d if d else ""


def _is_pin_id(p: str) -> bool:
    return str(p or "").startswith("flat:")


def _same_phone(a: str, b: str) -> bool:
    da, db_ = _digits(a), _digits(b)
    return bool(da and db_) and da[-10:] == db_[-10:]


def _norm_plate(p: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(p or "").upper())


def _norm_flat(f: str) -> str:
    return re.sub(r"\s+", "", str(f or "").upper())


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _all_directory_flats() -> list:
    """
    Every flat we know about, as dicts {flat_no, owner_name, whatsapp}.
    Source 1: flat_directory.py (whatever list function it exposes).
    Source 2: a flats-like table in the DB (detected by columns).
    Never raises.
    """
    out = []
    try:
        import flat_directory as fd
        for fn in ("all_flats", "list_flats", "get_all_flats", "get_flats",
                   "flats"):
            f = getattr(fd, fn, None)
            if callable(f):
                try:
                    rows = f() or []
                    for r in rows:
                        if isinstance(r, dict):
                            out.append(r)
                        else:
                            try:
                                out.append(dict(r))
                            except Exception:
                                pass
                    if out:
                        return out
                except Exception:
                    continue
    except Exception:
        pass
    # DB fallback — find a table with a whatsapp/phone column and a flat column
    try:
        con = _con()
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            if "flat" not in t.lower():
                continue          # never mistake visitors/residents tables for the directory
            cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
            lc = [c.lower() for c in cols]
            flat_col = next((c for c in cols if c.lower() in
                             ("flat_no", "flat", "flat_number", "unit")), None)
            ph_col = next((c for c in cols if c.lower() in
                           ("whatsapp", "phone", "mobile", "owner_phone")), None)
            name_col = next((c for c in cols if c.lower() in
                             ("owner_name", "name", "resident_name")), None)
            if flat_col and ph_col and "plate" not in " ".join(lc):
                for r in con.execute(f"SELECT * FROM {t}"):
                    out.append({"flat_no": r[flat_col],
                                "owner_name": r[name_col] if name_col else "",
                                "whatsapp": r[ph_col]})
                if out:
                    break
        con.close()
    except Exception as e:
        logger.debug(f"[RESIDENT] directory DB scan: {e}")
    return out


def _resident_plates() -> list:
    """All vehicle records from resident_db (residents.json)."""
    try:
        from resident_db import db as rdb
        return rdb.get_all() or []
    except Exception as e:
        logger.debug(f"[RESIDENT] resident_db unavailable: {e}")
        return []


def _resolve_resident_by_phone(phone: str):
    """
    phone -> {flat_no, name, phone, plates:[...]} or None.
    Checks the flat directory first, then the vehicle DB. A phone that
    appears in the vehicle DB alone is still allowed in (the committee
    gave us that number with the plate list).
    """
    phone = _norm_phone(phone)
    if not phone:
        return None
    flat_no, name = "", ""
    for f in _all_directory_flats():
        if _same_phone(f.get("whatsapp") or f.get("phone") or "", phone):
            flat_no = _norm_flat(f.get("flat_no") or f.get("flat") or "")
            name = f.get("owner_name") or f.get("name") or ""
            break
    plates, veh = [], _resident_plates()
    for v in veh:
        if _same_phone(v.get("phone", ""), phone):
            plates.append(v)
            if not flat_no:
                fl = v.get("flat_number") or ""
                bl = v.get("block") or ""
                flat_no = _norm_flat(f"{bl}-{fl}" if bl and fl and "-" not in fl
                                     else fl)
            if not name:
                name = v.get("resident_name") or ""
    if not flat_no and not plates:
        return None
    # plates registered to the flat under a different phone still count
    if flat_no:
        for v in veh:
            fl = _norm_flat(v.get("flat_number") or "")
            bl = _norm_flat(v.get("block") or "")
            combos = {fl, f"{bl}-{fl}" if bl else fl, f"{bl}{fl}" if bl else fl}
            if flat_no in combos and v not in plates:
                plates.append(v)
    return {
        "flat_no": flat_no or "—",
        "name": name or "Resident",
        "phone": phone,
        "plates": [{"plate": _norm_plate(p.get("plate_number")),
                    "model": p.get("vehicle_model") or "",
                    "color": p.get("vehicle_color") or "",
                    "type": p.get("vehicle_type") or "Car"} for p in plates],
    }


def _resolve_resident_by_flat(flat_no: str):
    """PIN-login identity: no phone stored. prefs/push key = 'flat:<FLAT>'."""
    flat_no = _canonical_flat(flat_no)
    if not flat_no:
        return None
    name = ""
    for f in _all_directory_flats():
        if _flat_matches(f.get("flat_no") or f.get("flat") or "", flat_no):
            name = f.get("owner_name") or f.get("name") or ""
            break
    plates = []
    for v in _resident_plates():
        fl = _norm_flat(v.get("flat_number") or "")
        bl = _norm_flat(v.get("block") or "")
        combos = {fl, f"{bl}-{fl}" if bl else fl, (bl + fl) if bl else fl}
        if flat_no in combos:
            plates.append({"plate": _norm_plate(v.get("plate_number")),
                           "model": v.get("vehicle_model") or "",
                           "color": v.get("vehicle_color") or "",
                           "type": v.get("vehicle_type") or "Car"})
    return {"flat_no": flat_no, "name": name or "Resident",
            "phone": f"flat:{flat_no}", "pin_login": True, "plates": plates}


# ════════════════════════════════════════════════════════════════════
# Resident token (HMAC-signed, separate from the dashboard JWT)
# ════════════════════════════════════════════════════════════════════

def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _make_token(res: dict) -> str:
    payload = {
        "sub": res["phone"], "flat": res["flat_no"], "name": res["name"],
        "role": "RESIDENT",
        "exp": int(time.time()) + TOKEN_TTL_DAYS * 86400,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def _read_token(tok: str):
    try:
        body, sig = tok.split(".", 1)
        good = _b64e(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        p = json.loads(_b64d(body))
        if p.get("role") != "RESIDENT" or p.get("exp", 0) < time.time():
            return None
        return p
    except Exception:
        return None


def resident_required(fn):
    @wraps(fn)
    def _wrap(*a, **kw):
        auth = request.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else request.args.get("rt", "")
        p = _read_token(tok) if tok else None
        if not p:
            return jsonify({"success": False,
                            "message": "Please log in again"}), 401
        # refresh the resident's record each call (plates may change)
        if _is_pin_id(p["sub"]):
            res = _resolve_resident_by_flat(p["flat"]) or {
                "flat_no": p["flat"], "name": p["name"], "phone": p["sub"],
                "pin_login": True, "plates": []}
        else:
            res = _resolve_resident_by_phone(p["sub"]) or {
                "flat_no": p["flat"], "name": p["name"], "phone": p["sub"],
                "plates": []}
        request.resident = res
        return fn(*a, **kw)
    return _wrap


# ════════════════════════════════════════════════════════════════════
# WhatsApp send (reuses the credential path visitor_notify already has)
# ════════════════════════════════════════════════════════════════════

def _twilio():
    """(client, from_number) or (None, reason)."""
    try:
        from visitor_notify import _resolve_credentials
        return _resolve_credentials()
    except Exception as e:
        return None, f"visitor_notify unavailable: {e}"


def _send_wa(to: str, body: str) -> dict:
    """Send a WhatsApp text. Tries whatsapp_alerts._send_whatsapp first
    (the path every other Octa alert uses), then the Twilio client.
    PIN-login identities ('flat:B-302') have no number — silently skipped."""
    if _is_pin_id(to):
        return {"success": False, "error": "pin identity — no number on file"}
    to = _norm_phone(to)
    if not to:
        return {"success": False, "error": "no number"}
    try:
        from whatsapp_alerts import _send_whatsapp
        # whatsapp_alerts expects the Twilio channel form ("whatsapp:+91...")
        # — that's how SECURITY_WHATSAPP is stored. Try that first; if the
        # helper ever starts adding the prefix itself, Twilio reports a
        # channel mismatch (error 21910) and we retry with the plain number.
        r = _send_whatsapp("whatsapp:" + to, body)
        if isinstance(r, dict) and not r.get("success") and \
                "channel" in str(r.get("error", "")).lower():
            r = _send_whatsapp(to, body)
        if isinstance(r, dict):
            return r
        return {"success": bool(r)}
    except Exception as e:
        logger.debug(f"[RESIDENT] whatsapp_alerts path unavailable: {e}")
    client, from_ = _twilio()
    if client is None:
        return {"success": False, "error": from_}
    try:
        m = client.messages.create(from_=from_, to="whatsapp:" + to, body=body)
        return {"success": True, "sid": m.sid}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def _site_name() -> str:
    try:
        p = os.path.join(_data_dir(), "site_config.json")
        if not os.path.exists(p):
            p = os.path.join(_BASE_DIR, "site_config.json")
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        return (cfg.get("society", {}) or {}).get("name") or cfg.get("name") \
            or "Your society"
    except Exception:
        return "Your society"


# ════════════════════════════════════════════════════════════════════
# OTP login
# ════════════════════════════════════════════════════════════════════

@resident_app_bp.route("/api/resident/otp/request", methods=["POST"])
def otp_request():
    data = request.get_json(silent=True) or {}
    phone = _norm_phone(data.get("phone", ""))
    if len(_digits(phone)) < 10:
        return jsonify({"success": False,
                        "message": "Enter your 10-digit mobile number"}), 400

    res = _resolve_resident_by_phone(phone)
    if not res:
        # Deliberately the same shape as success timing-wise, but honest:
        return jsonify({"success": False,
                        "message": "This number isn't registered with the "
                                   "society. Ask your committee to add it."}), 404

    con = _con()
    hour_ago = time.time() - 3600
    n = con.execute("SELECT COUNT(*) FROM resident_otp_requests "
                    "WHERE phone=? AND requested_at>?",
                    (phone, hour_ago)).fetchone()[0]
    if n >= OTP_REQUESTS_PER_HOUR:
        con.close()
        return jsonify({"success": False,
                        "message": "Too many codes requested. Try again in "
                                   "an hour."}), 429

    otp = f"{secrets.randbelow(10**6):06d}"
    con.execute("INSERT OR REPLACE INTO resident_otp "
                "(phone, otp_hash, expires_at, attempts, created_at) "
                "VALUES (?,?,?,0,?)",
                (phone, _otp_hash(phone, otp),
                 time.time() + OTP_TTL_SECONDS, _now_str()))
    con.execute("INSERT INTO resident_otp_requests (phone, requested_at) "
                "VALUES (?,?)", (phone, time.time()))
    con.commit()
    con.close()

    body = (f"🔐 *Defender Octa* — {_site_name()}\n\n"
            f"Your login code is *{otp}*\n"
            f"Valid for 5 minutes. Never share it — the guard or committee "
            f"will never ask for it.\n_S&N GuardianGrid_")
    r = _send_wa(phone, body)
    debug = os.environ.get("OCTA_OTP_DEBUG", "") == "1"
    if not r.get("success"):
        logger.warning(f"[RESIDENT] OTP send failed for {phone}: "
                       f"{r.get('error')}")
        if debug:
            print(f"[RESIDENT] OTP for {phone}: {otp}  (WhatsApp send failed: "
                  f"{r.get('error')})")
        else:
            return jsonify({"success": False,
                            "message": "Couldn't send the code on WhatsApp "
                                       "right now. Try again in a minute."}), 502
    out = {"success": True, "message": "Code sent on WhatsApp",
           "flat_hint": res["flat_no"]}
    if debug:
        out["debug_otp"] = otp        # ONLY when OCTA_OTP_DEBUG=1
    return jsonify(out)


def _otp_hash(phone: str, otp: str) -> str:
    return hmac.new(_SECRET, f"{phone}:{otp}".encode(), hashlib.sha256).hexdigest()


@resident_app_bp.route("/api/resident/otp/verify", methods=["POST"])
def otp_verify():
    data = request.get_json(silent=True) or {}
    phone = _norm_phone(data.get("phone", ""))
    otp = _digits(data.get("otp", ""))
    if not phone or len(otp) != 6:
        return jsonify({"success": False, "message": "Enter the 6-digit code"}), 400
    con = _con()
    row = con.execute("SELECT * FROM resident_otp WHERE phone=?",
                      (phone,)).fetchone()
    if not row:
        con.close()
        return jsonify({"success": False, "message": "Request a code first"}), 400
    if row["expires_at"] < time.time():
        con.execute("DELETE FROM resident_otp WHERE phone=?", (phone,))
        con.commit(); con.close()
        return jsonify({"success": False, "message": "Code expired — request a new one"}), 400
    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        con.execute("DELETE FROM resident_otp WHERE phone=?", (phone,))
        con.commit(); con.close()
        return jsonify({"success": False, "message": "Too many wrong tries — request a new code"}), 400
    if not hmac.compare_digest(row["otp_hash"], _otp_hash(phone, otp)):
        con.execute("UPDATE resident_otp SET attempts=attempts+1 WHERE phone=?",
                    (phone,))
        con.commit(); con.close()
        return jsonify({"success": False, "message": "That code isn't right"}), 401
    con.execute("DELETE FROM resident_otp WHERE phone=?", (phone,))
    con.commit(); con.close()

    res = _resolve_resident_by_phone(phone)
    if not res:
        return jsonify({"success": False, "message": "Number no longer registered"}), 404
    try:
        con = _con()
        con.execute("INSERT INTO resident_logins (phone, flat_no, first_login, last_login, logins) "
                    "VALUES (?,?,?,?,1) ON CONFLICT(phone) DO UPDATE SET last_login=excluded.last_login, "
                    "flat_no=excluded.flat_no, logins=logins+1",
                    (phone, res["flat_no"], _now_str(), _now_str()))
        con.commit(); con.close()
    except sqlite3.Error:
        pass
    return jsonify({"success": True, "token": _make_token(res),
                    "resident": res, "site": _site_name()})


@resident_app_bp.route("/api/resident/me")
@resident_required
def me():
    return jsonify({"success": True, "resident": request.resident,
                    "site": _site_name()})


# ════════════════════════════════════════════════════════════════════
# Data access — this flat's events, scoped hard
# ════════════════════════════════════════════════════════════════════

def _cols(con, table):
    try:
        return [c[1] for c in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _pick(cols, *names):
    lc = {c.lower(): c for c in cols}
    for n in names:
        if n in lc:
            return lc[n]
    return None


def _ts_key(ts):
    return str(ts or "").replace("T", " ")[:19]


def _fmt_time(ts):
    try:
        return datetime.strptime(_ts_key(ts), "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
    except Exception:
        return str(ts or "")[11:16]


def _fmt_day(ts):
    try:
        d = datetime.strptime(_ts_key(ts), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""
    today = datetime.now().date()
    if d.date() == today:
        return "today"
    if d.date() == today - timedelta(days=1):
        return "yesterday"
    return d.strftime("%a %d %b")


def _flat_matches(stored, flat_no):
    s, f = _norm_flat(stored), _norm_flat(flat_no)
    if not s or not f:
        return False
    return s == f or s.replace("-", "") == f.replace("-", "")


def _vehicle_events(plates, since: datetime, until: datetime = None):
    """vehicle_events rows for these plates in [since, until]."""
    plates = [_norm_plate(p) for p in plates if p]
    if not plates:
        return []
    con = _con()
    cols = _cols(con, "vehicle_events")
    if not cols:
        con.close(); return []
    until = until or datetime.now()
    rows = con.execute(
        "SELECT * FROM vehicle_events WHERE REPLACE(timestamp,'T',' ') "
        "BETWEEN ? AND ? ORDER BY timestamp DESC LIMIT 2000",
        (since.strftime("%Y-%m-%d %H:%M:%S"),
         until.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        if _norm_plate(d.get("plate")) not in plates:
            continue
        ev = str(d.get("event") or "").upper()
        out.append({
            "kind": "vehicle",
            "plate": _norm_plate(d.get("plate")),
            "event": ev or "SEEN",
            "direction": "out" if "EXIT" in ev or "OUT" in ev else "in",
            "camera": d.get("camera") or "Gate",
            "image": os.path.basename(d.get("image") or "") if d.get("image") else "",
            "timestamp": _ts_key(d.get("timestamp")),
            "time": _fmt_time(d.get("timestamp")),
            "day": _fmt_day(d.get("timestamp")),
        })
    return out


def _visitors(flat_no, since: datetime, until: datetime = None):
    con = _con()
    cols = _cols(con, "visitors")
    if not cols:
        con.close(); return []
    flat_col = _pick(cols, "flat", "flat_no", "flat_number", "unit")
    t_in = _pick(cols, "entry_time", "time_in", "created_at", "timestamp",
                 "entered_at", "in_time")
    t_out = _pick(cols, "exit_time", "time_out", "left_at", "out_time")
    name_col = _pick(cols, "name", "visitor_name")
    purpose_col = _pick(cols, "purpose", "reason")
    photo_col = _pick(cols, "photo", "image", "snapshot")
    if not (flat_col and t_in):
        con.close(); return []
    until = until or datetime.now()
    rows = con.execute(
        f"SELECT * FROM visitors WHERE REPLACE({t_in},'T',' ') BETWEEN ? AND ? "
        f"ORDER BY {t_in} DESC LIMIT 1000",
        (since.strftime("%Y-%m-%d %H:%M:%S"),
         until.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        if not _flat_matches(d.get(flat_col), flat_no):
            continue
        out.append({
            "kind": "visitor",
            "name": d.get(name_col) if name_col else "Visitor",
            "purpose": (d.get(purpose_col) if purpose_col else "") or "",
            "image": os.path.basename(d.get(photo_col) or "") if photo_col and d.get(photo_col) else "",
            "entered": _fmt_time(d.get(t_in)),
            "left": _fmt_time(d.get(t_out)) if t_out and d.get(t_out) else "",
            "inside": not (t_out and d.get(t_out)),
            "timestamp": _ts_key(d.get(t_in)),
            "time": _fmt_time(d.get(t_in)),
            "day": _fmt_day(d.get(t_in)),
        })
    return out


def _unknown_entries(since: datetime, until: datetime = None):
    """Site-level: unknown/unregistered vehicle entries (no plates exposed
    beyond a count + last one, for the 'confident negative')."""
    con = _con()
    cols = _cols(con, "vehicle_events")
    if not cols:
        con.close(); return 0, None
    until = until or datetime.now()
    rows = con.execute(
        "SELECT plate, access, state, timestamp, camera FROM vehicle_events "
        "WHERE REPLACE(timestamp,'T',' ') BETWEEN ? AND ? "
        "ORDER BY timestamp DESC LIMIT 3000",
        (since.strftime("%Y-%m-%d %H:%M:%S"),
         until.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    con.close()
    unk = [dict(r) for r in rows
           if "UNKNOWN" in str(r["access"] or r["state"] or "").upper()]
    last = None
    if unk:
        p = _norm_plate(unk[0].get("plate"))
        last = {"plate_hint": (p[:4] + "…") if len(p) > 4 else p,
                "time": _fmt_time(unk[0]["timestamp"]),
                "day": _fmt_day(unk[0]["timestamp"]),
                "camera": unk[0].get("camera") or "Gate"}
    return len(unk), last


def _live_score():
    try:
        from morning_report import collect, compute_score
        d = collect(12)
        score, label, color = compute_score(d)
        return {"score": score, "label": label, "color": color,
                "incidents": d.get("incidents_total", 0),
                "unknown": d.get("vehicles_unknown", 0)}
    except Exception as e:
        logger.debug(f"[RESIDENT] live score unavailable: {e}")
        return {"score": None, "label": "Monitoring", "color": "#34d399",
                "incidents": 0, "unknown": 0}


def _protection_state(score):
    """Calm-state line for the resident home."""
    s = score.get("score")
    hour = datetime.now().hour
    if s is None:
        return {"state": "PROTECTED", "headline": "SOCIETY PROTECTED",
                "line": "Octa on duty · monitoring the gates"}
    if s >= 85:
        return {"state": "PROTECTED", "headline": "SOCIETY PROTECTED",
                "line": f"Octa on duty · {'quiet night' if hour < 7 or hour >= 22 else 'all calm'} · score {s}/100"}
    if s >= 65:
        return {"state": "WATCHING", "headline": "OCTA IS WATCHING",
                "line": f"Some activity being verified · score {s}/100"}
    return {"state": "ALERT", "headline": "SECURITY ALERT",
            "line": f"Guards are responding · score {s}/100"}


# ════════════════════════════════════════════════════════════════════
# Home
# ════════════════════════════════════════════════════════════════════

@resident_app_bp.route("/api/resident/home")
@resident_required
def home():
    res = request.resident
    since = datetime.now() - timedelta(hours=HOME_LOOKBACK_HOURS)
    plates = [p["plate"] for p in res["plates"]]
    items = _vehicle_events(plates, since) + _visitors(res["flat_no"], since)
    # passes used today
    con = _con()
    used = con.execute(
        "SELECT * FROM gate_passes WHERE flat_no=? AND last_used_at IS NOT NULL "
        "AND REPLACE(last_used_at,'T',' ')>=? ORDER BY last_used_at DESC",
        (res["flat_no"], since.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    con.close()
    seen_v = {(i.get("name"), i["timestamp"][:16]) for i in items if i["kind"] == "visitor"}
    for u in used:
        if (u["visitor_name"], _ts_key(u["last_used_at"])[:16]) in seen_v:
            continue      # already shown as the visitor entry the guard logged
        items.append({"kind": "pass", "name": u["visitor_name"],
                      "purpose": f"{u['visitor_type']} · pass {u['code']}",
                      "timestamp": _ts_key(u["last_used_at"]),
                      "time": _fmt_time(u["last_used_at"]),
                      "day": _fmt_day(u["last_used_at"]), "image": ""})
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    score = _live_score()
    # per-vehicle last movement (7 days) for the "my vehicles" strip
    week = _vehicle_events(plates, datetime.now() - timedelta(days=7))
    vehicles = []
    for v in res["plates"]:
        last = next((e for e in week if e["plate"] == v["plate"]), None)
        vehicles.append({**v, "last": last,
                         "inside": (last["direction"] == "in") if last else None})
    return jsonify({
        "success": True,
        "site": _site_name(),
        "resident": res,
        "protection": _protection_state(score),
        "vehicles": vehicles,
        "prefs": _get_prefs(res["phone"]),
        "items": items[:30],
        "counts": {"vehicle": sum(1 for i in items if i["kind"] == "vehicle"),
                   "visitors": sum(1 for i in items if i["kind"] in ("visitor", "pass"))},
    })


@resident_app_bp.route("/api/resident/image/<path:filename>")
@resident_required
def resident_image(filename):
    """Serve an event photo only if it belongs to this flat's vehicles or
    visitors (checked against the last 30 days)."""
    res = request.resident
    fn = os.path.basename(filename)
    since = datetime.now() - timedelta(days=30)
    plates = [p["plate"] for p in res["plates"]]
    allowed = {i["image"] for i in _vehicle_events(plates, since) if i["image"]}
    allowed |= {i["image"] for i in _visitors(res["flat_no"], since) if i["image"]}
    try:
        con = _con()
        allowed |= {r[0] for r in con.execute(
            "SELECT photo FROM arrival_requests WHERE flat_no=? AND photo IS NOT NULL",
            (res["flat_no"],))}
        con.close()
    except sqlite3.Error:
        pass
    if fn not in allowed:
        return jsonify({"success": False, "message": "Not your photo"}), 403
    arr = os.path.join(_data_dir(), "arrivals")
    if os.path.exists(os.path.join(arr, fn)):
        return send_from_directory(arr, fn)
    for folder in ("output/webcam", "output", "data/captures", "captures",
                   "output/gate", "data/uploads"):
        d = os.path.join(_BASE_DIR, folder)
        if os.path.exists(os.path.join(d, fn)):
            return send_from_directory(d, fn)
        d2 = os.path.join("/data", folder.replace("data/", ""))
        if os.path.exists(os.path.join(d2, fn)):
            return send_from_directory(d2, fn)
    return jsonify({"success": False, "message": "Photo not found"}), 404


# ════════════════════════════════════════════════════════════════════
# Gate passes
# ════════════════════════════════════════════════════════════════════

VISITOR_TYPES = ("Guest", "Delivery", "Cab", "Vendor", "Family", "Staff")


def _new_code(con) -> str:
    for _ in range(20):
        code = f"{PASS_CODE_PREFIX}-{secrets.randbelow(9000) + 1000}"
        if not con.execute("SELECT 1 FROM gate_passes WHERE code=? AND status='ACTIVE'",
                           (code,)).fetchone():
            return code
    return f"{PASS_CODE_PREFIX}-{secrets.randbelow(90000) + 10000}"


def _validity(kind: str, custom_to: str = ""):
    now = datetime.now()
    end_today = now.replace(hour=23, minute=59, second=59, microsecond=0)
    if kind == "weekend":
        # up to Sunday 23:59
        days_to_sun = (6 - now.weekday()) % 7
        return now, (now + timedelta(days=days_to_sun)).replace(
            hour=23, minute=59, second=59, microsecond=0)
    if kind == "custom" and custom_to:
        try:
            to = datetime.strptime(custom_to[:16].replace("T", " "), "%Y-%m-%d %H:%M")
            if to > now:
                return now, min(to, now + timedelta(days=7))
        except ValueError:
            pass
    # "today" (default) and "allday" both end tonight
    return now, end_today


def _pass_dict(r):
    d = dict(r)
    now = _now_str()
    exp = d["valid_to"] and d["valid_to"] < now
    if d["status"] == "ACTIVE" and exp:
        d["status"] = "EXPIRED"
    d["valid_to_h"] = _fmt_time(d["valid_to"]) + (" " + _fmt_day(d["valid_to"]) if _fmt_day(d["valid_to"]) != "today" else " today")
    return d


@resident_app_bp.route("/api/resident/passes")
@resident_required
def list_passes():
    con = _con()
    rows = con.execute(
        "SELECT * FROM gate_passes WHERE flat_no=? "
        "ORDER BY created_at DESC LIMIT 50", (request.resident["flat_no"],)).fetchall()
    con.close()
    passes = [_pass_dict(r) for r in rows]
    return jsonify({"success": True, "passes": passes,
                    "active": [p for p in passes if p["status"] == "ACTIVE"],
                    "types": VISITOR_TYPES})


@resident_app_bp.route("/api/resident/passes", methods=["POST"])
@resident_required
def create_pass():
    res = request.resident
    data = request.get_json(silent=True) or {}
    name = (data.get("visitor_name") or "").strip()[:60]
    vtype = (data.get("visitor_type") or "Guest").strip().title()
    if vtype not in VISITOR_TYPES:
        vtype = "Guest"
    plate = _norm_plate(data.get("vehicle_plate", ""))
    multi = 1 if data.get("multi_entry") else 0
    valid_from, valid_to = _validity(data.get("valid", "today"),
                                     data.get("valid_to", ""))
    if not name:
        return jsonify({"success": False, "message": "Who is coming? Add a name."}), 400
    con = _con()
    active = con.execute("SELECT COUNT(*) FROM gate_passes WHERE flat_no=? "
                         "AND status='ACTIVE' AND valid_to>=?",
                         (res["flat_no"], _now_str())).fetchone()[0]
    if active >= 10:
        con.close()
        return jsonify({"success": False,
                        "message": "10 active passes already — cancel one first"}), 400
    code = _new_code(con)
    con.execute(
        "INSERT INTO gate_passes (code, flat_no, resident_name, resident_phone, "
        "visitor_name, visitor_type, vehicle_plate, valid_from, valid_to, "
        "multi_entry, uses, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,'ACTIVE',?)",
        (code, res["flat_no"], res["name"], res["phone"], name, vtype, plate,
         valid_from.strftime("%Y-%m-%d %H:%M:%S"),
         valid_to.strftime("%Y-%m-%d %H:%M:%S"), multi, _now_str()))
    con.commit()
    row = con.execute("SELECT * FROM gate_passes WHERE code=?", (code,)).fetchone()
    con.close()
    p = _pass_dict(row)
    site = _site_name()
    p["share_text"] = (
        f"{site} gate pass for {name}\n"
        f"Code: {code}\n"
        f"Flat: {res['flat_no']} ({res['name']})\n"
        f"Valid till {p['valid_to_h']}"
        + (f"\nVehicle: {plate}" if plate else "")
        + "\nShow this code at the gate — Defender Octa")
    return jsonify({"success": True, "pass": p})


@resident_app_bp.route("/api/resident/passes/<code>/cancel", methods=["POST"])
@resident_required
def cancel_pass(code):
    con = _con()
    n = con.execute("UPDATE gate_passes SET status='CANCELLED' WHERE code=? "
                    "AND flat_no=? AND status='ACTIVE'",
                    (code.upper(), request.resident["flat_no"])).rowcount
    con.commit(); con.close()
    if not n:
        return jsonify({"success": False, "message": "Pass not found or already used"}), 404
    return jsonify({"success": True})


# ── Guard side (dashboard JWT) ───────────────────────────────────

def _pass_check(code: str):
    con = _con()
    row = con.execute("SELECT * FROM gate_passes WHERE code=?",
                      (code.upper().strip(),)).fetchone()
    con.close()
    if not row:
        return None, "No such pass"
    p = _pass_dict(row)
    if p["status"] == "CANCELLED":
        return p, "Pass was cancelled by the resident"
    if p["status"] == "EXPIRED" or p["valid_to"] < _now_str():
        return p, "Pass has expired"
    if p["status"] == "USED":
        return p, "Pass already used (single entry)"
    if p["valid_from"] > _now_str():
        return p, "Pass not valid yet"
    return p, ""


@resident_app_bp.route("/api/gate/pass/<code>")
def gate_pass_lookup(code):
    p, problem = _pass_check(code)
    if p is None:
        return jsonify({"success": False, "valid": False, "message": problem}), 404
    safe = {k: p[k] for k in ("code", "flat_no", "resident_name", "visitor_name",
                              "visitor_type", "vehicle_plate", "valid_to",
                              "valid_to_h", "multi_entry", "uses", "status")}
    return jsonify({"success": True, "valid": not problem, "message": problem,
                    "pass": safe})


@resident_app_bp.route("/api/gate/pass/<code>/use", methods=["POST"])
def gate_pass_use(code):
    p, problem = _pass_check(code)
    if p is None or problem:
        return jsonify({"success": False, "message": problem}), 400
    data = request.get_json(silent=True) or {}
    operator = (data.get("operator") or
                (getattr(request, "auth_user", None) or {}).get("username")
                or "guard")
    plate_seen = _norm_plate(data.get("plate", "")) or p["vehicle_plate"]
    now = _now_str()
    new_status = "ACTIVE" if p["multi_entry"] else "USED"
    con = _con()
    con.execute("UPDATE gate_passes SET uses=uses+1, last_used_at=?, used_by=?, "
                "status=? WHERE code=?", (now, operator, new_status, p["code"]))
    con.commit(); con.close()

    # Log as a visitor entry so the register, brief and search all see it
    vid = None
    try:
        from db import add_visitor
        vid = add_visitor(p["visitor_name"], p["flat_no"], "",
                          f"{p['visitor_type']} · pre-approved {p['code']}"
                          + (f" · {plate_seen}" if plate_seen else ""))
    except Exception as e:
        logger.warning(f"[RESIDENT] add_visitor failed: {e}")

    _push_to_phones([p["resident_phone"]], {
        "title": f"✅ {p['visitor_name']} entered",
        "body": f"On your pass {p['code']} at {_fmt_time(now)}.",
        "tag": f"pass-{p['code']}", "url": "/resident"})

    # Tell the resident their guest is in (WhatsApp, background)
    def _notify():
        msg = (f"✅ *Defender Octa* — {_site_name()}\n\n"
               f"{p['visitor_name']} ({p['visitor_type']}) entered on your pass "
               f"{p['code']} at {_fmt_time(now)}."
               + (f"\nVehicle: {plate_seen}" if plate_seen else "")
               + f"\nLogged by gate security.")
        _send_wa(p["resident_phone"], msg)
    threading.Thread(target=_notify, daemon=True).start()

    return jsonify({"success": True, "visitor_id": vid,
                    "message": f"Admitted {p['visitor_name']} → {p['flat_no']}",
                    "remaining": "multi-entry" if p["multi_entry"] else "single use — now closed"})


# ════════════════════════════════════════════════════════════════════
# Ask Octa — scoped to this flat
# ════════════════════════════════════════════════════════════════════

_WORDS = {
    "vehicle": ("car", "gaadi", "gadi", "vehicle", "scooty", "bike", "cab",
                "plate", "gaadhi"),
    "left": ("nikli", "nikla", "gayi", "gaya", "left", "exit", "out", "bahar",
             "nikle", "departed", "went"),
    "came": ("aayi", "aaya", "aai", "wapas", "returned", "return", "entered",
             "entry", "came", "back", "andar", "lauti", "pahunchi"),
    "visitor": ("visitor", "visitors", "guest", "mehmaan", "mehman", "delivery",
                "courier", "kaun", "kon", "who", "aaya tha", "milne"),
    "unknown": ("unknown", "anjaan", "anjan", "unregistered", "stranger",
                "suspicious", "shak", "ajnabi", "outsider"),
    "pass": ("pass", "passes", "approved", "pre-approved"),
}


def _window_from_text(q: str):
    now = datetime.now()
    ql = q.lower()
    if any(w in ql for w in ("hafte", "hafta", "week", "saptah")):
        return now - timedelta(days=7), now, "this week"
    if "parso" in ql:
        d = (now - timedelta(days=2)).date()
        return datetime.combine(d, datetime.min.time()), \
            datetime.combine(d, datetime.max.time()), "day before yesterday"
    if "kal" in ql or "yesterday" in ql:
        d = (now - timedelta(days=1)).date()
        if any(w in ql for w in ("raat", "night")):
            s = datetime.combine(d, datetime.min.time()).replace(hour=20)
            return s, s + timedelta(hours=11), "last night"
        return datetime.combine(d, datetime.min.time()), \
            datetime.combine(d, datetime.max.time()), "yesterday"
    if any(w in ql for w in ("raat", "night", "tonight")):
        s = (now - timedelta(days=1)).replace(hour=20, minute=0, second=0)
        return s, now, "last night"
    if any(w in ql for w in ("aaj", "today", "abhi", "subah", "morning")):
        return now.replace(hour=0, minute=0, second=0), now, "today"
    if any(w in ql for w in ("month", "mahine", "mahina")):
        return now - timedelta(days=30), now, "this month"
    return now - timedelta(hours=24), now, "the last 24 hours"


def _has(q, key):
    ql = q.lower()
    return any(w in ql for w in _WORDS[key])


@resident_app_bp.route("/api/resident/ask", methods=["POST"])
@resident_required
def ask():
    res = request.resident
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()[:300]
    if not q:
        return jsonify({"success": False, "message": "Ask something first"}), 400
    since, until, label = _window_from_text(q)
    plates = [p["plate"] for p in res["plates"]]
    flat = res["flat_no"]
    photos, items = [], []

    # A specific plate typed in? Only allowed if it's theirs.
    typed = re.findall(r"[A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,3}\s?\d{3,4}", q.upper())
    typed = [_norm_plate(t) for t in typed if _norm_plate(t) in plates]
    if typed:
        plates = typed

    if _has(q, "unknown"):
        n, last = _unknown_entries(since, until)
        if n == 0:
            answer = (f"No unknown vehicles entered {label}. Every entry was a "
                      f"registered resident, a logged visitor, or a verified pass.")
        else:
            answer = (f"{n} unknown vehicle{'s' if n > 1 else ''} entered {label}, "
                      f"all logged by the gate with photos. Latest: {last['plate_hint']} "
                      f"at {last['time']} {last['day']} ({last['camera']}). "
                      f"None were linked to flat {flat}.")
        return jsonify({"success": True, "answer": answer, "items": [],
                        "window": label})

    if _has(q, "visitor") or _has(q, "pass"):
        vis = _visitors(flat, since, until)
        if not vis:
            answer = f"Nobody visited flat {flat} {label}."
        else:
            names = ", ".join(f"{v['name']} ({v['purpose'] or 'visitor'}, {v['time']} {v['day']})"
                              for v in vis[:6])
            answer = (f"{len(vis)} visitor{'s' if len(vis) > 1 else ''} to flat {flat} "
                      f"{label}: {names}." + (" All logged with photos." if any(v['image'] for v in vis) else ""))
        return jsonify({"success": True, "answer": answer, "items": vis[:10],
                        "window": label})

    # Vehicle questions (default)
    evs = _vehicle_events(plates, since, until)
    if not plates:
        return jsonify({"success": True, "window": label, "items": [],
                        "answer": f"No vehicle is registered to flat {flat} yet — "
                                  f"ask your committee to add your plate."})
    if not evs:
        return jsonify({"success": True, "window": label, "items": [],
                        "answer": f"Your vehicle{'s' if len(plates) > 1 else ''} "
                                  f"({', '.join(plates)}) didn't pass the gate {label}."})
    want_left = _has(q, "left") and not _has(q, "came")
    want_came = _has(q, "came") and not _has(q, "left")
    if want_left:
        pick = [e for e in evs if e["direction"] == "out"] or evs
        e = pick[0]
        answer = f"{e['plate']} left {e['day']} at {e['time']} from {e['camera']}."
    elif want_came:
        pick = [e for e in evs if e["direction"] == "in"] or evs
        e = pick[0]
        answer = f"{e['plate']} came in {e['day']} at {e['time']} at {e['camera']}."
    else:
        e = evs[0]
        parts = [f"{x['plate']} {'left' if x['direction']=='out' else 'entered'} "
                 f"{x['time']} {x['day']}" for x in evs[:4]]
        answer = (f"{len(evs)} gate movement{'s' if len(evs) > 1 else ''} for your "
                  f"vehicle{'s' if len(plates) > 1 else ''} {label}: " + "; ".join(parts) + ".")
    return jsonify({"success": True, "answer": answer, "items": evs[:10],
                    "window": label})


# ════════════════════════════════════════════════════════════════════
# Society pulse
# ════════════════════════════════════════════════════════════════════

def _reports(n=8):
    """Latest morning-brief JSONs from reports/ (newest first)."""
    out = []
    for rdir in (os.path.join(_BASE_DIR, "reports"), "/data/reports", "reports"):
        if not os.path.isdir(rdir):
            continue
        for f in sorted(os.listdir(rdir), reverse=True):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(rdir, f), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except Exception:
                continue
            if len(out) >= n:
                break
        if out:
            break
    return out


def _brief_text(rep: dict) -> str:
    for k in ("narrative", "brief", "summary", "text", "message", "headline"):
        v = rep.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v:
            return " ".join(str(x) for x in v[:4])
    sc = rep.get("score")
    return f"Security score {sc}/100." if sc is not None else "Report available."


@resident_app_bp.route("/api/resident/pulse")
@resident_required
def pulse():
    now = time.time()
    with _lock:
        if _pulse_cache["payload"] and now - _pulse_cache["at"] < PULSE_CACHE_SECONDS:
            payload = dict(_pulse_cache["payload"])
    if not (_pulse_cache["payload"] and now - _pulse_cache["at"] < PULSE_CACHE_SECONDS):
        reps = _reports(8)
        latest = reps[0] if reps else {}
        trend = []
        for r in reversed(reps[:7]):
            trend.append({"date": str(r.get("date") or r.get("day") or "")[:10],
                          "score": r.get("score")})
        live = _live_score()
        # ack stats (guard response) — best effort
        ack = {}
        try:
            con = _con()
            row = con.execute(
                "SELECT AVG(response_seconds) AS avg_s, COUNT(*) AS n, "
                "SUM(escalated) AS esc FROM ack_log "
                "WHERE created_at >= date('now','-30 days')").fetchone()
            con.close()
            if row and row["n"]:
                ack = {"avg_seconds": round(row["avg_s"] or 0),
                       "incidents": row["n"], "escalated": row["esc"] or 0}
        except sqlite3.Error:
            pass
        payload = {
            "site": _site_name(),
            "brief": {"date": str(latest.get("date") or "")[:10],
                      "text": _brief_text(latest) if latest else
                      "The first morning brief arrives after tonight's watch.",
                      "score": latest.get("score")},
            "live": live,
            "trend": trend,
            "guard_response": ack,
            "health": {"status": "MONITORED",
                       "line": "Gates monitored round the clock"},
        }
        with _lock:
            _pulse_cache.update(at=now, payload=payload)
        payload = dict(payload)
    con = _con()
    notices = [dict(r) for r in con.execute(
        "SELECT id, title, body, author, created_at, expires_at FROM resident_notices "
        "WHERE expires_at IS NULL OR expires_at >= ? ORDER BY created_at DESC LIMIT 10",
        (_now_str(),))]
    con.close()
    payload["notices"] = notices
    payload["success"] = True
    return jsonify(payload)


# ── Notices (admin/guard side) ───────────────────────────────────

@resident_app_bp.route("/api/gate/notices")
def notices_list():
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM resident_notices ORDER BY created_at DESC LIMIT 50")]
    con.close()
    return jsonify({"success": True, "notices": rows})


@resident_app_bp.route("/api/gate/notices", methods=["POST"])
def notices_add():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:80]
    body = (data.get("body") or "").strip()[:500]
    days = int(data.get("days") or 7)
    if not title:
        return jsonify({"success": False, "message": "title required"}), 400
    author = (getattr(request, "auth_user", None) or {}).get("username") or "committee"
    exp = (datetime.now() + timedelta(days=max(1, min(days, 90)))).strftime("%Y-%m-%d %H:%M:%S")
    con = _con()
    cur = con.execute("INSERT INTO resident_notices (title, body, author, created_at, expires_at) "
                      "VALUES (?,?,?,?,?)", (title, body, author, _now_str(), exp))
    con.commit(); nid = cur.lastrowid; con.close()
    return jsonify({"success": True, "id": nid})


@resident_app_bp.route("/api/gate/notices/<int:nid>", methods=["DELETE"])
def notices_delete(nid):
    con = _con()
    n = con.execute("DELETE FROM resident_notices WHERE id=?", (nid,)).rowcount
    con.commit(); con.close()
    return jsonify({"success": bool(n)})


# ════════════════════════════════════════════════════════════════════
# Resident SOS → CRITICAL incident (ack loop) + WhatsApp
# ════════════════════════════════════════════════════════════════════

@resident_app_bp.route("/api/resident/sos", methods=["POST"])
@resident_required
def sos():
    res = request.resident
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()[:200]
    flat = res["flat_no"]
    now = time.time()
    last = _sos_last.get(flat, 0)
    if now - last < SOS_COOLDOWN_SECONDS:
        return jsonify({"success": True, "duplicate": True,
                        "message": "SOS already raised — help is on the way"})
    _sos_last[flat] = now

    title = f"RESIDENT SOS — Flat {flat}"
    desc = (f"{res['name']} (flat {flat}, {res['phone']}) pressed SOS in the "
            f"resident app at {_fmt_time(_now_str())}."
            + (f" Note: {note}" if note else ""))
    incident_id = None
    try:
        from backend.incidents.incident_service import create_new_incident
        inc = create_new_incident({
            "title": title, "description": desc, "severity": "CRITICAL",
            "camera_name": "Resident App", "evidence_image": None,
            "plate_number": "--", "resident_name": res["name"],
            "flat_number": flat, "confidence": 100,
        })
        if isinstance(inc, dict):
            incident_id = inc.get("incident_id") or inc.get("id")
        else:
            incident_id = getattr(inc, "incident_id", None) or str(inc)
    except Exception as e:
        logger.error(f"[RESIDENT] SOS incident create failed: {e}")

    # WhatsApp: security head + committee (never blocks the response)
    def _notify():
        sent = []
        try:
            import whatsapp_config as cfg
            targets = [("security", getattr(cfg, "SECURITY_WHATSAPP", "")),
                       ("committee", getattr(cfg, "COMMITTEE_WHATSAPP", ""))]
        except Exception:
            targets = []
        msg = (f"🆘 *DEFENDER OCTA — RESIDENT SOS*\n\n"
               f"🏠 Flat {flat} · {res['name']}\n"
               f"📞 {res['phone']}\n"
               f"🕒 {_fmt_time(_now_str())}"
               + (f"\n📝 {note}" if note else "")
               + f"\n\nGuard has 3 minutes to acknowledge on the dashboard, "
                 f"then this escalates automatically."
               + (f"\n🆔 {incident_id}" if incident_id else "")
               + "\n_S&N GuardianGrid Security System_")
        for who, num in targets:
            if num:
                r = _send_wa(num, msg)
                sent.append(f"{who}:{'ok' if r.get('success') else 'fail'}")
        try:
            con = _con()
            con.execute("UPDATE resident_sos SET notified=? WHERE id=?",
                        (",".join(sent) or "none", sos_id))
            con.commit(); con.close()
        except sqlite3.Error:
            pass

    con = _con()
    cur = con.execute("INSERT INTO resident_sos (flat_no, phone, resident_name, note, "
                      "created_at, incident_id, notified) VALUES (?,?,?,?,?,?,?)",
                      (flat, res["phone"], res["name"], note, _now_str(),
                       incident_id, "pending"))
    con.commit(); sos_id = cur.lastrowid; con.close()
    threading.Thread(target=_notify, daemon=True).start()

    # Also drop it on the dashboard activity feed if we can reach it
    try:
        import api_server as srv
        srv.activity_feed.appendleft({"time": datetime.now().isoformat(),
                                      "event": f"🆘 RESIDENT SOS — Flat {flat}",
                                      "type": "incident"})
    except Exception:
        pass

    return jsonify({"success": True, "incident_id": incident_id,
                    "message": "SOS raised — guard alerted, 3-minute clock started"})


# ════════════════════════════════════════════════════════════════════
# v1.1 — Activity (any day)
# ════════════════════════════════════════════════════════════════════

@resident_app_bp.route("/api/resident/activity")
@resident_required
def activity():
    res = request.resident
    day = re.sub(r"[^0-9-]", "", request.args.get("date", ""))[:10]
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now().date()
    except ValueError:
        d = datetime.now().date()
    since = datetime.combine(d, datetime.min.time())
    until = datetime.combine(d, datetime.max.time())
    plates = [p["plate"] for p in res["plates"]]
    items = _vehicle_events(plates, since, until) + _visitors(res["flat_no"], since, until)
    con = _con()
    for a in con.execute(
            "SELECT * FROM arrival_requests WHERE flat_no=? AND decision IS NOT NULL "
            "AND REPLACE(created_at,'T',' ') BETWEEN ? AND ?",
            (res["flat_no"], since.strftime("%Y-%m-%d %H:%M:%S"),
             until.strftime("%Y-%m-%d %H:%M:%S"))):
        items.append({"kind": "arrival", "name": a["visitor_name"],
                      "purpose": f"{a['purpose'] or 'Visitor'} · you said {a['decision'].lower()}",
                      "image": a["photo"] or "", "decision": a["decision"],
                      "timestamp": _ts_key(a["created_at"]), "time": _fmt_time(a["created_at"]),
                      "day": _fmt_day(a["created_at"])})
    con.close()
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify({"success": True, "date": d.isoformat(),
                    "label": "Today" if d == datetime.now().date() else
                    ("Yesterday" if d == datetime.now().date() - timedelta(days=1)
                     else d.strftime("%a %d %b")),
                    "items": items, "is_today": d == datetime.now().date()})


# ════════════════════════════════════════════════════════════════════
# v1.1 — Arrival approval (guard holds → resident decides → guard admits)
# ════════════════════════════════════════════════════════════════════

def _arrival_dict(r, for_resident=False):
    d = dict(r)
    now = _now_str()
    if d["status"] == "PENDING" and d["expires_at"] < now:
        d["status"] = "EXPIRED"
    d["seconds_left"] = max(0, int((datetime.strptime(d["expires_at"], "%Y-%m-%d %H:%M:%S")
                                   - datetime.now()).total_seconds())) if d["status"] == "PENDING" else 0
    d["time"] = _fmt_time(d["created_at"])
    if for_resident:
        d.pop("guard", None)
    return d


@resident_app_bp.route("/api/gate/arrival", methods=["POST"])
def gate_arrival():
    """Guard: a visitor is at the gate for a flat — hold and ask the resident.
    Accepts JSON or multipart (with an optional 'photo' file)."""
    if request.content_type and "multipart" in request.content_type:
        data = request.form.to_dict()
        photo_file = request.files.get("photo")
    else:
        data = request.get_json(silent=True) or {}
        photo_file = None
    name = (data.get("name") or "").strip()[:60]
    flat = _norm_flat(data.get("flat") or "")
    purpose = (data.get("purpose") or "").strip()[:80]
    guard = (data.get("operator") or
             (getattr(request, "auth_user", None) or {}).get("username") or "guard")
    if not name or not flat:
        return jsonify({"success": False, "message": "visitor name and flat required"}), 400
    now = datetime.now()
    con = _con()
    cur = con.execute(
        "INSERT INTO arrival_requests (flat_no, visitor_name, purpose, guard, status, "
        "created_at, expires_at) VALUES (?,?,?,?,'PENDING',?,?)",
        (flat, name, purpose, guard, now.strftime("%Y-%m-%d %H:%M:%S"),
         (now + timedelta(seconds=ARRIVAL_WINDOW_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")))
    aid = cur.lastrowid
    photo = None
    if photo_file and photo_file.filename:
        photo = f"arr_{aid}.jpg"
        try:
            photo_file.save(os.path.join(_data_dir(), "arrivals", photo))
            con.execute("UPDATE arrival_requests SET photo=? WHERE id=?", (photo, aid))
        except Exception as e:
            logger.warning(f"[RESIDENT] arrival photo save failed: {e}")
            photo = None
    con.commit()
    row = con.execute("SELECT * FROM arrival_requests WHERE id=?", (aid,)).fetchone()
    con.close()

    # Ring the phone instantly (push), then WhatsApp as the fallback nudge
    _push_to_phones(_flat_phones(flat) + [f"flat:{flat}"], {
        "title": f"🚪 {name} is at your gate",
        "body": f"{purpose or 'Visitor'} for flat {flat} — guard is holding. "
                f"Let in / Wait / Decline (3 min).",
        "tag": f"arrival-{aid}", "urgent": True, "url": "/resident"})

    def _nudge():
        phones = _flat_phones(flat)
        msg = (f"🚪 *Defender Octa* — {_site_name()}\n\n"
               f"{name}{' (' + purpose + ')' if purpose else ''} is at the gate for "
               f"flat {flat}.\nThe guard is holding them — open the app to "
               f"*Let in / Wait / Decline* within 3 minutes.")
        for ph in phones[:2]:
            _send_wa(ph, msg)
    threading.Thread(target=_nudge, daemon=True).start()
    return jsonify({"success": True, "arrival": _arrival_dict(row),
                    "message": f"Holding {name} — asking flat {flat}"})


def _flat_phones(flat_no: str) -> list:
    out = []
    for f in _all_directory_flats():
        if _flat_matches(f.get("flat_no") or f.get("flat") or "", flat_no):
            ph = f.get("whatsapp") or f.get("phone") or ""
            if ph:
                out.append(ph)
    # union with the vehicle DB — a flat often has two numbers on file
    # (directory = who gets visitor messages, vehicle DB = who owns the car)
    for v in _resident_plates():
        fl = v.get("flat_number") or ""
        bl = v.get("block") or ""
        if _flat_matches(f"{bl}-{fl}" if bl and "-" not in fl else fl, flat_no) and v.get("phone"):
            out.append(v["phone"])
    seen, uniq = set(), []
    for p in out:
        n = _norm_phone(p)
        if n and n not in seen:
            seen.add(n); uniq.append(n)
    return uniq


@resident_app_bp.route("/api/gate/arrivals")
def gate_arrivals():
    con = _con()
    rows = con.execute(
        "SELECT * FROM arrival_requests WHERE created_at >= ? ORDER BY id DESC LIMIT 40",
        ((datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
    con.close()
    return jsonify({"success": True, "arrivals": [_arrival_dict(r) for r in rows]})


@resident_app_bp.route("/api/gate/arrivals/<int:aid>/admit", methods=["POST"])
def gate_arrival_admit(aid):
    con = _con()
    r = con.execute("SELECT * FROM arrival_requests WHERE id=?", (aid,)).fetchone()
    if not r:
        con.close(); return jsonify({"success": False, "message": "not found"}), 404
    if r["admitted_at"]:
        con.close(); return jsonify({"success": True, "message": "already admitted"})
    if r["decision"] == "DENY":
        con.close(); return jsonify({"success": False, "message": "Resident declined entry"}), 400
    vid = None
    try:
        from db import add_visitor
        tag = {"ALLOW": "approved in app", "WAIT": "resident asked to wait",
               None: "no answer in 3 min"}.get(r["decision"], "")
        vid = add_visitor(r["visitor_name"], r["flat_no"], "",
                          f"{r['purpose'] or 'Visitor'} · {tag}".strip(" ·"))
    except Exception as e:
        logger.warning(f"[RESIDENT] add_visitor failed: {e}")
    con.execute("UPDATE arrival_requests SET admitted_at=?, visitor_id=?, "
                "status=CASE WHEN status='PENDING' THEN 'ADMITTED' ELSE status END WHERE id=?",
                (_now_str(), vid, aid))
    con.commit(); con.close()
    return jsonify({"success": True, "visitor_id": vid,
                    "message": f"Logged {r['visitor_name']} → {r['flat_no']}"})


@resident_app_bp.route("/api/resident/arrivals/pending")
@resident_required
def resident_arrivals_pending():
    con = _con()
    rows = con.execute(
        "SELECT * FROM arrival_requests WHERE flat_no=? AND status='PENDING' "
        "AND expires_at >= ? ORDER BY id DESC",
        (request.resident["flat_no"], _now_str())).fetchall()
    con.close()
    return jsonify({"success": True,
                    "arrivals": [_arrival_dict(r, for_resident=True) for r in rows]})


@resident_app_bp.route("/api/resident/arrivals/<int:aid>/decide", methods=["POST"])
@resident_required
def resident_arrival_decide(aid):
    res = request.resident
    data = request.get_json(silent=True) or {}
    decision = (data.get("decision") or "").upper()
    if decision not in ("ALLOW", "WAIT", "DENY"):
        return jsonify({"success": False, "message": "decision must be ALLOW, WAIT or DENY"}), 400
    con = _con()
    r = con.execute("SELECT * FROM arrival_requests WHERE id=? AND flat_no=?",
                    (aid, res["flat_no"])).fetchone()
    if not r:
        con.close(); return jsonify({"success": False, "message": "not found"}), 404
    if r["status"] != "PENDING" or r["expires_at"] < _now_str():
        con.close(); return jsonify({"success": False, "message": "This request has already closed"}), 400
    status = {"ALLOW": "APPROVED", "WAIT": "WAITING", "DENY": "DECLINED"}[decision]
    con.execute("UPDATE arrival_requests SET status=?, decision=?, decided_by=?, decided_at=? "
                "WHERE id=?", (status, decision, res["name"], _now_str(), aid))
    con.commit(); con.close()
    try:
        import api_server as srv
        word = {"ALLOW": "LET IN", "WAIT": "WAIT", "DENY": "DECLINED"}[decision]
        srv.activity_feed.appendleft({"time": datetime.now().isoformat(),
                                      "event": f"RESIDENT {word}: {r['visitor_name']} → {r['flat_no']} ({res['name']})",
                                      "type": "visitor"})
    except Exception:
        pass
    return jsonify({"success": True, "status": status,
                    "message": {"ALLOW": "Guard will let them in",
                                "WAIT": "Guard will ask them to wait",
                                "DENY": "Guard will not admit them"}[decision]})


# ════════════════════════════════════════════════════════════════════
# v1.1 — Household (resident requests, committee approves)
# ════════════════════════════════════════════════════════════════════

HOUSEHOLD_KINDS = ("vehicle", "help", "family")


@resident_app_bp.route("/api/resident/household")
@resident_required
def household_list():
    res = request.resident
    con = _con()
    members = [dict(r) for r in con.execute(
        "SELECT id, kind, name, phone, note, added_at FROM household_members "
        "WHERE flat_no=? ORDER BY kind, name", (res["flat_no"],))]
    pending = [dict(r) for r in con.execute(
        "SELECT id, kind, name, plate, phone, note, status, created_at, decided_at "
        "FROM household_requests WHERE flat_no=? AND status IN ('PENDING','REJECTED') "
        "AND created_at >= ? ORDER BY id DESC",
        (res["flat_no"], (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")))]
    con.close()
    return jsonify({"success": True, "vehicles": res["plates"], "members": members,
                    "requests": pending, "kinds": HOUSEHOLD_KINDS})


@resident_app_bp.route("/api/resident/household", methods=["POST"])
@resident_required
def household_request():
    res = request.resident
    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "").lower()
    name = (data.get("name") or "").strip()[:60]
    plate = _norm_plate(data.get("plate", ""))
    phone = _norm_phone(data.get("phone", "")) if data.get("phone") else ""
    note = (data.get("note") or "").strip()[:120]
    if kind not in HOUSEHOLD_KINDS:
        return jsonify({"success": False, "message": "kind must be vehicle, help or family"}), 400
    if kind == "vehicle" and not plate:
        return jsonify({"success": False, "message": "Vehicle number is required"}), 400
    if kind != "vehicle" and not name:
        return jsonify({"success": False, "message": "Name is required"}), 400
    if kind == "vehicle" and plate in [p["plate"] for p in res["plates"]]:
        return jsonify({"success": False, "message": f"{plate} is already on your flat"}), 400
    con = _con()
    if kind == "vehicle":
        dup = con.execute("SELECT 1 FROM household_requests WHERE flat_no=? AND kind='vehicle' "
                          "AND plate=? AND status='PENDING'", (res["flat_no"], plate)).fetchone()
    else:
        dup = con.execute("SELECT 1 FROM household_requests WHERE flat_no=? AND kind=? "
                          "AND LOWER(name)=LOWER(?) AND status='PENDING'",
                          (res["flat_no"], kind, name)).fetchone()
    if dup:
        con.close(); return jsonify({"success": False, "message": "Already requested — waiting for committee"}), 400
    cur = con.execute(
        "INSERT INTO household_requests (flat_no, resident_phone, resident_name, kind, name, "
        "plate, phone, note, status, created_at) VALUES (?,?,?,?,?,?,?,?,'PENDING',?)",
        (res["flat_no"], res["phone"], res["name"], kind, name or plate, plate, phone, note, _now_str()))
    con.commit(); rid = cur.lastrowid; con.close()
    try:
        import api_server as srv
        srv.activity_feed.appendleft({"time": datetime.now().isoformat(),
                                      "event": f"HOUSEHOLD REQUEST: {res['flat_no']} wants to add {kind} {name or plate}",
                                      "type": "resident"})
    except Exception:
        pass
    return jsonify({"success": True, "id": rid,
                    "message": "Sent to the committee for approval"})


@resident_app_bp.route("/api/resident/household/<int:rid>", methods=["DELETE"])
@resident_required
def household_withdraw(rid):
    con = _con()
    n = con.execute("DELETE FROM household_requests WHERE id=? AND flat_no=? AND status='PENDING'",
                    (rid, request.resident["flat_no"])).rowcount
    con.commit(); con.close()
    return jsonify({"success": bool(n)})


@resident_app_bp.route("/api/gate/household/pending")
def household_pending():
    con = _con()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM household_requests WHERE status='PENDING' ORDER BY id")]
    con.close()
    return jsonify({"success": True, "requests": rows})


@resident_app_bp.route("/api/gate/household/<int:rid>/<action>", methods=["POST"])
def household_decide(rid, action):
    if action not in ("approve", "reject"):
        return jsonify({"success": False, "message": "approve or reject"}), 404
    who = (getattr(request, "auth_user", None) or {}).get("username") or "committee"
    con = _con()
    r = con.execute("SELECT * FROM household_requests WHERE id=? AND status='PENDING'",
                    (rid,)).fetchone()
    if not r:
        con.close(); return jsonify({"success": False, "message": "not pending"}), 404
    if action == "approve":
        if r["kind"] == "vehicle":
            try:
                from resident_db import db as rdb, Resident
                fl, bl = r["flat_no"], ""
                if "-" in fl:
                    bl, fl = fl.split("-", 1)
                rdb.add(Resident(plate_number=r["plate"], resident_name=r["resident_name"],
                                 flat_number=fl, block=bl, phone=r["resident_phone"],
                                 vehicle_model=r["note"] or "", status="KNOWN",
                                 notes="added via resident app"))
            except Exception as e:
                con.close()
                return jsonify({"success": False, "message": f"resident_db error: {e}"}), 500
        else:
            con.execute("INSERT INTO household_members (flat_no, kind, name, phone, note, added_at, added_by) "
                        "VALUES (?,?,?,?,?,?,?)", (r["flat_no"], r["kind"], r["name"], r["phone"],
                                                   r["note"], _now_str(), who))
    con.execute("UPDATE household_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
                ("APPROVED" if action == "approve" else "REJECTED", who, _now_str(), rid))
    con.commit(); con.close()
    def _tell():
        what = r["plate"] if r["kind"] == "vehicle" else f"{r['name']} ({r['kind']})"
        msg = (f"{'✅' if action == 'approve' else '❌'} *Defender Octa* — {_site_name()}\n\n"
               f"Your request to add {what} to flat {r['flat_no']} was "
               f"{'approved' if action == 'approve' else 'not approved'} by the committee.")
        _send_wa(r["resident_phone"], msg)
    threading.Thread(target=_tell, daemon=True).start()
    return jsonify({"success": True, "status": "APPROVED" if action == "approve" else "REJECTED"})


# ════════════════════════════════════════════════════════════════════
# v1.1 — My alerts (prefs) + background loops
# ════════════════════════════════════════════════════════════════════

def _get_prefs(phone: str) -> dict:
    try:
        con = _con()
        r = con.execute("SELECT vehicle_alerts, daily_brief FROM resident_prefs WHERE phone=?",
                        (phone,)).fetchone()
        con.close()
        if r:
            return {"vehicle_alerts": bool(r["vehicle_alerts"]), "daily_brief": bool(r["daily_brief"])}
    except sqlite3.Error:
        pass
    return {"vehicle_alerts": False, "daily_brief": False}


@resident_app_bp.route("/api/resident/prefs")
@resident_required
def prefs_get():
    return jsonify({"success": True, "prefs": _get_prefs(request.resident["phone"])})


@resident_app_bp.route("/api/resident/prefs", methods=["POST"])
@resident_required
def prefs_set():
    data = request.get_json(silent=True) or {}
    cur = _get_prefs(request.resident["phone"])
    va = 1 if data.get("vehicle_alerts", cur["vehicle_alerts"]) else 0
    db_ = 1 if data.get("daily_brief", cur["daily_brief"]) else 0
    con = _con()
    con.execute("INSERT OR REPLACE INTO resident_prefs (phone, vehicle_alerts, daily_brief, updated_at) "
                "VALUES (?,?,?,?)", (request.resident["phone"], va, db_, _now_str()))
    con.commit(); con.close()
    return jsonify({"success": True, "prefs": {"vehicle_alerts": bool(va), "daily_brief": bool(db_)}})


def _state_get(key, default=None):
    try:
        con = _con()
        r = con.execute("SELECT value FROM resident_state WHERE key=?", (key,)).fetchone()
        con.close()
        return r["value"] if r else default
    except sqlite3.Error:
        return default


def _state_set(key, value):
    try:
        con = _con()
        con.execute("INSERT OR REPLACE INTO resident_state (key, value) VALUES (?,?)", (key, str(value)))
        con.commit(); con.close()
    except sqlite3.Error:
        pass


def _opted_in_phones_by_plate() -> dict:
    """plate -> [phones with vehicle_alerts on]"""
    try:
        con = _con()
        phones = {r["phone"] for r in con.execute(
            "SELECT phone FROM resident_prefs WHERE vehicle_alerts=1")}
        con.close()
    except sqlite3.Error:
        return {}
    if not phones:
        return {}
    out = {}
    for v in _resident_plates():
        ph = _norm_phone(v.get("phone", ""))
        if ph in phones:
            out.setdefault(_norm_plate(v.get("plate_number")), []).append(ph)
    return out


_alert_last = {}    # plate -> epoch of last alert (dedupe)


def _vehicle_alert_tick():
    con = _con()
    if not _cols(con, "vehicle_events"):
        con.close(); return
    last_id = int(_state_get("alert_last_event_id", -1))
    if last_id < 0:
        # first run: start from now, never replay history
        row = con.execute("SELECT COALESCE(MAX(id),0) FROM vehicle_events").fetchone()
        con.close(); _state_set("alert_last_event_id", row[0]); return
    rows = con.execute("SELECT id, plate, event, camera, timestamp FROM vehicle_events "
                       "WHERE id > ? ORDER BY id LIMIT 200", (last_id,)).fetchall()
    con.close()
    if not rows:
        return
    targets = _opted_in_phones_by_plate()
    for r in rows:
        last_id = max(last_id, r["id"])
        plate = _norm_plate(r["plate"])
        phones = targets.get(plate)
        if not phones:
            continue
        ev = str(r["event"] or "").upper()
        if time.time() - _alert_last.get(plate + ev, 0) < 60:
            continue
        _alert_last[plate + ev] = time.time()
        verb = "left" if ("EXIT" in ev or "OUT" in ev) else "entered"
        _push_to_phones(phones, {"title": f"🚗 {plate} {verb}",
                                 "body": f"{r['camera'] or 'Gate'} · {_fmt_time(r['timestamp'])}",
                                 "tag": f"veh-{plate}", "url": "/resident"})
        msg = (f"🚗 *Defender Octa* — {_site_name()}\n\n"
               f"{plate} {verb} {r['camera'] or 'the gate'} at {_fmt_time(r['timestamp'])}."
               f"\n_You asked to be told when your vehicle moves — turn this off in the app._")
        for ph in phones:
            _send_wa(ph, msg)
    _state_set("alert_last_event_id", last_id)


def _daily_brief_tick():
    now = datetime.now()
    if now.strftime("%H:%M") != DAILY_BRIEF_TIME:
        return
    today = now.strftime("%Y-%m-%d")
    if _state_get("brief_sent_on") == today:
        return
    _state_set("brief_sent_on", today)
    try:
        con = _con()
        phones = [r["phone"] for r in con.execute(
            "SELECT phone FROM resident_prefs WHERE daily_brief=1")]
        con.close()
    except sqlite3.Error:
        return
    if not phones:
        return
    reps = _reports(1)
    text = _brief_text(reps[0]) if reps else "Quiet night — no incidents reported."
    score = reps[0].get("score") if reps else None
    msg = (f"☀️ *Defender Octa* — {_site_name()} morning brief\n\n{text}"
           + (f"\n\nSecurity score: {score}/100" if score is not None else "")
           + "\n_Daily brief — turn off in the app._")
    for ph in phones:
        _send_wa(ph, msg)


def _arrival_expiry_tick():
    """Holds nobody answered: mark EXPIRED. The guard console shows this and
    falls back to the normal visitor log (with the usual WhatsApp)."""
    try:
        con = _con()
        con.execute("UPDATE arrival_requests SET status='EXPIRED' WHERE status='PENDING' AND expires_at < ?",
                    (_now_str(),))
        con.commit(); con.close()
    except sqlite3.Error:
        pass


def _background_loop():
    time.sleep(20)
    n = 0
    while True:
        try:
            _vehicle_alert_tick()
            _arrival_expiry_tick()
            if n % 3 == 0:            # once a minute
                _daily_brief_tick()
        except Exception as e:
            logger.error(f"[RESIDENT] background error: {e}")
        n += 1
        time.sleep(ALERT_POLL_SECONDS)


# ════════════════════════════════════════════════════════════════════
# v1.1 — PWA manifest + icons for the resident app
# ════════════════════════════════════════════════════════════════════

ICON_192 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAFYUlEQVR42u3dT27TQBiGcXtUiQMgsaUXYM0qEufiAJyrklesuUC7ReoBuoJVIITEf2JPxjPf75GQaIVUx32f73sHxW3f7Yx37z/+6tAsb68v/Z6up99j2D98fpSUBvn5/Xl3QvSlQy/shCgpQ18i+FOhvzQpUDdLv+f3kqHfQ/AFnhTX8pBbhL5E8AUec2S4hwi94GPvMuQUoc8RfsFHThG2lKDfMvjnFy342FqErbdBv1X4BR81boNe+FH7NlgjQRJ+1MRpxo7ZW/P2mX6L8As+Sm6DNZsgCT9q3wZrNkESfkSWIAk/IkuQhB+RJUjCj8gSzNoA3rOP2pib2TRn+p8bBux9C1zL8GwBVB9EqEJpzhoRftQuwSIB/GQGtMa1TCfTH5G3QDL9EXkLJNMfkbdAMv0ReQsk0x+Rt0Ay/RF5CyTTH5G3QHJrEJmk/iByDUrn9QdondOsp2tdCWj1HOAMABAA6LrkAIzIB+F0eijQ/xHlHHDMvAoEZwCAAAABcG8+PX11Ewry4BaUD/3pxz++fHODCBB32h//DREIELri2AoEiBD6w8nfBzIQIMqkP4x8blCRCND6tLcVCCD0ZCCA0C+XQUUiQDW9PqcItgIBmp32ZCCA0KtIBBB6W4EAAXo9GQhg2qtIBBB6W4EABYLgHkxshlZp9oGYkel/EP5l96Xlh3Yegn2DsdFZwQYACAAQACAAQACAAAABAAIABAAIABAAIABAAIAAAAEAAgAEAAgAEADzGNwCAkQPPwkIEH7yk4AA4WsPCQgQvvOTgADhD7wkIEDY8HedH/5FAOEHAYQfBBB+EED4QQDhBwH2E1zhJ0B+LvxanyFz+IeGwj9M3EsC4GJwhwbCbwPYAquCO1Qe/jDT3wbIV1mGSsNvA9gCmwV3qDD8oaa/DXAbh4WBGioJvw1gC2QL5lBB+MNNfxugjAQmPwGa2AK5gloi/CGnvw2wv8Ca/ASobgscg3uoNPxhp78NsJ9tYPIToPotcGuYS4Y/9PS3AcpvApOfAE1ugTnhLh3+8NPfBii3CUx+AoTYAsewn/8pjelvA0wHw2skQLQt0HpAhpn3gAAAAWwB058A4Th4bQSIvAVCnAFMfwJM1YGhxeALPwFW9WavgQAOxA6+BAAIYAuY/gRwHnCtBIiwBbwmAqhCqg8BVCHXRgBVyGsggCqk+hBAFXItBFCFXDMBVCHVhwCqkOpDAFXINRJAFVJ9CAAQwBYw/QlAAuEnAAmEnwBBJNj6t9ILPwGqkmCrbTAs/JogQDMSCH9mHtyC7SX49PT1WpAPgk+AECJckGDVNhB+FaiVSiT8NkD4SiT4BCCC4BNALYIzAEAAgAAAAQACAAQACAAQACAAQAAglwAfPj+6I2iaY8Z/fn/+K8DxAyASb68vfXp7fendCqhAAAGAoAIczwEOwohyAP4jgHMAIh6AVSCoQKcfqEGIVH/+EUANQrT6owJBBTr/hBqEKPXnPwHUIESqP1crkC2ACNP/ogC2AKJM/9FDsC2A1qf/VQHOTSEBag3/VLMZ/W9QzwmgdqYynKb6kiqE2qvP2Lk2LTk0kAAthX9SAFUILfb+RQKoQmit9y/eACRAa9VnkQAkQIvhXyQACdBa+Luu625628O79x9/XfriQIkD763hv1mASxIQAbWFf3EFGqtDKhFqC/+qDWAToHTw14Z/EwFOJSACapj6mwswtg2IgC2CnyP8mwswtg2IgLXB3zr8WQQgArYIfe7gZxdgjgiEEPqpPOR+RPduz//OEYEQ8cJ+7Xt+r2fT7/4A/KkIc24M2qVU6IsKMCYDKeKEvWTodyPAEilQP3v8kTu/AT3uGha+P8l6AAAAAElFTkSuQmCC"
ICON_512 = "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"


@resident_app_bp.route("/api/resident/manifest.webmanifest")
def manifest():
    m = {
        "name": f"Defender Octa — {_site_name()}",
        "short_name": "Octa Resident",
        "description": "Your society's gate, in your pocket.",
        "start_url": "/resident",
        "scope": "/resident",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#04070f",
        "theme_color": "#04070f",
        "icons": [
            {"src": "/api/resident/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/api/resident/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(json.dumps(m), mimetype="application/manifest+json")


@resident_app_bp.route("/api/resident/icon-192.png")
def icon_192():
    return Response(base64.b64decode(ICON_192), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


@resident_app_bp.route("/api/resident/icon-512.png")
def icon_512():
    return Response(base64.b64decode(ICON_512), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=604800"})


# ════════════════════════════════════════════════════════════════════
# v1.2 — Instant notifications (Web Push) + service worker + Play Store
# ════════════════════════════════════════════════════════════════════
# Web Push rings the phone even when the app is closed — the piece that
# makes the installed PWA feel native. Uses pywebpush (add `pywebpush`
# to requirements.txt). If the library is missing, every push route
# degrades gracefully and the app quietly falls back to polling +
# WhatsApp nudges, so deploying before the rebuild is safe.

def _push_available():
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:
        return False


def _vapid_keys():
    """Load or create the site's VAPID keypair (data/vapid_private.pem)."""
    priv_path = os.path.join(_data_dir(), "vapid_private.pem")
    from py_vapid import Vapid02, b64urlencode
    if os.path.exists(priv_path):
        v = Vapid02.from_file(priv_path)
    else:
        v = Vapid02()
        v.generate_keys()
        v.save_key(priv_path)
    raw = v.public_key.public_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.X962,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.UncompressedPoint)
    return priv_path, b64urlencode(raw)


@resident_app_bp.route("/api/resident/push/key")
def push_key():
    if not _push_available():
        return jsonify({"success": False, "supported": False,
                        "message": "Push not enabled on this server yet"}), 200
    try:
        _, pub = _vapid_keys()
        return jsonify({"success": True, "supported": True, "key": pub})
    except Exception as e:
        logger.error(f"[RESIDENT] vapid: {e}")
        return jsonify({"success": False, "supported": False, "message": str(e)[:120]}), 200


@resident_app_bp.route("/api/resident/push/subscribe", methods=["POST"])
@resident_required
def push_subscribe():
    sub = (request.get_json(silent=True) or {}).get("subscription")
    if not sub or "endpoint" not in sub:
        return jsonify({"success": False, "message": "subscription required"}), 400
    con = _con()
    con.execute("CREATE TABLE IF NOT EXISTS resident_push (endpoint TEXT PRIMARY KEY, "
                "phone TEXT, flat_no TEXT, subscription TEXT, created_at TEXT)")
    con.execute("INSERT OR REPLACE INTO resident_push (endpoint, phone, flat_no, subscription, created_at) "
                "VALUES (?,?,?,?,?)",
                (sub["endpoint"], request.resident["phone"], request.resident["flat_no"],
                 json.dumps(sub), _now_str()))
    con.commit(); con.close()
    return jsonify({"success": True})


@resident_app_bp.route("/api/resident/push/unsubscribe", methods=["POST"])
@resident_required
def push_unsubscribe():
    ep = (request.get_json(silent=True) or {}).get("endpoint", "")
    con = _con()
    try:
        con.execute("DELETE FROM resident_push WHERE endpoint=? AND phone=?",
                    (ep, request.resident["phone"]))
        con.commit()
    except sqlite3.Error:
        pass
    con.close()
    return jsonify({"success": True})


def _push_to_phones(phones: list, payload: dict):
    """Fire-and-forget web push to every subscription of these phones.
    Dead subscriptions (404/410) are pruned. Never raises."""
    if not _push_available() or not phones:
        return
    phones = [p if _is_pin_id(p) else _norm_phone(p) for p in phones]
    phones = [p for p in phones if p]

    def _run():
        try:
            from pywebpush import webpush, WebPushException
            priv_path, _ = _vapid_keys()
            con = _con()
            try:
                rows = con.execute(
                    "SELECT endpoint, subscription FROM resident_push WHERE phone IN (%s)"
                    % ",".join("?" * len(phones)), phones).fetchall()
            except sqlite3.Error:
                rows = []
            data = json.dumps(payload)
            for r in rows:
                try:
                    webpush(subscription_info=json.loads(r["subscription"]), data=data,
                            vapid_private_key=priv_path,
                            vapid_claims={"sub": "mailto:alerts@snguardiangrid.com"},
                            ttl=180)
                except WebPushException as e:
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    if code in (404, 410):
                        con.execute("DELETE FROM resident_push WHERE endpoint=?", (r["endpoint"],))
                        con.commit()
                    else:
                        logger.debug(f"[PUSH] {code}: {e}")
                except Exception as e:
                    logger.debug(f"[PUSH] send: {e}")
            con.close()
        except Exception as e:
            logger.debug(f"[PUSH] loop: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ── Service worker (root-scoped so it controls /resident) ────────
_SW_JS = r"""
/* Defender Octa Resident — service worker (push + notification click) */
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) {}
  const title = d.title || 'Defender Octa';
  const opts = {
    body: d.body || '',
    icon: '/api/resident/icon-192.png',
    badge: '/api/resident/icon-192.png',
    tag: d.tag || 'octa',
    renotify: !!d.urgent,
    vibrate: d.urgent ? [200, 80, 200, 80, 200] : [120],
    data: { url: d.url || '/resident' },
    requireInteraction: !!d.urgent,
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/resident';
  event.waitUntil((async () => {
    const all = await clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if (c.url.includes('/resident')) { c.focus(); c.navigate(url); return; }
    }
    await clients.openWindow(url);
  })());
});
"""


@resident_app_bp.route("/resident-sw.js")
def resident_sw():
    return Response(_SW_JS, mimetype="application/javascript",
                    headers={"Cache-Control": "no-cache"})


# ── Play Store (TWA) trust file ──────────────────────────────────
# Android checks https://<site>/.well-known/assetlinks.json to open the
# wrapped app full-screen without browser chrome. Put the SHA-256 of the
# Play signing key in data/assetlinks_fingerprint.txt (one line) or the
# OCTA_TWA_FINGERPRINT env var; until then this serves an empty list,
# which is harmless.

@resident_app_bp.route("/.well-known/assetlinks.json")
def assetlinks():
    fp = os.environ.get("OCTA_TWA_FINGERPRINT", "").strip()
    if not fp:
        try:
            with open(os.path.join(_data_dir(), "assetlinks_fingerprint.txt")) as f:
                fp = f.read().strip()
        except OSError:
            fp = ""
    body = []
    if fp:
        body = [{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {"namespace": "android_app",
                       "package_name": os.environ.get("OCTA_TWA_PACKAGE", "in.defenderocta.resident"),
                       "sha256_cert_fingerprints": [fp]},
        }]
    return Response(json.dumps(body), mimetype="application/json")


# ════════════════════════════════════════════════════════════════════
# Flat + PIN login (privacy option: no phone number stored)
# ════════════════════════════════════════════════════════════════════

PIN_MAX_ATTEMPTS = 5
PIN_LOCK_SECONDS = 10 * 60


def _canonical_flat(entered: str) -> str:
    """Match whatever the resident typed ("b302", "B 302", "302-B") to the
    flat as stored in the directory / PIN table. Falls back to the
    normalised input."""
    n = _norm_flat(entered)
    if not n:
        return ""
    candidates = [_norm_flat(f.get("flat_no") or f.get("flat") or "")
                  for f in _all_directory_flats()]
    try:
        con = _con()
        candidates += [r[0] for r in con.execute("SELECT flat_no FROM flat_pins")]
        con.close()
    except sqlite3.Error:
        pass
    for c in candidates:
        if c and _flat_matches(c, n):
            return c
    return n


def _pin_hash(flat_no: str, pin: str) -> str:
    return hmac.new(_SECRET, f"pin:{_norm_flat(flat_no)}:{pin}".encode(),
                    hashlib.sha256).hexdigest()


@resident_app_bp.route("/api/resident/pin/login", methods=["POST"])
def pin_login():
    data = request.get_json(silent=True) or {}
    flat = _canonical_flat(data.get("flat", ""))
    pin = _digits(data.get("pin", ""))
    if not flat or len(pin) != 6:
        return jsonify({"success": False, "message": "Enter your flat and the 6-digit PIN"}), 400
    con = _con()
    row = con.execute("SELECT * FROM flat_pins WHERE flat_no=?", (flat,)).fetchone()
    if not row:
        con.close()
        return jsonify({"success": False,
                        "message": "No PIN is set for this flat — ask your committee for your PIN slip"}), 404
    if row["locked_until"] and row["locked_until"] > time.time():
        con.close()
        mins = int((row["locked_until"] - time.time()) / 60) + 1
        return jsonify({"success": False,
                        "message": f"Too many wrong tries — locked for {mins} more minute{'s' if mins > 1 else ''}"}), 429
    if not hmac.compare_digest(row["pin_hash"], _pin_hash(flat, pin)):
        att = (row["attempts"] or 0) + 1
        lock = time.time() + PIN_LOCK_SECONDS if att >= PIN_MAX_ATTEMPTS else 0
        con.execute("UPDATE flat_pins SET attempts=?, locked_until=? WHERE flat_no=?",
                    (0 if lock else att, lock, flat))
        con.commit(); con.close()
        return jsonify({"success": False, "message": "That PIN isn't right"}), 401
    con.execute("UPDATE flat_pins SET attempts=0, locked_until=0, last_login=? WHERE flat_no=?",
                (_now_str(), flat))
    con.commit(); con.close()
    res = _resolve_resident_by_flat(flat)
    try:
        con = _con()
        con.execute("INSERT INTO resident_logins (phone, flat_no, first_login, last_login, logins) "
                    "VALUES (?,?,?,?,1) ON CONFLICT(phone) DO UPDATE SET last_login=excluded.last_login, "
                    "logins=logins+1", (res["phone"], flat, _now_str(), _now_str()))
        con.commit(); con.close()
    except sqlite3.Error:
        pass
    return jsonify({"success": True, "token": _make_token(res), "resident": res,
                    "site": _site_name()})


# ── Admin: generate / reset / status (dashboard JWT via global guard) ──

def _gen_pin() -> str:
    return f"{secrets.randbelow(10**6):06d}"


@resident_app_bp.route("/api/admin/flats/pins")
def pins_status():
    con = _con()
    have = {r["flat_no"]: r for r in con.execute("SELECT * FROM flat_pins")}
    con.close()
    flats = sorted({_norm_flat(f.get("flat_no") or f.get("flat") or "")
                    for f in _all_directory_flats()} - {""})
    out = [{"flat_no": f, "has_pin": f in have,
            "last_login": have[f]["last_login"] if f in have else None} for f in flats]
    for f, r in have.items():          # PINs for flats not in the directory yet
        if f not in {x["flat_no"] for x in out}:
            out.append({"flat_no": f, "has_pin": True, "last_login": r["last_login"]})
    return jsonify({"success": True, "flats": out,
                    "with_pin": sum(1 for x in out if x["has_pin"]),
                    "total": len(out)})


@resident_app_bp.route("/api/admin/flats/pins/generate", methods=["POST"])
def pins_generate():
    """Generate PINs. Returns the plain PINs ONCE — they are stored only as
    hashes, so this response is the moment to print the slips."""
    data = request.get_json(silent=True) or {}
    only_missing = data.get("only_missing", True)
    wanted = [_norm_flat(f) for f in (data.get("flats") or [])]
    who = (getattr(request, "auth_user", None) or {}).get("username") or "committee"
    flats = wanted or sorted({_norm_flat(f.get("flat_no") or f.get("flat") or "")
                              for f in _all_directory_flats()} - {""})
    if not flats:
        return jsonify({"success": False,
                        "message": "No flats on file yet — import residents first, or pass a flats list"}), 400
    con = _con()
    have = {r[0] for r in con.execute("SELECT flat_no FROM flat_pins")}
    made = []
    for f in flats:
        if only_missing and f in have and f not in wanted:
            continue
        pin = _gen_pin()
        con.execute("INSERT OR REPLACE INTO flat_pins (flat_no, pin_hash, created_at, created_by, "
                    "attempts, locked_until, last_login) VALUES (?,?,?,?,0,0,"
                    "(SELECT last_login FROM flat_pins WHERE flat_no=?))",
                    (f, _pin_hash(f, pin), _now_str(), who, f))
        made.append({"flat_no": f, "pin": pin})
    con.commit(); con.close()
    return jsonify({"success": True, "generated": len(made), "pins": made,
                    "site": _site_name(),
                    "note": "PINs are shown only once — print the slips now. "
                            "Generating again replaces a flat's PIN."})


@resident_app_bp.route("/api/admin/flats/pins/reset", methods=["POST"])
def pins_reset():
    flat = _norm_flat((request.get_json(silent=True) or {}).get("flat", ""))
    if not flat:
        return jsonify({"success": False, "message": "flat required"}), 400
    who = (getattr(request, "auth_user", None) or {}).get("username") or "committee"
    pin = _gen_pin()
    con = _con()
    con.execute("INSERT OR REPLACE INTO flat_pins (flat_no, pin_hash, created_at, created_by, "
                "attempts, locked_until, last_login) VALUES (?,?,?,?,0,0,NULL)",
                (flat, _pin_hash(flat, pin), _now_str(), who))
    con.commit(); con.close()
    return jsonify({"success": True, "flat_no": flat, "pin": pin,
                    "note": "Shown once — hand it to the resident."})
