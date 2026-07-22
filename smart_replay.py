r"""
GuardianGrid — Smart Replay
============================
Builds the daily highlight reel: finds every notable event (incidents +
unknown/blacklisted vehicle detections), locates the recorded segment that
contains each timestamp, cuts a clip around it, and concatenates everything
into one MP4:

    reports\replays\replay_YYYY-MM-DD.mp4

Depends on segment_recorder.py's layout:
    recordings\<Camera>\<YYYY-MM-DD>\seg_HH-MM-SS.mp4   (10-minute chunks)

Requires ffmpeg on PATH.

Usage (from the backend folder):
  python smart_replay.py                     # yesterday's reel
  python smart_replay.py --date 2026-07-16   # specific day
  python smart_replay.py --selftest          # fabricate footage + fake events,
                                             # build a reel, verify durations
Clip window: PRE_SECONDS before each event to POST_SECONDS after.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
DB_FILE        = os.path.join(BASE_DIR, "guardiangrid.db")
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
REPLAY_DIR     = os.path.join(BASE_DIR, "reports", "replays")
SEGMENT_SECONDS = 600
PRE_SECONDS, POST_SECONDS = 15, 25
MAX_CLIPS = 12

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def ffprobe_duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0

# ------------------------------------------------------------ event query ---

def gather_events(date):
    """Notable moments for the reel: incidents + flagged vehicle events."""
    import sqlite3
    start, end = f"{date} 00:00:00", f"{date} 23:59:59"
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    def rows(sql, params):
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        except sqlite3.Error:
            return []

    events = []
    for r in rows(
        "SELECT title AS label, camera_name AS camera, created_at AS ts "
        "FROM incidents WHERE REPLACE(created_at,'T',' ') BETWEEN ? AND ?",
        (start, end)):
        events.append(dict(r))
    for r in rows(
        "SELECT ('Vehicle ' || plate) AS label, camera, timestamp AS ts "
        "FROM vehicle_events WHERE REPLACE(timestamp,'T',' ') BETWEEN ? AND ? AND ("
        "UPPER(COALESCE(access,'')) LIKE '%BLACK%' OR UPPER(COALESCE(state,'')) LIKE '%BLACK%' OR "
        "UPPER(COALESCE(access,'')) LIKE '%UNKNOWN%' OR UPPER(COALESCE(access,'')) LIKE '%UNREGISTER%' OR "
        "UPPER(COALESCE(state,'')) LIKE '%UNKNOWN%')",
        (start, end)):
        events.append(dict(r))
    con.close()

    cleaned = []
    for e in events:
        t = str(e["ts"] or "").replace("T", " ")[:19]
        try:
            e["dt"] = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
            cleaned.append(e)
        except ValueError:
            continue
    cleaned.sort(key=lambda e: e["dt"])

    # de-duplicate moments closer than 60s on the same camera
    deduped = []
    for e in cleaned:
        if deduped and e.get("camera") == deduped[-1].get("camera") and \
           (e["dt"] - deduped[-1]["dt"]).total_seconds() < 60:
            continue
        deduped.append(e)
    return deduped[:MAX_CLIPS]

# ------------------------------------------------------ segment resolution ---

def parse_seg_start(fname, date):
    m = re.fullmatch(r"seg_(\d{2})-(\d{2})-(\d{2})\.mp4", fname)
    if not m:
        return None
    h, mi, s = map(int, m.groups())
    return datetime.strptime(date, "%Y-%m-%d").replace(hour=h, minute=mi, second=s)

def find_segment(camera, dt, date):
    """Return (segment_path, offset_seconds) containing dt, else None."""
    candidates = []
    cam_dirs = [camera] if camera else []
    if os.path.isdir(RECORDINGS_DIR):
        cam_dirs = cam_dirs or os.listdir(RECORDINGS_DIR)
    for cam in cam_dirs:
        day_dir = os.path.join(RECORDINGS_DIR, cam, date)
        if not os.path.isdir(day_dir):
            continue
        for f in os.listdir(day_dir):
            seg_start = parse_seg_start(f, date)
            if seg_start is None:
                continue
            offset = (dt - seg_start).total_seconds()
            if 0 <= offset < SEGMENT_SECONDS + 5:
                candidates.append((os.path.join(day_dir, f), offset))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[1])  # tightest containing segment

# -------------------------------------------------------------- reel build ---

def build_reel(date, verbose=True):
    events = gather_events(date)
    if verbose:
        print(f"=== Smart replay \u00b7 {date} \u00b7 {len(events)} notable event(s) ===")
    if not events:
        print("Nothing notable to replay — no reel generated.")
        return None

    os.makedirs(REPLAY_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ggreplay_")
    clips = []
    for i, e in enumerate(events):
        seg = find_segment(e.get("camera"), e["dt"], date)
        if not seg:
            if verbose:
                print(f"  [skip] {e['dt'].strftime('%H:%M:%S')} {e['label']} — no footage")
            continue
        seg_path, offset = seg
        start = max(0, offset - PRE_SECONDS)
        clip = os.path.join(tmp, f"clip_{i:02d}.mp4")
        r = run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-ss", f"{start:.2f}", "-i", seg_path,
                 "-t", str(PRE_SECONDS + POST_SECONDS),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-an", clip])
        if r.returncode == 0 and os.path.exists(clip) and ffprobe_duration(clip) > 1:
            clips.append(clip)
            if verbose:
                print(f"  [clip] {e['dt'].strftime('%H:%M:%S')} \u00b7 {e['label']} "
                      f"\u00b7 {os.path.basename(seg_path)} @ {offset:.0f}s")
        elif verbose:
            print(f"  [fail] {e['label']}: {r.stderr.strip()[:120]}")

    if not clips:
        print("No clips could be cut — is the recorder writing segments?")
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    concat_list = os.path.join(tmp, "list.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{c}'\n")
    out = os.path.join(REPLAY_DIR, f"replay_{date}.mp4")
    r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", concat_list,
             "-c", "copy", out])
    shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0:
        print(f"[FAIL] concat: {r.stderr.strip()[:200]}")
        return None
    dur = ffprobe_duration(out)
    print(f"[OK] {out}  ({len(clips)} clips, {dur:.0f}s)")
    meta = {"date": date, "clips": len(clips), "duration_s": round(dur),
            "file": os.path.basename(out),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(REPLAY_DIR, f"replay_{date}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return out

# ---------------------------------------------------------------- selftest ---

def selftest():
    """Fabricates segments + a temp DB with fake events; builds a reel."""
    global DB_FILE, RECORDINGS_DIR, REPLAY_DIR
    import sqlite3
    work = tempfile.mkdtemp(prefix="ggselftest_")
    DB_FILE = os.path.join(work, "guardiangrid.db")
    RECORDINGS_DIR = os.path.join(work, "recordings")
    REPLAY_DIR = os.path.join(work, "replays")
    date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # two 10-min dummy segments for "Main Gate": 10:00:00 and 10:10:00
    day_dir = os.path.join(RECORDINGS_DIR, "Main Gate", date)
    os.makedirs(day_dir, exist_ok=True)
    for hms in ("10-00-00", "10-10-00"):
        r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={SEGMENT_SECONDS}",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 os.path.join(day_dir, f"seg_{hms}.mp4")])
        assert r.returncode == 0, r.stderr

    # fake DB: one incident at 10:03:30, one blacklist vehicle at 10:14:10
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    cur.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY, incident_id TEXT, "
                "title TEXT, severity TEXT, status TEXT, created_at TEXT, camera_name TEXT)")
    cur.execute("CREATE TABLE vehicle_events (id INTEGER PRIMARY KEY, plate TEXT, "
                "state TEXT, access TEXT, camera TEXT, timestamp TEXT)")
    cur.execute("INSERT INTO incidents (incident_id,title,severity,status,created_at,camera_name) "
                "VALUES ('GG-9001','Selftest intrusion','High','OPEN',?, 'Main Gate')",
                (f"{date}T10:03:30",))
    cur.execute("INSERT INTO vehicle_events (plate,state,access,camera,timestamp) "
                "VALUES ('TEST0001','Denied','Blacklisted','Main Gate',?)",
                (f"{date} 10:14:10",))
    con.commit(); con.close()

    out = build_reel(date)
    assert out and os.path.exists(out), "reel was not created"
    dur = ffprobe_duration(out)
    expected = 2 * (PRE_SECONDS + POST_SECONDS)
    assert abs(dur - expected) <= 6, f"duration {dur:.0f}s far from expected ~{expected}s"
    print(f"[SELFTEST PASS] 2 events \u2192 2 clips \u2192 {dur:.0f}s reel "
          f"(expected \u2248{expected}s)")
    shutil.rmtree(work, ignore_errors=True)

def main():
    ap = argparse.ArgumentParser(description="GuardianGrid smart replay")
    ap.add_argument("--date", default=(datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if not os.path.exists(DB_FILE):
        print("[ABORT] guardiangrid.db not found — cd to the backend folder first.")
        sys.exit(1)
    build_reel(args.date)

if __name__ == "__main__":
    main()
