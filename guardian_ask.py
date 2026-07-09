"""
guardian_ask.py — GuardianGrid Voice Assistant Backend
========================================================
One endpoint: POST /api/guardian/ask   { "question": "any alerts today?" }
Returns:                                { "answer": "You have 3 open incidents..." }

The dashboard's voice box sends the guard's spoken question here (as text,
transcribed by the browser). This turns it into a real answer using your
EXISTING GuardianGrid data — incidents, stats, etc.

Two modes:
  1. INTENT mode (default, no API key needed): fast keyword routing to your
     data functions. Reliable, offline, free. Handles the common questions.
  2. LLM mode (optional): if an LLM key is configured, unclear questions fall
     through to the LLM for a natural answer. Set GUARDIAN_LLM=1 to enable.

Wire it in api_server.py:
    from guardian_ask import register_guardian_ask
    register_guardian_ask(app)
"""

import logging
from datetime import datetime
from flask import request, jsonify

logger = logging.getLogger("guardian-ask")

# Reuse YOUR existing data functions. Imported lazily inside handlers so a
# missing one degrades gracefully instead of breaking server startup.


def _get_incident_stats():
    from backend.incidents.incident_service import get_incident_stats
    return get_incident_stats()


def _get_all_incidents():
    from backend.incidents.incident_service import list_incidents
    return list_incidents()


def _today(iso):
    try:
        return datetime.fromisoformat(iso).date() == datetime.now().date()
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# INTENT ROUTING — maps a spoken question to a real answer.
# Each handler returns a short spoken-style string.
# ─────────────────────────────────────────────────────────────────────────────
def _answer_alerts(q):
    incidents = _get_all_incidents()
    open_ones = [i for i in incidents if i.get("status") == "OPEN"]
    today = [i for i in incidents if _today(i.get("created_at", ""))]
    if "today" in q:
        n = len(today)
        if n == 0:
            return "No incidents logged today, boss. All quiet."
        parts = [f"{i.get('title','incident')}" for i in today[:3]]
        more = f", and {n-3} more" if n > 3 else ""
        return f"{n} incident{'s' if n!=1 else ''} today: " + "; ".join(parts) + more + "."
    n = len(open_ones)
    if n == 0:
        return "No open alerts right now, boss. Everything's clear."
    top = open_ones[0]
    return (f"You have {n} open alert{'s' if n!=1 else ''}, boss. "
            f"Most recent: {top.get('title','incident')} "
            f"— {top.get('description','')[:80]}.")


def _answer_summary(q):
    s = _get_incident_stats()
    return (f"Here's your summary, boss. "
            f"{s.get('total',0)} total incidents: "
            f"{s.get('open',0)} open, "
            f"{s.get('in_progress',0)} being handled, "
            f"{s.get('resolved',0)} resolved.")


def _answer_status(q):
    s = _get_incident_stats()
    openc = s.get("open", 0)
    if openc == 0:
        return "All clear, boss. Nothing open needs attention."
    return f"{openc} open incident{'s' if openc!=1 else ''} still need attention, boss."


def _answer_help(q):
    return ("You can ask me: any alerts, alerts today, "
            "give me a summary, visitors today, how's the traffic, "
            "who's inside, or what's the status.")


# ── Vehicle traffic (entered / exited / inside) ────────────────
def _answer_traffic(q):
    stats = _vehicle_stats()
    entered = stats.get("entries", stats.get("entered", 0))
    exited  = stats.get("exits", stats.get("exited", 0))
    inside  = _inside_count()
    return (f"Traffic today, boss: {entered} vehicle{'s' if entered!=1 else ''} entered, "
            f"{exited} exited, and {inside} currently inside.")


def _answer_inside(q):
    rows = _inside_list()
    n = len(rows)
    if n == 0:
        return "Nobody's inside right now, boss. The premises are empty."
    names = []
    for r in rows[:3]:
        who = r.get("resident", "Unknown")
        plate = r.get("plate", "")
        names.append(f"{who} ({plate})" if who and who != "Unknown" else plate)
    more = f", and {n-3} more" if n > 3 else ""
    return f"{n} inside right now, boss: " + "; ".join(names) + more + "."


def _answer_visitors(q):
    visitors = _visitors_today()
    n = len(visitors) if isinstance(visitors, list) else 0
    if n == 0:
        return "No visitors logged today, boss."
    return f"{n} visitor{'s' if n!=1 else ''} logged today, boss."


# ── Internal data access (no network, no auth — same data the routes serve) ──
def _vehicle_stats():
    try:
        import api_server
        with api_server.lock:
            return dict(api_server.vehicle_stats)
    except Exception as e:
        logger.error("vehicle_stats read failed: %s", e)
        return {}


def _inside_count():
    try:
        import api_server
        with api_server.lock:
            return len(api_server.entry_times)
    except Exception:
        return 0


def _inside_list():
    try:
        import api_server
        with api_server.lock:
            plates = dict(api_server.entry_times)
            records = dict(api_server.vehicle_db)
        out = []
        for plate, t in plates.items():
            rec = records.get(plate, {})
            r = api_server.resident_db.lookup(plate)
            out.append({
                "plate": plate,
                "resident": r.resident_name if r else "Unknown",
                "flat": r.flat_number if r else "-",
            })
        return out
    except Exception as e:
        logger.error("inside_list read failed: %s", e)
        return []


def _visitors_today():
    try:
        import api_server
        return api_server.visitors_today()
    except Exception as e:
        logger.error("visitors_today read failed: %s", e)
        return []


# order matters — first match wins
_INTENTS = [
    (("alert", "incident", "threat"), _answer_alerts),
    (("summary", "analytic", "report", "overview"), _answer_summary),
    (("traffic", "vehicle", "entered", "exited", "how many car"), _answer_traffic),
    (("inside", "who's in", "whos in", "present", "on premises"), _answer_inside),
    (("visitor", "guest"), _answer_visitors),
    (("status", "clear", "anything open", "all good"), _answer_status),
    (("help", "what can you", "commands"), _answer_help),
]


def _route(question: str) -> str:
    q = (question or "").lower().strip()
    if not q:
        return "I didn't catch that, boss. Could you say it again?"
    for keywords, handler in _INTENTS:
        if any(k in q for k in keywords):
            try:
                return handler(q)
            except Exception as e:
                logger.error("intent handler failed: %s", e)
                return "I hit a snag pulling that up, boss. Try again in a moment."
    # No intent matched — optional LLM fallback
    return _llm_fallback(question)


def _llm_fallback(question: str) -> str:
    """Optional: send unclear questions to an LLM with data context.
    Only used if GUARDIAN_LLM=1 and a key is set. Otherwise a helpful nudge."""
    import os
    if os.getenv("GUARDIAN_LLM") != "1":
        return ("I'm not sure how to answer that yet, boss. "
                "Try asking about alerts, a summary, or the status.")
    # Placeholder for LLM wiring — kept minimal on purpose.
    try:
        stats = _get_incident_stats()
        # (You can wire your Sarvam/Gemini call here, passing `stats` as context.)
        return ("I can only give data answers for now, boss. "
                f"Currently {stats.get('open',0)} open incidents.")
    except Exception:
        return "I couldn't reach the data just now, boss."


# ─────────────────────────────────────────────────────────────────────────────
# The endpoint
# ─────────────────────────────────────────────────────────────────────────────
def register_guardian_ask(app):
    @app.route("/api/guardian/ask", methods=["POST"])
    def guardian_ask():
        try:
            data = request.get_json(silent=True) or {}
            question = data.get("question", "")
            logger.info("Guardian asked: %r", question)
            answer = _route(question)
            if not answer:
                answer = "I didn't find anything to say about that, boss."
            return jsonify({"answer": answer, "question": question})
        except Exception as e:
            # Never return an empty/500 — the UI shows a blank otherwise.
            import traceback
            logger.error("guardian_ask failed: %s\n%s", e, traceback.format_exc())
            return jsonify({
                "answer": f"I hit an error pulling that up, boss. ({e})",
                "question": data.get("question", "") if 'data' in dir() else "",
            })

    @app.route("/guardian", methods=["GET"])
    def guardian_assistant_page():
        """Serve the voice assistant from the SAME origin as the API, so the
        browser doesn't block the fetch (fixes 'Couldn't reach Guardian')."""
        from pathlib import Path
        from flask import Response
        html_path = Path("guardian_assistant.html")
        if html_path.exists():
            return Response(html_path.read_text(encoding="utf-8"),
                            mimetype="text/html")
        return Response("guardian_assistant.html not found in project root.",
                        status=404)

    logger.info("Guardian ask endpoint registered at /api/guardian/ask")
    logger.info("Guardian assistant page at /guardian")
