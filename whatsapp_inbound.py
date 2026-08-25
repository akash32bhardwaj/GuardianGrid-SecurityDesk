r"""
whatsapp_inbound.py — DEFENDER OCTA "Clip-on-Demand" (Wow Priority 4)
----------------------------------------------------------------------
Makes every outgoing WhatsApp alert interactive. The client replies:

    "show" / "dikhao" / "video"  ->  15-sec clip of their last alert
    "photo" / "pic"              ->  snapshot image of their last alert
    "status" / "kya hua"         ->  today's site summary (search engine)
    anything else                ->  help message

Routes (both must be AUTH-EXEMPT — Twilio cannot log in):
    POST /api/whatsapp/inbound          Twilio webhook (signature-verified)
    GET  /api/whatsapp/media/<token>    tokenized media fetch (1h expiry)

Integration in api_server.py:
    from whatsapp_inbound import whatsapp_bp, init_whatsapp_inbound
    init_whatsapp_inbound(base_dir=BASE_DIR)
    app.register_blueprint(whatsapp_bp)
  ...and add "/api/whatsapp/" to AUTH_EXEMPT_PREFIXES.

whatsapp_alerts.py calls record_alert_context() after each successful
security send, so "show" knows which event the person means.

Config (whatsapp_config.py — add one line):
    PUBLIC_BASE_URL = "https://agi.snguardiangrid.com"   # no trailing slash
Falls back to request host if missing (works behind Cloudflare tunnel).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta

from flask import Blueprint, request, Response, send_file, abort

logger = logging.getLogger(__name__)
whatsapp_bp = Blueprint("whatsapp_inbound", __name__)

_BASE_DIR = "."
_DB_PATH = "guardiangrid.db"
_RECORDINGS = "recordings"
_CLIP_CACHE = "clips_cache"
_CLIP_SECONDS = 15
_TOKEN_TTL = 3600          # media links valid for 1 hour
_SEGMENT_SECONDS = 600     # must match segment_recorder.py

try:
    import whatsapp_config as cfg
    _CFG = True
except ImportError:
    _CFG = False
    logger.warning("whatsapp_config.py not found — inbound WhatsApp disabled")


def init_whatsapp_inbound(base_dir: str):
    global _BASE_DIR, _DB_PATH, _RECORDINGS, _CLIP_CACHE
    _BASE_DIR = base_dir
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")
    _RECORDINGS = os.path.join(base_dir, "recordings")
    _CLIP_CACHE = os.path.join(base_dir, "clips_cache")
    os.makedirs(_CLIP_CACHE, exist_ok=True)
    _ensure_table()


# ════════════════════════════════════════════════════════════════════
# Context memory — which alert was last sent to which number
# ════════════════════════════════════════════════════════════════════

def _ensure_table():
    try:
        con = sqlite3.connect(_DB_PATH)
        con.execute(
            "CREATE TABLE IF NOT EXISTS wa_context ("
            " phone TEXT PRIMARY KEY,"
            " plate TEXT, camera TEXT, event_ts TEXT,"
            " snapshot TEXT, updated_at TEXT)"
        )
        con.commit()
        con.close()
    except sqlite3.Error as e:
        logger.error(f"wa_context table init failed: {e}")


def record_alert_context(phone: str, plate: str = "", camera: str = "",
                         event_ts: str = "", snapshot: str = ""):
    """Called by whatsapp_alerts.py right after a successful send.
    phone: 'whatsapp:+91XXXXXXXXXX' (stored as-is)."""
    if not phone:
        return
    try:
        con = sqlite3.connect(_DB_PATH)
        con.execute(
            "INSERT INTO wa_context (phone, plate, camera, event_ts,"
            " snapshot, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(phone) DO UPDATE SET plate=excluded.plate,"
            " camera=excluded.camera, event_ts=excluded.event_ts,"
            " snapshot=excluded.snapshot, updated_at=excluded.updated_at",
            (phone, plate, camera,
             event_ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             snapshot or "",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        con.commit()
        con.close()
    except sqlite3.Error as e:
        logger.error(f"record_alert_context failed: {e}")


def _get_context(phone: str):
    try:
        con = sqlite3.connect(_DB_PATH)
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT * FROM wa_context WHERE phone = ?",
                        (phone,)).fetchone()
        con.close()
        return dict(r) if r else None
    except sqlite3.Error:
        return None


# ════════════════════════════════════════════════════════════════════
# Signed media tokens — public links without exposing the filesystem
# ════════════════════════════════════════════════════════════════════

def _secret() -> bytes:
    # Prefer the app's JWT secret; fall back to the Twilio auth token.
    try:
        from config import SECRET_KEY
        return str(SECRET_KEY).encode()
    except Exception:
        pass
    if _CFG:
        return str(getattr(cfg, "TWILIO_AUTH_TOKEN", "octa")).encode()
    return b"octa-fallback"


def make_media_token(file_path: str) -> str:
    """Signed token embedding the ABSOLUTE file path + expiry."""
    exp = int(time.time()) + _TOKEN_TTL
    payload = f"{file_path}|{exp}".encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + b"." + sig).decode()


def read_media_token(token: str):
    """Return file_path if token is valid and unexpired, else None."""
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        payload, sig = raw.rsplit(b".", 1)
        good = hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(sig, good):
            return None
        path, exp = payload.decode().rsplit("|", 1)
        if int(exp) < time.time():
            return None
        return path
    except Exception:
        return None


def _public_base(req) -> str:
    if _CFG and getattr(cfg, "PUBLIC_BASE_URL", ""):
        return cfg.PUBLIC_BASE_URL.rstrip("/")
    # Behind Cloudflare tunnel the original scheme is https
    proto = req.headers.get("X-Forwarded-Proto", req.scheme)
    return f"{proto}://{req.host}"


# ════════════════════════════════════════════════════════════════════
# Clip extraction from segment_recorder's folder convention
#   recordings/<Camera Name>/<YYYY-MM-DD>/seg_HH-MM-SS.mp4
# ════════════════════════════════════════════════════════════════════

def _parse_seg_start(day: str, fname: str):
    m = re.match(r"seg_(\d{2})-(\d{2})-(\d{2})\.mp4$", fname)
    if not m:
        return None
    try:
        return datetime.strptime(f"{day} {m.group(1)}:{m.group(2)}:{m.group(3)}",
                                 "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def find_clip_for_event(camera: str, event_ts: str):
    """Cut a ~15s clip around event_ts from the camera's segments.
    Returns clip path or None (no recordings / ffmpeg missing / too old)."""
    if not camera or not event_ts:
        return None
    try:
        ts = datetime.strptime(event_ts[:19].replace("T", " "),
                               "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    # camera folder: exact, or safe_name-style relaxed match
    cam_dir = os.path.join(_RECORDINGS, camera)
    if not os.path.isdir(cam_dir):
        if not os.path.isdir(_RECORDINGS):
            return None
        low = camera.lower().replace(" ", "")
        for d in os.listdir(_RECORDINGS):
            if d.lower().replace(" ", "") == low:
                cam_dir = os.path.join(_RECORDINGS, d)
                break
        else:
            return None

    day = ts.strftime("%Y-%m-%d")
    day_dir = os.path.join(cam_dir, day)
    if not os.path.isdir(day_dir):
        return None

    best, best_start = None, None
    for f in os.listdir(day_dir):
        start = _parse_seg_start(day, f)
        if start and start <= ts < start + timedelta(seconds=_SEGMENT_SECONDS):
            if best_start is None or start > best_start:
                best, best_start = os.path.join(day_dir, f), start
    if not best:
        return None

    offset = max(0, (ts - best_start).total_seconds() - 7)  # 7s pre-roll
    out = os.path.join(
        _CLIP_CACHE,
        f"clip_{re.sub(r'[^A-Za-z0-9]', '', camera)}_"
        f"{ts.strftime('%Y%m%d_%H%M%S')}.mp4")
    if os.path.exists(out):
        return out
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-ss", str(offset), "-i", best,
             "-t", str(_CLIP_SECONDS), "-c", "copy", "-y", out],
            capture_output=True, timeout=30)
        if r.returncode == 0 and os.path.getsize(out) > 1000:
            return out
        if os.path.exists(out):
            os.remove(out)
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        logger.warning(f"clip cut failed: {e}")
    return None


def _find_snapshot(ctx):
    """Best snapshot for the context: stored path, else vehicle_events image."""
    snap = (ctx or {}).get("snapshot") or ""
    if snap and os.path.exists(snap):
        return snap
    if snap:
        p = os.path.join(_BASE_DIR, snap)
        if os.path.exists(p):
            return p
    plate = (ctx or {}).get("plate")
    if not plate:
        return None
    try:
        con = sqlite3.connect(_DB_PATH)
        r = con.execute(
            "SELECT image FROM vehicle_events WHERE plate = ? "
            "AND image IS NOT NULL AND image != '' "
            "ORDER BY REPLACE(timestamp,'T',' ') DESC LIMIT 1",
            (plate,)).fetchone()
        con.close()
        if r and r[0]:
            for cand in (r[0], os.path.join(_BASE_DIR, r[0]),
                         os.path.join(_BASE_DIR, "vehicle_snapshots",
                                      os.path.basename(r[0]))):
                if os.path.exists(cand):
                    return cand
    except sqlite3.Error:
        pass
    return None


def _today_status_line():
    """Reuse the search engine's answer style for a 'status' reply."""
    try:
        con = sqlite3.connect(_DB_PATH)
        today = datetime.now().strftime("%Y-%m-%d")
        v = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN UPPER(COALESCE(access,state))"
            " LIKE '%UNKNOWN%' THEN 1 ELSE 0 END) FROM vehicle_events "
            "WHERE REPLACE(timestamp,'T',' ') >= ?",
            (f"{today} 00:00:00",)).fetchone()
        i = con.execute(
            "SELECT COUNT(*) FROM incidents "
            "WHERE REPLACE(created_at,'T',' ') >= ?",
            (f"{today} 00:00:00",)).fetchone()
        last = con.execute(
            "SELECT plate, camera, REPLACE(timestamp,'T',' ') "
            "FROM vehicle_events ORDER BY REPLACE(timestamp,'T',' ') DESC "
            "LIMIT 1").fetchone()
        con.close()
        total, unk = v[0] or 0, v[1] or 0
        line = (f"📊 *Today so far*\n"
                f"🚗 Vehicle events: {total} ({unk} unknown)\n"
                f"🚨 Incidents: {i[0] or 0}")
        if last:
            line += f"\n🕐 Last movement: {last[0]} at {last[1]} — {last[2][:16]}"
        return line
    except sqlite3.Error:
        return "Status unavailable right now."


# ════════════════════════════════════════════════════════════════════
# Twilio signature verification
# ════════════════════════════════════════════════════════════════════

def _verify_twilio(req) -> bool:
    if not _CFG:
        return False
    token = getattr(cfg, "TWILIO_AUTH_TOKEN", "")
    sig = req.headers.get("X-Twilio-Signature", "")
    if not token or not sig:
        return False
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(token)
        url = _public_base(req) + req.path
        return validator.validate(url, req.form.to_dict(), sig)
    except ImportError:
        logger.warning("twilio package missing — signature NOT verified")
        return True   # don't brick alerts if lib is absent; log loudly
    except Exception as e:
        logger.error(f"signature check error: {e}")
        return False


def _twiml(body: str, media_url: str = None) -> Response:
    """Minimal TwiML so we don't depend on the twilio lib to reply."""
    from xml.sax.saxutils import escape
    media = f"<Media>{escape(media_url)}</Media>" if media_url else ""
    xml = (f"<?xml version='1.0' encoding='UTF-8'?><Response>"
           f"<Message><Body>{escape(body)}</Body>{media}</Message></Response>")
    return Response(xml, mimetype="application/xml")


# ════════════════════════════════════════════════════════════════════
# Routes
# ════════════════════════════════════════════════════════════════════

_HELP = ("🤖 *DEFENDER OCTA*\n"
         "Reply with:\n"
         "▶️ *show* — video clip of the last alert\n"
         "📷 *photo* — snapshot of the last alert\n"
         "📊 *status* — today's site summary")

_CMD_CLIP = re.compile(r"\b(show|video|clip|dikhao|dikha)\b", re.I)
_CMD_PHOTO = re.compile(r"\b(photo|pic|image|snapshot|tasveer)\b", re.I)
_CMD_STATUS = re.compile(r"\b(status|summary|kya\s*hua|report)\b", re.I)


@whatsapp_bp.route("/api/whatsapp/inbound", methods=["POST"])
def whatsapp_inbound():
    if not _verify_twilio(request):
        abort(403)

    frm = request.form.get("From", "")            # 'whatsapp:+91...'
    body = (request.form.get("Body", "") or "").strip()
    logger.info(f"[WA-IN] {frm}: {body[:80]}")

    if _CMD_STATUS.search(body):
        return _twiml(_today_status_line())

    ctx = _get_context(frm)
    if not ctx:
        return _twiml("No recent alert found for this number.\n\n" + _HELP)

    label = (f"{ctx.get('plate') or 'event'} at "
             f"{ctx.get('camera') or 'site'} — "
             f"{(ctx.get('event_ts') or '')[:16]}")

    if _CMD_CLIP.search(body):
        clip = find_clip_for_event(ctx.get("camera"), ctx.get("event_ts"))
        if clip:
            url = (_public_base(request) +
                   "/api/whatsapp/media/" + make_media_token(clip))
            return _twiml(f"▶️ Clip: {label}", url)
        snap = _find_snapshot(ctx)
        if snap:
            url = (_public_base(request) +
                   "/api/whatsapp/media/" + make_media_token(snap))
            return _twiml(f"⚠️ Clip not available — snapshot instead.\n"
                          f"📷 {label}", url)
        return _twiml(f"⚠️ No clip or snapshot available for {label}.")

    if _CMD_PHOTO.search(body):
        snap = _find_snapshot(ctx)
        if snap:
            url = (_public_base(request) +
                   "/api/whatsapp/media/" + make_media_token(snap))
            return _twiml(f"📷 {label}", url)
        return _twiml(f"⚠️ No snapshot available for {label}.")

    return _twiml(_HELP)


@whatsapp_bp.route("/api/whatsapp/media/<token>")
def whatsapp_media(token):
    path = read_media_token(token)
    if not path or not os.path.exists(path):
        abort(404)
    # Serve only from expected locations — defense in depth
    allowed = (os.path.realpath(_CLIP_CACHE), os.path.realpath(_RECORDINGS),
               os.path.realpath(_BASE_DIR))
    real = os.path.realpath(path)
    if not any(real.startswith(a) for a in allowed):
        abort(403)
    mime = "video/mp4" if real.endswith(".mp4") else "image/jpeg"
    return send_file(real, mimetype=mime)
