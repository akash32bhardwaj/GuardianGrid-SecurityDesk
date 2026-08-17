"""
rtmp_proxy.py
--------------
Reads each camera's RTSP stream, runs motion-triggered AI detection,
records to disk via CamRecorderManager, and re-serves as MJPEG.

CHANGE LOG
----------
v2 — box persistence:
    Every frame read from the camera is streamed (no more frozen images).
    The last detection's BOXES are cached and redrawn onto each new frame,
    so boxes persist smoothly between inference runs.

v3 — offline handling (this file):
    * Connect/read timeouts now ACTUALLY apply. The old code called
      cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000) AFTER the capture was
      already constructed — by then FFmpeg had already tried to connect,
      so the setting did nothing and a dead camera hung for 30 seconds.
      FFmpeg reads its options from an environment variable at capture
      construction time, so it is set once at module import instead.
    * Each camera now has an honest online/offline state, with the age of
      its last good frame. get_cam_stats() reports the real thing rather
      than "is a URL configured".
    * A dead camera backs off (2s → 30s) instead of retrying in a tight
      loop, and logs one line per attempt instead of a wall of text.
    * An offline camera serves a black "CAMERA OFFLINE" placeholder over
      MJPEG. Previously the feed yielded nothing at all, so the browser
      sat on an endless spinner with no indication anything was wrong.

Recording
---------
  Cameras in CONTINUOUS_RECORD_CAMERAS  → continuous 24/7
  All others                            → motion-triggered only
  Files saved to: recordings/<CameraName>/<CameraName>_YYYY-MM-DD_HH-MM.mp4
  Retention: 7 days (auto-deleted)
"""

import os

# ══════════════════════════════════════════════════════════════════
# FFMPEG OPTIONS — must be set BEFORE cv2 opens any capture.
# ══════════════════════════════════════════════════════════════════
# This is read by FFmpeg at cv2.VideoCapture() construction time. It is a
# process-wide environment variable, which is why it is set once here at
# import rather than per-thread (four camera threads setting it
# concurrently would race).
#
#   rtsp_transport;tcp  → TCP instead of UDP. Far more reliable over
#                         Tailscale/WAN; UDP drops packets and produces
#                         the smeared/torn frames you get on a bad link.
#   stimeout;5000000    → socket timeout in MICROSECONDS (5 seconds).
#                         This is the setting that actually stops the
#                         30-second hang on a dead camera.
#   max_delay;500000    → 0.5s reorder buffer.
#
# If your FFmpeg build is newer and ignores `stimeout`, change it to
# `timeout;5000000` — the option was renamed. Symptom of the wrong one:
# dead cameras hang for 30s again.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000|max_delay;500000"
)

import cv2
import time
import threading
from datetime import datetime

import numpy as np
from flask import Response
from site_config import CONFIG

try:
    import requests as _requests
except Exception:
    _requests = None


# ══════════════════════════════════════════════════════════════════
# TUNING — the knobs worth touching before a demo
# ══════════════════════════════════════════════════════════════════

JPEG_QUALITY      = 65     # MJPEG output quality (lower = less bandwidth)

AI_EVERY_N_FRAMES = 8      # while motion is present, run YOLO every Nth frame
AI_IDLE_INTERVAL  = 2.0    # seconds — re-run YOLO at least this often so
                           # parked/stationary objects stay boxed

BOX_TTL           = 3.0    # seconds a cached box stays on screen before it
                           # is dropped (must be > AI_IDLE_INTERVAL)

MOTION_EVERY_N_FRAMES = 3  # only run the motion check every Nth frame

FACE_INTERVAL     = 2.0    # seconds between face-recognition passes
FACE_TTL          = 2.5    # seconds a cached face label stays on screen

# Reconnect backoff — a dead camera walks up this ladder instead of
# retrying every 5 seconds forever.
RECONNECT_BACKOFF = [2, 5, 10, 20, 30]

# If cap.read() fails this many times in a row, drop the connection and
# reconnect rather than spinning on a half-dead stream.
MAX_READ_FAILURES = 15

# A camera is reported OFFLINE if no good frame has arrived in this long.
OFFLINE_AFTER_SECONDS = 10.0

# Cameras that record 24/7 regardless of motion. Everything else is
# motion-triggered. Names must match site_config.json exactly.
CONTINUOUS_RECORD_CAMERAS = {"Main Gate"}

# ══════════════════════════════════════════════════════════════════
# TIER-2 MOTION GATING — per-camera "tier" in site_config.json
# ══════════════════════════════════════════════════════════════════
#   "tier": "continuous"  (default) — Tier 1. YOLO re-runs at least every
#                         AI_IDLE_INTERVAL even when still. For decision
#                         cameras: gates, chokepoints.
#   "tier": "motion"      — Tier 2. YOLO SLEEPS while the scene is quiet.
#                         Motion wakes it for WAKE_SECONDS (extended while
#                         motion continues), then it sleeps again. A rare
#                         idle probe (IDLE_PROBE_SECONDS) keeps parked-
#                         object boxes from going permanently stale and
#                         acts as a safety net.
# Tunables live in site_config.json under "motion_gating"; defaults here.
def _load_gating_config():
    import json as _json
    defaults = {"wake_seconds": 10.0, "idle_probe_seconds": 60.0,
                "min_area": 1500}
    try:
        with open("site_config.json", encoding="utf-8") as f:
            cfg = _json.load(f).get("motion_gating", {}) or {}
        return {**defaults, **{k: cfg[k] for k in defaults if k in cfg}}
    except Exception:
        return defaults

_GATING = _load_gating_config()
WAKE_SECONDS       = float(_GATING["wake_seconds"])
IDLE_PROBE_SECONDS = float(_GATING["idle_probe_seconds"])
MOTION_MIN_AREA    = int(_GATING["min_area"])

# Face label colours (BGR)
_FACE_COLOR = {
    "KNOWN":     (0, 220, 130),
    "WATCHLIST": (0, 60, 255),
    "UNKNOWN":   (0, 200, 255),
}


# ══════════════════════════════════════════════════════════════════
# Face recognition (lazy-loaded, one camera only)
# ══════════════════════════════════════════════════════════════════

_face = None


def _get_face():
    global _face
    if _face is None:
        import face_engine
        _face = face_engine
    return _face


def _fire_face_alert(cam_id, camera_name, fr, frame):
    if _requests is None:
        return
    try:
        os.makedirs("output/faces", exist_ok=True)
        fname = f"face_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{fr['status']}.jpg"
        cv2.imwrite(os.path.join("output", "faces", fname), frame)
        _requests.post("http://127.0.0.1:5000/internal/face_alert",
                       json={"name": fr["name"], "status": fr["status"],
                             "reason": fr.get("reason", ""),
                             "camera": camera_name, "snapshot": fname},
                       timeout=3)
    except Exception as e:
        print(f"[CAM {cam_id}] face alert post failed: {e}")


def _draw_cached_faces(frame, faces):
    """
    Redraw cached face labels onto a fresh frame. Only used when the face
    engine returns a 'bbox' in its results; if it doesn't, this is a no-op
    and face labels simply appear on the pass frames as before.
    """
    for f in faces:
        bbox = f.get("bbox")
        if not bbox:
            continue
        try:
            x1, y1, x2, y2 = [int(v) for v in bbox]
        except Exception:
            continue

        status = f.get("status", "UNKNOWN")
        color = _FACE_COLOR.get(status, (255, 255, 255))
        label = f.get("name") or status
        if status == "WATCHLIST":
            label = f"! {label}"

        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, label, (x1, max(y1 - 8, 16)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
    return frame


# ══════════════════════════════════════════════════════════════════
# Offline placeholder
# ══════════════════════════════════════════════════════════════════

_placeholder_cache: dict = {}


def _offline_placeholder(camera_name: str) -> bytes:
    """
    A black 640x360 JPEG reading 'CAMERA OFFLINE' with the camera name.
    Served over MJPEG so the dashboard shows a clear offline tile instead
    of an endless loading spinner.
    """
    cached = _placeholder_cache.get(camera_name)
    if cached:
        return cached

    img = np.zeros((360, 640, 3), dtype=np.uint8)
    img[:] = (18, 18, 22)

    cv2.rectangle(img, (10, 10), (630, 350), (60, 60, 70), 2)
    cv2.putText(img, "CAMERA OFFLINE", (150, 165),
                cv2.FONT_HERSHEY_SIMPLEX, 1.1, (60, 60, 255), 2, cv2.LINE_AA)
    cv2.putText(img, camera_name, (150, 205),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (180, 180, 190), 1, cv2.LINE_AA)
    cv2.putText(img, "Reconnecting...", (150, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (120, 120, 130), 1, cv2.LINE_AA)

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    data = buf.tobytes() if ok else b""
    _placeholder_cache[camera_name] = data
    return data


# ══════════════════════════════════════════════════════════════════
# Camera registry
# ══════════════════════════════════════════════════════════════════

# Camera list comes from site_config.json. Each entry there is
# {"name": ..., "url": ..., "ai_mode": ...(optional)}.
# IDs are assigned by position.
RTSP_CAMERAS = [
    {"id": i + 1, "name": c.get("name", f"Camera {i+1}"),
     "url": c.get("url", ""), "ai_mode": c.get("ai_mode", ""),
     "tier": (c.get("tier", "continuous") or "continuous").lower()}
    for i, c in enumerate(CONFIG.rtsp_cameras)
]

_frames: dict = {}
_locks:  dict = {}
_stats:  dict = {}
_status: dict = {}   # cam_id -> {"online", "last_frame_ts", "last_error", "reconnects"}
_detections: dict = {}  # cam_id -> latest boxes + source dimensions + timestamp

# Shared recorder — set by init_rtsp_cams()
_recorder = None


def _set_status(cam_id, **kw):
    with _locks[cam_id]:
        _status.setdefault(cam_id, {}).update(kw)


def _is_online(cam_id) -> bool:
    st = _status.get(cam_id, {})
    if not st.get("connected"):
        return False
    last = st.get("last_frame_ts", 0)
    return (time.time() - last) <= OFFLINE_AFTER_SECONDS


# ══════════════════════════════════════════════════════════════════
# Worker
# ══════════════════════════════════════════════════════════════════

def _camera_worker(cam: dict):
    cam_id  = cam["id"]
    url     = cam["url"]
    name    = cam["name"]
    ai_mode = cam.get("ai_mode", "")
    tier    = cam.get("tier", "continuous")
    gated   = (tier == "motion")          # Tier 2: AI sleeps when quiet

    always_record = name in CONTINUOUS_RECORD_CAMERAS

    # Person/vehicle detection — only for cameras with an ai_mode set.
    if ai_mode:
        from person_detector import detect_boxes as ai_detect_boxes
        from person_detector import draw_boxes as ai_draw_boxes
        from person_detector import MotionGate
        motion_detector = MotionGate(min_area=MOTION_MIN_AREA)
    else:
        ai_detect_boxes = None
        ai_draw_boxes   = None
        motion_detector = None

    # Face recognition — only on the one camera named in site_config.json
    try:
        do_faces = bool(CONFIG.face_enabled) and name == CONFIG.face_camera
    except Exception:
        do_faces = False
    if do_faces:
        print(f"[CAM {cam_id}] {name} — face recognition ENABLED")

    attempt = 0

    while True:
        # ── Connect ─────────────────────────────────────────────
        if attempt == 0:
            print(f"[CAM {cam_id}] {name} — connecting...")

        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            cap.release()
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            _set_status(cam_id, connected=False,
                        last_error="cannot open stream",
                        reconnects=attempt)
            # Only log the first few attempts, then every 10th, so a
            # camera that is down overnight doesn't flood the console.
            if attempt <= 3 or attempt % 10 == 0:
                print(f"[CAM {cam_id}] {name} — OFFLINE "
                      f"(attempt {attempt}) retrying in {delay}s")
            time.sleep(delay)
            continue

        attempt = 0
        _set_status(cam_id, connected=True, last_error="",
                    last_frame_ts=time.time())

        print(f"[CAM {cam_id}] {name} — open ✓  AI: {ai_mode or 'none'}"
              f"{'  [TIER-2 motion-gated]' if (ai_mode and gated) else ''}"
              f"{'  REC: 24/7' if always_record else ''}")

        # Per-connection state — reset on every reconnect
        frame_count    = 0
        read_failures  = 0
        has_motion     = True
        last_ai_ts     = 0.0
        awake_until    = 0.0    # tier-2: AI runs while now < awake_until
        cached_boxes   = []
        cached_box_ts  = 0.0
        cached_faces   = []
        cached_face_ts = 0.0
        last_face_ts   = 0.0

        while True:
            ret, frame = cap.read()

            if not ret or frame is None:
                read_failures += 1
                if read_failures >= MAX_READ_FAILURES:
                    print(f"[CAM {cam_id}] {name} — stream lost, reconnecting...")
                    _set_status(cam_id, connected=False,
                                last_error="stream lost")
                    break
                time.sleep(0.05)
                continue

            read_failures = 0
            frame_count += 1
            now_ts = time.time()
            _set_status(cam_id, last_frame_ts=now_ts)

            # ── 1. Motion + detection ───────────────────────────
            if ai_detect_boxes and ai_mode:
                # Motion check is throttled — it is not free at 1080p.
                if frame_count % MOTION_EVERY_N_FRAMES == 0:
                    try:
                        has_motion = motion_detector.has_motion(frame)
                    except Exception as e:
                        print(f"[CAM {cam_id}] motion error: {e}")
                        has_motion = True

                # Tier-2 wake/sleep: motion opens (or extends) a wake
                # window; the AI only runs inside it. Falling asleep is
                # logged once when the window lapses.
                if gated:
                    if has_motion:
                        if now_ts >= awake_until:   # was asleep → waking
                            pct = getattr(motion_detector,
                                          "last_activity_pct", 0.0)
                            print(f"[MOTION] {name} — woke AI "
                                  f"(activity {pct}%)")
                            with _locks[cam_id]:
                                st = _status.setdefault(cam_id, {})
                                st["wakes"] = st.get("wakes", 0) + 1
                        awake_until = now_ts + WAKE_SECONDS
                    elif awake_until and now_ts >= awake_until:
                        print(f"[MOTION] {name} — quiet, AI sleeping")
                        awake_until = 0.0
                    _set_status(cam_id, awake=(now_ts < awake_until))

                awake = (not gated) or (now_ts < awake_until)

                # Run YOLO if: awake and motion (every Nth frame), OR the
                # keep-fresh timer fired. Tier 1 keeps the short idle
                # interval (parked objects stay boxed); Tier 2 sleeps and
                # only probes rarely as a safety net.
                idle_interval = IDLE_PROBE_SECONDS if gated else AI_IDLE_INTERVAL
                due_by_motion = (awake and has_motion
                                 and frame_count % AI_EVERY_N_FRAMES == 0)
                due_by_timer  = (now_ts - last_ai_ts) >= idle_interval

                if due_by_motion or due_by_timer:
                    try:
                        boxes, counts = ai_detect_boxes(frame, mode=ai_mode)
                        cached_boxes  = boxes
                        cached_box_ts = now_ts
                        last_ai_ts    = now_ts
                        with _locks[cam_id]:
                            _stats[cam_id] = counts
                            _detections[cam_id] = {
                                "boxes": [dict(box) for box in boxes],
                                "frame_width": int(frame.shape[1]),
                                "frame_height": int(frame.shape[0]),
                                "updated_at": now_ts,
                            }
                    except Exception as e:
                        print(f"[CAM {cam_id}] AI error: {e}")
                        last_ai_ts = now_ts   # don't hammer a failing model

                # Expire stale boxes so nothing ghosts on screen forever
                if cached_boxes and (now_ts - cached_box_ts) > BOX_TTL:
                    cached_boxes = []
                    with _locks[cam_id]:
                        _stats[cam_id] = {"persons": 0, "vehicles": 0}
                        detection = _detections.setdefault(cam_id, {})
                        detection["boxes"] = []
                        detection["updated_at"] = now_ts
            else:
                has_motion = True   # no AI on this camera

            # Expire stale face labels
            if cached_faces and (now_ts - cached_face_ts) > FACE_TTL:
                cached_faces = []

            # ── 2. Write the CLEAN frame to disk (before drawing) ─
            if _recorder:
                _recorder.write_frame(
                    cam_id, frame,
                    has_motion=has_motion or always_record,
                )

            # ── 3. Build the display frame ──────────────────────
            face_due        = do_faces and (now_ts - last_face_ts) >= FACE_INTERVAL
            show_motion_tag = bool(ai_mode) and has_motion
            needs_drawing   = (bool(cached_boxes) or show_motion_tag
                               or face_due or bool(cached_faces))

            # Only pay for a frame copy when we actually draw on it.
            display_frame = frame.copy() if needs_drawing else frame

            if cached_boxes and ai_draw_boxes:
                ai_draw_boxes(display_frame, cached_boxes)

            if show_motion_tag:
                cv2.putText(display_frame, "● MOTION",
                            (display_frame.shape[1] - 120, 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                            (0, 0, 255), 2, cv2.LINE_AA)

            # ── 4. Face recognition pass (throttled) ────────────
            if face_due:
                try:
                    fe = _get_face()
                    display_frame, face_results = fe.recognise(display_frame)
                    last_face_ts = now_ts

                    # Cache only results that carry coordinates, so they can
                    # be redrawn on the frames in between passes.
                    cached_faces = [f for f in (face_results or []) if f.get("bbox")]
                    cached_face_ts = now_ts

                    for fr in (face_results or []):
                        if fe.should_alert(fr["status"]):
                            _fire_face_alert(cam_id, name, fr, display_frame)
                except Exception as e:
                    print(f"[CAM {cam_id}] face error: {e}")
            elif cached_faces:
                _draw_cached_faces(display_frame, cached_faces)

            # ── 5. Publish for MJPEG ────────────────────────────
            ok, buf = cv2.imencode(
                ".jpg", display_frame,
                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if ok:
                with _locks[cam_id]:
                    _frames[cam_id] = buf.tobytes()

        cap.release()
        _set_status(cam_id, connected=False)
        time.sleep(RECONNECT_BACKOFF[0])


# ══════════════════════════════════════════════════════════════════
# Public API — unchanged signatures
# ══════════════════════════════════════════════════════════════════

def init_rtsp_cams(app, recorder=None):
    global _recorder
    _recorder = recorder

    for cam in RTSP_CAMERAS:
        cam_id = cam["id"]
        _frames[cam_id] = b""
        _locks[cam_id]  = threading.Lock()
        _stats[cam_id]  = {"persons": 0, "vehicles": 0}
        _status[cam_id] = {"connected": False, "last_frame_ts": 0.0,
                           "last_error": "", "reconnects": 0}

        if not cam.get("url"):
            continue

        t = threading.Thread(
            target=_camera_worker,
            args=(cam,),
            daemon=True,
            name=f"rtsp-cam-{cam_id}",
        )
        t.start()

    active = sum(1 for c in RTSP_CAMERAS if c.get("url"))
    print(f"[RTSP] {active} camera(s) started")


# Alias so api_server.py import doesn't break
init_rtmp_cams = init_rtsp_cams


def _mjpeg_generator(cam_id: int, camera_name: str = ""):
    """
    Streams the newest frame. If the camera is offline, streams the
    OFFLINE placeholder at a low rate so the dashboard shows a clear
    offline tile rather than an endless spinner.
    """
    while True:
        online = _is_online(cam_id)

        if online:
            with _locks[cam_id]:
                frame = _frames.get(cam_id, b"")
            delay = 0.03
        else:
            frame = _offline_placeholder(camera_name)
            delay = 0.5   # no point pushing a static image at 30fps

        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame +
                b"\r\n"
            )
        time.sleep(delay)


def rtmp_feed(cam_id: int):
    cam = next((c for c in RTSP_CAMERAS if c["id"] == cam_id), None)
    if cam is None:
        from flask import abort
        abort(404)
    if not cam.get("url"):
        from flask import jsonify
        return jsonify({"error": "Camera not configured yet"}), 503
    return Response(
        _mjpeg_generator(cam_id, cam["name"]),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


def get_cam_stats(cam_id: int) -> dict:
    cam = next((c for c in RTSP_CAMERAS if c["id"] == cam_id), None)
    if cam is None:
        return {"error": "Unknown camera"}

    with _locks.get(cam_id, threading.Lock()):
        counts = dict(_stats.get(cam_id, {"persons": 0, "vehicles": 0}))
        st = dict(_status.get(cam_id, {}))
        detection = dict(_detections.get(cam_id, {}))

    last_ts = st.get("last_frame_ts", 0) or 0
    return {
        "cam_id":     cam_id,
        "name":       cam["name"],
        "ai_mode":    cam.get("ai_mode", ""),
        "tier":       cam.get("tier", "continuous"),
        "awake":      bool(st.get("awake", True)) if cam.get("tier") == "motion" else True,
        "wakes":      st.get("wakes", 0),
        "configured": bool(cam.get("url")),
        "online":     _is_online(cam_id),
        "last_frame_age": round(time.time() - last_ts, 1) if last_ts else None,
        "reconnects": st.get("reconnects", 0),
        "last_error": st.get("last_error", ""),
        "detections": detection.get("boxes", []),
        "frame_width": detection.get("frame_width"),
        "frame_height": detection.get("frame_height"),
        "detection_age": (
            round(time.time() - detection["updated_at"], 2)
            if detection.get("updated_at") else None
        ),
        **counts,
    }


def get_all_cam_stats() -> list:
    """Convenience for a dashboard health panel."""
    return [get_cam_stats(c["id"]) for c in RTSP_CAMERAS]
