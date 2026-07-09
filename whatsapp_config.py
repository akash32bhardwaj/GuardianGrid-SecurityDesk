"""
whatsapp_config.py — YOUR CREDENTIALS GO HERE
------------------------------------------------
⚠️ IMPORTANT: This file contains secret keys.
   - NEVER share this file with anyone
   - NEVER upload this file to GitHub/GitHub
   - NEVER paste these values in chat

Fill in the 4 values below from your Twilio Console:
https://console.twilio.com
"""

# ── Twilio Credentials ──────────────────────────────────────────
# Find these on your Twilio Console dashboard (top of page)
TWILIO_ACCOUNT_SID = "TWILIO_ACCOUNT_SID"     # starts with AC...
TWILIO_AUTH_TOKEN  = "TWILIO_AUTH_TOKEN"      # click "show" to reveal

# ── Twilio WhatsApp Sandbox Number ──────────────────────────────
# This is usually the same for everyone during sandbox testing
TWILIO_WHATSAPP_FROM = "whatsapp:+TWILIO_WHATSAPP_FROM"

# ── Recipients ───────────────────────────────────────────────────
# Security guard / admin number — receives ALL alerts
# Format: whatsapp:+918847406740  (with country code, no spaces/dashes)
SECURITY_WHATSAPP = "whatsapp:+918847406740"

# Default flat owner number — used if resident has no phone in database
# (Each resident's own phone from resident_db.py is used when available)
DEFAULT_OWNER_WHATSAPP = "whatsapp:+918847406740"

# ── Alert Settings ───────────────────────────────────────────────
ENABLE_WHATSAPP_ALERTS = True

# Send alert for KNOWN residents too? (or only unknown/blacklisted)
ALERT_ON_KNOWN_VEHICLES = True
