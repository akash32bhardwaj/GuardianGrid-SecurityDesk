r"""
GuardianGrid — Segment Recorder
================================
Continuously records each camera's RTSP stream into 10-minute MP4 segments:

    recordings\<Camera Name>\<YYYY-MM-DD>\seg_HH-MM-SS.mp4

Segments are stream-copied (no re-encode -> near-zero CPU). A cleanup pass
deletes day-folders older than RETENTION_DAYS. smart_replay.py depends on
this exact folder/filename convention.

Requires ffmpeg on PATH  (winget install ffmpeg).

Camera list resolution order:
  1. "rtsp_cameras" in site_config.json  (the same list the live wall uses)
     e.g. { "rtsp_cameras": [ {"name": "Main Gate", "url": "rtsp://..."} ] }
  2. "cameras" in site_config.json       (legacy format: name + rtsp keys)
  3. the CAMERAS dict below.

Usage (from the backend folder):
  python segment_recorder.py                          # record ALL cameras
  python segment_recorder.py --only "Parking A,Parking B"   # just these two
  python segment_recorder.py --list                   # show resolved list, exit
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
SEGMENT_SECONDS = 600
RETENTION_DAYS  = 7

# Last-resort fallback if site_config.json has no camera lists at all.
CAMERAS = {
    # "Main Gate": "rtsp://admin:password@192.168.31.100:554/cam/realmonitor?channel=1&subtype=1",
}

def safe_name(name):
    return re.sub(r"[^A-Za-z0-9 _-]", "", name).strip() or "Camera"

def load_cameras():
    cfg_path = os.path.join(BASE_DIR, "site_config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)

            # 1) Preferred: the same "rtsp_cameras" list the live wall uses.
            cams = cfg.get("rtsp_cameras") or []
            resolved = {safe_name(c["name"]): c["url"]
                        for c in cams if c.get("name") and c.get("url")}
            if resolved:
                return resolved

            # 2) Legacy format: "cameras" with name + rtsp keys.
            cams = cfg.get("cameras") or []
            resolved = {safe_name(c["name"]): c["rtsp"]
                        for c in cams if c.get("name") and c.get("rtsp")}
            if resolved:
                return resolved
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return {safe_name(k): v for k, v in CAMERAS.items()}

def filter_cameras(cams, only):
    """Keep only cameras whose name matches the --only list (case-insensitive)."""
    if not only:
        return cams
    wanted = {w.strip().lower() for w in only.split(",") if w.strip()}
    kept = {n: u for n, u in cams.items() if n.lower() in wanted}
    missing = wanted - {n.lower() for n in kept}
    for m in missing:
        print(f"[WARN] --only camera not found in config: '{m}'")
    return kept

def record_camera(name, rtsp, stop_event):
    """One ffmpeg process per camera; restarts on failure with backoff."""
    while not stop_event.is_set():
        day_dir = os.path.join(RECORDINGS_DIR, name, datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)
        out_pattern = os.path.join(day_dir, "seg_%H-%M-%S.mp4")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", rtsp,
            "-map", "0:v:0",   # video track only
            "-an",             # drop audio (NVR G.711/pcm_mulaw won't fit in MP4)
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(SEGMENT_SECONDS),
            "-segment_format", "mp4",
            "-reset_timestamps", "1",
            "-strftime", "1",
            out_pattern,
        ]
        print(f"[{name}] recording -> {day_dir}")
        try:
            proc = subprocess.Popen(cmd)
            # poll so Ctrl+C and midnight rollover are handled
            day = datetime.now().date()
            while proc.poll() is None:
                if stop_event.is_set():
                    proc.terminate()
                    break
                if datetime.now().date() != day:
                    proc.terminate()   # restart into the new day folder
                    break
                time.sleep(2)
        except FileNotFoundError:
            print(f"[{name}] ffmpeg not found on PATH — install it and restart.")
            return
        if not stop_event.is_set():
            print(f"[{name}] stream ended/failed — retrying in 10s")
            time.sleep(10)

def cleanup_loop(stop_event):
    while not stop_event.is_set():
        cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        if os.path.isdir(RECORDINGS_DIR):
            for cam in os.listdir(RECORDINGS_DIR):
                cam_dir = os.path.join(RECORDINGS_DIR, cam)
                if not os.path.isdir(cam_dir):
                    continue
                for day in os.listdir(cam_dir):
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and day < cutoff:
                        shutil.rmtree(os.path.join(cam_dir, day), ignore_errors=True)
                        print(f"[cleanup] removed {cam}/{day}")
        stop_event.wait(3600)

def main():
    ap = argparse.ArgumentParser(description="GuardianGrid segment recorder")
    ap.add_argument("--list", action="store_true", help="show cameras and exit")
    ap.add_argument("--only", default="",
                    help='comma-separated camera names to record, e.g. --only "Parking A,Parking B"')
    args = ap.parse_args()

    cams = filter_cameras(load_cameras(), args.only)
    if args.list or not cams:
        print("Cameras resolved:")
        for n, u in cams.items():
            print(f"  {n}: {u[:60]}...")
        if not cams:
            print("  (none — add cameras to site_config.json or the CAMERAS dict)")
        return

    stop = threading.Event()
    threads = [threading.Thread(target=cleanup_loop, args=(stop,), daemon=True)]
    for name, rtsp in cams.items():
        threads.append(threading.Thread(target=record_camera, args=(name, rtsp, stop), daemon=True))
    for t in threads:
        t.start()
    print(f"Recording {len(cams)} camera(s). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        print("Stopping…")
        time.sleep(3)

if __name__ == "__main__":
    main()
