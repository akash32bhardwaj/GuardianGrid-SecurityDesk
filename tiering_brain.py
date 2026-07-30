"""
tiering_brain.py — GuardianGrid Voice/Alert Tiering
====================================================
The decision layer that sits between detection and the outputs (dashboard,
voice, WhatsApp). It answers ONE question for every detection:

    "How loudly should we respond, and to whom?"

It does NOT suppress anything — every event is always logged (Tier 1 minimum).
The tier only decides added loudness: silent → spoken → escalate.

Design (agreed in planning):
  - Three factors scored and summed: TIME, ZONE, IDENTITY.
  - FAIL UPWARD: anything we can't classify scores HIGHER, never lower.
  - Tier 1 = silent (dashboard + db only)
  - Tier 2 = spoken at desk (Guardian voice), once
  - Tier 3 = urgent voice + WhatsApp to ops, both at once

It plugs into threat_detector.py's existing seam:
    from threat_detector import register_alert_callback
    from tiering_brain import handle_threat
    register_alert_callback(handle_threat)

------------------------------------------------------------------------------
HONEST NOTE ON CURRENT LIMITATIONS (read this):
Your ThreatEvent currently carries: threat_type, severity, description,
confidence, person_count, bbox, snapshot_path, timestamp.

It does NOT yet carry `zone` or `identity`. So:
  - TIME factor      → fully works (from timestamp)
  - THREAT-TYPE      → fully works (WEAPON/INTRUSION/CROWD/LOITER as risk proxy)
  - ZONE factor      → only works if you pass camera/zone in (see enrich_event)
  - IDENTITY factor  → only works for the ANPR/vehicle path (known vs unknown)

To light up ZONE fully later, add a `zone` field to ThreatEvent (one line).
Until then this scores on TIME + THREAT-TYPE, which already solves the core
"don't talk on every detection" problem. Nothing here pretends zone works
when it doesn't — unknown zone scores as MODERATE risk (fail-upward).
------------------------------------------------------------------------------
"""

import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("tiering")

# ── False-escalation tracking ────────────────────────────────────────────────
# Imported defensively: if escalation_metrics.py is missing, the alert path
# must still work. Metrics are never allowed to break an alarm.
try:
    from escalation_metrics import record_escalation as _record_escalation
except Exception:                                    # pragma: no cover
    _record_escalation = None
    logger.warning("escalation_metrics not available — false-escalation "
                   "tracking is OFF")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — tune these to your society. All in one place on purpose.
# ─────────────────────────────────────────────────────────────────────────────

# Deep-night window (24h clock). Your stated worst-case hours.
DEEP_NIGHT_START = 23   # 11 PM
DEEP_NIGHT_END   = 5    # 5 AM

# Zones considered CRITICAL (wall, basement, perimeter — NOT the gate, where
# the guard normally is). Match these to your camera/zone names.
CRITICAL_ZONES = {"wall", "east_wall", "boundary", "perimeter", "basement", "parking_rear"}
# Low-risk zones where presence is normal (the guard is there).
LOW_RISK_ZONES = {"gate", "main_gate", "lobby", "reception"}

# How each threat_type contributes to risk (identity/type proxy).
THREAT_TYPE_RISK = {
    "WEAPON":    3,   # always maximally serious
    "INTRUSION": 2,
    "CROWD":     2,
    "LOITER":    1,
}

# Tier thresholds on the summed score.
TIER2_THRESHOLD = 2   # score >= 2 → speak
TIER3_THRESHOLD = 4   # score >= 4 → escalate


# ─────────────────────────────────────────────────────────────────────────────
# The scored decision
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TierDecision:
    tier: int                 # 1, 2, or 3
    score: int
    reasons: list             # human-readable why, for the incident log
    is_deep_night: bool
    zone: str
    identity: str             # "known" / "unknown" / "unclear"

    @property
    def should_speak(self) -> bool:
        return self.tier >= 2

    @property
    def should_escalate(self) -> bool:
        return self.tier >= 3


# ─────────────────────────────────────────────────────────────────────────────
# Scoring factors
# ─────────────────────────────────────────────────────────────────────────────
def _is_deep_night(ts_iso: str) -> bool:
    """Deep night from the event's own timestamp (not server 'now')."""
    try:
        hour = datetime.fromisoformat(ts_iso).hour
    except Exception:
        # Can't read the time → fail upward: assume it COULD be night.
        return True
    if DEEP_NIGHT_START > DEEP_NIGHT_END:      # window crosses midnight
        return hour >= DEEP_NIGHT_START or hour < DEEP_NIGHT_END
    return DEEP_NIGHT_START <= hour < DEEP_NIGHT_END


def _time_score(is_night: bool) -> tuple[int, str]:
    return (2, "deep night") if is_night else (0, "daytime")


def _zone_score(zone: Optional[str]) -> tuple[int, str]:
    if not zone:
        # Unknown zone → fail upward: treat as moderate, never zero.
        return 1, "zone unknown (fail-upward)"
    z = zone.lower()
    if z in CRITICAL_ZONES:
        return 2, f"critical zone ({zone})"
    if z in LOW_RISK_ZONES:
        return 0, f"low-risk zone ({zone})"
    return 1, f"ordinary zone ({zone})"


def _identity_score(identity: Optional[str]) -> tuple[int, str]:
    if identity == "known":
        return 0, "known/registered"
    if identity == "unknown":
        return 2, "unknown/unregistered"
    # None or "unclear" → fail upward.
    return 1, "identity unclear (fail-upward)"


def _threat_type_score(threat_type: str) -> tuple[int, str]:
    risk = THREAT_TYPE_RISK.get(threat_type)
    if risk is None:
        # Unrecognized threat type → fail upward.
        return 2, f"unrecognized threat '{threat_type}' (fail-upward)"
    return risk, f"{threat_type.lower()} risk"


# ─────────────────────────────────────────────────────────────────────────────
# Main scoring
# ─────────────────────────────────────────────────────────────────────────────
def score_event(threat_type: str, timestamp: str,
                zone: Optional[str] = None,
                identity: Optional[str] = None) -> TierDecision:
    """
    Combine all factors into a tier. This is the whole brain.
    zone/identity are optional because the current ThreatEvent doesn't carry
    them yet — when absent they score fail-upward (moderate), never zero.
    """
    is_night = _is_deep_night(timestamp)

    reasons = []
    total = 0
    for fn, arg in (
        (_time_score, is_night),
        (_zone_score, zone),
        (_identity_score, identity),
        (_threat_type_score, threat_type),
    ):
        pts, why = fn(arg)
        total += pts
        if pts > 0:
            reasons.append(f"+{pts} {why}")

    # WEAPON is a hard override: always Tier 3 regardless of the rest.
    if threat_type == "WEAPON":
        return TierDecision(3, max(total, TIER3_THRESHOLD),
                            reasons + ["weapon override → Tier 3"],
                            is_night, zone or "unknown", identity or "unclear")

    if total >= TIER3_THRESHOLD:
        tier = 3
    elif total >= TIER2_THRESHOLD:
        tier = 2
    else:
        tier = 1

    return TierDecision(tier, total, reasons, is_night,
                        zone or "unknown", identity or "unclear")


# ─────────────────────────────────────────────────────────────────────────────
# The callback that plugs into threat_detector.register_alert_callback
# ─────────────────────────────────────────────────────────────────────────────

# These are injected by whoever wires this up (api_server.py), so the brain
# stays decoupled from HOW voice/whatsapp/incidents actually happen.
_voice_fn = None       # callable(text: str)               → Guardian speaks
_escalate_fn = None    # callable(event_dict, decision)     → whatsapp + incident


def configure(voice_fn=None, escalate_fn=None):
    """Wire the brain to real outputs. Call once at startup."""
    global _voice_fn, _escalate_fn
    _voice_fn = voice_fn
    _escalate_fn = escalate_fn


def handle_threat(event):
    """
    Registered via threat_detector.register_alert_callback(handle_threat).
    `event` is a ThreatEvent. Every event is already logged by fire_alert;
    here we only add loudness per tier.
    """
    # Pull optional zone/identity if a future ThreatEvent carries them.
    zone = getattr(event, "zone", None)
    identity = getattr(event, "identity", None)

    decision = score_event(
        threat_type=event.threat_type,
        timestamp=event.timestamp,
        zone=zone,
        identity=identity,
    )

    logger.info("Tier %d (score %d) for %s — %s",
                decision.tier, decision.score, event.threat_type,
                "; ".join(decision.reasons))

    if decision.tier == 1:
        # Silent. Dashboard + db already have it. NOT an escalation: no human
        # was disturbed, so it must not enter the false-escalation denominator.
        return decision

    # ── Log the escalation ───────────────────────────────────────────────
    # Tier 2+ means a human gets interrupted, which is the definition we
    # measure against. Logged here rather than downstream because this is
    # the one place that knows tier, zone and threat type together.
    #
    # channel reflects what the human actually receives:
    #   tier 2 → voice at the desk
    #   tier 3 → voice AND WhatsApp to ops
    #
    # NOTE: whatsapp_alerts.send_threat_alert() is deliberately NOT hooked.
    # The escalate_fn downstream calls it, so hooking both would count every
    # Tier 3 twice and halve your apparent false rate.
    if _record_escalation is not None:
        _row_id = _record_escalation(
            tier=decision.tier,
            trigger_type=(event.threat_type or "other").lower(),
            camera=getattr(event, "camera", None),
            zone=decision.zone if decision.zone != "unknown" else None,
            channel="voice+whatsapp" if decision.tier >= 3 else "voice",
            subject=getattr(event, "description", None),
        )
        # Stash the row id on the decision so guardian_wiring can link it to
        # the incident it is about to create. The escalation is logged before
        # the incident exists, so this is the only way the two ever get tied
        # together — and without the tie, a guard cannot pass a verdict.
        decision.escalation_row_id = _row_id

    # Tier 2+: speak
    if decision.should_speak and _voice_fn:
        spoken = _compose_spoken(event, decision)
        try:
            _voice_fn(spoken)
        except Exception as e:
            logger.error("voice_fn failed: %s", e)

    # Tier 3: escalate (whatsapp + tracked incident with ack loop)
    if decision.should_escalate and _escalate_fn:
        try:
            _escalate_fn(event.to_dict(), decision)
        except Exception as e:
            logger.error("escalate_fn failed: %s", e)

    return decision


def _compose_spoken(event, decision: TierDecision) -> str:
    """What Guardian says. Calm for Tier 2, urgent for Tier 3."""
    where = f" in the {decision.zone}" if decision.zone != "unknown" else ""
    if decision.tier >= 3:
        return (f"Alert, boss. {event.threat_type.title()}{where}. "
                f"{event.description}. Escalating now.")
    return (f"Heads up, boss. {event.threat_type.title()}{where}. "
            f"{event.description}.")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone self-test — proves the scoring with no dependencies.
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    def fake(threat, hour, zone=None, identity=None):
        ts = datetime.now().replace(hour=hour, minute=0).isoformat()
        d = score_event(threat, ts, zone, identity)
        print(f"{threat:9} {hour:02d}:00 zone={str(zone):12} id={str(identity):8}"
              f" → TIER {d.tier} (score {d.score}) | {'; '.join(d.reasons)}")

    print("\n=== Tiering brain self-test ===\n")
    fake("LOITER",    14, "gate",      "known")     # daytime, known, gate → silent
    fake("LOITER",    3,  "gate",      None)        # 3am gate, unclear → notice
    fake("INTRUSION", 14, "gate",      "known")     # daytime resident at gate
    fake("INTRUSION", 3,  "east_wall", "unknown")   # THE worst case → escalate
    fake("INTRUSION", 3,  None,        None)        # 3am, unknown zone+id → fail up
    fake("WEAPON",    14, "lobby",     "known")     # weapon override → escalate
    fake("CROWD",     23, "parking_rear", None)     # night crowd, critical zone
    print()
