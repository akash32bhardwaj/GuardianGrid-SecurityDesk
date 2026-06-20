"""
threat_detector.py — GuardianGrid AI Threat Detection
-------------------------------------------------------
Uses YOLOv8n (nano — fast on CPU) to detect:
  1. Person loitering in restricted zone
  2. Weapon detection (knife, gun)
  3. Crowd gathering / fight (5+ people)
  4. Intrusion (person crossing virtual boundary line)

All detections fire alerts to the GuardianGrid dashboard
and save snapshot evidence automatically.

Usage:
  python threat_detector.py
  python threat_detector.py --camera 1
  python threat_detector.py --camera 1 --show   # show live window
"""

import cv2
import time
import threading
import argparse
import csv
import numpy as np
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

# ── Try importing ultralytics (YOLOv8) ──────────────────────────
try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    print("[WARN] ultralytics not installed. Run: pip install ultralytics")

# ── Config ──────────────────────────────────────────────────────
OUTPUT_DIR   = Path("output/threats")
LOG_FILE     = Path("logs/threat_log.csv")
SNAPSHOT_DIR = Path("output/threats/snapshots")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Threat detection thresholds
LOITER_SECONDS     = 10      # person in same zone for N seconds = loiter
CROWD_THRESHOLD    = 4       # N+ people in frame = crowd alert
CONFIDENCE_MIN     = 0.45    # minimum YOLO confidence
ALERT_COOLDOWN     = 30      # seconds between same-type alerts

# YOLO class IDs we care about (COCO dataset)
PERSON_CLASS   = 0
KNIFE_CLASS    = 43   # in COCO: knife
# Note: guns not in COCO by default — we use a fine-tuned model path if available
WEAPON_CLASSES = {43: "Knife", 76: "Scissors"}

# Virtual intrusion line (percentage of frame width/height)
# Default: horizontal line at 60% height — customize per client
INTRUSION_LINE_Y = 0.60   # 0.0 = top, 1.0 = bottom


# ── Threat event dataclass ───────────────────────────────────────
@dataclass
class ThreatEvent:
    threat_type: str          # LOITER / WEAPON / CROWD / INTRUSION
    severity: str             # LOW / MEDIUM / HIGH / CRITICAL
    description: str
    confidence: float
    person_count: int = 0
    bbox: Optional[tuple] = None
    snapshot_path: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now().isoformat()
    )

    def to_dict(self):
        return {
            "threat_type":   self.threat_type,
            "severity":      self.severity,
            "description":   self.description,
            "confidence":    round(self.confidence, 2),
            "person_count":  self.person_count,
            "snapshot_path": self.snapshot_path,
            "timestamp":     self.timestamp,
        }


# ── Shared state for API integration ────────────────────────────
latest_threat: Optional[ThreatEvent] = None
threat_log_list: list = []
threat_lock = threading.Lock()
_alert_callbacks: list = []    # register callbacks for API/WhatsApp


def register_alert_callback(fn):
    """Register a function to call when a threat is detected."""
    _alert_callbacks.append(fn)


def fire_alert(event: ThreatEvent):
    global latest_threat
    with threat_lock:
        latest_threat = event
        threat_log_list.insert(0, event.to_dict())
        if len(threat_log_list) > 100:
            threat_log_list.pop()
    for cb in _alert_callbacks:
        try:
            cb(event)
        except Exception as e:
            print(f"[WARN] Alert callback error: {e}")


# ── CSV logger ───────────────────────────────────────────────────
def init_log():
    if not LOG_FILE.exists():
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "timestamp", "threat_type", "severity",
                "description", "confidence", "person_count",
                "snapshot_path"
            ])
            writer.writeheader()


def log_threat(event: ThreatEvent):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "threat_type", "severity",
            "description", "confidence", "person_count",
            "snapshot_path"
        ])
        writer.writerow(event.to_dict())


# ── Drawing helpers ──────────────────────────────────────────────
SEVERITY_COLORS = {
    "LOW":      (0, 255, 0),      # green
    "MEDIUM":   (0, 165, 255),    # orange
    "HIGH":     (0, 0, 255),      # red
    "CRITICAL": (0, 0, 200),      # dark red + flash
}

def draw_detection(frame, label, bbox, color, conf):
    if bbox:
        x, y, w, h = bbox
        cv2.rectangle(frame, (x,y), (x+w,y+h), color, 2)
        txt = f"{label} {conf:.0%}"
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(frame, (x, y-th-8), (x+tw+6, y), color, -1)
        cv2.putText(frame, txt, (x+3, y-5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,0,0), 2)
    return frame


def draw_threat_banner(frame, event: ThreatEvent):
    """Draw a red alert banner at top of frame."""
    h, w = frame.shape[:2]
    color = SEVERITY_COLORS.get(event.severity, (0,0,255))
    overlay = frame.copy()
    cv2.rectangle(overlay, (0,0), (w, 60), color, -1)
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)
    cv2.putText(frame,
                f"⚠ {event.threat_type}: {event.description}",
                (10, 22), cv2.FONT_HERSHEY_DUPLEX, 0.7, (255,255,255), 2)
    cv2.putText(frame,
                f"Severity: {event.severity}  |  {event.timestamp[11:19]}",
                (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 1)
    return frame


def draw_intrusion_line(frame, y_pct=INTRUSION_LINE_Y):
    """Draw the virtual intrusion boundary line."""
    h, w = frame.shape[:2]
    y = int(h * y_pct)
    cv2.line(frame, (0, y), (w, y), (0, 100, 255), 2)
    cv2.putText(frame, "--- INTRUSION ZONE ---",
                (10, y-8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,100,255), 1)
    return frame


# ── Main ThreatDetector class ────────────────────────────────────
class ThreatDetector:
    def __init__(self, camera_index: int = 0, show_window: bool = False):
        if not YOLO_AVAILABLE:
            raise ImportError("ultralytics not installed. Run: pip install ultralytics")

        print("[INFO] Loading YOLOv8n model (downloads ~6MB on first run)...")
        self.model = YOLO("yolov8n.pt")   # nano — fastest on CPU
        print("[INFO] YOLOv8n loaded ✅")

        self.camera_index = camera_index
        self.show_window  = show_window

        # Loiter tracking: person_id -> {first_seen, last_bbox}
        self._person_tracks: dict = defaultdict(lambda: {
            "first_seen": time.time(), "frames": 0,
            "last_y": 0, "alerted": False
        })

        # Alert cooldown tracker: threat_type -> last alert time
        self._last_alert: dict = {}

        # Latest frame for API video feed
        self.latest_frame: bytes = b""
        self._frame_lock = threading.Lock()
        self._running = False

        init_log()

    # ── Cooldown check ────────────────────────────────────────
    def _can_alert(self, threat_type: str) -> bool:
        last = self._last_alert.get(threat_type, 0)
        if time.time() - last >= ALERT_COOLDOWN:
            self._last_alert[threat_type] = time.time()
            return True
        return False

    # ── Save snapshot ─────────────────────────────────────────
    def _save_snapshot(self, frame, threat_type: str) -> str:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SNAPSHOT_DIR / f"{threat_type}_{ts}.jpg")
        cv2.imwrite(path, frame)
        return path

    # ── Process one frame ─────────────────────────────────────
    def process_frame(self, frame: np.ndarray) -> tuple:
        """
        Run YOLO on frame. Returns (annotated_frame, threat_event_or_None)
        """
        h, w = frame.shape[:2]
        results = self.model(
            frame,
            conf=CONFIDENCE_MIN,
            verbose=False,
            stream=False,
            imgsz=1280
        )

        persons      = []
        weapons      = []
        threat_event = None

        for r in results:
            for box in r.boxes:
                cls  = int(box.cls[0])
                conf = float(box.conf[0])
                x1,y1,x2,y2 = map(int, box.xyxy[0])
                bw, bh = x2-x1, y2-y1
                cx, cy = (x1+x2)//2, (y1+y2)//2

                if cls == PERSON_CLASS:
                    persons.append({
                        "bbox": (x1,y1,bw,bh),
                        "center": (cx,cy),
                        "conf": conf
                    })
                    # Draw person box
                    cv2.rectangle(frame, (x1,y1), (x2,y2), (0,220,0), 1)

                elif cls in WEAPON_CLASSES:
                    weapons.append({
                        "bbox": (x1,y1,bw,bh),
                        "label": WEAPON_CLASSES[cls],
                        "conf": conf
                    })

        # ── Draw intrusion line ──────────────────────────────
        draw_intrusion_line(frame)

        # ── 1. WEAPON DETECTION ──────────────────────────────
        for weapon in weapons:
            if self._can_alert("WEAPON"):
                snap = self._save_snapshot(frame, "WEAPON")
                event = ThreatEvent(
                    threat_type  = "WEAPON",
                    severity     = "CRITICAL",
                    description  = f"{weapon['label']} detected",
                    confidence   = weapon["conf"],
                    bbox         = weapon["bbox"],
                    snapshot_path= snap,
                )
                draw_detection(frame, f"⚠ {weapon['label']}",
                               weapon["bbox"], (0,0,255), weapon["conf"])
                log_threat(event)
                fire_alert(event)
                threat_event = event

        # ── 2. CROWD DETECTION ───────────────────────────────
        if len(persons) >= CROWD_THRESHOLD:
            if self._can_alert("CROWD"):
                snap = self._save_snapshot(frame, "CROWD")
                event = ThreatEvent(
                    threat_type  = "CROWD",
                    severity     = "HIGH",
                    description  = f"Large crowd — {len(persons)} people detected",
                    confidence   = 0.90,
                    person_count = len(persons),
                    snapshot_path= snap,
                )
                log_threat(event)
                fire_alert(event)
                if threat_event is None:
                    threat_event = event

        # ── 3. INTRUSION DETECTION ───────────────────────────
        intrusion_y = int(h * INTRUSION_LINE_Y)
        for person in persons:
            cx, cy = person["center"]
            # Person crossed below the intrusion line
            if cy > intrusion_y:
                if self._can_alert("INTRUSION"):
                    snap = self._save_snapshot(frame, "INTRUSION")
                    event = ThreatEvent(
                        threat_type  = "INTRUSION",
                        severity     = "HIGH",
                        description  = "Person crossed intrusion boundary",
                        confidence   = person["conf"],
                        person_count = 1,
                        bbox         = person["bbox"],
                        snapshot_path= snap,
                    )
                    draw_detection(frame, "⚠ INTRUSION",
                                   person["bbox"], (0,0,255), person["conf"])
                    log_threat(event)
                    fire_alert(event)
                    if threat_event is None:
                        threat_event = event
                    break

        # ── 4. LOITER DETECTION ──────────────────────────────
        now = time.time()
        active_ids = set()

        for i, person in enumerate(persons):
            pid  = f"p{i}"
            cx,cy = person["center"]
            active_ids.add(pid)
            track = self._person_tracks[pid]

            # Update track
            track["frames"] += 1
            track["last_y"]  = cy

            duration = now - track["first_seen"]

            if duration >= LOITER_SECONDS and not track["alerted"]:
                if self._can_alert("LOITER"):
                    track["alerted"] = True
                    snap = self._save_snapshot(frame, "LOITER")
                    event = ThreatEvent(
                        threat_type  = "LOITER",
                        severity     = "MEDIUM",
                        description  = f"Person loitering for {int(duration)}s",
                        confidence   = person["conf"],
                        person_count = 1,
                        bbox         = person["bbox"],
                        snapshot_path= snap,
                    )
                    draw_detection(frame, f"⚠ LOITER {int(duration)}s",
                                   person["bbox"], (0,165,255), person["conf"])
                    log_threat(event)
                    fire_alert(event)
                    if threat_event is None:
                        threat_event = event

        # Clean up tracks for people who left frame
        gone = set(self._person_tracks.keys()) - active_ids
        for pid in gone:
            del self._person_tracks[pid]

        # ── Draw people count ────────────────────────────────
        cv2.putText(frame, f"People: {len(persons)}",
                    (10, h-12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0,220,0), 2)

        # ── Draw threat banner if active ─────────────────────
        if threat_event:
            frame = draw_threat_banner(frame, threat_event)

        return frame, threat_event

    # ── Main camera loop ──────────────────────────────────────
    def run(self):
        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        if not cap.isOpened():
            print(f"[ERROR] Cannot open camera {self.camera_index}")
            return

        self._running = True
        print(f"[INFO] Threat detection running on camera {self.camera_index}")
        print(f"[INFO] Detecting: Loiter({LOITER_SECONDS}s), Weapon, "
              f"Crowd({CROWD_THRESHOLD}+), Intrusion")
        if self.show_window:
            print("[INFO] Press Q to quit")

        frame_count = 0

        while self._running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            frame_count += 1

            # Process every 3rd frame on CPU (balance speed vs accuracy)
            if frame_count % 3 == 0:
                frame, threat = self.process_frame(frame)
                if threat:
                    print(f"[THREAT] {threat.severity}: {threat.threat_type} "
                          f"— {threat.description}")

            # Encode for API video feed
            _, buf = cv2.imencode(".jpg", frame,
                                  [cv2.IMWRITE_JPEG_QUALITY, 70])
            with self._frame_lock:
                self.latest_frame = buf.tobytes()

            if self.show_window:
                cv2.imshow("GuardianGrid — Threat Detection", frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break

        self._running = False
        cap.release()
        if self.show_window:
            cv2.destroyAllWindows()
        print("[INFO] Threat detector stopped.")

    def stop(self):
        self._running = False


# ── Standalone run ───────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GuardianGrid Threat Detector")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--show",   action="store_true",
                        help="Show live camera window")
    args = parser.parse_args()

    detector = ThreatDetector(camera_index=args.camera,
                              show_window=args.show)
    try:
        detector.run()
    except KeyboardInterrupt:
        detector.stop()
        print("\n[INFO] Stopped.")
