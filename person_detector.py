"""
person_detector.py — YOLO person/vehicle detection (proof of concept)
----------------------------------------------------------------------
Isolated, lazy-loaded detector for RTSP cameras. Kept separate from
the ANPR engine so it can't destabilise plate reading.

Usage (already wired in rtmp_proxy via the edit):
    from person_detector import detect, MotionGate
    annotated, counts = detect(frame, mode="person")

The model loads ONCE, on first call, so importing this file is cheap.
On CPU this is heavy — that's why rtmp_proxy motion-gates it and only
runs it every Nth frame. Prove it on ONE camera before enabling more.
"""

import cv2
import numpy as np

_model = None          # lazy-loaded YOLO model
_MODEL_NAME = "yolov8n.pt"   # 'n' = nano = smallest/fastest. Auto-downloads once.

# Which YOLO class IDs count as what (COCO dataset)
_PERSON_CLASSES  = {0}                     # person
_VEHICLE_CLASSES = {2, 3, 5, 7}            # car, motorcycle, bus, truck

_BOX_COLOR   = {"person": (0, 200, 255), "vehicle": (0, 220, 130)}
_CONF_THRESH = 0.40


def _load_model():
    global _model
    if _model is None:
        from ultralytics import YOLO   # imported here so the app starts even if not installed
        print(f"[PERSON] Loading YOLO model ({_MODEL_NAME})…")
        _model = YOLO(_MODEL_NAME)
        print("[PERSON] YOLO ready.")
    return _model


def detect(frame, mode="person"):
    """
    Run detection on one frame.
    mode: "person", "vehicle", or "person+vehicle".
    Returns (annotated_frame, counts_dict).
    """
    want_person  = "person"  in mode
    want_vehicle = "vehicle" in mode
    wanted = set()
    if want_person:  wanted |= _PERSON_CLASSES
    if want_vehicle: wanted |= _VEHICLE_CLASSES

    model = _load_model()
    # verbose=False keeps the console quiet; imgsz small = faster on CPU
    results = model.predict(frame, conf=_CONF_THRESH, imgsz=480, verbose=False)

    counts = {"persons": 0, "vehicles": 0}
    annotated = frame.copy()

    if results:
        r = results[0]
        for box in r.boxes:
            cls = int(box.cls[0])
            if cls not in wanted:
                continue
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])

            if cls in _PERSON_CLASSES:
                label_type = "person"; counts["persons"] += 1
            else:
                label_type = "vehicle"; counts["vehicles"] += 1

            color = _BOX_COLOR[label_type]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, f"{label_type} {conf:.0%}",
                        (x1, max(y1 - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, color, 1, cv2.LINE_AA)

    return annotated, counts


class MotionGate:
    """Cheap frame-difference motion detector — only run YOLO when
    something actually moves, to save CPU on quiet cameras."""

    def __init__(self, min_area=1500):
        self._prev = None
        self._min_area = min_area

    def has_motion(self, frame) -> bool:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        if self._prev is None:
            self._prev = gray
            return True   # first frame — assume motion so we prime detection
        delta = cv2.absdiff(self._prev, gray)
        self._prev = gray
        thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        return any(cv2.contourArea(c) >= self._min_area for c in contours)
