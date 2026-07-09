"""
rtmp_proxy.py
--------------
Reads each camera's RTSP stream, runs motion-triggered AI detection,
records to disk via CamRecorderManager, and re-serves as MJPEG.

Recording
---------
  Main Gate  → continuous 24/7
  All others → motion-triggered only
  Files saved to: recordings/<CameraName>/<CameraName>_YYYY-MM-DD_HH-MM.mp4
  Retention: 7 days (auto-deleted)
"""

import cv2
import time
import threading
from flask import Response
from site_config import CONFIG

# Camera list comes from site_config.json. Each entry there is
# {"name": ..., "url": ..., "ai_mode": ...(optional)}.
# IDs are assigned by position.
RTSP_CAMERAS = [
    {"id": i + 1, "name": c.get("name", f"Camera {i+1}"),
     "url": c.get("url", ""), "ai_mode": c.get("ai_mode", "")}
    for i, c in enumerate(CONFIG.rtsp_cameras)
]

RECONNECT_DELAY   = 5
JPEG_QUALITY      = 65
AI_EVERY_N_FRAMES = 8

_frames: dict = {}
_locks:  dict = {}
_stats:  dict = {}

# Shared recorder — set by init_rtsp_cams()
_recorder = None


def _camera_worker(cam: dict):
    cam_id  = cam["id"]
    url     = cam["url"]
    name    = cam["name"]
    ai_mode = cam.get("ai_mode", "")

    # Person/vehicle detection — only for cameras with an ai_mode set.
    if ai_mode:
        from person_detector import detect as ai_detect, MotionGate
        motion_detector = MotionGate()
    else:
        ai_detect = None
        motion_detector = None

    frame_count    = 0
    last_annotated = None
    has_motion     = False

    while True:
        print(f"[CAM {cam_id}] {name} — connecting...")
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)

        if not cap.isOpened():
            print(f"[CAM {cam_id}] Cannot open — retrying in {RECONNECT_DELAY}s")
            cap.release()
            time.sleep(RECONNECT_DELAY)
            continue

        print(f"[CAM {cam_id}] {name} — open ✓  AI: {ai_mode or 'none'}")

        while True:
            ret, frame = cap.read()
            if not ret:
                print(f"[CAM {cam_id}] {name} — lost, reconnecting...")
                break

            frame_count += 1

            if ai_detect and ai_mode:
                has_motion = motion_detector.has_motion(frame)

                if has_motion and frame_count % AI_EVERY_N_FRAMES == 0:
                    try:
                        annotated, counts = ai_detect(frame, mode=ai_mode)
                        last_annotated = annotated
                        with _locks[cam_id]:
                            _stats[cam_id] = counts
                    except Exception as e:
                        print(f"[CAM {cam_id}] AI error: {e}")
                        last_annotated = frame
                elif not has_motion:
                    last_annotated = None
                    with _locks[cam_id]:
                        _stats[cam_id] = {"persons": 0, "vehicles": 0}

                display_frame = last_annotated if last_annotated is not None else frame

                if has_motion:
                    cv2.putText(display_frame, "● MOTION",
                                (display_frame.shape[1] - 120, 22),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                                (0, 0, 255), 2, cv2.LINE_AA)
            else:
                display_frame = frame
                has_motion    = True  # Main Gate records continuously

            # ── Write to recording ──────────────────────────────
            if _recorder:
                _recorder.write_frame(cam_id, frame, has_motion=has_motion)

            ok, buf = cv2.imencode(
                ".jpg", display_frame,
                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )
            if ok:
                with _locks[cam_id]:
                    _frames[cam_id] = buf.tobytes()

        cap.release()
        time.sleep(RECONNECT_DELAY)


def init_rtsp_cams(app, recorder=None):
    global _recorder
    _recorder = recorder

    for cam in RTSP_CAMERAS:
        cam_id = cam["id"]
        _frames[cam_id] = b""
        _locks[cam_id]  = threading.Lock()
        _stats[cam_id]  = {"persons": 0, "vehicles": 0}

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


def _mjpeg_generator(cam_id: int):
    while True:
        with _locks[cam_id]:
            frame = _frames.get(cam_id, b"")
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame +
                b"\r\n"
            )
        time.sleep(0.03)


def rtmp_feed(cam_id: int):
    cam = next((c for c in RTSP_CAMERAS if c["id"] == cam_id), None)
    if cam is None:
        from flask import abort
        abort(404)
    if not cam.get("url"):
        from flask import jsonify
        return jsonify({"error": "Camera not configured yet"}), 503
    return Response(
        _mjpeg_generator(cam_id),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


def get_cam_stats(cam_id: int) -> dict:
    cam = next((c for c in RTSP_CAMERAS if c["id"] == cam_id), None)
    if cam is None:
        return {"error": "Unknown camera"}
    with _locks.get(cam_id, threading.Lock()):
        counts = dict(_stats.get(cam_id, {"persons": 0, "vehicles": 0}))
    return {
        "cam_id":  cam_id,
        "name":    cam["name"],
        "ai_mode": cam.get("ai_mode", ""),
        "online":  bool(cam.get("url")),
        **counts,
    }
