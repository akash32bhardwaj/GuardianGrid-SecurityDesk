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
    PUBLIC_BASE_URL = ""    # leave empty — see below
Leave it EMPTY and media links are built from the request's own host, which
is what a per-site deployment wants: each society's links carry its own
hostname. Set it only to force one. It used to default to a former client's
host, so every site's links pointed there (OCT-99).
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
_SIG_BYTES = 16            # truncated HMAC length; a FIXED width
_MEDIA_SECRET = b""        # filled by _secret(), never a credential
_MEDIA_SECRET_SOURCE = ""  # "env" | "file" — reported at boot

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
    check_media_wiring()


def check_media_wiring():
    """Say at BOOT what the two silent failures would be.

    Both of the things this reports used to be discoverable only by their
    symptoms: a signing key that did not exist produced links nobody could
    open, and a missing twilio library produced an inbound webhook that
    accepted anything. Neither announced itself."""
    try:
        import twilio.request_validator  # noqa: F401
        logger.info("[WA-INBOUND] twilio present — webhook signatures verified")
    except ImportError:
        logger.error("[WA-INBOUND] twilio package MISSING. Inbound WhatsApp "
                     "requests cannot be verified and will all be REJECTED. "
                     "twilio is pinned in requirements.txt; this image did "
                     "not get it.")
    try:
        fp = media_key_fingerprint()
        logger.info("[WA-MEDIA] signing key ready (%s, source=%s)",
                    fp, _MEDIA_SECRET_SOURCE)
    except Exception as e:
        logger.error("[WA-MEDIA] no signing key (%s). Media links will not "
                     "be issued; alerts still go out as text.", e)


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

class MediaTokenUnavailable(RuntimeError):
    """No signing key, so no link can be signed. Raised rather than
    returned, because a None here used to be formatted straight into a
    URL and sent to a resident as the word 'None'."""


def _media_key_path() -> str:
    """Beside the database, which is the one directory that is persistent
    on every deployment -- /data in the container, the app dir otherwise."""
    d = os.path.dirname(os.path.abspath(_DB_PATH)) or "."
    return os.path.join(d, "media_token.key")


def _secret() -> bytes:
    """The key media links are signed with.

    This used to read, in order: config.SECRET_KEY, then the TWILIO AUTH
    TOKEN, then the literal b"octa-fallback".

    config.SECRET_KEY does not exist. It is not defined anywhere in this
    codebase -- that import was the only occurrence of the name, so it
    raised ImportError on every single call and the "preferred" branch was
    dead code from the day it was written. Every media link ever issued
    was therefore signed with the Twilio auth token: a CREDENTIAL, used as
    a signing key, shared with a third party, and rotated on Twilio's
    schedule rather than ours. Rotating the Twilio token silently
    invalidated every outstanding media link; leaking it -- and it has
    been pasted into a chat window on this project -- let anyone mint one.

    Now: a dedicated key, generated on first use, stored beside the
    database, used for nothing else. If it cannot be read or written we
    REFUSE to sign. A media link that cannot be trusted is worth less than
    no media link, and the alert still goes out as text."""
    global _MEDIA_SECRET, _MEDIA_SECRET_SOURCE
    if _MEDIA_SECRET:
        return _MEDIA_SECRET

    env = os.environ.get("OCTA_MEDIA_SECRET", "").strip()
    if env:
        if len(env) < 32:
            raise MediaTokenUnavailable(
                "OCTA_MEDIA_SECRET is %d characters; at least 32 are "
                "required. Refusing to sign media links with a weak key."
                % len(env))
        _MEDIA_SECRET, _MEDIA_SECRET_SOURCE = env.encode(), "env"
        return _MEDIA_SECRET

    path = _media_key_path()
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                k = f.read().strip()
            if len(k) >= 32:
                _MEDIA_SECRET, _MEDIA_SECRET_SOURCE = k, "file"
                return _MEDIA_SECRET
            logger.error("media key at %s is %d bytes, expected >= 32. "
                         "Not overwriting it; fix or delete it.", path, len(k))
            raise MediaTokenUnavailable("media key on disk is too short")
        k = secrets.token_urlsafe(48).encode()
        with open(path, "wb") as f:
            f.write(k)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass          # Windows and some mounts; the key is still written
        _MEDIA_SECRET, _MEDIA_SECRET_SOURCE = k, "file"
        logger.info("[WA-MEDIA] generated a new media signing key at %s", path)
        return _MEDIA_SECRET
    except MediaTokenUnavailable:
        raise
    except OSError as e:
        raise MediaTokenUnavailable("cannot read or create %s: %s" % (path, e))


def media_key_fingerprint() -> str:
    """For boot logging. Never the key itself."""
    try:
        return hashlib.sha256(_secret()).hexdigest()[:16]
    except MediaTokenUnavailable:
        return "unavailable"


# The signature is appended with NO delimiter and split off by LENGTH.
#
# It used to be joined with b"." and recovered with rsplit(b".", 1). The
# signature is 16 RAW bytes, and 0x2E is a perfectly ordinary byte for an
# HMAC to produce: P(at least one in 16) = 1-(255/256)^16 = 6.07%. When it
# happened, rsplit cut INSIDE the signature, the payload grew a few bytes,
# the signature lost them, and verification failed. About one media link
# in sixteen had never worked, which reads as "sometimes the video doesn't
# open" and is almost impossible to chase from the symptom.
#
# Measured on this code before the change: 1231 failures in 20000 round
# trips (6.16%), and the failures were the same 1231 tokens whose
# signature contained 0x2E -- a 1:1 match, not a correlation.
#
# A fixed-width field cannot have this bug: there is no byte whose value
# changes where the boundary is.

def make_media_token(file_path: str) -> str:
    """Signed token embedding the ABSOLUTE file path + expiry.

    Raises MediaTokenUnavailable when there is no signing key."""
    exp = int(time.time()) + _TOKEN_TTL
    payload = f"{file_path}|{exp}".encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()[:_SIG_BYTES]
    return base64.urlsafe_b64encode(payload + sig).decode()


def read_media_token(token: str):
    """Return file_path if token is valid and unexpired, else None."""
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        if len(raw) <= _SIG_BYTES:
            return None
        payload, sig = raw[:-_SIG_BYTES], raw[-_SIG_BYTES:]
        good = hmac.new(_secret(), payload, hashlib.sha256).digest()[:_SIG_BYTES]
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
        # This used to `return True`, with the comment "don't brick alerts
        # if lib is absent; log loudly". Logging loudly is not a control.
        # The webhook's only authentication is this signature, so a missing
        # library turned it into an open endpoint that anyone could post
        # to, and the only sign of it was a warning line in a log that --
        # until 5 Oct -- was not even being printed.
        #
        # An unverifiable request is not a verified one. check_media_wiring()
        # reports the missing library at boot so this is found then, rather
        # than by whoever finds the endpoint first.
        logger.error("twilio package missing — cannot verify signature, "
                     "REJECTING request. Install twilio (it is pinned in "
                     "requirements.txt) or inbound WhatsApp stays closed.")
        return False
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

    def _link(path):
        """Signed URL, or None when the key is missing. make_media_token
        RAISES rather than returning None, so that a missing key can never
        be formatted into a url as the word 'None' -- but a resident who
        typed 'show' should get an apology, not a 500 from a webhook."""
        try:
            return _public_base(request) + "/api/whatsapp/media/" + \
                make_media_token(path)
        except MediaTokenUnavailable as e:
            logger.error("[WA-MEDIA] cannot sign a media link: %s", e)
            return None

    if _CMD_CLIP.search(body):
        clip = find_clip_for_event(ctx.get("camera"), ctx.get("event_ts"))
        if clip:
            url = _link(clip)
            if url:
                return _twiml(f"▶️ Clip: {label}", url)
            return _twiml(f"⚠️ Clip found but media links are unavailable "
                          f"right now.\n{label}")
        snap = _find_snapshot(ctx)
        if snap:
            url = _link(snap)
            if url:
                return _twiml(f"⚠️ Clip not available — snapshot instead.\n"
                              f"📷 {label}", url)
            return _twiml(f"⚠️ Snapshot found but media links are "
                          f"unavailable right now.\n{label}")
        return _twiml(f"⚠️ No clip or snapshot available for {label}.")

    if _CMD_PHOTO.search(body):
        snap = _find_snapshot(ctx)
        if snap:
            url = _link(snap)
            if url:
                return _twiml(f"📷 {label}", url)
            return _twiml(f"⚠️ Snapshot found but media links are "
                          f"unavailable right now.\n{label}")
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
