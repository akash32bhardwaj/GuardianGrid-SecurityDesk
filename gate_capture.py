r"""
gate_capture.py — DEFENDER OCTA "Octa Assist" guard capture
------------------------------------------------------------
For gates WITHOUT ANPR cameras: the guard photographs the vehicle or
person on their phone/tablet, and the photo enters the exact same
pipeline as a camera detection — vehicle_events / visitors, WhatsApp
alerts, search, reports.

Routes (JWT-protected by the global before_request guard):
  POST /api/gate/capture      multipart photo -> saved + plate OCR attempt
  POST /api/gate/log_vehicle  confirmed plate -> vehicle_events + alert
  POST /api/gate/log_person   visitor details -> visitors + notify

Integration in api_server.py:
  from gate_capture import gate_capture_bp, init_gate_capture
  init_gate_capture(base_dir=BASE_DIR)
  app.register_blueprint(gate_capture_bp)

Plate OCR: tries the project's own detect_image module first (several
common entry-point names), so whatever engine powers bulk_process is
reused here. If none is importable, capture still works — the guard
types the plate (2 extra taps, never a dead end).
"""

import logging
import os
import re
import sqlite3
from datetime import datetime

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
gate_capture_bp = Blueprint("gate_capture", __name__)

_BASE_DIR = "."
_DB_PATH = "guardiangrid.db"
_CAPTURE_DIR = "gate_captures"
_MAX_UPLOAD = 8 * 1024 * 1024      # 8 MB


_SNAP_DIR = "output/webcam"     # same dir the ANPR pipeline writes to;
                                # /vehicle_image/<name> serves from here


def init_gate_capture(base_dir: str):
    global _BASE_DIR, _DB_PATH, _CAPTURE_DIR, _SNAP_DIR
    _BASE_DIR = base_dir
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")
    _CAPTURE_DIR = os.path.join(base_dir, "gate_captures")
    _SNAP_DIR = os.path.join(base_dir, "output", "webcam")
    os.makedirs(_CAPTURE_DIR, exist_ok=True)
    os.makedirs(_SNAP_DIR, exist_ok=True)


# ── Plate OCR adapter — reuse whatever the project already has ──────

def _try_ocr(image_path: str):
    """Return (plate, confidence) or (None, 0). Never raises."""
    try:
        import detect_image as di
    except Exception:
        return None, 0
    for fn_name in ("detect_plate_from_image", "detect_plate", "read_plate",
                    "process_image", "detect", "run"):
        fn = getattr(di, fn_name, None)
        if not callable(fn):
            continue
        try:
            out = fn(image_path)
            # accept: "PB10AB1234" | ("PB10AB1234", 0.9) |
            #         {"plate": "...", "confidence": 0.9} | [{...}, ...]
            if isinstance(out, str) and out.strip():
                return _clean_plate(out), 0.8
            if isinstance(out, tuple) and out and isinstance(out[0], str):
                return _clean_plate(out[0]), float(out[1]) if len(out) > 1 else 0.8
            if isinstance(out, dict) and out.get("plate"):
                return _clean_plate(out["plate"]), float(out.get("confidence", 0.8))
            if isinstance(out, list) and out and isinstance(out[0], dict) \
                    and out[0].get("plate"):
                best = max(out, key=lambda d: d.get("confidence", 0))
                return _clean_plate(best["plate"]), float(best.get("confidence", 0.8))
        except Exception as e:
            logger.warning(f"OCR via detect_image.{fn_name} failed: {e}")
    return None, 0


def _clean_plate(p: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (p or "").upper())[:12]


def _lookup(plate: str) -> dict:
    """Resident lookup — defensive import so this file works standalone."""
    try:
        from resident_db import lookup_plate
        r = lookup_plate(plate)
        if isinstance(r, dict):
            return r
    except Exception:
        pass
    return {"found": False, "status": "UNKNOWN"}


def _save_upload(file, kind: str):
    """Vehicle photos -> output/webcam (so the Vehicles page and
    /vehicle_image serve them like any camera snapshot).
    Person photos -> gate_captures/<day>/."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if kind == "vehicle":
        folder = _SNAP_DIR
        name = f"gate_{stamp}{ext}"
    else:
        folder = os.path.join(_CAPTURE_DIR, datetime.now().strftime("%Y-%m-%d"))
        name = f"person_{stamp}{ext}"
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    file.save(path)
    return path


# ── Routes ──────────────────────────────────────────────────────────

@gate_capture_bp.route("/api/gate/capture", methods=["POST"])
def gate_capture():
    """Photo upload. kind=vehicle -> attempt plate OCR + lookup.
    kind=person -> just store; details come in log_person."""
    f = request.files.get("photo")
    if not f:
        return jsonify({"success": False, "message": "photo required"}), 400
    if request.content_length and request.content_length > _MAX_UPLOAD:
        return jsonify({"success": False, "message": "photo too large"}), 413
    kind = (request.form.get("kind") or "vehicle").lower()
    if kind not in ("vehicle", "person"):
        kind = "vehicle"

    path = _save_upload(f, kind)
    rel = os.path.relpath(path, _BASE_DIR)
    out = {"success": True, "image": rel, "kind": kind,
           "plate": None, "confidence": 0, "lookup": None}

    if kind == "vehicle":
        plate, conf = _try_ocr(path)
        out["plate"] = plate
        out["confidence"] = round(conf, 2)
        if plate:
            out["lookup"] = _lookup(plate)
    return jsonify(out)


@gate_capture_bp.route("/api/gate/log_vehicle", methods=["POST"])
def gate_log_vehicle():
    """Guard confirmed (or typed) the plate -> write the event + alert.
    Body: {plate, event: ENTRY|EXIT, image, camera?}"""
    data = request.get_json(silent=True) or {}
    plate = _clean_plate(data.get("plate", ""))
    if len(plate) < 4:
        return jsonify({"success": False, "message": "valid plate required"}), 400
    event = "EXIT" if str(data.get("event", "ENTRY")).upper() == "EXIT" else "ENTRY"
    image = str(data.get("image") or "")[:300]
    camera = str(data.get("camera") or "Gate Console")[:60]

    info = _lookup(plate)
    status = info.get("status", "UNKNOWN") if info.get("found") else "UNKNOWN"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # WHO logged this, from the session. Same rule as the gate pass and the
    # arrival admit: an audit field the caller can choose is not a record.
    operator = str((getattr(request, "auth_user", None) or {})
                   .get("username", "")).strip() or "guard"

    # ── Log through commit_vehicle_event, not around it ──────────────
    #
    # This used to run its own INSERT. Three things went wrong because of
    # it, every time a guard used the console:
    #
    #   access  was built here as {"KNOWN": "RESIDENT", "BLACKLISTED":
    #           "BLACKLIST"} instead of canon_access(), so the column
    #           gained RESIDENT and BLACKLIST next to the canonical
    #           KNOWN/BLACKLISTED. That is OCT-61 exactly -- "normalise on
    #           the way in, so the column stops accumulating new
    #           spellings" -- re-committed by the one path that bypassed
    #           the function holding the rule.
    #
    #   vtype   went in as the raw body value, so "car", "truck" and
    #           "bike" landed beside Car, Truck and Motorcycle and every
    #           grouping counted the same type twice.
    #
    #   state   got the RESIDENT STATUS. That column holds the number
    #           plate's issuing state -- Punjab, Haryana -- everywhere
    #           else. Two meanings in one column, which is OCT-50's shape:
    #           a value written in a vocabulary the column does not use.
    #
    #   and     vehicle_stats and entry_times were never updated, so a
    #           manually logged vehicle was invisible to the dashboard
    #           totals and to occupancy.
    #
    # Lazy import avoids a circular dependency: api_server imports this
    # module at startup, and by request time it is fully in sys.modules.
    event_id, committed = None, False
    try:
        import api_server as _srv
        rec = _srv.commit_vehicle_event(
            plate, event,
            vtype=data.get("vtype") or "Car",
            # The guard typed a plate; they did not tell us which state
            # issued it. Empty is what the camera path writes when it does
            # not know, and it is the majority value in the column.
            state=str(data.get("state") or ""),
            confidence=float(data.get("confidence") or 1.0),
            snapshot_path=image,
            operator=operator,
            camera=camera,
            # NO INCIDENT FROM THIS PATH, which is what happens today.
            #
            # commit_vehicle_event raises a MEDIUM "Unknown Vehicle
            # Entered" incident for an unregistered ENTRY. For the camera
            # that is right. Here the guard is standing at the gate making
            # a conscious decision, and REQUIRE_GUARD_DECISION means this
            # console is precisely where unknown vehicles get admitted --
            # so every delivery scooter and cab would raise an incident,
            # and ack_watchdog would start an escalation timer on each one.
            # Flooding a live society's escalation queue is a bigger
            # change than the bug being fixed.
            #
            # Whether a guard admitting an unregistered vehicle SHOULD
            # raise an incident is a real question, and a separate one.
            raise_unknown_incident=False,
        )
        event_id, committed = rec.get("id"), True
    except Exception as e:
        logger.error(f"commit_vehicle_event unavailable, falling back: {e}")

    if not committed:
        # api_server could not be reached. Still log it -- losing the event
        # is worse -- but normalise with the SAME functions, so the
        # fallback cannot reintroduce the bug above.
        try:
            from db import record_event
            from api_server import canon_access, canon_vtype
            event_id = record_event({
                "plate": plate, "type": canon_vtype(data.get("vtype") or "Car"),
                "state": str(data.get("state") or ""), "event": event,
                "confidence": float(data.get("confidence") or 1.0),
                "image": image, "timestamp": ts,
                "access": canon_access(status), "camera": camera,
            })
        except Exception as e:
            return jsonify({"success": False, "message": f"DB error: {e}"}), 500

    # Keep the live "inside" list in sync so the guard's existing Exit
    # buttons work on captured vehicles too. The activity_feed entry that
    # used to be here is gone: commit_vehicle_event already writes one, and
    # both running meant every gate-console event appeared in the feed
    # twice.
    try:
        import api_server as _srv
        with _srv.lock:
            if event == "ENTRY":
                _srv.gate_state[plate] = {"status": "INSIDE",
                                          "since": datetime.now().isoformat()}
            else:
                _srv.gate_state.pop(plate, None)
    except Exception as e:
        logger.warning(f"gate_state sync skipped: {e}")

    # Same alert path as camera detections — defensive, never blocks logging
    alert = None
    try:
        from whatsapp_alerts import send_vehicle_alert
        abs_img = os.path.join(_BASE_DIR, image) if image else ""
        alert = send_vehicle_alert(plate=plate, event=event,
                                   resident_info=info,
                                   snapshot_path=abs_img,
                                   trigger_type="gate_console",
                                   camera=camera)
    except Exception as e:
        logger.warning(f"gate alert failed: {e}")

    return jsonify({"success": True, "id": event_id, "plate": plate,
                    "event": event, "status": status, "timestamp": ts,
                    "alert_sent": bool(alert and alert.get("sent"))})


@gate_capture_bp.route("/api/gate/log_person", methods=["POST"])
def gate_log_person():
    """Visitor entry from the console.
    Body: {name, flat, purpose, phone?, image?}"""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:80]
    if not name:
        return jsonify({"success": False, "message": "name required"}), 400
    flat = str(data.get("flat") or "").strip()[:20]
    purpose = str(data.get("purpose") or "Visitor").strip()[:80]
    phone = str(data.get("phone") or "").strip()[:20]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        con = sqlite3.connect(_DB_PATH)
        cur = con.execute(
            "INSERT INTO visitors (name, flat, phone, purpose, in_time)"
            " VALUES (?,?,?,?,?)",
            (name, flat, phone, purpose, ts))
        con.commit()
        visitor_id = cur.lastrowid
        con.close()
    except sqlite3.Error as e:
        return jsonify({"success": False, "message": f"DB error: {e}"}), 500

    # Notify the flat owner if the project has visitor_notify wired
    try:
        import visitor_notify as vn
        for fn_name in ("notify_visitor", "send_visitor_alert", "notify"):
            fn = getattr(vn, fn_name, None)
            if callable(fn):
                fn(name=name, flat=flat, purpose=purpose)
                break
    except Exception:
        pass

    return jsonify({"success": True, "id": visitor_id, "name": name,
                    "flat": flat, "purpose": purpose, "timestamp": ts})
