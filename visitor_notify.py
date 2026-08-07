"""
visitor_notify.py — Resident visitor notification  (Visitor Notify v1)
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
  1) Paste them below in the CONFIG block (simplest).
  2) Environment variables: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
  3) Auto-discovery from your existing whatsapp_alerts.py — it scans
     that module for a twilio Client and a from-number so you don't
     have to duplicate secrets.

Never crashes the server: every failure returns a status string and is
logged to the visitor_notifications table instead of raising.
"""

import os

# ── CONFIG ───────────────────────────────────────────────────────
# Do NOT edit here anymore — put your settings in notify_config.py
# (a separate file that updates to this module never overwrite).
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")

try:
    import notify_config as _cfg
    TWILIO_ACCOUNT_SID   = getattr(_cfg, "TWILIO_ACCOUNT_SID", "") or TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN    = getattr(_cfg, "TWILIO_AUTH_TOKEN", "") or TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM = getattr(_cfg, "TWILIO_WHATSAPP_FROM", "") or TWILIO_WHATSAPP_FROM
    print(f"[VISITOR-NOTIFY] notify_config.py loaded "
          f"(From: {TWILIO_WHATSAPP_FROM or 'not set'})")
except ImportError:
    print("[VISITOR-NOTIFY] notify_config.py not found — "
          "using env vars / auto-discovery")

from flat_directory import get_flat, record_notification  # noqa: E402


# ── Credential resolution ────────────────────────────────────────
def _resolve_credentials():
    """Return (client, from_number) or (None, reason_string)."""
    sid   = TWILIO_ACCOUNT_SID  or os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = TWILIO_AUTH_TOKEN   or os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_ = TWILIO_WHATSAPP_FROM or os.environ.get("TWILIO_WHATSAPP_FROM", "")

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
        # An explicitly configured From (CONFIG block or env) always wins
        # over discovery — fixes wrong-channel/wrong-number guesses.
        if from_:
            found_from = from_
        if found_from and not found_from.startswith("whatsapp:"):
            found_from = "whatsapp:" + found_from
        if found_client and found_from:
            print(f"[VISITOR-NOTIFY] using From: {found_from}")
            return found_client, found_from
        return None, ("no credentials: fill CONFIG block in visitor_notify.py "
                      "or set TWILIO_* environment variables")
    except ImportError:
        return None, ("no credentials and whatsapp_alerts.py not found — "
                      "fill CONFIG block in visitor_notify.py")
    except Exception as e:
        return None, f"credential discovery error: {e}"


# ── Message ──────────────────────────────────────────────────────
def _build_message(flat, visitor_name, purpose):
    from datetime import datetime
    when = datetime.now().strftime("%I:%M %p, %d %b")
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
    Every outcome is also written to visitor_notifications.
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

    body = _build_message(flat, visitor_name, purpose)
    try:
        msg = client.messages.create(from_=from_or_reason, to=to_number,
                                     body=body)
        global _LAST_SID
        _LAST_SID = msg.sid
        detail = f"→ {flat['owner_name']} ({flat['whatsapp']}) sid={msg.sid}"
        record_notification(visitor_id, flat_no, "sent", detail)
        print(f"[VISITOR-NOTIFY] sent {flat_no.upper()} {detail}")
        return "sent", detail
    except Exception as e:
        code = getattr(e, "code", None)
        msg = (getattr(e, "msg", "") or str(e)).replace("\n", " ").strip()
        detail = (f"Twilio error {code}: {msg}" if code else msg)[:250]
        record_notification(visitor_id, flat_no, "failed", detail)
        print(f"[VISITOR-NOTIFY] FAILED {flat_no.upper()}: {detail}")
        return "failed", detail


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
                        print("[VISITOR-NOTIFY] FIX: from the recipient "
                              "phone, WhatsApp the sandbox number "
                              "+1 415 523 8886 (join code first time, "
                              "any message to reopen the 24h window), "
                              "then retest.")
    else:
        print("usage: python visitor_notify.py B-302 \"Test Courier\"")
