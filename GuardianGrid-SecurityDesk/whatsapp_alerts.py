"""
whatsapp_alerts.py — GuardianGrid WhatsApp Notifications
-----------------------------------------------------------
Sends WhatsApp alerts via Twilio when vehicles are detected.

3 Priority Levels:
  🟢 KNOWN       — Normal message to flat owner ("Your vehicle entered")
  🟡 UNKNOWN     — Warning to security guard ("Unregistered vehicle")
  🔴 BLACKLISTED — URGENT alert to security guard ("⚠️ FLAGGED VEHICLE")

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


def _format_unknown_message(plate, event, time_str):
    icon = "🟡"
    direction = "entered" if event == "ENTRY" else "exited"
    return (
        f"{icon} *GuardianGrid — Unregistered Vehicle*\n\n"
        f"An unregistered vehicle has {direction}.\n\n"
        f"🚗 *Plate:* {plate}\n"
        f"🕐 *Time:* {time_str}\n\n"
        f"⚠️ This vehicle is not in your resident database.\n"
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


# ── Core send function ────────────────────────────────────────────
def _send_whatsapp(to: str, message: str) -> dict:
    """Send a WhatsApp message via Twilio. Returns result dict."""
    if not CONFIG_LOADED:
        return {"success": False, "error": "whatsapp_config.py not found"}

    if not TWILIO_AVAILABLE:
        return {"success": False, "error": "twilio package not installed"}

    if not cfg.ENABLE_WHATSAPP_ALERTS:
        return {"success": False, "error": "Alerts disabled in config"}

    if "PASTE_YOUR" in cfg.TWILIO_ACCOUNT_SID:
        return {"success": False, "error": "Twilio credentials not configured yet"}

    try:
        client = Client(cfg.TWILIO_ACCOUNT_SID, cfg.TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            from_=cfg.TWILIO_WHATSAPP_FROM,
            to=to,
            body=message,
        )
        logger.info(f"WhatsApp sent to {to} — SID: {msg.sid}")
        return {"success": True, "sid": msg.sid}
    except Exception as e:
        logger.error(f"WhatsApp send failed: {e}")
        return {"success": False, "error": str(e)}


# ── Main alert function — called from api_server.py ──────────────
def send_vehicle_alert(plate: str, event: str,
                       resident_info: dict = None,
                       snapshot_path: str = "") -> dict:
    """
    Send WhatsApp alert based on vehicle status.

    Args:
      plate: Plate number (e.g. "PB08EY5332")
      event: "ENTRY" or "EXIT"
      resident_info: dict from resident_db lookup (or None/unknown)
      snapshot_path: path to snapshot image (not sent in sandbox mode)

    Returns: dict with results for each message sent
    """
    if not CONFIG_LOADED:
        return {"sent": False, "reason": "config not loaded"}

    time_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    results  = []

    status = (resident_info or {}).get("status", "UNKNOWN")
    found  = (resident_info or {}).get("found", False)

    # ── 🔴 BLACKLISTED — highest priority ──────────────────────
    if found and status == "BLACKLISTED":
        msg = _format_blacklist_message(plate, event, resident_info, time_str)
        r = _send_whatsapp(cfg.SECURITY_WHATSAPP, msg)
        results.append({"to": "security", "type": "BLACKLISTED", **r})

    # ── 🟢 KNOWN resident ────────────────────────────────────────
    elif found and status == "KNOWN":
        if cfg.ALERT_ON_KNOWN_VEHICLES:
            msg = _format_known_message(plate, event, resident_info, time_str)
            # Send to resident's own phone if available
            phone = resident_info.get("phone", "").strip()
            if phone:
                to = f"whatsapp:+91{phone.replace('-','').replace(' ','')}" \
                     if not phone.startswith("+") else f"whatsapp:{phone}"
                r = _send_whatsapp(to, msg)
                results.append({"to": "resident", "type": "KNOWN", **r})
            else:
                r = _send_whatsapp(cfg.DEFAULT_OWNER_WHATSAPP, msg)
                results.append({"to": "default_owner", "type": "KNOWN", **r})

    # ── 🟡 UNKNOWN vehicle ───────────────────────────────────────
    else:
        msg = _format_unknown_message(plate, event, time_str)
        r = _send_whatsapp(cfg.SECURITY_WHATSAPP, msg)
        results.append({"to": "security", "type": "UNKNOWN", **r})

    return {"sent": True, "results": results}


def send_threat_alert(threat_type: str, severity: str,
                      description: str) -> dict:
    """Send WhatsApp alert for AI threat detection (weapon, crowd, etc)."""
    if not CONFIG_LOADED:
        return {"sent": False, "reason": "config not loaded"}

    time_str = datetime.now().strftime("%d %b %Y, %I:%M %p")
    msg = _format_threat_message(threat_type, severity, description, time_str)
    r = _send_whatsapp(cfg.SECURITY_WHATSAPP, msg)
    return {"sent": True, "result": r}


# ── Standalone test ────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing WhatsApp alerts...")
    print(f"Config loaded: {CONFIG_LOADED}")
    print(f"Twilio available: {TWILIO_AVAILABLE}")

    if not CONFIG_LOADED:
        print("\n❌ Create whatsapp_config.py first!")
        exit()

    if "PASTE_YOUR" in cfg.TWILIO_ACCOUNT_SID:
        print("\n❌ Please fill in your Twilio credentials in whatsapp_config.py")
        exit()

    print("\nSending test alert (KNOWN resident)...")
    result = send_vehicle_alert(
        plate="PB08EY5332",
        event="ENTRY",
        resident_info={
            "found": True, "status": "KNOWN",
            "resident_name": "Akash Singh",
            "flat_number": "302", "block": "B",
            "phone": "",   # leave empty to send to DEFAULT_OWNER_WHATSAPP
        }
    )
    print(result)
