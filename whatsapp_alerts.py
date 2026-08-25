"""
whatsapp_alerts.py — GuardianGrid WhatsApp Notifications
-----------------------------------------------------------
Sends WhatsApp alerts via Twilio when vehicles are detected.

Routing is controlled by alert_settings.py (Smart Alert Routing):
  🟢 KNOWN       — to resident's own phone (if they opted in)
                   + to security head ONLY if "notify_known" enabled
  🟡 UNKNOWN     — to security head (if "notify_unknown" enabled)
  🔴 BLACKLISTED — to security head ALWAYS (cannot be disabled)
  🔴 THREAT      — to security head ALWAYS (cannot be disabled)

Quiet Hours pause UNKNOWN/KNOWN alerts overnight but never
block BLACKLISTED or THREAT alerts.

Usage:
  from whatsapp_alerts import send_vehicle_alert
  send_vehicle_alert(plate="PB08EY5332", event="ENTRY",
                     resident_info=lookup_result, snapshot_path="...")
"""

import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Load config ──────────────────────────────────────────────────
try:
    import whatsapp_config as cfg
    CONFIG_LOADED = True
except ImportError:
    CONFIG_LOADED = False
    logger.warning("whatsapp_config.py not found — WhatsApp alerts disabled")

# ── Load smart alert routing settings ───────────────────────────
from alert_settings import settings

# ── False-escalation tracking ───────────────────────────────────
# Defensive import: a metrics failure must never stop an alert going out.
try:
    from escalation_metrics import record_escalation as _record_escalation
except Exception:
    _record_escalation = None
    logger.warning("escalation_metrics not available — false-escalation "
                   "tracking is OFF")

# ── Clip-on-Demand context (whatsapp_inbound.py) ────────────────
# After every successful send we remember WHICH event went to WHICH number,
# so a "show" reply knows what to fetch. Defensive: never block an alert.
try:
    from whatsapp_inbound import record_alert_context as _record_wa_context
except Exception:
    _record_wa_context = None

# ── Try importing Twilio ────────────────────────────────────────
try:
    from twilio.rest import Client
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False
    logger.warning("twilio not installed. Run: pip install twilio")


# ── Message templates ─────────────────────────────────────────────
def _format_known_message(plate, event, resident, time_str):
    icon = "🟢" if event == "ENTRY" else "🔵"
    direction = "entered" if event == "ENTRY" else "exited"
    return (
        f"{icon} *GuardianGrid Alert*\n\n"
        f"Your vehicle has {direction} the premises.\n\n"
        f"🚗 *Plate:* {plate}\n"
        f"📍 *Flat:* {resident.get('flat_number','—')}"
        f"{' · Block ' + resident.get('block','') if resident.get('block') else ''}\n"
        f"🕐 *Time:* {time_str}\n\n"
        f"_S&N GuardianGrid Security System_"
    )


def _format_known_security_message(plate, event, resident, time_str):
    """Sent to security head for KNOWN vehicles, only if they opted in."""
    icon = "🟢" if event == "ENTRY" else "🔵"
    direction = "entered" if event == "ENTRY" else "exited"
    return (
        f"{icon} *GuardianGrid — Resident Vehicle*\n\n"
        f"{resident.get('resident_name','Resident')}'s vehicle has {direction}.\n\n"
        f"🚗 *Plate:* {plate}\n"
        f"📍 *Flat:* {resident.get('flat_number','—')}"
        f"{' · Block ' + resident.get('block','') if resident.get('block') else ''}\n"
        f"🕐 *Time:* {time_str}\n\n"
        f"_S&N GuardianGrid Security System_"
    )


def _format_unknown_message(plate, event, time_str):
    icon = "🟡"
    direction = "entered" if event == "ENTRY" else "exited"
    return (
        f"{icon} *GuardianGrid — Unregistered Vehicle*\n\n"
        f"An unregistered vehicle has {direction}.\n\n"
        f"🚗 *Plate:* {plate}\n"
        f"🕐 *Time:* {time_str}\n\n"
        f"⚠️ This vehicle is not in your resident database.\n"
        f"↩️ Reply *show* for the video clip.\n"
        f"_S&N GuardianGrid Security System_"
    )


def _format_blacklist_message(plate, event, resident, time_str):
    return (
        f"🔴 *URGENT — FLAGGED VEHICLE DETECTED*\n\n"
        f"🚨 A BLACKLISTED vehicle has been detected!\n\n"
        f"🚗 *Plate:* {plate}\n"
        f"📝 *Reason:* {resident.get('notes','No reason specified')}\n"
        f"🕐 *Time:* {time_str}\n\n"
        f"⚠️ *IMMEDIATE ACTION MAY BE REQUIRED*\n"
        f"_S&N GuardianGrid Security System_"
    )


def _format_threat_message(threat_type, severity, description, time_str):
    icons = {"WEAPON": "🔫", "CROWD": "👥", "INTRUSION": "🚨", "LOITER": "⏱️"}
    icon = icons.get(threat_type, "⚠️")
    return (
        f"🔴 *GuardianGrid — SECURITY THREAT*\n\n"
        f"{icon} *{threat_type} DETECTED*\n\n"
        f"📝 {description}\n"
        f"⚡ *Severity:* {severity}\n"
        f"🕐 *Time:* {time_str}\n\n"
        f"⚠️ *PLEASE CHECK THE LIVE FEED IMMEDIATELY*\n"
        f"_S&N GuardianGrid Security System_"
    )


# ── Media URL helper — turn a local snapshot into a public link ──
def _media_url_for(snapshot_path: str):
    """Signed, expiring public URL for a snapshot (via whatsapp_inbound).
    Returns None when there's no file or no public base configured —
    the alert then goes out as text, exactly like before."""
    if not snapshot_path:
        return None
    try:
        import os as _os
        if not _os.path.exists(snapshot_path):
            return None
        from whatsapp_inbound import make_media_token
        base = getattr(cfg, "PUBLIC_BASE_URL", "").rstrip("/") if CONFIG_LOADED else ""
        if not base:
            return None
        return f"{base}/api/whatsapp/media/{make_media_token(snapshot_path)}"
    except Exception as e:
        logger.warning(f"media url skipped: {e}")
        return None


# ── Core send function ────────────────────────────────────────────
def _send_whatsapp(to: str, message: str, media_url: str = None) -> dict:
    """Send a WhatsApp message via Twilio. Returns result dict."""
    if not CONFIG_LOADED:
        return {"success": False, "error": "whatsapp_config.py not found"}

    if not TWILIO_AVAILABLE:
        return {"success": False, "error": "twilio package not installed"}

    if not cfg.ENABLE_WHATSAPP_ALERTS:
        return {"success": False, "error": "Alerts disabled in config"}

    if "PASTE_YOUR" in cfg.TWILIO_ACCOUNT_SID:
        return {"success": False, "error": "Twilio credentials not configured yet"}

    if not to or "XXXXXXXXXX" in to:
        return {"success": False, "error": "Recipient number not configured"}

    try:
        client = Client(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
        kwargs = dict(from_=cfg.TWILIO_WHATSAPP_FROM, to=to, body=message)
        if media_url:
            kwargs["media_url"] = [media_url]
        msg = client.messages.create(**kwargs)
        logger.info(f"WhatsApp sent to {to} — SID: {msg.sid}")
        return {"success": True, "sid": msg.sid}
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        return {"success": False, "error": str(e)}


def _security_number() -> str:
    """Security head's number — dashboard setting overrides config default."""
    configured = settings.get("security_whatsapp", "").strip()
    if configured:
        return configured
    return getattr(cfg, "SECURITY_WHATSAPP", "") if CONFIG_LOADED else ""


def _resident_number(resident_info: dict) -> str:
    phone = (resident_info or {}).get("phone", "").strip()
    if not phone:
        return getattr(cfg, "DEFAULT_OWNER_WHATSAPP", "") if CONFIG_LOADED else ""
    if phone.startswith("+"):
        return f"whatsapp:{phone}"
    digits = phone.replace("-", "").replace(" ", "")
    return f"whatsapp:+91{digits}"


# ── Main alert function — called from api_server.py ──────────────
def send_vehicle_alert(plate: str, event: str,
                       resident_info: dict = None,
                       snapshot_path: str = "",
                       trigger_type: str = "anpr",
                       camera: str = None,
                       record_metric: bool = True) -> dict:
    """
    Send WhatsApp alert(s) based on vehicle status and Smart Alert
    Routing settings (alert_settings.py).

    Args:
      plate: Plate number (e.g. "PB08EY5332")
      event: "ENTRY" or "EXIT"
      resident_info: dict from resident_db lookup (or None/unknown)
      snapshot_path: path to snapshot image (not sent in sandbox mode)
      trigger_type: what produced this alert — "anpr" or "face". The face
        recognition path in api_server.py reuses this function, so it must
        say so, otherwise every face detection is filed as an ANPR event
        and the breakdown-by-trigger report is wrong.
      camera: originating camera name, for the breakdown report.
      record_metric: set False when the CALLER logs the escalation itself.
        The face path does this, because it creates an incident and needs the
        escalation tied to that incident id — which it can only do from the
        caller side. Leaving this True there would log the escalation twice.

    Returns: dict with results for each message sent/skipped
    """
    if not CONFIG_LOADED:
        return {"sent": False, "reason": "config not loaded"}

    time_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    results  = []

    status = (resident_info or {}).get("status", "UNKNOWN")
    found  = (resident_info or {}).get("found", False)
    sec_no = _security_number()
    media  = _media_url_for(snapshot_path)

    # ── 🔴 BLACKLISTED — always alerts security, ignores quiet hours ──
    if found and status == "BLACKLISTED":
        if settings.should_alert_security("BLACKLISTED"):
            msg = _format_blacklist_message(plate, event, resident_info, time_str)
            r = _send_whatsapp(sec_no, msg, media_url=media)
            results.append({"to": "security", "type": "BLACKLISTED", **r})
            if r.get("success") and _record_wa_context:
                _record_wa_context(sec_no, plate=plate,
                                   camera=camera or "Main Gate",
                                   snapshot=snapshot_path)
        else:
            results.append({"to": "security", "type": "BLACKLISTED",
                           "success": False, "error": "routing disabled (unexpected)"})

    # ── 🟢 KNOWN resident ────────────────────────────────────────
    elif found and status == "KNOWN":
        # 1. To the resident themselves — respects their personal opt-out
        if resident_info.get("notify_enabled", True):
            msg = _format_known_message(plate, event, resident_info, time_str)
            to  = _resident_number(resident_info)
            r   = _send_whatsapp(to, msg, media_url=media)
            results.append({"to": "resident", "type": "KNOWN", **r})
            if r.get("success") and _record_wa_context:
                _record_wa_context(to, plate=plate,
                                   camera=camera or "Main Gate",
                                   snapshot=snapshot_path)
        else:
            results.append({"to": "resident", "type": "KNOWN",
                           "success": False, "error": "resident opted out"})

        # 2. To security head — OFF by default (notification fatigue fix)
        if settings.should_alert_security("KNOWN"):
            msg2 = _format_known_security_message(plate, event, resident_info, time_str)
            r2   = _send_whatsapp(sec_no, msg2, media_url=media)
            results.append({"to": "security", "type": "KNOWN", **r2})
            if r2.get("success") and _record_wa_context:
                _record_wa_context(sec_no, plate=plate,
                                   camera=camera or "Main Gate",
                                   snapshot=snapshot_path)
        else:
            results.append({"to": "security", "type": "KNOWN",
                           "success": False, "error": "routing disabled or quiet hours"})

    # ── 🟡 UNKNOWN vehicle ───────────────────────────────────────
    else:
        if settings.should_alert_security("UNKNOWN"):
            msg = _format_unknown_message(plate, event, time_str)
            r = _send_whatsapp(sec_no, msg, media_url=media)
            results.append({"to": "security", "type": "UNKNOWN", **r})
            if r.get("success") and _record_wa_context:
                _record_wa_context(sec_no, plate=plate,
                                   camera=camera or "Main Gate",
                                   snapshot=snapshot_path)
        else:
            results.append({"to": "security", "type": "UNKNOWN",
                           "success": False, "error": "routing disabled or quiet hours"})

    # ── Log the escalation, if one actually happened ─────────────
    # Three deliberate rules here, each one protects the metric:
    #
    #  1. Only messages to SECURITY count. A resident's own phone buzzing
    #     because their car came home did not consume an operator. Counting
    #     those would bury the real false rate under routine notifications.
    #
    #  2. Only SUCCESSFUL sends count. Every branch above appends a result
    #     even when it was skipped for quiet hours, disabled routing, or a
    #     Twilio failure. Those never reached a human, so they are not
    #     escalations — logging the attempt would inflate the denominator
    #     and flatter the false rate downward.
    #
    #  3. One row per alert event, even if several messages went out.
    _reached_security = [r for r in results
                         if r.get("to") == "security" and r.get("success")]
    if _record_escalation is not None and _reached_security and record_metric:
        _record_escalation(
            tier=3 if status in ("BLACKLISTED", "WATCHLIST") else 2,
            trigger_type=trigger_type,
            camera=camera or "Main Gate",
            zone="gate",
            channel="whatsapp",
            subject=f"{plate} ({status} {event})",
        )

    return {"sent": True, "results": results}


def send_threat_alert(threat_type: str, severity: str,
                      description: str) -> dict:
    """Send WhatsApp alert for AI threat detection — ALWAYS sent,
    ignores quiet hours, cannot be disabled from settings.

    ⚠️ DO NOT add record_escalation() here.
    This function is called by the escalate_fn downstream of
    tiering_brain.handle_threat(), which has ALREADY logged the escalation
    with full tier and zone context. Hooking it here as well would count
    every Tier 3 threat twice, which halves the apparent false-escalation
    rate — the exact direction of error that makes the metric useless.
    """
    if not CONFIG_LOADED:
        return {"sent": False, "reason": "config not loaded"}

    if not settings.should_alert_security("THREAT"):
        # Should never happen (THREAT is force-enabled) but guard anyway
        return {"sent": False, "reason": "routing disabled"}

    time_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    msg = _format_threat_message(threat_type, severity, description, time_str)
    r = _send_whatsapp(_security_number(), msg)
    return {"sent": True, "result": r}


# ── Standalone test ────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing WhatsApp alerts with Smart Alert Routing...")
    print(f"Config loaded: {CONFIG_LOADED}")
    print(f"Twilio available: {TWILIO_AVAILABLE}")
    print(f"\nCurrent routing settings:")
    for k, v in settings.get_all().items():
        print(f"  {k:22} = {v}")

    if not CONFIG_LOADED:
        print("\n❌ Create whatsapp_config.py first!")
        exit()

    if "PASTE_YOUR" in cfg.TWILIO_ACCOUNT_SID:
        print("\n❌ Please fill in your Twilio credentials in whatsapp_config.py")
        exit()

    print("\nSending test alert (KNOWN resident, ENTRY)...")
    result = send_vehicle_alert(
        plate="PB08EY5332",
        event="ENTRY",
        resident_info={
            "found": True, "status": "KNOWN",
            "resident_name": "Akash Singh",
            "flat_number": "302", "block": "B",
            "phone": "", "notify_enabled": True,
        }
    )
    print(result)
