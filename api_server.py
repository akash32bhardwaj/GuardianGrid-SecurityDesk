"""
api_server.py — GuardianGrid Backend + Frontend Server
--------------------------------------------------------
Serves BOTH the React frontend AND the API from one Flask server.
No need for separate Vite/Node server in production.

Usage:
  python api_server.py --camera 1
  python api_server.py --camera 1 --port 5000
"""

import cv2
import os
import sys
import json
import sqlite3
import subprocess
import time
import re
import threading
import argparse
from pathlib import Path
from site_config import CONFIG
from backup import run_daily_backup
from datetime import datetime, timedelta
from collections import deque
from backend.auth.auth_service import decode_token
from backend.incidents.incident_routes import register_incident_routes
from flask import Flask, Response, jsonify, send_file, abort, send_from_directory, request
from flask_cors import CORS
from db import init_db, record_event, hourly_stats as db_hourly, \
               daily_summary, events_for_date, vehicle_summary, \
               init_visitors, add_visitor, visitors_today, visitor_exit, \
               rebuild_today_state


from core.anpr_engine import ANPREngine, PlateResult, PlateVoter
from resident_db import db as resident_db	
from config import ADMIN_USERNAME, ADMIN_PASSWORD
from backend.auth.auth_routes import register_auth_routes
from backend.incidents.incident_service import create_new_incident
from rtmp_proxy import (
    init_rtsp_cams,
    rtmp_feed
)
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer
)

from reportlab.lib.styles import getSampleStyleSheet


try:
    from whatsapp_alerts import send_vehicle_alert
    WHATSAPP_AVAILABLE = True
except ImportError:
    WHATSAPP_AVAILABLE = False
    print("[WARN] whatsapp_alerts.py not found — WhatsApp alerts disabled")

# ── Config ──────────────────────────────────────────────────────
OUTPUT_DIR   = Path("output/webcam")
MIN_CONF     = CONFIG.min_confidence
DEBOUNCE_SEC = CONFIG.debounce_seconds
EXIT_MINUTES = CONFIG.exit_minutes

# Voting config — accumulate readings before confirming a plate.
# Window is wide because each detection can take 1-3s on CPU.
VOTE_WINDOW_SECONDS = CONFIG.vote_window_seconds
VOTE_MIN_SAMPLES    = CONFIG.vote_min_samples

# ── Where the built React app lives ─────────────────────────────
# After running "npm run build" in your React project,
# copy the "dist" folder into indian_anpr and rename it "frontend"
FRONTEND_DIR = Path("frontend")

# Absolute folder this file lives in — used by DB-reading routes so they
# work regardless of the current working directory.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    static_folder=str(FRONTEND_DIR) if FRONTEND_DIR.exists() else None,
)
CORS(app)


register_auth_routes(app)
register_incident_routes(app)
from guardian_ask import register_guardian_ask
register_guardian_ask(app)
from correction_routes import correction_bp
app.register_blueprint(correction_bp)
from resident_routes import resident_bp
app.register_blueprint(resident_bp)

# False-escalation tracking. Must be registered AFTER app exists (above) and
# BEFORE the route printout, so it appears in the startup route list.
from escalation_metrics import escalation_bp
app.register_blueprint(escalation_bp)

print("\nREGISTERED ROUTES:")
for rule in app.url_map.iter_rules():
    print(rule)
print()

# ── Authentication API ─────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()

    username = data.get("username", "").strip()
    password = data.get("password", "")

    if (
        username == ADMIN_USERNAME and
        password == ADMIN_PASSWORD
    ):
        return jsonify({
            "success": True,
            "token": f"gg-{username}-{int(time.time())}",
            "user": {
                "id": username,
                "name": "GuardianGrid Administrator",
                "role": "admin"
            }
        })

    return jsonify({
        "success": False,
        "error": "Invalid username or password"
    }), 401

# ── Shared state ─────────────────────────────────────────────────
lock = threading.Lock()

latest_detection = {
    "plate": "", "state": "", "type": "",
    "confidence": 0, "alert": "No vehicle detected",
    "timestamp": "", "event": "",
}

latest_resident = {}

@app.route("/resident_lookup")
def resident_lookup():

    return jsonify(
        latest_resident
    )

vehicle_stats = {
    "entries": 0, "exits": 0,
    "cars": 0, "motorcycles": 0, "buses": 0, "trucks": 0,
    "total": 0,
}

vehicle_log: deque = deque(maxlen=50)
activity_feed: deque = deque(maxlen=100)
notification_feed: deque = deque(maxlen=100)
vehicle_db:  dict  = {}
last_seen:   dict  = {}
entry_times: dict  = {}
latest_frame: bytes = b""
camera_running = False


# ── Helpers ──────────────────────────────────────────────────────
def classify_vehicle_type(plate_label: str) -> str:
    label = plate_label.lower()
    if "bus"   in label:                       return "Bus"
    if "truck" in label or "commercial" in label: return "Truck"
    if "motorcycle" in label:                  return "Motorcycle"
    return "Car"

# ── Guard-decision flow ──────────────────────────────────────────
# Unknown/blacklisted plates are NOT logged automatically; they wait
# for the guard's Entry/Hold/Exit in the Gate Console (post-correction).
# Residents & approved visitors still auto-log.
REQUIRE_GUARD_DECISION = True

pending_detections = {}   # normalized plate -> detection details

def _norm(p):
    return re.sub(r"[^A-Z0-9]", "", (p or "").upper())

def commit_vehicle_event(plate, event_type, *, vtype="Manual", state="",
                         confidence=100.0, snapshot_path="", operator="system"):
    """THE single place an event becomes real: stats, log, SQLite,
    activity feed, plus unknown-vehicle incident on ENTRY."""
    plate = re.sub(r"[^A-Z0-9]", "", (plate or "").upper())
    now = datetime.now()
    resident_info = resident_db.lookup(plate)
    access = resident_info.status if resident_info else "UNKNOWN"

    with lock:
        if event_type == "ENTRY":
            vehicle_stats["entries"] += 1
            vehicle_stats["total"]   += 1
            entry_times[plate] = now
            if vtype == "Car":          vehicle_stats["cars"]        += 1
            elif vtype == "Motorcycle": vehicle_stats["motorcycles"] += 1
            elif vtype == "Bus":        vehicle_stats["buses"]       += 1
            elif vtype == "Truck":      vehicle_stats["trucks"]      += 1
        elif event_type == "EXIT":
            vehicle_stats["exits"] += 1
            entry_times.pop(plate, None)

        record = {
            "vehicle_id": f"VH{len(vehicle_db)+1:04d}",
            "plate": plate, "type": vtype, "state": state,
            "event": event_type, "confidence": round(confidence, 1),
            "time": now.strftime("%H:%M:%S"),
            "timestamp": now.isoformat(),
            "image": Path(snapshot_path).name if snapshot_path else "",
            "access": access,
        }
        vehicle_log.appendleft(record)
        vehicle_db[plate] = record
        activity_feed.appendleft({
            "time": now.isoformat(),
            "event": f"{event_type} ({operator}): {plate}",
            "type": "vehicle",
        })
    record_event(record)

    # Unknown vehicle actually ENTERING → now it's incident-worthy
    if event_type == "ENTRY" and not resident_info:
        notification_feed.appendleft({
            "time": now.isoformat(),
            "title": "UNKNOWN VEHICLE ENTERED",
            "message": plate, "severity": "MEDIUM",
        })
        create_new_incident({
            "title": "Unknown Vehicle Entered",
            "description": f"Vehicle {plate} was allowed entry by {operator} but is not registered.",
            "severity": "MEDIUM", "camera_name": "Entry Gate",
            "evidence_image": Path(snapshot_path).name if snapshot_path else None,
            "plate_number": plate, "resident_name": "Unknown",
            "flat_number": "--", "confidence": round(confidence, 1),
        })
    return record

def process_entry_exit(result: PlateResult, snapshot_path: str = ""):
    plate = result.plate_number
    if not plate:
        return
    now = datetime.now()
    with lock:
        last = last_seen.get(plate)
        if last and (now - last).total_seconds() < DEBOUNCE_SEC:
            return
        last_seen[plate] = now
    vtype = classify_vehicle_type(result.plate_label)

    # Always update the live display panels
    resident_info = resident_db.lookup(plate)
    with lock:
        latest_detection.update({
            "plate": plate, "state": result.state_name, "type": vtype,
            "confidence": round(result.confidence, 1),
            "alert": f"DETECTED: {plate} ({result.state_name})",
            "timestamp": now.isoformat(), "event": "DETECTED",
        })
        if resident_info:
            latest_resident.update({
                "plate": plate, "name": resident_info.resident_name,
                "flat": resident_info.flat_number,
                "phone": resident_info.phone, "status": resident_info.status,
            })
        else:
            latest_resident.update({
                "plate": plate, "name": "Unknown Vehicle",
                "flat": "-", "phone": "-", "status": "UNKNOWN",
            })

    # Blacklisted → alert + incident IMMEDIATELY (never wait), but the
    # gate event itself still waits for the guard's decision.
    if resident_info and resident_info.status == "BLACKLISTED":
        notification_feed.appendleft({
            "time": now.isoformat(), "title": "BLACKLISTED VEHICLE",
            "message": plate, "severity": "HIGH",
        })
        activity_feed.appendleft({
            "time": now.isoformat(),
            "event": f"BLACKLISTED ALERT: {plate}", "type": "critical",
        })
        _bl_incident = create_new_incident({
            "title": "BLACKLISTED VEHICLE DETECTED",
            "description": f"Vehicle {plate} belongs to {resident_info.resident_name}. Reason: {resident_info.notes}",
            "severity": "HIGH", "camera_name": "Entry Gate",
            "evidence_image": Path(snapshot_path).name if snapshot_path else None,
            "plate_number": plate, "resident_name": resident_info.resident_name,
            "flat_number": resident_info.flat_number,
            "confidence": round(result.confidence, 1),
        })
        try:
            from guardian_wiring import on_blacklisted_vehicle
            on_blacklisted_vehicle(
                plate=plate,
                resident_name=resident_info.resident_name,
                reason=resident_info.notes or "",
                incident=_bl_incident,
            )
        except Exception as e:
            print(f"[WARN] Guardian blacklist hook failed: {e}")

    trusted = resident_info and resident_info.status in ("KNOWN", "VISITOR")

    if REQUIRE_GUARD_DECISION and not trusted:
        # Park it as pending — guard will correct (if needed) and decide.
        pending_detections[_norm(plate)] = {
            "vtype": vtype, "state": result.state_name,
            "confidence": round(result.confidence, 1),
            "snapshot": snapshot_path,
        }
        activity_feed.appendleft({
            "time": now.isoformat(),
            "event": f"AWAITING GUARD DECISION: {plate}", "type": "warning",
        })
        return

    # Trusted (resident/approved visitor) → auto entry/exit as before
    with lock:
        entry_time = entry_times.get(plate)
    if entry_time is None:
        event_type = "ENTRY"
    elif (now - entry_time).total_seconds() / 60 >= EXIT_MINUTES:
        event_type = "EXIT"
    else:
        return
    record = commit_vehicle_event(
        plate, event_type, vtype=vtype, state=result.state_name,
        confidence=result.confidence, snapshot_path=snapshot_path,
        operator="ANPR auto",
    )
    if WHATSAPP_AVAILABLE:
        threading.Thread(target=send_vehicle_alert, kwargs={
            "plate": plate, "event": event_type,
            "resident_info": {"found": True, "status": resident_info.status,
                              "resident_name": resident_info.resident_name,
                              "flat_number": resident_info.flat_number,
                              "block": resident_info.block,
                              "phone": resident_info.phone,
                              "notes": resident_info.notes},
            "snapshot_path": snapshot_path,
        }, daemon=True).start()


# ── Camera thread ─────────────────────────────────────────────────
def camera_thread(camera_index: int):
    global latest_frame, camera_running
    engine = ANPREngine(use_gpu=False)
    voter  = PlateVoter(window_seconds=VOTE_WINDOW_SECONDS,
                        min_samples=VOTE_MIN_SAMPLES)
    cap    = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    if not cap.isOpened():
        print(f"[ERROR] Cannot open camera {camera_index}")
        camera_running = False
        return

    camera_running = True
    frame_count     = 0
    processing      = False
    confirmed_result = None
    last_confirmed_plate = None

    def async_detect(frame_copy):
        nonlocal confirmed_result, processing, last_confirmed_plate
        try:
            r = engine.process_image(frame_copy)
            if r.detected and r.confidence >= MIN_CONF:
                voter.add(r)
                consensus = voter.get_consensus()
                if consensus and consensus.plate_number_raw != last_confirmed_plate:

                    # ── Fuzzy-snap to a known resident plate ──────────
                    # If this reading is 1 character off from a
                    # registered resident's plate, trust the
                    # registered plate instead (residents pass by
                    # repeatedly, so their plate is the "ground truth")
                    close_match = resident_db.fuzzy_lookup(
                        consensus.plate_number_raw, max_distance=1
                    )
                    if close_match and close_match.plate_number != consensus.plate_number_raw:
                        corrected_raw = close_match.plate_number
                        consensus.plate_number_raw = corrected_raw
                        consensus.plate_number = ANPREngine._format_plate(
                            corrected_raw, consensus.series
                        )
                        consensus.notes += " | snapped to known resident plate"

                    confirmed_result = consensus
                    last_confirmed_plate = consensus.plate_number_raw

                    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
                    snap = str(OUTPUT_DIR / f"{consensus.plate_number_raw}_{ts}.jpg")
                    cv2.imwrite(snap, frame_copy)
                    process_entry_exit(consensus, snap)
                    print(f"[CONFIRMED] {consensus.plate_number} "
                          f"({consensus.state_name}) {consensus.confidence:.0f}%")
        finally:
            processing = False

    print("[INFO] Camera started.")
    while camera_running:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue
        frame_count += 1
        if frame_count % 2 == 0 and not processing:
            processing = True
            threading.Thread(target=async_detect,
                             args=(frame.copy(),), daemon=True).start()

        # Clear confirmed result once the voter window expires
        # (plate has left the frame / no recent matching reads)
        if confirmed_result and not voter.get_consensus():
            confirmed_result = None
            last_confirmed_plate = None

        if confirmed_result and confirmed_result.detected and confirmed_result.bbox:
            frame = engine.draw_result(frame, confirmed_result)

        if confirmed_result and confirmed_result.detected:
            h, w = frame.shape[:2]
            overlay = frame.copy()
            cv2.rectangle(overlay, (0, h-60), (w, h), (0,0,0), -1)
            cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
            color_map = {"yellow":(0,215,255),"green":(0,220,0),
                         "black":(180,180,180),"white":(0,220,130)}
            color = color_map.get(confirmed_result.plate_type, (0,220,130))
            cv2.putText(frame, confirmed_result.plate_number,
                        (16, h-30), cv2.FONT_HERSHEY_DUPLEX, 1.2, color, 2)
            cv2.putText(frame,
                        f"{confirmed_result.state_name}  |  "
                        f"{confirmed_result.confidence:.0f}%  |  CONFIRMED",
                        (16, h-8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200,200,200), 1)

        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        with lock:
            latest_frame = buf.tobytes()
        time.sleep(0.03)

    cap.release()


# ── API Routes ────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "camera": camera_running,
                    "timestamp": datetime.now().isoformat()})

@app.route("/alerts")
def alerts():
    with lock:
        data = dict(latest_detection)
    conf = data.get("confidence", 0)
    data["threat_level"] = "HIGH" if conf>=90 else "MODERATE" if conf>=70 else "LOW"
    return jsonify(data)

@app.route("/vehicle_stats")
def vehicle_stats_route():
    with lock:
        return jsonify(dict(vehicle_stats))

@app.route("/activity_feed")
def activity_feed_route():
    with lock:
        return jsonify(list(activity_feed))

@app.route("/notifications")
def notifications_route():

    with lock:
        return jsonify(
            list(notification_feed)
        )

@app.route("/vehicle_log")
def vehicle_log_route():
    with lock:
        return jsonify(list(vehicle_log))

@app.route("/search_vehicle/<plate_query>")
def search_vehicle(plate_query):
    query = re.sub(r"[^A-Z0-9]", "", plate_query.upper())
    with lock:
        if query in vehicle_db:
            return jsonify(vehicle_db[query])
        for plate, record in vehicle_db.items():
            if query in plate.replace(" ", ""):
                return jsonify(record)
    return jsonify({"error": f"No vehicle found: '{plate_query}'"}), 404

@app.route("/vehicle_image/<filename>")
def vehicle_image(filename):
    filename = Path(filename).name
    img_path = OUTPUT_DIR / filename
    if img_path.exists():
        return send_file(str(img_path), mimetype="image/jpeg")
    abort(404)

@app.route("/video_feed")
def video_feed():
    def generate():
        while True:
            with lock:
                frame = latest_frame
            if frame:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + frame + b"\r\n")
            time.sleep(0.03)
    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/cam/<int:cam_id>")
def camera_stream(cam_id):
    return rtmp_feed(cam_id)

@app.route("/generate_report")
def generate_report():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from db import hourly_stats as db_hourly, access_mix, events_for_date
    from backend.incidents.incident_models import get_all_incidents
    import io

    day = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=18)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=colors.HexColor("#0e7490"))
    body = styles["BodyText"]
    story = []

    story.append(Paragraph("DEFENDER OCTA — Daily Security Report", h1))
    story.append(Paragraph(f"S&N GuardianGrid Technologies · {day} · Generated {datetime.now().strftime('%d %b %Y, %H:%M')}", body))
    story.append(Spacer(1, 14))

    # 1. Traffic summary
    hourly = db_hourly(day)
    tot_in = sum(h["entered"] for h in hourly)
    tot_out = sum(h["exited"] for h in hourly)
    peak = max(hourly, key=lambda h: h["entered"] + h["exited"])["h"] if hourly else "—"
    story.append(Paragraph("1. Traffic Summary", h2))
    story.append(Paragraph(
        f"Total entries: <b>{tot_in}</b> &nbsp;·&nbsp; Total exits: <b>{tot_out}</b> "
        f"&nbsp;·&nbsp; Peak hour: <b>{peak}</b>", body))
    story.append(Spacer(1, 8))

    # 2. Access mix
    story.append(Paragraph("2. Access Mix (who came through)", h2))
    mix = access_mix(day)
    mix_total = sum(m["count"] for m in mix) or 1
    tdata = [["Status", "Vehicles", "Share"]] + [
        [m["status"], str(m["count"]), f"{round(m['count']/mix_total*100)}%"] for m in mix]
    t = Table(tdata, colWidths=[6*cm, 3*cm, 3*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7490")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.append(t)
    verified = sum(m["count"] for m in mix if m["status"] in ("KNOWN", "VISITOR", "RESIDENT"))
    story.append(Paragraph(f"Verified traffic: <b>{round(verified/mix_total*100)}%</b>", body))
    story.append(Spacer(1, 8))

    # 3. Incidents
    story.append(Paragraph("3. Incidents & Case Files", h2))
    incidents = [i for i in get_all_incidents() if (i.get("created_at") or "").startswith(day)]
    if not incidents:
        story.append(Paragraph("No incidents recorded on this date.", body))
    else:
        idata = [["Case", "Title", "Plate", "Severity", "Status", "Operator"]] + [
            [i["incident_id"], (i.get("title") or "")[:34], i.get("plate_number") or "—",
             i.get("severity") or "—", (i.get("status") or "").replace("_", " "),
             i.get("operator") or "Unassigned"] for i in incidents]
        it = Table(idata, colWidths=[2*cm, 5.6*cm, 3*cm, 2*cm, 2.6*cm, 2.6*cm])
        it.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7490")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        story.append(it)
    story.append(Spacer(1, 8))

    # 4. Event log (last 30)
    story.append(Paragraph("4. Vehicle Event Log (latest 30)", h2))
    events = events_for_date(day, limit=30)
    if not events:
        story.append(Paragraph("No events recorded.", body))
    else:
        edata = [["Time", "Plate", "Type", "Event", "Conf."]] + [
            [e["timestamp"][11:19], e["plate"], e.get("type") or "—",
             e["event"], f"{e.get('confidence') or 0}%"] for e in events]
        et = Table(edata, colWidths=[2.6*cm, 4*cm, 3*cm, 2.6*cm, 2*cm])
        et.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7490")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        story.append(et)

    doc.build(story)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"defender-octa-report-{day}.pdf",
                     mimetype="application/pdf")

@app.route("/hourly_stats")
def hourly_stats_route():
    """?date=YYYY-MM-DD (default today) — hourly chart data."""
    return jsonify(db_hourly(request.args.get("date")))

@app.route("/calendar_summary")
def calendar_summary_route():
    """?year=2026&month=7 — per-day totals for the calendar."""
    now = datetime.now()
    year  = request.args.get("year",  now.year,  type=int)
    month = request.args.get("month", now.month, type=int)
    return jsonify(daily_summary(year, month))

@app.route("/events_by_date")
def events_by_date_route():
    """?date=YYYY-MM-DD — full event list for the drill-down."""
    date_str = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    return jsonify(events_for_date(date_str))

@app.route("/vehicle_summary")
def vehicle_summary_route():
    """Per-vehicle registry with real visit counts."""
    return jsonify(vehicle_summary())

@app.route("/inside_now")
def inside_now():
    """Every vehicle currently inside or on hold, with details."""
    out = []
    with lock:
        plates = dict(entry_times)          # inside (ANPR + manual)
        holds  = {p: s for p, s in gate_state.items() if s["status"] == "HOLD"}
        records = dict(vehicle_db)
    for plate, t in plates.items():
        rec = records.get(plate, {})
        r = resident_db.lookup(plate)
        out.append({
            "plate": plate,
            "since": t.isoformat(),
            "type": rec.get("type", "—"),
            "resident": r.resident_name if r else "Unknown",
            "flat": r.flat_number if r else "—",
            "access": r.status if r else "UNKNOWN",
            "state": "INSIDE",
        })
    for plate, s in holds.items():
        if plate in plates:
            continue
        r = resident_db.lookup(plate)
        out.append({
            "plate": plate, "since": s["since"], "type": "—",
            "resident": r.resident_name if r else "Unknown",
            "flat": r.flat_number if r else "—",
            "access": r.status if r else "UNKNOWN",
            "state": "HOLD",
        })
    out.sort(key=lambda x: x["since"], reverse=True)
    return jsonify(out)

@app.route("/camera_heat")
def camera_heat_route():
    from db import camera_heat
    return jsonify(camera_heat(request.args.get("date")))

@app.route("/cameras")
def cameras_route():
    from rtmp_proxy import RTSP_CAMERAS
    with lock:
        anpr_online = camera_running
    cams = [{
        "name": "CAM 01 — Main Gate (ANPR)",
        "status": "online" if anpr_online else "offline",
        "uptime": "—", "fps": 25 if anpr_online else 0,
        "stream": "/video_feed",
    }]
    for cam in RTSP_CAMERAS:
        if not cam.get("url"):        # skip cameras with no URL yet
            continue
        cams.append({
            "name": f"CAM {cam['id']:02d} — {cam['name']}",
            "status": "online", "uptime": "—", "fps": 25,
            "stream": f"/cam/{cam['id']}",
        })
    return jsonify(cams)

@app.route("/cameras/ai_status")
def cameras_ai_status():
    """Per-camera AI attention state for the patrol widget:
    tier (continuous/motion), awake, wakes (today), online, counts."""
    from rtmp_proxy import get_all_cam_stats
    return jsonify(get_all_cam_stats())

@app.route("/api/reports")
def list_reports():
    out = []
    rdir = "reports"
    if os.path.isdir(rdir):
        for f in sorted(os.listdir(rdir), reverse=True):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(rdir, f), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (json.JSONDecodeError, OSError):
                continue  # skip corrupt/partial files instead of crashing
    return jsonify(out[:30])

@app.route("/api/reports/<date>/pdf")
def report_pdf(date):
    safe = re.sub(r"[^0-9-]", "", date)
    path = os.path.join("reports", f"brief_{safe}.pdf")
    if not os.path.exists(path):
        return jsonify({"error": "report not found"}), 404
    return send_from_directory("reports", f"brief_{safe}.pdf")

@app.route("/api/day/<date>")
def day_detail(date):
    safe = re.sub(r"[^0-9-]", "", date)
    start, end = f"{safe} 00:00:00", f"{safe} 23:59:59"
    con = sqlite3.connect("guardiangrid.db")
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    incidents = [dict(r) for r in cur.execute(
        "SELECT COALESCE(incident_id,'GG-'||id) AS id, title, severity, status, "
        "created_at, camera_name FROM incidents "
        "WHERE REPLACE(created_at,'T',' ') BETWEEN ? AND ? ORDER BY created_at",
        (start, end))]
    events = [dict(r) for r in cur.execute(
        "SELECT plate, event, access, state, camera, timestamp FROM vehicle_events "
        "WHERE REPLACE(timestamp,'T',' ') BETWEEN ? AND ? "
        "ORDER BY timestamp DESC LIMIT 200",
        (start, end))]
    con.close()
    return jsonify({"date": safe, "incidents": incidents, "events": events})

# ── Face recognition alert sink ──────────────────────────────────
_face_alert_cooldown = {}   # "status:name" -> last alert epoch

@app.route("/internal/face_alert", methods=["POST"])
def internal_face_alert():
    import time as _t
    from site_config import CONFIG
    data = request.get_json() or {}
    name   = data.get("name", "UNKNOWN")
    status = data.get("status", "UNKNOWN")
    reason = data.get("reason", "")
    camera = data.get("camera", "Gate")
    snap   = data.get("snapshot", "")

    # de-dupe: one alert per person per cooldown window
    key = f"{status}:{name}"
    now = _t.time()
    cooldown = getattr(CONFIG, "face_cooldown", 60)
    if now - _face_alert_cooldown.get(key, 0) < cooldown:
        return jsonify({"ok": True, "skipped": "cooldown"})
    _face_alert_cooldown[key] = now

    title = ("WATCHLIST FACE DETECTED" if status == "WATCHLIST"
             else "UNKNOWN FACE DETECTED")
    sev = "HIGH" if status == "WATCHLIST" else "MEDIUM"
    desc = f"{title} at {camera}."
    if status == "WATCHLIST":
        desc += f" Identified as {name}."
        if reason:
            desc += f" Reason: {reason}."

    _face_incident = create_new_incident({
        "title": title,
        "description": desc,
        "severity": sev,
        "camera_name": camera,
        "evidence_image": snap or None,
        "plate_number": "--",
        "resident_name": name,
        "flat_number": "--",
        "confidence": 0,
    })

    # Log the escalation HERE rather than inside send_vehicle_alert, because
    # this is the only place that has both the alert and the incident id.
    # send_vehicle_alert is called in a thread below with record_metric=False
    # so it does not log a second, unlinked copy.
    try:
        from escalation_metrics import record_escalation as _rec_esc
        _rec_esc(
            incident_id=(_face_incident or {}).get("incident_id"),
            tier=3 if status == "WATCHLIST" else 2,
            trigger_type="face",
            camera=camera,
            zone="gate",
            channel="voice+whatsapp" if status == "WATCHLIST" else "whatsapp",
            subject=f"{name} ({status})",
        )
    except Exception as _e:
        print(f"[WARN] escalation log failed: {_e}")
    notification_feed.appendleft({
        "time": datetime.now().isoformat(),
        "title": title, "message": name, "severity": sev,
    })
    activity_feed.appendleft({
        "time": datetime.now().isoformat(),
        "event": f"{title}: {name}", "type": "critical",
    })

    if WHATSAPP_AVAILABLE and snap:
        try:
            threading.Thread(target=send_vehicle_alert, kwargs={
                "plate": name, "event": title,
                "resident_info": {"found": status == "WATCHLIST",
                                  "status": status,
                                  "resident_name": name,
                                  "flat_number": "--", "block": "",
                                  "phone": "", "notes": reason},
                "snapshot_path": snap,
                # Tag the escalation correctly. This path is face
                # recognition reusing the vehicle alert plumbing; without
                # these it would be logged as an ANPR escalation and the
                # breakdown-by-trigger view would be wrong.
                "trigger_type": "face",
                "camera": camera,
                "record_metric": False,   # already logged above, with the
                                          # incident id attached
            }, daemon=True).start()
        except Exception as e:
            print(f"[FACE] whatsapp error: {e}")

    return jsonify({"ok": True})

# ── Visitors ─────────────────────────────────────────────────────
@app.route("/visitors_today")
def visitors_today_route():
    return jsonify(visitors_today())

@app.route("/visitors", methods=["POST"])
def add_visitor_route():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400
    vid = add_visitor(name, (data.get("flat") or "").strip(),
                      (data.get("phone") or "").strip(),
                      (data.get("purpose") or "").strip())
    activity_feed.appendleft({
        "time": datetime.now().isoformat(),
        "event": f"VISITOR IN: {name} → {data.get('flat', '')}",
        "type": "visitor",
    })
    return jsonify({"success": True, "id": vid})

@app.route("/visitors/<int:vid>/exit", methods=["POST"])
def visitor_exit_route(vid):
    ok = visitor_exit(vid)
    return jsonify({"success": ok})

# ── API authentication guard ────────────────────────────────────
# Every route requires a valid JWT except the exempt list below.
AUTH_EXEMPT_PREFIXES = (
    "/api/auth/login",
    "/api/auth/test",
    "/video_feed",
    "/cam/",
    "/frontend",
    "/static",
    "/assets",           # React JS/CSS bundle (must load before login)
    "/favicon",          # favicon.svg
    "/icons",            # icons.svg
    "/logo",             # logo.png
    "/sounds",           # UI sound assets
    "/manifest",         # PWA manifest, if present
    "/robots",           # robots.txt, if present
    "/guardian",
    "/api/guardian/",
    "/internal/",        # rtmp_proxy posts face alerts here (local, tokenless)
)

@app.before_request
def require_auth():
    if request.method == "OPTIONS":          # CORS preflight
        return
    p = request.path
    if p == "/" or any(p.startswith(e) for e in AUTH_EXEMPT_PREFIXES):
        return
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.args.get("token", "")
    if not token:
        return jsonify({"success": False, "message": "Authentication required"}), 401
    try:
        request.auth_user = decode_token(token)   # available to routes if needed
    except Exception:
        return jsonify({"success": False, "message": "Invalid or expired token"}), 401

    # ── VIEWER role: read-only enforcement ─────────────────────────
    # Viewers (demo/QR visitors) may look but never touch:
    #   * every non-GET request is refused
    #   * the resident directory is refused even for reading — names,
    #     flats and phone numbers are not demo material
    # Structural rule in ONE place, so no route can forget to check.
    if (request.auth_user or {}).get("role") == "VIEWER":
        if request.method != "GET":
            return jsonify({"success": False,
                            "message": "Viewer access is read-only"}), 403
        if p.startswith("/residents"):
            return jsonify({"success": False,
                            "message": "Resident directory is not available "
                                       "to viewer accounts"}), 403

# ── Manual gate control ──────────────────────────────────────────
gate_state = {}   # plate → {"status": "INSIDE"/"HOLD", "since": iso}

@app.route("/gate/action", methods=["POST"])
def gate_action():
    data = request.get_json()
    plate    = (data.get("plate") or "").strip().upper()
    action   = (data.get("action") or "").upper()
    operator = data.get("operator", "guard")
    if not plate or action not in ("ENTRY", "HOLD", "EXIT"):
        return jsonify({"success": False, "error": "plate and valid action required"}), 400

    # Pull pending ANPR details — check the plate itself AND the last
    # raw detection (so a corrected plate inherits the misread's snapshot)
    pend = pending_detections.pop(_norm(plate), None)
    if pend is None and pending_detections:
        # correction case: adopt the most recent pending detection
        pend = pending_detections.pop(next(reversed(pending_detections)), None)
    meta = pend or {"vtype": "Manual", "state": "", "confidence": 100.0, "snapshot": ""}

    now = datetime.now()
    if action == "HOLD":
        with lock:
            gate_state[plate] = {"status": "HOLD", "since": now.isoformat()}
            activity_feed.appendleft({
                "time": now.isoformat(),
                "event": f"HOLD (manual by {operator}): {plate}", "type": "vehicle",
            })
        if pend:  # keep details for the eventual Entry/Exit
            pending_detections[_norm(plate)] = pend
        return jsonify({"success": True, "plate": plate, "action": action})

    if action == "ENTRY":
        with lock:
            gate_state[plate] = {"status": "INSIDE", "since": now.isoformat()}
    else:  # EXIT
        with lock:
            gate_state.pop(plate, None)

    commit_vehicle_event(
        plate, action, vtype=meta["vtype"], state=meta["state"],
        confidence=meta["confidence"], snapshot_path=meta["snapshot"],
        operator=f"manual by {operator}",
    )
    return jsonify({"success": True, "plate": plate, "action": action})

@app.route("/gate/inside")
def gate_inside():
    """Vehicles currently inside or on hold — powers the Exit buttons."""
    with lock:
        return jsonify([
            {"plate": p, **s} for p, s in gate_state.items()
        ])

@app.route("/access_mix")
def access_mix_route():
    from db import access_mix
    return jsonify(access_mix(request.args.get("date")))

# ── Serve React frontend ──────────────────────────────────────────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if not FRONTEND_DIR.exists():
        return (
            "<h2>GuardianGrid API is running ✅</h2>"
            "<p>To serve the dashboard here, copy your React "
            "<code>dist</code> folder into <code>indian_anpr/frontend/</code></p>"
            "<p>Run: <code>npm run build</code> in your React project first.</p>"
        ), 200

    # Vite is built with base '/frontend/', so asset URLs arrive as
    # "frontend/assets/...". FRONTEND_DIR is already the frontend folder,
    # so strip the leading "frontend/" to avoid frontend/frontend/ nesting.
    if path == "frontend" or path.startswith("frontend/"):
        path = path[len("frontend"):].lstrip("/")

    # Serve static file if it exists
    file_path = FRONTEND_DIR / path
    if path and file_path.exists():
        return send_from_directory(str(FRONTEND_DIR), path)

    # Otherwise serve index.html (React Router handles the rest)
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return send_file(str(index))

    abort(404)


# ── Threat Detection Integration ─────────────────────────────────
# Add these routes to your existing api_server.py
# Import at top: from threat_detector import ThreatDetector, threat_log_list, latest_threat, threat_lock

@app.route("/threat_status")
def threat_status():
    """Latest threat event — polled by frontend."""
    from threat_detector import latest_threat, threat_lock
    with threat_lock:
        if latest_threat:
            return jsonify(latest_threat.to_dict())
    return jsonify({"threat_type": None, "severity": "NONE",
                    "description": "All clear", "timestamp": ""})


@app.route("/threat_log")
def threat_log_route():
    """Last 50 threat events."""
    from threat_detector import threat_log_list, threat_lock
    with threat_lock:
        return jsonify(list(threat_log_list)[:50])


@app.route("/threat_snapshot/<filename>")
def threat_snapshot(filename):
    """Serve threat snapshot images."""
    from pathlib import Path
    snap_dir = Path("output/threats/snapshots")
    fname    = Path(filename).name
    img_path = snap_dir / fname
    if img_path.exists():
        return send_file(str(img_path), mimetype="image/jpeg")
    abort(404)


# ── AI Intelligence: 7-day threat forecast ───────────────────────
@app.route("/api/forecast")
def threat_forecast():
    """Next-7-days risk forecast from historical weekday x hour patterns."""
    lookback_days = 60
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(os.path.join(BASE_DIR, "guardiangrid.db"))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    buckets = {}   # (weekday, hour) -> weight
    days_seen = set()

    def add(ts, w):
        t = str(ts or "").replace("T", " ")
        try:
            dt = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return
        days_seen.add(t[:10])
        key = (dt.weekday(), dt.hour)
        buckets[key] = buckets.get(key, 0) + w

    try:
        for r in cur.execute(
            "SELECT timestamp, access, state FROM vehicle_events "
            "WHERE REPLACE(timestamp,'T',' ') >= ?", (since,)):
            a = f"{r['access'] or ''} {r['state'] or ''}".upper()
            if "BLACK" in a:
                add(r["timestamp"], 5)
            elif "UNKNOWN" in a or "UNREGISTER" in a:
                add(r["timestamp"], 2)
        for r in cur.execute(
            "SELECT created_at, severity FROM incidents "
            "WHERE REPLACE(created_at,'T',' ') >= ?", (since,)):
            sev = (r["severity"] or "").upper()
            add(r["created_at"], 6 if sev in ("CRITICAL", "HIGH") else 3)
    except sqlite3.Error:
        pass
    con.close()

    n_days = len(days_seen)
    max_w = max(buckets.values()) if buckets else 1
    out_days = []
    today = datetime.now()
    for i in range(1, 8):
        d = today + timedelta(days=i)
        wd = d.weekday()
        day_buckets = {h: w for (w_, h), w in buckets.items() if w_ == wd}
        total = sum(day_buckets.values())
        risk = min(100, round((total / max_w) * 60)) if max_w else 0
        peak_hour = max(day_buckets, key=day_buckets.get) if day_buckets else None
        def fmt(h):
            return f"{h % 12 or 12}{'AM' if h < 12 else 'PM'}"
        out_days.append({
            "date": d.strftime("%Y-%m-%d"),
            "day": d.strftime("%a"),
            "risk": risk,
            "level": "High" if risk >= 60 else "Medium" if risk >= 30 else "Low",
            "peak_window": f"{fmt(peak_hour)}\u2013{fmt((peak_hour + 2) % 24)}" if peak_hour is not None else None,
        })
    confidence = ("high" if n_days >= 30 else "medium" if n_days >= 14 else "low")
    return jsonify({
        "days": out_days,
        "days_of_data": n_days,
        "confidence": confidence,
        "note": f"Prediction based on {n_days} day(s) of site history. "
                f"Accuracy improves as monitoring data accumulates.",
    })


# ── AI Intelligence: live security score ─────────────────────────
# Computes the score on demand from the DB using the SAME formula as the
# daily brief (morning_report.collect + compute_score), so the live KPI
# and the written brief can never disagree. Cached for 60s so frontend
# polling doesn't hammer SQLite.
_live_score_cache = {"at": 0.0, "payload": None}

@app.route("/api/score/live")
def live_score():
    now = time.time()
    if _live_score_cache["payload"] and now - _live_score_cache["at"] < 60:
        return jsonify(_live_score_cache["payload"])
    hours = min(max(int(request.args.get("hours", 12) or 12), 1), 48)
    try:
        from morning_report import collect, compute_score
        d = collect(hours)
        score, label, color = compute_score(d)
        payload = {
            "score": score,
            "label": label,
            "color": color,
            "hours": hours,
            "vehicles_total": d.get("vehicles_total", 0),
            "vehicles_unknown": d.get("vehicles_unknown", 0),
            "vehicles_blacklisted": d.get("vehicles_blacklisted", 0),
            "incidents_total": d.get("incidents_total", 0),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "live": True,
        }
    except Exception as e:
        payload = {"score": None, "label": "Unavailable", "error": str(e), "live": False}
    _live_score_cache["at"] = now
    _live_score_cache["payload"] = payload
    return jsonify(payload)


# ── AI Intelligence: Smart Replay ────────────────────────────────
# Maps event timestamps to recorded segments. Folder convention comes from
# segment_recorder.py:  recordings\<Camera>\<YYYY-MM-DD>\seg_HH-MM-SS.mp4
# Each segment covers [start, start + REPLAY_SEGMENT_SECONDS).
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
REPLAY_SEGMENT_SECONDS = 600

# Events sometimes log under a different name than the recorded camera folder
# (e.g. incidents say "Entry Gate", ANPR webcam defaults to "Main Gate").
# Map logged-name -> recordings folder name here.
REPLAY_CAMERA_ALIASES = {
    "Entry Gate": "Main Gate",
}

def _replay_folder(camera):
    return REPLAY_CAMERA_ALIASES.get(camera, camera)

def _replay_safe(name):
    return re.sub(r"[^A-Za-z0-9 _\-\.]", "", str(name or "")).strip()

def _replay_segments(camera, date):
    """List segments for camera+date as [{file, start_iso, end_iso}], sorted."""
    day_dir = os.path.join(RECORDINGS_DIR, _replay_safe(_replay_folder(camera)), _replay_safe(date))
    out = []
    if not os.path.isdir(day_dir):
        return out
    for f in sorted(os.listdir(day_dir)):
        m = re.fullmatch(r"seg_(\d{2})-(\d{2})-(\d{2})\.mp4", f)
        if not m:
            continue
        try:
            start = datetime.strptime(f"{date} {m[1]}:{m[2]}:{m[3]}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append({
            "file": f,
            "start": start.isoformat(timespec="seconds"),
            "end": (start + timedelta(seconds=REPLAY_SEGMENT_SECONDS)).isoformat(timespec="seconds"),
        })
    return out

@app.route("/api/replay/segments")
def replay_segments():
    """?camera=Parking A&date=YYYY-MM-DD → all segments for that day."""
    camera = request.args.get("camera", "")
    date = _replay_safe(request.args.get("date", datetime.now().strftime("%Y-%m-%d")))
    return jsonify({"camera": camera, "date": date,
                    "segments": _replay_segments(camera, date)})

@app.route("/api/replay/for_event")
def replay_for_event():
    """?camera=Parking A&timestamp=2026-07-17T19:02:06 → the segment containing
    that moment, plus offset_seconds to seek to inside the clip."""
    camera = request.args.get("camera", "")
    ts_raw = str(request.args.get("timestamp", "")).replace("T", " ")[:19]
    try:
        ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return jsonify({"error": "timestamp must be YYYY-MM-DDTHH:MM:SS"}), 400
    date = ts.strftime("%Y-%m-%d")
    best = None
    for seg in _replay_segments(camera, date):
        start = datetime.fromisoformat(seg["start"])
        if start <= ts < start + timedelta(seconds=REPLAY_SEGMENT_SECONDS):
            best = {**seg,
                    "offset_seconds": int((ts - start).total_seconds()),
                    "url": f"/api/replay/clip/{_replay_safe(_replay_folder(camera))}/{date}/{seg['file']}"}
            break
    if not best:
        return jsonify({"found": False, "camera": camera, "timestamp": ts_raw,
                        "note": "No recorded segment covers this moment "
                                "(recorder not running, or camera not recorded)."}), 404
    return jsonify({"found": True, "camera": camera, "timestamp": ts_raw, **best})

@app.route("/api/replay/clip/<camera>/<date>/<filename>")
def replay_clip(camera, date, filename):
    """Serve one segment MP4 (streams in <video> tags / browser)."""
    camera, date, filename = _replay_safe(camera), _replay_safe(date), _replay_safe(filename)
    if not re.fullmatch(r"seg_\d{2}-\d{2}-\d{2}\.mp4", filename):
        abort(404)
    day_dir = os.path.join(RECORDINGS_DIR, camera, date)
    path = os.path.join(day_dir, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_from_directory(day_dir, filename, mimetype="video/mp4",
                               conditional=True)  # conditional → seek support


# ── AI Intelligence: security score watchdog ─────────────────────
# Background monitor: recomputes the live score every WATCHDOG_INTERVAL s.
# Triggers an alarm when the score crosses below WATCHDOG_MIN_SCORE, or
# drops by WATCHDOG_DROP_POINTS within WATCHDOG_DROP_WINDOW minutes.
# Alarm = console + booth voice (if wired) + WhatsApp (if Twilio env set)
# + /api/score/alerts for a dashboard banner. Cooldown prevents spam.
WATCHDOG_INTERVAL      = 60          # seconds between checks
WATCHDOG_MIN_SCORE     = 60          # absolute floor → alarm
WATCHDOG_DROP_POINTS   = 15          # relative drop → alarm
WATCHDOG_DROP_WINDOW   = 10          # minutes for the drop comparison
WATCHDOG_COOLDOWN      = 600         # seconds between repeated alarms
WATCHDOG_HOURS         = 2           # scoring window (match your test window)

_watchdog_state = {"history": deque(maxlen=120), "alerts": deque(maxlen=20),
                   "last_alarm": 0.0}

def _watchdog_alarm(kind, message, score):
    now = time.time()
    if now - _watchdog_state["last_alarm"] < WATCHDOG_COOLDOWN:
        return
    _watchdog_state["last_alarm"] = now
    alert = {"kind": kind, "message": message, "score": score,
             "at": datetime.now().isoformat(timespec="seconds")}
    _watchdog_state["alerts"].appendleft(alert)
    print(f"[WATCHDOG ALARM] {message}")
    try:
        from booth_voice import speak
        speak(f"Attention. {message}")
    except Exception:
        pass
    try:
        from morning_report import send_whatsapp
        send_whatsapp(f"🚨 GuardianGrid watchdog: {message}")
    except Exception:
        pass

def _watchdog_loop():
    from morning_report import collect, compute_score
    while True:
        try:
            d = collect(WATCHDOG_HOURS)
            score, label, _ = compute_score(d)
            now = time.time()
            hist = _watchdog_state["history"]
            hist.append((now, score))
            # relative drop check
            baseline = None
            for t, s in hist:
                if now - t >= WATCHDOG_DROP_WINDOW * 60:
                    baseline = s
                else:
                    break
            if score < WATCHDOG_MIN_SCORE:
                _watchdog_alarm(
                    "threshold",
                    f"Security score fell to {score} ({label}) — below alert floor "
                    f"{WATCHDOG_MIN_SCORE}. Review incidents now.", score)
            elif baseline is not None and baseline - score >= WATCHDOG_DROP_POINTS:
                _watchdog_alarm(
                    "drop",
                    f"Security score dropped {baseline - score} points in "
                    f"{WATCHDOG_DROP_WINDOW} minutes (now {score}). "
                    f"Threat activity rising.", score)
        except Exception as e:
            print(f"[WATCHDOG] check failed: {e}")
        time.sleep(WATCHDOG_INTERVAL)

@app.route("/api/score/alerts")
def score_alerts():
    """Recent watchdog alarms + current trend, for a dashboard banner."""
    hist = list(_watchdog_state["history"])[-30:]
    return jsonify({
        "alerts": list(_watchdog_state["alerts"]),
        "trend": [{"at": datetime.fromtimestamp(t).isoformat(timespec="seconds"),
                   "score": s} for t, s in hist],
        "thresholds": {"min_score": WATCHDOG_MIN_SCORE,
                       "drop_points": WATCHDOG_DROP_POINTS,
                       "drop_window_min": WATCHDOG_DROP_WINDOW},
    })


# ── AI Intelligence: daily highlight reels (smart_replay.py output) ──
# smart_replay.py writes:  reports\replays\replay_YYYY-MM-DD.mp4
#                          reports\replays\replay_YYYY-MM-DD.json  (metadata)
REEL_DIR = os.path.join(BASE_DIR, "reports", "replays")

@app.route("/api/replays")
def list_replays():
    """All generated reels, newest first: [{date, clips, duration_s}]."""
    out = []
    if os.path.isdir(REEL_DIR):
        for f in os.listdir(REEL_DIR):
            m = re.fullmatch(r"replay_(\d{4}-\d{2}-\d{2})\.mp4", f)
            if not m:
                continue
            date = m[1]
            meta = {"date": date, "clips": None, "duration_s": 0}
            jpath = os.path.join(REEL_DIR, f"replay_{date}.json")
            if os.path.isfile(jpath):
                try:
                    with open(jpath, encoding="utf-8") as jf:
                        j = json.load(jf)
                    meta["clips"] = j.get("clips")
                    meta["duration_s"] = j.get("duration_s", 0)
                except (json.JSONDecodeError, OSError):
                    pass
            out.append(meta)
    out.sort(key=lambda r: r["date"], reverse=True)
    return jsonify(out)

@app.route("/api/replays/<date>/video")
def replay_reel_video(date):
    """Serve one reel MP4 (seekable in <video> tags)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    fname = f"replay_{date}.mp4"
    if not os.path.isfile(os.path.join(REEL_DIR, fname)):
        abort(404)
    return send_from_directory(REEL_DIR, fname, mimetype="video/mp4",
                               conditional=True)


# ── Startup ───────────────────────────────────────────────────────
def bootstrap():
    """Run every startup step the app needs before serving requests.

    Called by __main__ (Flask dev server) and by serve.py (waitress).
    Keep all startup here — anything added below the __main__ guard
    instead would silently not run under the production server.
    """
    CONFIG.warn_if_insecure()

    init_db()
    init_visitors()

    # ── Guardian voice/alert integration ─────────────────────────
    try:
        from guardian_wiring import wire_guardian
        from booth_voice import speak
        wire_guardian(voice_fn=speak)
    except Exception as e:
        print(f"[WARN] Guardian wiring failed to start: {e}")

    if CONFIG.backup_enabled:
        run_daily_backup(CONFIG.backup_keep_days)

    threading.Thread(target=_watchdog_loop, daemon=True).start()
    print("[WATCHDOG] score monitor started "
          f"(floor {WATCHDOG_MIN_SCORE}, drop {WATCHDOG_DROP_POINTS} pts/"
          f"{WATCHDOG_DROP_WINDOW} min)")

    # ── Optional: auto-start segment recorder as a child process ──
    # site_config.json:  "recording": { "auto_start": true,
    #                                   "only": "Parking A,Parking B" }
    # Off by default. Production sites should keep using the Task Scheduler
    # job instead, so recording survives Flask restarts.
    try:
        with open(os.path.join(BASE_DIR, "site_config.json"), encoding="utf-8") as _f:
            _rec_cfg = json.load(_f).get("recording", {}) or {}
        if _rec_cfg.get("auto_start"):
            _rec_cmd = [sys.executable, os.path.join(BASE_DIR, "segment_recorder.py")]
            if _rec_cfg.get("only"):
                _rec_cmd += ["--only", str(_rec_cfg["only"])]
            _rec_proc = subprocess.Popen(_rec_cmd)
            import atexit
            atexit.register(
                lambda: _rec_proc.poll() is None and _rec_proc.terminate())
            print(f"[RECORDER] auto-started (pid {_rec_proc.pid})"
                  + (f" — only: {_rec_cfg['only']}" if _rec_cfg.get("only") else " — all cameras"))
    except Exception as _e:
        print(f"[WARN] recorder auto-start skipped: {_e}")

    _stats, _inside = rebuild_today_state()
    vehicle_stats.update(_stats)
    entry_times.update(_inside)
    for p in _inside:
        gate_state[p] = {"status": "INSIDE", "since": _inside[p].isoformat()}
    print(f"[DB] Restored: {_stats['entries']} in / {_stats['exits']} out / {len(_inside)} inside")

    if CONFIG.camera_enabled:
        threading.Thread(target=camera_thread,
                         args=(CONFIG.camera_index,), daemon=True).start()
        print(f"[INFO] Camera {CONFIG.camera_index} starting...")
        time.sleep(2)

    init_rtsp_cams(app)

    print(f"\n{'='*50}")
    print(f"  DEFENDER OCTA — {CONFIG.society_name}")
    print(f"  Site: {CONFIG.site_id}  |  {CONFIG.location}")
    print(f"{'='*50}")
    print(f"  Dashboard : http://localhost:{CONFIG.port}")
    print(f"  Camera    : {'ON' if CONFIG.camera_enabled else 'OFF'}")
    print(f"{'='*50}\n")


# ── Main (development server) ─────────────────────────────────────
# Production deployments should use serve.py (waitress) instead.
if __name__ == "__main__":
    bootstrap()
    app.run(host=CONFIG.host, port=CONFIG.port, debug=False, threaded=True)
