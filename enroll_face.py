"""
enroll_face.py - register faces for Defender Octa (known or watchlist)
----------------------------------------------------------------------
Run from the backend folder (next to api_server.py, face_engine.py).

Enrol a RESIDENT / STAFF (green box, no alert):
    python enroll_face.py --name "Ramesh Kumar" --flat "B-402" --image ramesh.jpg

Enrol a WATCHLIST person (red box + incident + WhatsApp alert):
    python enroll_face.py --name "Ex-guard Suresh" --watchlist --reason "Terminated staff" --image suresh.jpg

From the webcam instead of a file (SPACE = capture, Q = cancel):
    python enroll_face.py --name "Ramesh Kumar" --flat "B-402" --webcam

List / remove:
    python enroll_face.py --list
    python enroll_face.py --remove "Ramesh Kumar"

Tips: frontal, well-lit face; one clear photo is enough. Enrol the same
name 2-3 times from slightly different angles to improve reliability.
"""

import argparse
import json
import sys
from pathlib import Path

import cv2

from face_engine import get_embedding, FACE_DB_PATH


def _load():
    if FACE_DB_PATH.exists():
        try:
            return json.loads(FACE_DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save(rows):
    FACE_DB_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def _add(name, flat, status, reason, emb):
    rows = _load()
    rows.append({"name": name, "flat": flat, "status": status,
                 "reason": reason, "encoding": emb})
    _save(rows)
    tag = "WATCHLIST" if status == "WATCHLIST" else "known"
    print(f"Enrolled '{name}' as {tag}. Total enrolled: {len(rows)}.")
    print("Restart NOT needed - the live feed reloads faces automatically.")


def enroll_from_image(name, flat, status, reason, image_path):
    img = cv2.imread(image_path)
    if img is None:
        sys.exit(f"ERROR: could not read image '{image_path}'.")
    emb, err = get_embedding(img)
    if err:
        sys.exit(f"ERROR: {err} Use a clearer, frontal photo.")
    _add(name, flat, status, reason, emb)


def enroll_from_webcam(name, flat, status, reason):
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        sys.exit("ERROR: could not open webcam.")
    print("Webcam open. Look at the camera. SPACE = capture, Q = cancel.")
    captured = None
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        preview = frame.copy()
        cv2.putText(preview, "SPACE = capture   Q = cancel",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 220, 130), 2)
        cv2.imshow("Enroll face", preview)
        key = cv2.waitKey(1) & 0xFF
        if key == ord(" "):
            captured = frame.copy()
            break
        if key == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()
    if captured is None:
        sys.exit("Cancelled - nothing enrolled.")
    emb, err = get_embedding(captured)
    if err:
        sys.exit(f"ERROR: {err} Try again with better lighting/framing.")
    _add(name, flat, status, reason, emb)


def list_enrolled():
    rows = _load()
    if not rows:
        print("No faces enrolled yet.")
        return
    counts = {}
    for r in rows:
        key = (r["name"], r.get("status", "KNOWN"))
        counts[key] = counts.get(key, 0) + 1
    print(f"{len(rows)} enrolled photo(s):")
    for (name, status), c in counts.items():
        tag = " [WATCHLIST]" if status == "WATCHLIST" else ""
        print(f"  - {name}{tag}  ({c} photo{'s' if c > 1 else ''})")


def remove(name):
    rows = _load()
    kept = [r for r in rows if r["name"] != name]
    removed = len(rows) - len(kept)
    _save(kept)
    print(f"Removed {removed} entr{'ies' if removed != 1 else 'y'} for '{name}'.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Enrol faces for Defender Octa")
    ap.add_argument("--name", help="person's name")
    ap.add_argument("--flat", default="", help="flat/unit number")
    ap.add_argument("--watchlist", action="store_true",
                    help="flag as watchlist (triggers alerts)")
    ap.add_argument("--reason", default="", help="watchlist reason")
    ap.add_argument("--image", help="path to a photo")
    ap.add_argument("--webcam", action="store_true", help="capture from webcam")
    ap.add_argument("--list", action="store_true", help="list enrolled faces")
    ap.add_argument("--remove", help="remove all entries for this name")
    args = ap.parse_args()

    status = "WATCHLIST" if args.watchlist else "KNOWN"

    if args.list:
        list_enrolled()
    elif args.remove:
        remove(args.remove)
    elif args.name and args.image:
        enroll_from_image(args.name, args.flat, status, args.reason, args.image)
    elif args.name and args.webcam:
        enroll_from_webcam(args.name, args.flat, status, args.reason)
    else:
        ap.print_help()
