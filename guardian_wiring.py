"""
guardian_wiring.py — GuardianGrid Step 3 Integration Glue
==========================================================
Connects the five pieces into one working pipeline:

  threat_detector (fire_alert)
        │  register_alert_callback
        ▼
  tiering_brain (handle_threat)  ── Tier 1 → silent (already logged)
        │                         ── Tier 2 → voice
        │                         ── Tier 3 → this module's escalate path
        ▼
  guardian_wiring:
        • creates an incident (your backend/incidents)
        • registers it with ack_watchdog (starts the timer)
        • fires WhatsApp via your send_threat_alert
        • speaks via Guardian voice (optional hook)

  ack_watchdog (independent thread)
        └─ on unacknowledged/unresolved → calls back here → WhatsApp again

ONE import + ONE call added to api_server.py startup (see bottom of this file).
Nothing else in your codebase changes.
"""

import logging
import threading

# Your existing pieces — imported exactly as they are.
from threat_detector import register_alert_callback
from backend.incidents.incident_service import create_new_incident

import tiering_brain
import ack_watchdog

logger = logging.getLogger("guardian-wiring")

# ── False-escalation tracking ────────────────────────────────────────────────
# Defensive: metrics must never be able to stop an alarm going out.
try:
    from escalation_metrics import (
        record_escalation as _record_escalation,
        link_incident as _link_incident,
    )
except Exception:                                    # pragma: no cover
    _record_escalation = None
    _link_incident = None
    logger.warning("escalation_metrics unavailable — false-escalation "
                   "tracking is OFF")

# WhatsApp: import your existing sender, degrade gracefully if unavailable.
try:
    from whatsapp_alerts import send_threat_alert
    _WHATSAPP = True
except Exception as e:      # pragma: no cover
    _WHATSAPP = False
    logger.warning("whatsapp_alerts unavailable (%s) — escalations will log only", e)


# ─────────────────────────────────────────────────────────────────────────────
# Voice hook — optional. If you run the Guardian voice agent on the same box,
# set this to a function that makes it speak. If not set, voice is skipped and
# everything else still works.
# ─────────────────────────────────────────────────────────────────────────────
_voice_fn = None

def set_voice_fn(fn):
    """fn(text: str) → makes Guardian speak. Optional."""
    global _voice_fn
    _voice_fn = fn

def _speak(text: str):
    if _voice_fn:
        try:
            _voice_fn(text)
        except Exception as e:
            logger.error("voice failed: %s", e)
    else:
        logger.info("[voice skipped] would say: %s", text)


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp escalation — reuses your existing send_threat_alert. Runs in a thread
# so a slow Twilio call never blocks detection.
# ─────────────────────────────────────────────────────────────────────────────
def _send_whatsapp(threat_type, severity, description):
    if not _WHATSAPP:
        logger.warning("[whatsapp skipped] %s: %s", threat_type, description)
        return
    threading.Thread(
        target=send_threat_alert,
        kwargs={"threat_type": threat_type, "severity": severity,
                "description": description},
        daemon=True,
    ).start()


# ─────────────────────────────────────────────────────────────────────────────
# Tier-3 path: create incident → register with watchdog → WhatsApp.
# Called by the tiering brain when it decides Tier 3.
# ─────────────────────────────────────────────────────────────────────────────
def _on_tier3(event_dict, decision):
    """event_dict = ThreatEvent.to_dict(); decision = TierDecision."""
    # 1. Create a tracked incident in YOUR incident system.
    incident = create_new_incident({
        "title":       f"{event_dict['threat_type']} — Tier 3",
        "description":  event_dict.get("description", ""),
        "severity":     event_dict.get("severity", "HIGH"),
        "camera_name":  decision.zone if decision.zone != "unknown" else "Unknown Camera",
        "operator":     "Guardian AI",
        "evidence_image": event_dict.get("snapshot_path") or None,
        "confidence":   event_dict.get("confidence"),
    })
    logger.info("Tier-3 incident %s created", incident.get("incident_id"))

    # Attach this incident to the escalation row that tiering_brain logged a
    # moment ago. The escalation is recorded BEFORE the incident exists, so
    # the link has to be made here, after the fact. Without it, a guard
    # resolving this case has no escalation to pass a verdict on.
    if _link_incident is not None:
        row_id = getattr(decision, "escalation_row_id", None)
        if row_id and incident.get("incident_id"):
            _link_incident(row_id, incident["incident_id"])

    # 2. Start its acknowledgment timer (2 min deep-night / 4 min default).
    ack_watchdog.register_incident(
        incident, tier=3, deep_night=decision.is_deep_night
    )

    # 3. Fire the first WhatsApp now (the watchdog fires again if unacknowledged).
    _send_whatsapp(event_dict["threat_type"],
                   event_dict.get("severity", "HIGH"),
                   event_dict.get("description", ""))


# ─────────────────────────────────────────────────────────────────────────────
# Watchdog escalation callback: when an incident goes unacknowledged/unresolved,
# the watchdog calls this → another WhatsApp with the reason.
# ─────────────────────────────────────────────────────────────────────────────
def _on_watchdog_escalation(incident, reason):
    """
    ⚠️ DO NOT call record_escalation() here.

    This fires when an ALREADY-LOGGED escalation goes unacknowledged. It is a
    reminder about an existing alert, not a new one. Logging it would inflate
    the denominator every time a guard was slow to respond — which would make
    the detector look noisier the worse your response times got, exactly
    backwards from what the metric is for.
    """
    logger.info("Watchdog escalation for %s: %s",
                incident.get("incident_id"), reason)
    _send_whatsapp(
        incident.get("title", "Incident"),
        incident.get("severity", "HIGH"),
        f"{incident.get('description','')} — {reason} "
        f"[{incident.get('incident_id')}]",
    )


# ─────────────────────────────────────────────────────────────────────────────
# THE ONE FUNCTION you call from api_server.py startup.
# ─────────────────────────────────────────────────────────────────────────────
def wire_guardian(voice_fn=None):
    """
    Wire everything together and start the watchdog.
    Call once at server startup (after init_db, before/around app.run).
    Pass voice_fn if the Guardian voice agent runs on this box; else omit.
    """
    if voice_fn:
        set_voice_fn(voice_fn)

    # tiering brain speaks Tier 2+, and calls _on_tier3 for Tier 3
    tiering_brain.configure(voice_fn=_speak, escalate_fn=_on_tier3)

    # watchdog escalations go to WhatsApp
    ack_watchdog.configure(escalate_fn=_on_watchdog_escalation)

    # start the independent watchdog thread
    ack_watchdog.start_watchdog()

    # finally, subscribe the brain to the detector's existing callback seam
    register_alert_callback(tiering_brain.handle_threat)

    logger.info("Guardian wired: detector → tiering → voice/incident/whatsapp + watchdog")


# ─────────────────────────────────────────────────────────────────────────────
# ANPR / vehicle path — blacklisted or unknown plate.
# This is a SEPARATE entry point from the threat detector. Call it from the
# ANPR blacklist block in api_server.py. A blacklisted plate is Tier-3 by
# definition, so we skip scoring and go straight to voice + escalation.
# ─────────────────────────────────────────────────────────────────────────────
def on_blacklisted_vehicle(plate: str, resident_name: str = "",
                           reason: str = "", incident: dict = None,
                           deep_night: bool = False):
    """
    Call when a BLACKLISTED plate is detected. Speaks + starts ack timer.
    `incident` is the dict from your existing create_new_incident() call in the
    ANPR block — pass it so the watchdog tracks the SAME incident (no duplicate).
    If you don't pass one, no ack timer is started (voice + whatsapp only).
    """
    who = f" registered to {resident_name}" if resident_name else ""
    spoken = (f"Hey boss, a blacklisted vehicle has entered. "
              f"Plate {_say_plate(plate)}{who}. Please respond.")
    _speak(spoken)

    # WhatsApp — fire the threat-style alert so it joins the escalation flow.
    _send_whatsapp("BLACKLISTED VEHICLE", "HIGH",
                   f"Plate {plate}{who}. {reason}".strip())

    # ── Log the escalation ───────────────────────────────────────────────
    # This path is a SEPARATE entry point that bypasses tiering_brain
    # entirely, so nothing upstream has recorded it. Without this call,
    # blacklist hits — the highest-severity event the system produces —
    # would be completely absent from the false-escalation data.
    #
    # A blacklisted plate is Tier 3 by definition; no scoring involved.
    _esc_row = None
    if _record_escalation is not None:
        _esc_row = _record_escalation(
            incident_id=(incident or {}).get("incident_id"),
            tier=3,
            trigger_type="blacklist",
            camera="Entry Gate",
            zone="gate",
            channel="voice+whatsapp",
            subject=f"{plate} (BLACKLISTED)",
        )

    # If the caller passed the incident it already created, track it for ack.
    if incident and incident.get("incident_id"):
        ack_watchdog.register_incident(incident, tier=3, deep_night=deep_night)


def _say_plate(plate: str) -> str:
    """Space out a plate so TTS reads it clearly: 'DL3CAB5678' -> 'D L 3 C A B...'."""
    return " ".join(plate.upper().replace(" ", ""))


# ═══════════════════════════════════════════════════════════════════════════
# HOW TO ADD TO api_server.py  (two lines, inside `if __name__ == "__main__":`)
# ───────────────────────────────────────────────────────────────────────────
#   from guardian_wiring import wire_guardian
#   ...
#   init_db()
#   init_visitors()
#   wire_guardian()          # ← add this line (after init_db, before app.run)
#   ...
#   app.run(...)
#
# If the voice agent runs on the same machine, pass a hook:
#   wire_guardian(voice_fn=lambda text: my_voice_queue.put(text))
# ═══════════════════════════════════════════════════════════════════════════
