"""
visitor_notify.py — Resident visitor notification  (Visitor Notify v1.2)
----------------------------------------------------------------------
When the guard logs a visitor for a flat, this module WhatsApps the
flat owner:

    🔔 GuardianGrid — Visitor at gate for B-302
    Name: Raj (Delhivery)
    Purpose: Parcel delivery
    Time: 02:41 PM, 02 Aug
    Logged by gate security.

v1 is TEXT ONLY (no photo). Photo attach comes in v1.1 once the
snapshot upload path is confirmed.

CREDENTIALS — three ways, tried in this order:
  1) Environment variables: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
     TWILIO_WHATSAPP_FROM.
  2) notify_config.py — a local file that updates never overwrite.
  3) Auto-discovery from your existing whatsapp_alerts.py — it scans
     that module for a twilio Client and a from-number so you don't
     have to duplicate secrets.

THE ORDER CHANGED IN v1.2 AND THE ORDER IS THE POINT. It used to be
the other way round: a value in notify_config.py beat the environment.
On the droplet the environment is the only source of truth — deploy.sh
feeds it in through docker --env-file — and notify_config.py is a local
development convenience listed in .git/info/exclude, so it never enters
the image. The old order was therefore harmless only by accident: the
file that would have won is the file that is never there. The day
anyone copies notify_config.py into the image, visitor alerts keep
going out from whatever number that file remembers while every other
path has moved on, and nothing logs a word about it. That is OCT-96's
shape exactly. The environment wins now, and a disagreement between
the two is printed loudly instead of silently resolved.

TEMPLATES — a visitor alert is the most time-critical message this
system sends: somebody is standing at the gate. A freeform WhatsApp
body only reaches a phone that messaged us within the last 24 hours,
and no resident has. This worked on the Twilio sandbox because joining
the sandbox opens a 24-hour window, which is precisely why it would
have broken on the first real delivery from a production sender. Set
WA_TPL_VISITOR to an approved Content SID and the send goes out as a
template, which reaches a cold phone. Leave it unset and the behaviour
is exactly what it is today.

Never crashes the server: every failure returns a status string and is
logged to the visitor_notifications table instead of raising.
"""

import json
import os

# ── CONFIG ───────────────────────────────────────────────────────
# Do not put credentials here. Set them in the environment (what the
# droplet does) or in notify_config.py (local development only).
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()

# Content SID of the approved "visitor at gate" template. Unset means
# freeform, which is today's behaviour and is safe on the sandbox only.
WA_TPL_VISITOR = os.getenv("WA_TPL_VISITOR", "").strip()

_FROM_SOURCE = "environment" if TWILIO_WHATSAPP_FROM else "unset"

try:
    import notify_config as _cfg

    def _prefer_env(env_value, name):
        """Environment wins. The file fills a gap; it never overrides.

        Values are never printed — only the fact that they disagree.
        """
        file_value = (getattr(_cfg, name, "") or "").strip()
        if env_value and file_value and env_value != file_value:
            print(f"[VISITOR-NOTIFY] WARNING: {name} is set in both the "
                  f"environment and notify_config.py and they disagree. "
                  f"Using the environment. Remove it from "
                  f"notify_config.py to silence this.")
        return env_value or file_value

    TWILIO_ACCOUNT_SID = _prefer_env(TWILIO_ACCOUNT_SID,
                                     "TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = _prefer_env(TWILIO_AUTH_TOKEN,
                                    "TWILIO_AUTH_TOKEN")
    TWILIO_WHATSAPP_FROM = _prefer_env(TWILIO_WHATSAPP_FROM,
                                       "TWILIO_WHATSAPP_FROM")
    if _FROM_SOURCE == "unset" and TWILIO_WHATSAPP_FROM:
        _FROM_SOURCE = "notify_config.py"
    print(f"[VISITOR-NOTIFY] notify_config.py loaded (From: "
          f"{TWILIO_WHATSAPP_FROM or 'not set'}, source: {_FROM_SOURCE})")
except ImportError:
    print(f"[VISITOR-NOTIFY] notify_config.py not found — using env vars "
          f"/ auto-discovery (From: {TWILIO_WHATSAPP_FROM or 'not set'})")

from flat_directory import get_flat, record_notification  # noqa: E402


# ── Template helpers ─────────────────────────────────────────────
def _template_sid_looks_real(sid: str) -> bool:
    """Shape check only — a deliberate copy of whatsapp_alerts' guard.

    This module is written to keep working when whatsapp_alerts is
    missing, so it cannot import from it at module level. Five lines of
    duplication buys that independence. bool(sid) would accept a pasted
    placeholder and we would silently send nothing. This cannot tell an
    approved template from a rejected one; only Twilio can.
    """
    if not sid or not sid.startswith("HX") or len(sid) != 34:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in sid[2:])


def _content_vars(*values) -> str:
    """Twilio wants {"1": "...", "2": "..."} as a JSON string.

    Every variable must carry a value: a missing one fails the whole
    send, so an empty becomes an em dash rather than nothing.
    """
    out = {}
    for i, v in enumerate(values, 1):
        text = "" if v is None else str(v).strip()
        out[str(i)] = text or "—"
    return json.dumps(out, ensure_ascii=False)


# ── Credential resolution ────────────────────────────────────────
def _resolve_credentials():
    """Return (client, from_number) or (None, reason_string)."""
    # Environment first here too, so a late-set variable still wins and
    # this function cannot disagree with the module-level block above.
    sid = (os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
           or TWILIO_ACCOUNT_SID)
    token = (os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
             or TWILIO_AUTH_TOKEN)
    from_ = (os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()
             or TWILIO_WHATSAPP_FROM)

    # Options 1 & 2: explicit credentials
    if sid and token and from_:
        try:
            from twilio.rest import Client
            return Client(sid, token), from_
        except Exception as e:
            return None, f"twilio client error: {e}"

    # Option 3: borrow from whatsapp_alerts.py
    try:
        import whatsapp_alerts as wa
        try:
            from twilio.rest import Client as _TwClient
        except Exception as e:
            return None, f"twilio import failed: {e}"

        found_client, found_from = None, None
        for attr_name in dir(wa):
            if attr_name.startswith("_"):
                continue
            val = getattr(wa, attr_name, None)
            if isinstance(val, _TwClient) and found_client is None:
                found_client = val
            elif isinstance(val, str):
                v = val.strip()
                if (v.startswith("whatsapp:") and found_from is None
                        and "to" not in attr_name.lower()):
                    found_from = v
        # from-number stored without the whatsapp: prefix? second pass
        if found_client and not found_from:
            for attr_name in dir(wa):
                val = getattr(wa, attr_name, None)
                if (isinstance(val, str) and val.strip().startswith("+")
                        and "from" in attr_name.lower()):
                    found_from = "whatsapp:" + val.strip()
                    break
        # An explicitly configured From (environment or notify_config.py)
        # always wins over discovery — fixes wrong-channel/wrong-number
        # guesses.
        if from_:
            found_from = from_
        if found_from and not found_from.startswith("whatsapp:"):
            found_from = "whatsapp:" + found_from
        if found_client and found_from:
            print(f"[VISITOR-NOTIFY] using From: {found_from}")
            return found_client, found_from
        return None, ("no credentials: set TWILIO_* environment variables "
                      "or fill notify_config.py")
    except ImportError:
        return None, ("no credentials and whatsapp_alerts.py not found — "
                      "set TWILIO_* environment variables")
    except Exception as e:
        return None, f"credential discovery error: {e}"


# ── Message ──────────────────────────────────────────────────────
def _when_str():
    from datetime import datetime
    return datetime.now().strftime("%I:%M %p, %d %b")


def _build_message(flat, visitor_name, purpose, when=None):
    """The freeform body. It stays the source of truth for what the
    message says — the template is a copy of it, so the two must be
    edited together."""
    if when is None:
        when = _when_str()
    lines = [
        f"🔔 GuardianGrid — Visitor at gate for {flat['flat_no']}",
        f"Name: {visitor_name}",
    ]
    if purpose:
        lines.append(f"Purpose: {purpose}")
    lines.append(f"Time: {when}")
    lines.append("Logged by gate security.")
    return "\n".join(lines)


# ── Public API ───────────────────────────────────────────────────
def notify_flat(flat_no, visitor_name, purpose="", visitor_phone="",
                visitor_id=None):
    """
    Send the resident notification. Never raises.
    Returns (status, detail):
      status ∈ {"sent", "failed", "skipped"}
    Every outcome is also written to visitor_notifications. `detail`
    records which path was used, because "the alert went out" and "the
    alert went out in a way that can reach a cold phone" are different
    claims.
    """
    flat_no = (flat_no or "").strip()
    if not flat_no:
        record_notification(visitor_id, flat_no, "skipped", "no flat given")
        return "skipped", "no flat given"

    flat = get_flat(flat_no)
    if not flat:
        detail = f"flat {flat_no.upper()} not in directory"
        record_notification(visitor_id, flat_no, "skipped", detail)
        return "skipped", detail

    client, from_or_reason = _resolve_credentials()
    if client is None:
        record_notification(visitor_id, flat_no, "skipped", from_or_reason)
        print(f"[VISITOR-NOTIFY] skipped: {from_or_reason}")
        return "skipped", from_or_reason

    to_number = flat["whatsapp"]
    if not to_number.startswith("whatsapp:"):
        to_number = "whatsapp:" + to_number

    when = _when_str()
    body = _build_message(flat, visitor_name, purpose, when)

    def _create(use_template):
        if use_template:
            return client.messages.create(
                from_=from_or_reason, to=to_number,
                content_sid=WA_TPL_VISITOR,
                content_variables=_content_vars(
                    flat["flat_no"], visitor_name, purpose or "", when))
        return client.messages.create(from_=from_or_reason, to=to_number,
                                      body=body)

    def _fail(exc):
        code = getattr(exc, "code", None)
        text = (getattr(exc, "msg", "") or str(exc)).replace("\n", " ").strip()
        d = (f"Twilio error {code}: {text}" if code else text)[:250]
        record_notification(visitor_id, flat_no, "failed", d)
        print(f"[VISITOR-NOTIFY] FAILED {flat_no.upper()}: {d}")
        return "failed", d

    use_tpl = _template_sid_looks_real(WA_TPL_VISITOR)
    if WA_TPL_VISITOR and not use_tpl:
        print(f"[VISITOR-NOTIFY] WA_TPL_VISITOR is set but is not a Content "
              f"SID (expected HX + 32 hex, got {len(WA_TPL_VISITOR)} chars) "
              f"— sending freeform, which cannot reach a phone outside the "
              f"24-hour window")
    path = "template" if use_tpl else "freeform"

    try:
        msg = _create(use_tpl)
    except Exception as e:
        if not use_tpl:
            return _fail(e)
        # Falling back is deliberate, and matches whatsapp_alerts.send_alert:
        # a rejected or deleted template must not mean the resident hears
        # nothing. Freeform still reaches anyone inside the window, which is
        # better than silence.
        print(f"[VISITOR-NOTIFY] template send failed ({e}) — falling back "
              f"to freeform, which cannot reach a cold phone")
        path = "freeform-after-template-error"
        try:
            msg = _create(False)
        except Exception as e2:
            return _fail(e2)

    global _LAST_SID
    _LAST_SID = msg.sid
    detail = (f"→ {flat['owner_name']} ({flat['whatsapp']}) "
              f"sid={msg.sid} path={path}")
    record_notification(visitor_id, flat_no, "sent", detail)
    print(f"[VISITOR-NOTIFY] sent {flat_no.upper()} {detail}")
    return "sent", detail


# module-level: SID of the most recent send (for delivery checks)
_LAST_SID = None


# ── Quick manual test ────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import time
    from flat_directory import init_flats, _guard_cwd
    _guard_cwd()
    init_flats()
    if len(sys.argv) >= 3:
        status, detail = notify_flat(sys.argv[1], sys.argv[2],
                                     purpose="test message")
        print(f"result: {status} — {detail}")
        if status == "sent" and _LAST_SID:
            print("[VISITOR-NOTIFY] checking delivery status", end="",
                  flush=True)
            client, _ = _resolve_credentials()
            final = None
            for _i in range(6):          # poll for up to ~18 seconds
                time.sleep(3)
                print(".", end="", flush=True)
                try:
                    m = client.messages(_LAST_SID).fetch()
                    final = m
                    if m.status in ("delivered", "read", "failed",
                                    "undelivered"):
                        break
                except Exception as e:
                    print(f"\n[VISITOR-NOTIFY] status check error: {e}")
                    break
            print()
            if final is not None:
                print(f"[VISITOR-NOTIFY] delivery status: {final.status}")
                if final.error_code:
                    print(f"[VISITOR-NOTIFY] error {final.error_code}: "
                          f"{final.error_message}")
                    if str(final.error_code) in ("63015", "63016"):
                        print("[VISITOR-NOTIFY] FIX: this means the message "
                              "was freeform and the recipient has no open "
                              "24-hour window. On the Twilio sandbox you "
                              "can re-open one by messaging the sandbox "
                              "number from the recipient phone. On a "
                              "production sender there is no window to "
                              "open — set WA_TPL_VISITOR to an approved "
                              "Content SID. This is the entire reason "
                              "templates exist.")
    else:
        print("usage: python visitor_notify.py B-302 \"Test Courier\"")
