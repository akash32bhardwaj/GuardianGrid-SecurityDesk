r"""
resident_app.py — DEFENDER OCTA Resident App (v1 backend)
-----------------------------------------------------------
The flat-owner's side of Defender Octa. One blueprint, same server,
works per-site automatically.

WHO CALLS WHAT
  Resident phone (PWA at /resident)  ->  /api/resident/*   (resident token)
  Guard console (existing dashboard)  ->  /api/gate/pass/* (guard JWT)
                                          /api/gate/notices (guard JWT)

RESIDENT LOGIN  (phone number + WhatsApp OTP)
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

GUARD / ADMIN ROUTES  (normal dashboard JWT — the global guard applies)
  GET  /api/gate/pass/<code>           look a pass up at the gate
  POST /api/gate/pass/<code>/use       admit -> logs visitor + notifies flat
  GET  /api/gate/notices               committee notices (admin sees all)
  POST /api/gate/notices               {title, body, days?}
  DELETE /api/gate/notices/<id>

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

from flask import Blueprint, jsonify, request, send_from_directory

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

def init_resident_app(base_dir: str):
    global _BASE_DIR, _DB_PATH, _SECRET
    _BASE_DIR = base_dir
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")
    _SECRET = _load_or_create_secret()
    _ensure_tables()
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
    (the path every other Octa alert uses), then the Twilio client."""
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
    return jsonify({
        "success": True,
        "site": _site_name(),
        "resident": res,
        "protection": _protection_state(score),
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
    if fn not in allowed:
        return jsonify({"success": False, "message": "Not your photo"}), 403
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
