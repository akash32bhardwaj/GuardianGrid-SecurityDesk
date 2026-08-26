r"""
canvas_routes.py — DEFENDER OCTA Incident Command Canvas (backend)
-------------------------------------------------------------------
One endpoint that assembles everything the Canvas needs to take over
the screen, and two action endpoints to close the loop from it.

  GET  /api/canvas/active
        The most urgent active incident (OPEN/IN_PROGRESS, HIGH or
        CRITICAL) + its response-clock state + an evidence strip built
        from vehicle_events around the incident time + subject context.
        Returns {"active": null} when the site is calm — the frontend
        shows the normal dashboard in that case.

  POST /api/canvas/resolve      {incident_id, resolution, note?}
        resolution: "RESOLVED" | "FALSE_ALARM"
        Marks the incident RESOLVED with an audit note. (Close-with-
        proof photo requirement arrives in the next build — the field
        `proof_image` is already accepted and stored when present.)

  POST /api/canvas/escalate_now {incident_id}
        Manual escalation button — guard asks for help immediately
        instead of waiting out the ack window.

Integration in api_server.py:
    from canvas_routes import canvas_bp, init_canvas
    init_canvas(base_dir=BASE_DIR)
    app.register_blueprint(canvas_bp)
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
canvas_bp = Blueprint("canvas_routes", __name__)

_DB_PATH = "guardiangrid.db"
EVIDENCE_WINDOW_MIN = 5          # minutes each side of the incident
_ACTIVE_SEVERITIES = ("HIGH", "CRITICAL")

# Static SOPs v1 — per severity. Later: per-site site_config.json key.
SOPS = {
    "CRITICAL": [
        "Acknowledge the alert",
        "View live feed of the area",
        "Dispatch guard to location immediately",
        "Call supervisor",
        "Do not resolve until area is physically verified",
    ],
    "HIGH": [
        "Acknowledge the alert",
        "Verify on live feed",
        "Send guard to check the area",
        "Resolve with note (photo proof preferred)",
    ],
}


def _con():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_canvas(base_dir: str):
    global _DB_PATH
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")


# ── incident store adapters (same source of truth as ack loop) ──────

def _all_incidents():
    try:
        from backend.incidents.incident_models import get_all_incidents
        return get_all_incidents() or []
    except Exception as e:
        logger.warning(f"[CANVAS] get_all_incidents failed: {e}")
        return []


def _resolve_incident(iid: str, note: str) -> bool:
    try:
        from backend.incidents.incident_models import update_incident, add_note
    except Exception as e:
        logger.error(f"[CANVAS] incident store unavailable: {e}")
        return False
    for attempt in (
        lambda: update_incident(iid, {"status": "RESOLVED"}),
        lambda: update_incident(iid, status="RESOLVED"),
    ):
        try:
            attempt()
            break
        except TypeError:
            continue
        except Exception as e:
            logger.error(f"[CANVAS] resolve failed: {e}")
            return False
    else:
        return False
    try:
        add_note(iid, operator="Guardian/canvas", message=note)
    except Exception:
        pass
    return True


def _ack_state(iid: str):
    """Countdown + escalation state from the watchdog, if tracked."""
    try:
        import ack_watchdog
        with ack_watchdog._timers_lock:
            t = ack_watchdog._timers.get(iid)
        if not t:
            return None
        return {
            "seconds_left": max(0, int(t["ack_deadline"] - time.time())),
            "escalated": bool(t.get("escalated")),
        }
    except Exception:
        return None


def _ack_log_row(iid: str):
    try:
        con = _con()
        r = con.execute("SELECT created_at, acked_at, response_seconds,"
                        " escalated FROM ack_log WHERE incident_id=?",
                        (iid,)).fetchone()
        con.close()
        return dict(r) if r else None
    except sqlite3.Error:
        return None


def _evidence_strip(camera: str, around_iso: str, plate: str = None):
    """vehicle_events within ±EVIDENCE_WINDOW_MIN of the incident —
    before/during/after context the operator would otherwise scrub for."""
    try:
        ts = datetime.strptime(str(around_iso).replace("T", " ")[:19],
                               "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        ts = datetime.now()
    lo = (ts - timedelta(minutes=EVIDENCE_WINDOW_MIN)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=EVIDENCE_WINDOW_MIN)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    try:
        con = _con()
        sql = ("SELECT plate, event, access, camera, image,"
               " REPLACE(timestamp,'T',' ') AS ts FROM vehicle_events"
               " WHERE REPLACE(timestamp,'T',' ') BETWEEN ? AND ?")
        args = [lo, hi]
        if plate:
            sql += (" ORDER BY CASE WHEN REPLACE(UPPER(plate),' ','')"
                    " LIKE ? THEN 0 ELSE 1 END, ts")
            args.append(f"%{str(plate).upper().replace(' ', '')}%")
        else:
            sql += " ORDER BY ts"
        rows = con.execute(sql + " LIMIT 12", args).fetchall()
        con.close()
        out = []
        for r in rows:
            img = r["image"]
            out.append({
                "plate": r["plate"], "event": r["event"],
                "access": r["access"], "camera": r["camera"],
                "timestamp": r["ts"],
                "image": f"/vehicle_image/{os.path.basename(img)}"
                         if img else None,
                "phase": ("before" if r["ts"] < around_iso[:19]
                                      .replace("T", " ")
                          else "after"),
            })
        return out
    except sqlite3.Error as e:
        logger.warning(f"[CANVAS] evidence query failed: {e}")
        return []


def _subject_context(inc: dict):
    """Who/what is involved — plate history + resident lookup."""
    ctx = {"plate": inc.get("plate_number"),
           "resident_name": inc.get("resident_name"),
           "flat_number": inc.get("flat_number"),
           "history": None}
    plate = inc.get("plate_number")
    if not plate:
        return ctx
    try:
        con = _con()
        r = con.execute(
            "SELECT COUNT(*) AS n, MIN(REPLACE(timestamp,'T',' ')) AS fs,"
            " MAX(REPLACE(timestamp,'T',' ')) AS ls FROM vehicle_events"
            " WHERE REPLACE(UPPER(plate),' ','') ="
            " REPLACE(UPPER(?),' ','')", (plate,)).fetchone()
        con.close()
        if r and r["n"]:
            ctx["history"] = {"sightings": r["n"],
                              "first_seen": r["fs"], "last_seen": r["ls"]}
    except sqlite3.Error:
        pass
    if not ctx["resident_name"]:
        try:
            from resident_db import lookup_plate
            lr = lookup_plate(plate)
            if isinstance(lr, dict) and lr.get("found"):
                ctx["resident_name"] = lr.get("resident_name")
                ctx["flat_number"] = lr.get("flat_number")
                ctx["status"] = lr.get("status")
        except Exception:
            pass
    return ctx


# ════════════════════════════════════════════════════════════════════
# Routes
# ════════════════════════════════════════════════════════════════════

@canvas_bp.route("/api/canvas/active")
def canvas_active():
    active = [i for i in _all_incidents()
              if i.get("status") in ("OPEN", "IN_PROGRESS")
              and str(i.get("severity", "")).upper() in _ACTIVE_SEVERITIES]
    if not active:
        return jsonify({"success": True, "active": None})

    # most urgent first: CRITICAL over HIGH, then newest created_at
    active.sort(key=lambda i: str(i.get("created_at", "")), reverse=True)
    active.sort(key=lambda i:
                0 if str(i.get("severity")).upper() == "CRITICAL" else 1)
    inc = active[0]

    iid = inc.get("incident_id")
    sev = str(inc.get("severity", "HIGH")).upper()
    created = inc.get("created_at", "")
    return jsonify({
        "success": True,
        "active": {
            "incident_id": iid,
            "title": inc.get("title", "Incident"),
            "description": inc.get("description") or "",
            "severity": sev,
            "status": inc.get("status"),
            "camera": inc.get("camera_name") or inc.get("camera") or "",
            "created_at": created,
            "evidence_image": inc.get("evidence_image"),
            "others_waiting": len(active) - 1,
            "ack": _ack_state(iid),
            "ack_log": _ack_log_row(iid),
            "sop": SOPS.get(sev, SOPS["HIGH"]),
            "subject": _subject_context(inc),
            "evidence": _evidence_strip(
                inc.get("camera_name") or "", created,
                inc.get("plate_number")),
        },
    })


@canvas_bp.route("/api/canvas/resolve", methods=["POST"])
def canvas_resolve():
    data = request.get_json(silent=True) or {}
    iid = str(data.get("incident_id") or "").strip()
    resolution = str(data.get("resolution") or "RESOLVED").upper()
    if resolution not in ("RESOLVED", "FALSE_ALARM"):
        resolution = "RESOLVED"
    note = str(data.get("note") or "").strip()[:300]

    # Discipline: a CRITICAL incident cannot close without a note —
    # the SOP says "physically verified"; the record must say by what.
    inc_check = next((i for i in _all_incidents()
                      if i.get("incident_id") == iid), None)
    if inc_check and str(inc_check.get("severity", "")).upper() == \
            "CRITICAL" and not note:
        return jsonify({"success": False,
                        "message": "A resolution note is required for "
                                   "CRITICAL incidents."}), 400
    proof = str(data.get("proof_image") or "").strip()[:300]
    if not iid:
        return jsonify({"success": False,
                        "message": "incident_id required"}), 400
    who = (getattr(request, "auth_user", None) or {}).get("username", "guard")
    audit = f"Closed from Canvas by {who} as {resolution}."
    if note:
        audit += f" Note: {note}"
    if proof:
        audit += f" Proof: {proof}"
    if not _resolve_incident(iid, audit):
        return jsonify({"success": False,
                        "message": "could not resolve incident"}), 500
    try:
        con = _con()
        con.execute(
            "CREATE TABLE IF NOT EXISTS canvas_resolutions ("
            " incident_id TEXT PRIMARY KEY, resolution TEXT, note TEXT,"
            " proof_image TEXT, resolved_by TEXT, resolved_at TEXT)")
        con.execute(
            "INSERT OR REPLACE INTO canvas_resolutions VALUES (?,?,?,?,?,?)",
            (iid, resolution, note, proof, who,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        con.close()
    except sqlite3.Error as e:
        logger.warning(f"[CANVAS] resolution log failed: {e}")
    return jsonify({"success": True, "incident_id": iid,
                    "resolution": resolution})


@canvas_bp.route("/api/canvas/<iid>/report.pdf")
def canvas_report(iid):
    """Prove-stage artifact: the incident report PDF."""
    from flask import send_file, abort
    import incident_report
    incident_report.init_report(_DB_PATH.rsplit("/", 1)[0]
                                if _DB_PATH.startswith("/data")
                                else os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "reports", "incidents")
    os.makedirs(out_dir, exist_ok=True)
    safe = "".join(c for c in iid if c.isalnum() or c in "-_")
    out = os.path.join(out_dir, f"incident_{safe}.pdf")
    if not incident_report.generate_pdf(iid, out):
        abort(404)
    return send_file(out, mimetype="application/pdf",
                     download_name=f"Incident_{safe}.pdf")


@canvas_bp.route("/api/canvas/escalate_now", methods=["POST"])
def canvas_escalate_now():
    data = request.get_json(silent=True) or {}
    iid = str(data.get("incident_id") or "").strip()
    inc = next((i for i in _all_incidents()
                if i.get("incident_id") == iid), None)
    if not inc:
        return jsonify({"success": False, "message": "not found"}), 404
    try:
        import ack_watchdog
        ack_watchdog._escalate(inc, "manual escalation from Canvas")
        with ack_watchdog._timers_lock:
            if iid in ack_watchdog._timers:
                ack_watchdog._timers[iid]["escalated"] = True
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500
