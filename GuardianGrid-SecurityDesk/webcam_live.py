"""
webcam_live.py
--------------
Live webcam ANPR feed with:
  - Popup alerts on new detections
  - Entry/Exit tracking
  - CSV logging with snapshots
  - Temporal voting — accumulates readings over ~1.5s before confirming
    a plate, dramatically reducing wrong-character errors

Usage:
    python webcam_live.py
    python webcam_live.py --camera 1
    python webcam_live.py --camera 1 --save-frames

Controls:
    Q / ESC  — quit
    S        — save current frame manually
    R        — reset last result overlay
"""

import cv2
import time
import argparse
import threading
from pathlib import Path
from datetime import datetime

from core.anpr_engine import ANPREngine, PlateResult, PlateVoter
from alert_system import AlertSystem

# ── Config ──────────────────────────────────────────────────────
PROCESS_EVERY_N_FRAMES = 2
MIN_CONFIDENCE         = 30
OUTPUT_DIR             = Path("output/webcam")

# Voting config — wait for this many consistent readings before
# treating a plate as "confirmed" and firing alerts/logs.
# Window is wide because each detection can take 1-3s on CPU.
VOTE_WINDOW_SECONDS = 8.0
VOTE_MIN_SAMPLES    = 2


def draw_overlay(frame, result: PlateResult, fps: float, voting_count: int = 0):
    h, w = frame.shape[:2]

    # FPS
    cv2.putText(frame, f"FPS: {fps:.1f}", (10, 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,120), 2)

    # Controls hint
    cv2.putText(frame, "Q=Quit  S=Save  R=Reset",
                (10, h-12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180,180,180), 1)

    # Voting indicator — shows while accumulating samples
    if voting_count > 0 and voting_count < VOTE_MIN_SAMPLES:
        dots = "." * voting_count
        cv2.putText(frame, f"Reading{dots}",
                    (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,200,255), 2)

    if result and result.detected:
        # Semi-transparent bottom bar
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, h-80), (w, h), (0,0,0), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

        color_map = {
            "yellow":(0,215,255), "green":(0,220,0),
            "black":(180,180,180), "white":(0,220,130)
        }
        pc = color_map.get(result.plate_type, (0,220,130))

        cv2.putText(frame, result.plate_number,
                    (16, h-46), cv2.FONT_HERSHEY_DUPLEX, 1.4, pc, 3)
        cv2.putText(frame,
                    f"{result.plate_label}  |  {result.state_name}  |  {result.confidence:.0f}%  |  CONFIRMED",
                    (16, h-16), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200,200,200), 1)
    return frame


def run_webcam(camera_index: int = 0,
               save_frames: bool = False):

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    engine = ANPREngine(use_gpu=False)
    alerts = AlertSystem()
    voter  = PlateVoter(window_seconds=VOTE_WINDOW_SECONDS,
                        min_samples=VOTE_MIN_SAMPLES)

    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    if not cap.isOpened():
        print(f"[ERROR] Cannot open camera {camera_index}.")
        return

    print("[INFO] Camera opened. Press Q or ESC to quit.")
    print(f"[INFO] Voting enabled — confirms after {VOTE_MIN_SAMPLES} "
          f"consistent reads within {VOTE_WINDOW_SECONDS}s")

    confirmed_result: PlateResult = None
    last_confirmed_plate = None
    frame_count  = 0
    processing   = False
    fps_timer    = time.time()
    fps          = 0.0
    voting_count = 0

    def async_process(frame_copy):
        nonlocal confirmed_result, processing, last_confirmed_plate, voting_count
        try:
            r = engine.process_image(frame_copy)
            if r.detected and r.confidence >= MIN_CONFIDENCE:
                voter.add(r)
                voting_count = len(voter._samples)

                consensus = voter.get_consensus()
                if consensus and consensus.plate_number_raw != last_confirmed_plate:
                    confirmed_result = consensus
                    last_confirmed_plate = consensus.plate_number_raw

                    # Save snapshot if requested
                    snap_path = ""
                    if save_frames:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        snap_path = str(OUTPUT_DIR / f"{consensus.plate_number_raw}_{ts}.jpg")
                        cv2.imwrite(snap_path, frame_copy)

                    # Fire alert + entry/exit tracking
                    alerts.process_detection(
                        plate         = consensus.plate_number,
                        state_name    = consensus.state_name,
                        plate_label   = consensus.plate_label,
                        confidence    = consensus.confidence,
                        snapshot_path = snap_path,
                    )
                    print(f"[CONFIRMED] {consensus.plate_number} "
                          f"({consensus.state_name}) "
                          f"{consensus.confidence:.0f}% — {consensus.notes}")
            else:
                # No detection this frame — let voter window expire naturally
                pass
        finally:
            processing = False

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1
        now = time.time()
        if now - fps_timer >= 1.0:
            fps       = frame_count / (now - fps_timer)
            fps_timer = now
            frame_count = 0

        # Trigger async detection every N frames
        if frame_count % PROCESS_EVERY_N_FRAMES == 0 and not processing:
            processing = True
            t = threading.Thread(target=async_process,
                                 args=(frame.copy(),), daemon=True)
            t.start()

        # Clear confirmed result if voter window has expired (plate left view)
        if confirmed_result and not voter.get_consensus():
            confirmed_result = None
            last_confirmed_plate = None
            voting_count = 0

        # Draw overlay
        display = draw_overlay(frame.copy(), confirmed_result, fps, voting_count)
        if confirmed_result and confirmed_result.detected and confirmed_result.bbox:
            display = engine.draw_result(display, confirmed_result)

        cv2.imshow("GuardianGrid ANPR — Live Feed", display)
        key = cv2.waitKey(1) & 0xFF

        if key in (ord("q"), 27):
            break
        elif key == ord("s") and confirmed_result:
            ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = OUTPUT_DIR / f"{confirmed_result.plate_number_raw}_{ts}.jpg"
            cv2.imwrite(str(fname), display)
            print(f"[SAVE] {fname}")
        elif key == ord("r"):
            confirmed_result = None
            last_confirmed_plate = None
            voter.reset()
            voting_count = 0

    cap.release()
    cv2.destroyAllWindows()
    print("[INFO] Webcam closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GuardianGrid Live ANPR")
    parser.add_argument("--camera",      type=int, default=0)
    parser.add_argument("--save-frames", action="store_true")
    args = parser.parse_args()
    run_webcam(args.camera, args.save_frames)
