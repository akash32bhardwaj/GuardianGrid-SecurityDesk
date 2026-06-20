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
import time
import re
import threading
import argparse
from pathlib import Path
from datetime import datetime
from collections import deque

from flask import Flask, Response, jsonify, send_file, abort, send_from_directory
from flask_cors import CORS

from core.anpr_engine import ANPREngine, PlateResult, PlateVoter
from resident_db import db as resident_db

try:
    from whatsapp_alerts import send_vehicle_alert
    WHATSAPP_AVAILABLE = True
except ImportError:
    WHATSAPP_AVAILABLE = False
    print("[WARN] whatsapp_alerts.py not found — WhatsApp alerts disabled")

# ── Config ──────────────────────────────────────────────────────
OUTPUT_DIR   = Path("output/webcam")
MIN_CONF     = 30
DEBOUNCE_SEC = 30
EXIT_MINUTES = 5

# Voting config — accumulate readings before confirming a plate.
# Window is wide because each detection can take 1-3s on CPU.
VOTE_WINDOW_SECONDS = 8.0
VOTE_MIN_SAMPLES    = 2

# ── Where the built React app lives ─────────────────────────────
# After running "npm run build" in your React project,
# copy the "dist" folder into indian_anpr and rename it "frontend"
FRONTEND_DIR = Path("frontend")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    static_folder=str(FRONTEND_DIR) if FRONTEND_DIR.exists() else None,
)
CORS(app)

# ── Shared state ─────────────────────────────────────────────────
lock = threading.Lock()

latest_detection = {
    "plate": "", "state": "", "type": "",
    "confidence": 0, "alert": "No vehicle detected",
    "timestamp": "", "event": "",
}

vehicle_stats = {
    "entries": 0, "exits": 0,
    "cars": 0, "motorcycles": 0, "buses": 0, "trucks": 0,
    "total": 0,
}

vehicle_log: deque = deque(maxlen=50)
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

        entry_time = entry_times.get(plate)
        if entry_time is None:
            event_type = "ENTRY"
            entry_times[plate] = now
            vehicle_stats["entries"] += 1
            vehicle_stats["total"]   += 1
            vtype = classify_vehicle_type(result.plate_label)
            if vtype == "Car":         vehicle_stats["cars"]        += 1
            elif vtype == "Motorcycle":vehicle_stats["motorcycles"] += 1
            elif vtype == "Bus":       vehicle_stats["buses"]       += 1
            elif vtype == "Truck":     vehicle_stats["trucks"]      += 1
        else:
            minutes = (now - entry_time).total_seconds() / 60
            if minutes >= EXIT_MINUTES:
                event_type = "EXIT"
                vehicle_stats["exits"] += 1
                del entry_times[plate]
            else:
                return

        vehicle_id = f"VH{len(vehicle_db)+1:04d}"
        vtype      = classify_vehicle_type(result.plate_label)
        record = {
            "vehicle_id": vehicle_id,
            "plate":      plate,
            "type":       vtype,
            "state":      result.state_name,
            "event":      event_type,
            "confidence": round(result.confidence, 1),
            "time":       now.strftime("%H:%M:%S"),
            "timestamp":  now.isoformat(),
            "image":      Path(snapshot_path).name if snapshot_path else "",
        }
        vehicle_log.appendleft(record)
        vehicle_db[plate] = record
        latest_detection.update({
            "plate":      plate,
            "state":      result.state_name,
            "type":       vtype,
            "confidence": round(result.confidence, 1),
            "alert":      f"{event_type}: {plate} ({result.state_name})",
            "timestamp":  now.isoformat(),
            "event":      event_type,
        })

    # ── Fire WhatsApp alert (outside lock, background thread) ──────
    if WHATSAPP_AVAILABLE:
        resident_info = resident_db.lookup(plate)
        if resident_info:
            resident_dict = {
                "found":         True,
                "status":        resident_info.status,
                "resident_name": resident_info.resident_name,
                "flat_number":   resident_info.flat_number,
                "block":         resident_info.block,
                "phone":         resident_info.phone,
                "notes":         resident_info.notes,
            }
        else:
            resident_dict = {"found": False, "status": "UNKNOWN"}

        threading.Thread(
            target=send_vehicle_alert,
            kwargs={
                "plate":         plate,
                "event":         event_type,
                "resident_info": resident_dict,
                "snapshot_path": snapshot_path,
            },
            daemon=True
        ).start()


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

    # Serve static file if it exists
    file_path = FRONTEND_DIR / path
    if path and file_path.exists():
        return send_from_directory(str(FRONTEND_DIR), path)

    # Otherwise serve index.html (React Router handles the rest)
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return send_file(str(index))

    abort(404)


# ── Main ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GuardianGrid Server")
    parser.add_argument("--camera",    type=int, default=0)
    parser.add_argument("--port",      type=int, default=5000)
    parser.add_argument("--no-camera", action="store_true")
    args = parser.parse_args()

    if not args.no_camera:
        threading.Thread(target=camera_thread,
                         args=(args.camera,), daemon=True).start()
        print(f"[INFO] Camera {args.camera} starting...")
        time.sleep(2)

    print(f"\n{'='*50}")
    print(f"  GuardianGrid is RUNNING")
    print(f"{'='*50}")
    print(f"  Dashboard : http://localhost:{args.port}")
    print(f"  API       : http://localhost:{args.port}/alerts")
    print(f"  Camera    : {'ON' if not args.no_camera else 'OFF'}")
    print(f"{'='*50}\n")

    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


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
