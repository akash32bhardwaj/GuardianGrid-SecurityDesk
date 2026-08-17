"""
person_detector.py — YOLO person/vehicle detection for Defender Octa
----------------------------------------------------------------------
Isolated, lazy-loaded detector for RTSP cameras. Kept separate from
the ANPR engine so it can't destabilise plate reading.

WHAT CHANGED (box-persistence rewrite)
--------------------------------------
Previously this module only exposed detect(), which returned a fully
ANNOTATED COPY of the frame. rtmp_proxy then re-displayed that copy
between YOLO runs — so the live stream froze on an old image for up to
2 seconds at a time.

Now detection and drawing are separated:

    detect_boxes(frame, mode)  ->  (boxes, counts)   # no drawing
    draw_boxes(frame, boxes)   ->  frame             # draws in place

rtmp_proxy caches `boxes` and redraws them onto every NEW frame, so the
video stays smooth and the boxes persist between inference runs.

detect() is kept as a backward-compatible wrapper so any other file that
still imports it will not break.

The model loads ONCE, on first call, so importing this file is cheap.
"""

import cv2
import numpy as np

_model = None                 # lazy-loaded YOLO model
_MODEL_NAME = "yolov8n.pt"    # 'n' = nano = smallest/fastest. Auto-downloads once.

# Which YOLO class IDs count as what (COCO dataset)
_PERSON_CLASSES  = {0}                     # person
_VEHICLE_CLASSES = {2, 3, 5, 7}            # car, motorcycle, bus, truck

_BOX_COLOR   = {"person": (0, 200, 255), "vehicle": (0, 220, 130)}
_CONF_THRESH = 0.35

# 640 is YOLOv8's native training size and is measurably faster on CPU
# than 736 with no meaningful accuracy loss at gate/parking distances.
_IMG_SIZE    = 640


def _load_model():
    global _model
    if _model is None:
        # This cloud CPU cannot initialize NNPACK. Disable that unavailable
        # backend before YOLO inference to prevent thousands of warnings.
        import torch
        torch.backends.nnpack.set_flags(False)

        from ultralytics import YOLO   # imported here so the app starts even if not installed
        print(f"[PERSON] Loading YOLO model ({_MODEL_NAME})…")
        _model = YOLO(_MODEL_NAME)
        print("[PERSON] YOLO ready.")
    return _model


def detect_boxes(frame, mode="person"):
    """
    Run detection on one frame and return the RESULTS ONLY — no drawing.

    mode: "person", "vehicle", or "person+vehicle".

    Returns (boxes, counts) where:
        boxes  = [ {"type": "person"|"vehicle",
                    "conf": 0.0-1.0,
                    "xyxy": (x1, y1, x2, y2)}, ... ]
        counts = {"persons": int, "vehicles": int}
    """
    want_person  = "person"  in mode
    want_vehicle = "vehicle" in mode
    wanted = set()
    if want_person:
        wanted |= _PERSON_CLASSES
    if want_vehicle:
        wanted |= _VEHICLE_CLASSES

    model = _load_model()
    # verbose=False keeps the console quiet; imgsz small = faster on CPU
    results = model.predict(frame, conf=_CONF_THRESH, imgsz=_IMG_SIZE, verbose=False)

    boxes = []
    counts = {"persons": 0, "vehicles": 0}

    if results:
        r = results[0]
        for box in r.boxes:
            cls = int(box.cls[0])
            if cls not in wanted:
                continue
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])

            if cls in _PERSON_CLASSES:
                label_type = "person"
                counts["persons"] += 1
            else:
                label_type = "vehicle"
                counts["vehicles"] += 1

            boxes.append({
                "type": label_type,
                "conf": conf,
                "xyxy": (x1, y1, x2, y2),
            })

    return boxes, counts


def draw_boxes(frame, boxes):
    """
    Draw a cached list of boxes onto `frame`. Modifies frame IN PLACE
    and also returns it for convenience.

    Safe to call every frame — it is pure OpenCV drawing, no inference.
    """
    if not boxes:
        return frame

    for b in boxes:
        label_type = b.get("type", "person")
        conf = b.get("conf", 0.0)
        x1, y1, x2, y2 = b.get("xyxy", (0, 0, 0, 0))
        color = _BOX_COLOR.get(label_type, (255, 255, 255))

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
        cv2.putText(frame, f"{label_type} {conf:.0%}",
                    (x1, max(y1 - 8, 16)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, color, 2, cv2.LINE_AA)

    return frame


def detect(frame, mode="person"):
    """
    BACKWARD-COMPATIBLE wrapper. Returns (annotated_frame, counts) exactly
    as the old version did. New code should use detect_boxes + draw_boxes.
    """
    boxes, counts = detect_boxes(frame, mode=mode)
    annotated = frame.copy()
    draw_boxes(annotated, boxes)
    return annotated, counts


class MotionGate:
    """
    Cheap frame-difference motion detector — only run YOLO when something
    actually moves, to save CPU on quiet cameras.

    Now downscales before differencing. A 1920x1080 grayscale + 21x21
    Gaussian blur per frame per camera was itself a real CPU cost; doing
    it at 480px wide is ~16x cheaper with the same behaviour, because the
    area threshold is scaled to match.

    Set DOWNSCALE_WIDTH = 0 to disable downscaling and get the old
    full-resolution behaviour back.
    """

    DOWNSCALE_WIDTH = 480

    def __init__(self, min_area=1500):
        self._prev = None
        self._min_area = min_area
        self._scaled_min_area = min_area   # recomputed on first frame
        self.last_activity_pct = 0.0       # % of frame in motion, last check

    def _prepare(self, frame):
        h, w = frame.shape[:2]
        if self.DOWNSCALE_WIDTH and w > self.DOWNSCALE_WIDTH:
            scale = self.DOWNSCALE_WIDTH / float(w)
            small = cv2.resize(frame, (self.DOWNSCALE_WIDTH, max(1, int(h * scale))),
                               interpolation=cv2.INTER_AREA)
            # area scales with the SQUARE of the linear scale factor
            self._scaled_min_area = max(20.0, self._min_area * (scale ** 2))
        else:
            small = frame
            self._scaled_min_area = float(self._min_area)

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (21, 21), 0)

    def has_motion(self, frame) -> bool:
        gray = self._prepare(frame)

        if self._prev is None:
            self._prev = gray
            self.last_activity_pct = 0.0
            return True   # first frame — assume motion so we prime detection

        delta = cv2.absdiff(self._prev, gray)
        self._prev = gray
        thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Expose how much of the frame is moving (%) — used by the tier-2
        # wake logs ("woke AI (activity 3.2%)") and the patrol widget.
        moving = sum(cv2.contourArea(c) for c in contours
                     if cv2.contourArea(c) >= self._scaled_min_area)
        total = float(gray.shape[0] * gray.shape[1]) or 1.0
        self.last_activity_pct = round(100.0 * moving / total, 1)

        return moving > 0
