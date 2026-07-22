"""
face_engine.py — InsightFace face recognition for Defender Octa
----------------------------------------------------------------------
Lazy-loaded, isolated from ANPR and person_detector. Loads once.

    from face_engine import recognise, should_alert
    annotated, results = recognise(frame)     # results[i]["status"] in
                                              # KNOWN / WATCHLIST / UNKNOWN

Identity store: face_db.json — list of
    {name, flat, status, encoding[512], reason?}
where status is "KNOWN" (resident/staff) or "WATCHLIST" (banned/flagged).
Enrol with enroll_face.py; do NOT hand-edit encodings.

ALERT POLICY (see should_alert):
  - "watchlist" (default): only WATCHLIST faces raise incidents/alerts.
                           Ordinary unknowns are boxed but silent —
                           keeps a busy gate (and demos) clean.
  - "all":               every UNKNOWN or WATCHLIST face alerts.
Set via FACE_ALERT_MODE below or the "face" block in site_config.json.
"""

import json
import threading
from pathlib import Path

import cv2
import numpy as np

FACE_DB_PATH = Path("face_db.json")
_MATCH_THRESHOLD = 0.42     # cosine similarity; >= this = same person

# Default alert mode; overridden by site_config.json "face.alert_mode".
FACE_ALERT_MODE = "watchlist"   # "watchlist" | "all"

_app = None
_app_lock = threading.Lock()
_known = None


def _alert_mode():
    try:
        from site_config import CONFIG
        m = getattr(CONFIG, "face_alert_mode", None)
        if m in ("watchlist", "all"):
            return m
    except Exception:
        pass
    return FACE_ALERT_MODE


def should_alert(status: str) -> bool:
    """Decide whether a recognised face warrants an incident/alert."""
    if status == "WATCHLIST":
        return True
    if status == "UNKNOWN" and _alert_mode() == "all":
        return True
    return False


def _load_app():
    global _app
    if _app is None:
        with _app_lock:
            if _app is None:
                from insightface.app import FaceAnalysis
                print("[FACE] Loading InsightFace model (buffalo_l, CPU)...")
                app = FaceAnalysis(name="buffalo_l",
                                   providers=["CPUExecutionProvider"])
                app.prepare(ctx_id=-1, det_size=(480, 480))
                _app = app
                print("[FACE] InsightFace ready.")
    return _app


def _load_known():
    global _known
    if _known is None:
        rows = []
        if FACE_DB_PATH.exists():
            try:
                data = json.loads(FACE_DB_PATH.read_text(encoding="utf-8"))
                for r in data:
                    vec = np.array(r["encoding"], dtype=np.float32)
                    nrm = np.linalg.norm(vec)
                    if nrm > 0:
                        vec = vec / nrm
                    rows.append({
                        "name": r.get("name", "Unknown"),
                        "flat": r.get("flat", ""),
                        "status": r.get("status", "KNOWN").upper(),
                        "reason": r.get("reason", ""),
                        "vec": vec,
                    })
                n_watch = sum(1 for r in rows if r["status"] == "WATCHLIST")
                print(f"[FACE] Loaded {len(rows)} enrolled face(s) "
                      f"({n_watch} on watchlist).")
            except Exception as e:
                print(f"[FACE] Could not load face_db.json: {e}")
        else:
            print("[FACE] No face_db.json yet - all faces read as UNKNOWN.")
        _known = rows
    return _known


def reload_known():
    """Force reload after enrolment (no server restart needed)."""
    global _known
    _known = None
    return _load_known()


def _best_match(vec):
    known = _load_known()
    if not known:
        return None
    nrm = np.linalg.norm(vec)
    if nrm > 0:
        vec = vec / nrm
    best, best_score = None, -1.0
    for k in known:
        score = float(np.dot(vec, k["vec"]))
        if score > best_score:
            best, best_score = k, score
    if best and best_score >= _MATCH_THRESHOLD:
        return best, best_score
    return None


# Colours (BGR)
_C_KNOWN     = (0, 220, 130)    # green
_C_WATCHLIST = (0, 0, 235)      # bright red
_C_UNKNOWN   = (60, 90, 200)    # muted orange-red


def recognise(frame):
    """
    Detect + identify faces. Returns (annotated, results) where each result:
      {name, flat, status, reason, score, box:[x1,y1,x2,y2]}
    status in {KNOWN, WATCHLIST, UNKNOWN}
    """
    app = _load_app()
    annotated = frame.copy()
    results = []

    try:
        faces = app.get(frame)
    except Exception as e:
        print(f"[FACE] detection error: {e}")
        return annotated, results

    for f in faces:
        x1, y1, x2, y2 = map(int, f.bbox)
        match = _best_match(f.embedding)

        if match:
            rec, score = match
            status = rec["status"]
            name, flat, reason = rec["name"], rec["flat"], rec["reason"]
            if status == "WATCHLIST":
                color = _C_WATCHLIST
                label = f"WATCHLIST: {name}"
            else:
                status = "KNOWN"
                color = _C_KNOWN
                label = name + (f" - {flat}" if flat else "")
        else:
            status, name, flat, reason, score = "UNKNOWN", "UNKNOWN", "", "", 0.0
            color = _C_UNKNOWN
            label = "UNKNOWN"

        results.append({"name": name, "flat": flat, "status": status,
                        "reason": reason, "score": round(float(score), 2),
                        "box": [x1, y1, x2, y2]})

        thick = 3 if status == "WATCHLIST" else 2
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, thick)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(annotated, (x1, y1 - th - 8), (x1 + tw + 8, y1), color, -1)
        cv2.putText(annotated, label, (x1 + 4, y1 - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)

    return annotated, results


def get_embedding(frame):
    """Enrolment helper: embedding of the largest face, or (None, error)."""
    app = _load_app()
    faces = app.get(frame)
    if not faces:
        return None, "No face detected."
    if len(faces) > 1:
        faces = sorted(
            faces,
            key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]),
            reverse=True,
        )
    return faces[0].embedding.tolist(), None
