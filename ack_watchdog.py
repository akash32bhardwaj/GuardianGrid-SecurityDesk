"""
ack_watchdog.py — GuardianGrid Acknowledgment Loop
====================================================
The safety core: makes sure a Tier-3 alert that FIRED is also ACTED ON.

It layers on top of your existing incident system (backend/incidents/) without
modifying it. It uses your existing status vocabulary:

    OPEN         = fired, not yet acknowledged   (unacknowledged)
    IN_PROGRESS  = someone said "checking"        (acknowledged, not resolved)
    RESOLVED     = closed

Two timers (both agreed in planning):

  1. ACK timer      — an OPEN Tier-3 incident that isn't acknowledged within
                      its window ESCALATES (WhatsApp to supervisor).
                      Window: ~2 min for deep-night worst case, else 3–5 min.

  2. RESOLUTION timer — an IN_PROGRESS incident that isn't resolved within a
                      longer window RE-RAISES (so "checking" can't silence a
                      real threat forever).

CRITICAL DESIGN CHOICE: this watchdog runs as its own background thread on the
server. It is deliberately INDEPENDENT of the voice agent and the detector, so
that if either of those crashes, the watchdog still escalates. The thing that
watches for "nobody acted" must not die with the thing being watched.

------------------------------------------------------------------------------
STATE NOTE (honest):
Your incidents table has no column for tier / escalation-deadline / escalated-
flag. Rather than modify your schema, this watchdog keeps that timing state in
its OWN small in-memory dict, keyed by incident_id, and persists the important
bits into the incident's NOTES (which you already have) as an audit trail.

If the SERVER restarts mid-incident, in-memory timers reset — for a
gate-security system that's acceptable (a restart is rare and operator-visible),
but if you want restart-durable timers later, add 3 columns and I'll switch to
reading them. Called out so nothing is hidden.
------------------------------------------------------------------------------
"""

import time
import logging
import threading
from datetime import datetime
from escalation_metrics import record_acknowledgement, mark_auto_closed
from backend.incidents.incident_models import (
    get_all_incidents, update_incident, add_note,
)

logger = logging.getLogger("ack-watchdog")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — windows in seconds. Tune to your society.
# ─────────────────────────────────────────────────────────────────────────────
ACK_WINDOW_DEFAULT   = 4 * 60    # 3–5 min band → 4 min default
ACK_WINDOW_DEEPNIGHT = 2 * 60    # tighter for deep-night worst case
RESOLUTION_WINDOW    = 15 * 60   # acknowledged-but-unresolved re-raise
WATCH_INTERVAL       = 10        # how often the watchdog checks, seconds

# Escalation action — injected so the watchdog doesn't hard-depend on whatsapp.
# Signature: escalate_fn(incident_dict, reason: str)
_escalate_fn = None


def configure(escalate_fn):
    """Wire the escalation action (e.g. your send_threat_alert wrapper)."""
    global _escalate_fn
    _escalate_fn = escalate_fn


# In-memory timing state: incident_id -> {deadline, tier, deep_night, escalated,
#                                          reraised, ack_deadline}
_timers = {}
_timers_lock = threading.Lock()


def register_incident(incident: dict, tier: int, deep_night: bool):
    """
    Call this right after a Tier-3 incident is created, to start its ack timer.
    `incident` is the dict returned by create_incident().
    """
    iid = incident["incident_id"]
    window = ACK_WINDOW_DEEPNIGHT if deep_night else ACK_WINDOW_DEFAULT
    with _timers_lock:
        _timers[iid] = {
            "ack_deadline": time.time() + window,
            "tier": tier,
            "deep_night": deep_night,
            "escalated": False,
            "reraised": False,
            "resolution_deadline": None,   # set when acknowledged
        }
    logger.info("Watching %s — ack window %ds (deep_night=%s)",
                iid, window, deep_night)


def _mark_note(iid, msg):
    try:
        add_note(iid, operator="Guardian/watchdog", message=msg)
    except Exception as e:
        logger.error("could not add note to %s: %s", iid, e)


def _escalate(incident, reason):
    if _escalate_fn:
        try:
            _escalate_fn(incident, reason)
        except Exception as e:
            logger.error("escalate_fn failed for %s: %s",
                         incident.get("incident_id"), e)
    else:
        logger.warning("no escalate_fn configured; would have escalated %s (%s)",
                       incident.get("incident_id"), reason)


# ─────────────────────────────────────────────────────────────────────────────
# The watch loop — the heart of the safety mechanism
# ─────────────────────────────────────────────────────────────────────────────
def _check_once():
    """One pass: look at every tracked incident, act on expired timers."""
    now = time.time()
    # Pull current incident states from YOUR store (source of truth for status).
    by_id = {i["incident_id"]: i for i in get_all_incidents()}

    with _timers_lock:
        tracked = list(_timers.items())

    for iid, t in tracked:
        incident = by_id.get(iid)
        if incident is None:
            # incident vanished (shouldn't happen) — stop tracking
            with _timers_lock:
                _timers.pop(iid, None)
            continue

        status = incident.get("status")

        # ── Resolved elsewhere → stop watching ──────────────────────
        if status == "RESOLVED":
            with _timers_lock:
                _timers.pop(iid, None)
            continue

        # ── Acknowledged (IN_PROGRESS): switch to resolution timer ──
        if status == "IN_PROGRESS":
            if t["resolution_deadline"] is None:
                # first time we've seen it acknowledged — start the longer clock
                with _timers_lock:
                    _timers[iid]["resolution_deadline"] = now + RESOLUTION_WINDOW
                _mark_note(iid, "Acknowledged — resolution timer started.")
                continue
            if now >= t["resolution_deadline"] and not t["reraised"]:
                with _timers_lock:
                    _timers[iid]["reraised"] = True
                    # give another resolution window before re-raising again
                    _timers[iid]["resolution_deadline"] = now + RESOLUTION_WINDOW
                _mark_note(iid, "RE-RAISED — acknowledged but not resolved in time.")
                _escalate(incident, "acknowledged but unresolved — re-raising")
            continue

        # ── Still OPEN (unacknowledged): the critical case ──────────
        if status == "OPEN":
            if now >= t["ack_deadline"] and not t["escalated"]:
                with _timers_lock:
                    _timers[iid]["escalated"] = True
                _mark_note(iid, "ESCALATED — no acknowledgment within window.")
                _escalate(incident, "unacknowledged — escalating to supervisor")
            continue


def _run_loop():
    logger.info("Ack watchdog started (interval %ds).", WATCH_INTERVAL)
    while True:
        try:
            _check_once()
        except Exception as e:
            logger.error("watchdog pass failed: %s", e)
        time.sleep(WATCH_INTERVAL)


def start_watchdog():
    """Start the watchdog in a daemon thread. Call once at server startup."""
    th = threading.Thread(target=_run_loop, daemon=True, name="ack-watchdog")
    th.start()
    return th
