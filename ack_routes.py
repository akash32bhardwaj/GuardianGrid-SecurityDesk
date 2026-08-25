r"""
ack_routes.py — DEFENDER OCTA Guard Accountability Loop (Wow #8)
-----------------------------------------------------------------
Completes the loop around ack_watchdog.py, which was built but never
wired. After this module, the flow is:

  HIGH/CRITICAL incident created (any source: face, gate, patterns)
        v  auto-registered with the watchdog (15s sweep — no detector
        v  code needs changing)
  Guard sees a RED BANNER on the dashboard -> taps ACKNOWLEDGE
        v  status -> IN_PROGRESS, response time logged
  No tap within 3 minutes?
        v  WhatsApp escalation to SECURITY_WHATSAPP ("second in command")
  Every ack + escalation logged -> /api/acks/stats feeds the weekly
  report's guard-performance numbers.

Routes (JWT-protected by the global guard):
  GET  /api/acks/pending       unacknowledged incidents + countdown
  POST /api/acks/acknowledge   {incident_id} -> ack + log response time
  GET  /api/acks/stats?days=7  avg response seconds, counts, escalations

Integration in api_server.py:
    from ack_routes import ack_bp, init_ack_loop
    init_ack_loop(base_dir=BASE_DIR)
    app.register_blueprint(ack_bp)
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
ack_bp = Blueprint("ack_routes", __name__)

_DB_PATH = "guardiangrid.db"
SWEEP_INTERVAL = 15          # seconds between auto-registration sweeps
DEEP_NIGHT_FROM, DEEP_NIGHT_TO = 23, 5
_TRACK_SEVERITIES = ("HIGH", "CRITICAL")


def _con():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_ack_loop(base_dir: str, start_threads: bool = True):
    global _DB_PATH
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")
    _ensure_table()
    try:
        import ack_watchdog
        # guardian_wiring.py also configures an escalate_fn (its
        # send_threat_alert path). We must coexist, not fight over the
        # single hook: wrap ack_watchdog.configure so that WHOEVER
        # installs a sender, our ack_log marker always runs first and
        # exactly one WhatsApp goes out.
        _orig_configure = ack_watchdog.configure

        def _chained_configure(their_fn):
            def _both(incident, reason):
                _mark_escalated(incident)          # our bookkeeping
                their_fn(incident, reason)          # their sender
            _orig_configure(_both)

        ack_watchdog.configure = _chained_configure
        # baseline handler (used unless guardian_wiring replaces it):
        _orig_configure(lambda inc, reason: (
            _mark_escalated(inc),
            _send_escalation_whatsapp(inc, reason)))
        if start_threads:
            ack_watchdog.start_watchdog()
            threading.Thread(target=_sweep_loop, daemon=True,
                             name="ack-sweep").start()
        logger.info("[ACK] accountability loop armed "
                    f"(window {ack_watchdog.ACK_WINDOW_DEFAULT}s)")
    except Exception as e:
        logger.error(f"[ACK] could not arm watchdog: {e}")


def _ensure_table():
    try:
        con = _con()
        con.execute(
            "CREATE TABLE IF NOT EXISTS ack_log ("
            " incident_id TEXT PRIMARY KEY,"
            " title TEXT, severity TEXT, camera TEXT,"
            " created_at TEXT, acked_at TEXT,"
            " response_seconds REAL, escalated INTEGER DEFAULT 0)")
        con.commit()
        con.close()
    except sqlite3.Error as e:
        logger.error(f"[ACK] table init failed: {e}")


# ════════════════════════════════════════════════════════════════════
# Incident store adapters (backend/incidents is the source of truth)
# ════════════════════════════════════════════════════════════════════

def _all_incidents():
    try:
        from backend.incidents.incident_models import get_all_incidents
        return get_all_incidents() or []
    except Exception as e:
        logger.warning(f"[ACK] get_all_incidents failed: {e}")
        return []


def _set_in_progress(iid: str) -> bool:
    """Flexible against update_incident signatures."""
    try:
        from backend.incidents.incident_models import update_incident
    except Exception as e:
        logger.error(f"[ACK] no update_incident: {e}")
        return False
    for attempt in (
        lambda: update_incident(iid, status="IN_PROGRESS"),
        lambda: update_incident(iid, {"status": "IN_PROGRESS"}),
        lambda: update_incident(incident_id=iid, status="IN_PROGRESS"),
    ):
        try:
            attempt()
            return True
        except TypeError:
            continue
        except Exception as e:
            logger.error(f"[ACK] update_incident error: {e}")
            return False
    return False


def _note(iid: str, msg: str):
    try:
        from backend.incidents.incident_models import add_note
        add_note(iid, operator="Guardian/ack-loop", message=msg)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# Auto-registration sweep — feeds the watchdog from ANY incident source
# ════════════════════════════════════════════════════════════════════

def _sweep_once():
    import ack_watchdog
    now = datetime.now()
    deep_night = now.hour >= DEEP_NIGHT_FROM or now.hour < DEEP_NIGHT_TO
    con = _con()
    cur = con.cursor()
    for inc in _all_incidents():
        iid = inc.get("incident_id")
        if not iid or inc.get("status") != "OPEN":
            continue
        if str(inc.get("severity", "")).upper() not in _TRACK_SEVERITIES:
            continue
        seen = cur.execute("SELECT 1 FROM ack_log WHERE incident_id=?",
                           (iid,)).fetchone()
        if seen:
            continue
        ack_watchdog.register_incident(inc, tier=3, deep_night=deep_night)
        cur.execute(
            "INSERT OR IGNORE INTO ack_log (incident_id, title, severity,"
            " camera, created_at) VALUES (?,?,?,?,?)",
            (iid, inc.get("title", ""), inc.get("severity", ""),
             inc.get("camera_name") or inc.get("camera") or "",
             now.strftime("%Y-%m-%d %H:%M:%S")))
        logger.info(f"[ACK] tracking {iid} ({inc.get('severity')})")
    con.commit()
    con.close()


def _sweep_loop():
    time.sleep(30)
    while True:
        try:
            _sweep_once()
        except Exception as e:
            logger.error(f"[ACK] sweep error: {e}")
        time.sleep(SWEEP_INTERVAL)


# ════════════════════════════════════════════════════════════════════
# Escalation — the watchdog calls this when nobody acknowledged
# ════════════════════════════════════════════════════════════════════

def _mark_escalated(incident: dict):
    """Bookkeeping only — always runs, regardless of who sends."""
    iid = incident.get("incident_id", "?")
    try:
        con = _con()
        con.execute("UPDATE ack_log SET escalated=1 WHERE incident_id=?",
                    (iid,))
        con.commit()
        con.close()
    except sqlite3.Error:
        pass


def _send_escalation_whatsapp(incident: dict, reason: str):
    iid = incident.get("incident_id", "?")
    try:
        import whatsapp_config as cfg
        from whatsapp_alerts import _send_whatsapp
        to = getattr(cfg, "SECURITY_WHATSAPP", "")
        msg = (f"\u26a0\ufe0f *DEFENDER OCTA \u2014 ESCALATION*\n\n"
               f"\U0001F6A8 {incident.get('title', 'Incident')}\n"
               f"\U0001F4CD {incident.get('camera_name') or incident.get('camera') or 'site'}"
               f" \u00b7 {incident.get('severity', '')}\n"
               f"\U0001F194 {iid}\n\n"
               f"Reason: {reason}.\n"
               f"No guard acknowledgment on the dashboard \u2014 "
               f"please check the live feed / call the gate.\n"
               f"_S&N GuardianGrid Security System_")
        r = _send_whatsapp(to, msg)
        logger.info(f"[ACK] escalated {iid}: sent={r.get('success')}")
    except Exception as e:
        logger.error(f"[ACK] escalation send failed for {iid}: {e}")


# ════════════════════════════════════════════════════════════════════
# Routes
# ════════════════════════════════════════════════════════════════════

@ack_bp.route("/api/acks/pending")
def acks_pending():
    try:
        import ack_watchdog
        with ack_watchdog._timers_lock:
            timers = dict(ack_watchdog._timers)
    except Exception:
        timers = {}
    now = time.time()
    open_by_id = {i.get("incident_id"): i for i in _all_incidents()
                  if i.get("status") == "OPEN"}
    out = []
    for iid, t in timers.items():
        inc = open_by_id.get(iid)
        if not inc:
            continue
        out.append({
            "incident_id": iid,
            "title": inc.get("title", "Incident"),
            "severity": inc.get("severity", ""),
            "camera": inc.get("camera_name") or inc.get("camera") or "",
            "seconds_left": max(0, int(t["ack_deadline"] - now)),
            "escalated": bool(t.get("escalated")),
        })
    out.sort(key=lambda x: x["seconds_left"])
    return jsonify({"success": True, "pending": out, "count": len(out)})


@ack_bp.route("/api/acks/acknowledge", methods=["POST"])
def acks_acknowledge():
    data = request.get_json(silent=True) or {}
    iid = str(data.get("incident_id") or "").strip()
    if not iid:
        return jsonify({"success": False,
                        "message": "incident_id required"}), 400
    operator = getattr(request, "auth_user", None) or {}
    who = operator.get("username", "guard")

    if not _set_in_progress(iid):
        return jsonify({"success": False,
                        "message": "could not update incident"}), 500
    _note(iid, f"Acknowledged from dashboard by {who}.")

    response_seconds = None
    try:
        con = _con()
        row = con.execute("SELECT created_at FROM ack_log WHERE"
                          " incident_id=?", (iid,)).fetchone()
        if row and row["created_at"]:
            created = datetime.strptime(row["created_at"],
                                        "%Y-%m-%d %H:%M:%S")
            response_seconds = round(
                (datetime.now() - created).total_seconds(), 1)
        con.execute(
            "UPDATE ack_log SET acked_at=?, response_seconds=? "
            "WHERE incident_id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             response_seconds, iid))
        con.commit()
        con.close()
    except sqlite3.Error as e:
        logger.warning(f"[ACK] log update failed: {e}")

    # feed the existing escalation-metrics system, defensively
    try:
        from escalation_metrics import record_acknowledgement
        try:
            record_acknowledgement(incident_id=iid,
                                   response_seconds=response_seconds)
        except TypeError:
            record_acknowledgement(iid)
    except Exception:
        pass

    return jsonify({"success": True, "incident_id": iid,
                    "response_seconds": response_seconds})


@ack_bp.route("/api/acks/stats")
def acks_stats():
    days = min(max(int(request.args.get("days", 7)), 1), 90)
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    since = (datetime.now()
             .replace(hour=0, minute=0, second=0, microsecond=0))
    from datetime import timedelta as _td
    since = (since - _td(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = _con()
        r = con.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) AS acked,"
            " SUM(escalated) AS escalated,"
            " AVG(response_seconds) AS avg_s,"
            " MAX(response_seconds) AS worst_s"
            " FROM ack_log WHERE created_at >= ?", (since,)).fetchone()
        con.close()
        return jsonify({
            "success": True, "days": days,
            "alerts_tracked": r["total"] or 0,
            "acknowledged": r["acked"] or 0,
            "escalated": r["escalated"] or 0,
            "avg_response_seconds":
                round(r["avg_s"], 1) if r["avg_s"] else None,
            "worst_response_seconds":
                round(r["worst_s"], 1) if r["worst_s"] else None,
        })
    except sqlite3.Error as e:
        return jsonify({"success": False, "message": f"DB error: {e}"}), 500


def brief_line(_d=None):
    """Guard-performance sentence for the morning brief, or None."""
    try:
        from datetime import timedelta as _td
        since = (datetime.now() - _td(days=1)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        con = _con()
        r = con.execute(
            "SELECT COUNT(*) AS n, AVG(response_seconds) AS avg_s,"
            " SUM(escalated) AS esc FROM ack_log WHERE created_at >= ?",
            (since,)).fetchone()
        con.close()
        if not r or not r["n"]:
            return None
        parts = [f"\U0001F46E Guard response: {r['n']} alert"
                 f"{'s' if r['n'] != 1 else ''} tracked"]
        if r["avg_s"]:
            parts.append(f"avg ack {int(r['avg_s'])}s")
        if r["esc"]:
            parts.append(f"\u26a0\ufe0f {r['esc']} escalated unanswered")
        else:
            parts.append("none escalated")
        return ", ".join(parts) + "."
    except Exception:
        return None
