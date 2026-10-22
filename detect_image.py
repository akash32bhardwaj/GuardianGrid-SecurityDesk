"""
detect_image.py
---------------
Two jobs:

1. The ADAPTER the Gate Console uses. gate_capture._try_ocr() imports this
   module and looks for one of several function names; read_plate() is one
   of them, so wiring it here needs no change to gate_capture.py at all.

   OCT-175. Before this existed, _try_ocr probed six names, found none --
   this file held only main() -- and returned (None, 0) for every photo a
   guard ever took. Three months, not one plate read, and silently: the
   fallback is "the guard types it", which is indistinguishable from the
   OCR simply not finding a plate. The near miss was that _try_ocr probes
   for `process_image`, which IS the right name; it is a method on
   ANPREngine, not a module-level function here.

2. The command line, for one image or a whole folder:

    python detect_image.py car.jpg
    python detect_image.py car.jpg --show --save
    python detect_image.py --batch output/webcam        # measure accuracy
"""

import argparse
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ── the engine, loaded once, lazily ─────────────────────────────────
#
# ANPREngine.__init__ loads EasyOCR: ~1.2GB resident, several seconds.
# Constructing one per request would kill the container, and constructing
# one at import time would make every worker pay for it whether or not a
# photo is ever taken. So: one instance, built on first use, behind a lock
# because Waitress serves requests on multiple threads.
#
# _ENGINE_FAILED latches. If the engine cannot be built -- no easyocr, no
# weights, no network to fetch them -- say so once and return None after
# that, rather than raising on every photo a guard takes.

_ENGINE = None
_ENGINE_FAILED = False
_ENGINE_LOCK = threading.Lock()


def get_engine():
    """The shared ANPREngine, or None if it cannot be built. Never raises."""
    global _ENGINE, _ENGINE_FAILED
    if _ENGINE is not None or _ENGINE_FAILED:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None and not _ENGINE_FAILED:
            try:
                from core.anpr_engine import ANPREngine
                logger.info("[ANPR] loading engine (first plate read)")
                _ENGINE = ANPREngine(use_gpu=False)
                logger.info("[ANPR] engine ready")
            except Exception as e:
                _ENGINE_FAILED = True
                logger.error(
                    "[ANPR] engine unavailable, guards will type plates by "
                    "hand: %s: %s", type(e).__name__, e)
    return _ENGINE


def read_plate(image_path):
    """{plate, confidence, ...} for a photo, or None. Never raises.

    gate_capture._try_ocr() accepts exactly this dict shape.

    `confidence` is deliberately 0..1. PlateResult.confidence is a
    PERCENTAGE (the engine does `conf * 100`), while _try_ocr defaults to
    0.8 and gate_capture stores the value as-is -- so handing the raw 87.0
    through would put a confidence of 8700% on the event.
    """
    engine = get_engine()
    if engine is None:
        return None
    try:
        r = engine.process_image(str(image_path))
    except Exception as e:
        logger.warning("[ANPR] read failed for %s: %s", image_path, e)
        return None

    # OCT-179. The engine has now FOUND the plate; reading it is a separate
    # job, and EasyOCR alone is the weakest part of this pipeline (56% on the
    # 91-capture corpus, against 93% for two model families voting). Re-read
    # the detected region with the combiner and prefer its answer.
    #
    # Everything here degrades rather than fails: no bbox, no crop, no
    # combiner, or an exception, and the engine's own read stands.
    combined = None
    try:
        if r.bbox:
            import cv2
            from core.plate_combiner import read_plate_crop
            img = cv2.imread(str(image_path))
            if img is not None:
                x, y, w, h = r.bbox
                # process_image() shrinks anything wider than 800px BEFORE
                # detecting, so bbox is in resized coordinates. Cropping the
                # full-size original with them lands short and cuts the last
                # characters off every plate -- measured as DL3CAB56 for
                # DL3CAB5678, and 50.5% overall, worse than no combiner at
                # all. Map the box back to original pixels first.
                src_w = img.shape[1]
                if src_w > 800:
                    inv = src_w / 800.0
                    x, y, w, h = (int(x * inv), int(y * inv),
                                  int(w * inv), int(h * inv))
                # Pad generously vertically: the engine's box is tight on the
                # characters, and a recogniser trained on plates expects to
                # see the plate's border.
                # Padding swept on the corpus: 0.07/0.25 -> 83.5%,
                # 0.10/0.35 -> 84.6%, 0.14/0.45 -> 82.4%, 0.20/0.55 -> 74.7%.
                # Too little clips the characters, too much feeds the
                # recogniser bumper text and badge lettering to read.
                px, py = int(w * 0.10), int(h * 0.35)
                y0, y1 = max(0, y - py), min(img.shape[0], y + h + py)
                x0, x1 = max(0, x - px), min(img.shape[1], x + w + px)
                crop = img[y0:y1, x0:x1]
                if crop.size:
                    plate, conf, votes = read_plate_crop(crop, engine.reader)
                    if plate:
                        combined = (plate, conf, votes)
    except Exception as e:
        logger.warning("[ANPR] combiner unavailable for %s: %s: %s",
                       image_path, type(e).__name__, e)

    if combined:
        plate, conf, votes = combined
        logger.info("[ANPR] %s -> %s (%.2f) votes=%s", image_path, plate, conf,
                    [(v, p) for v, p, _ in votes])
        return {
            "plate": plate,
            "confidence": max(0.0, min(1.0, float(conf))),
            "state": r.state_name or "",
            "series": r.series or "",
            "plate_label": r.plate_label or "",
            "votes": [{"reader": v, "plate": p, "conf": round(float(c), 3)}
                      for v, p, c in votes],
        }

    # Not detected also covers a read the OCT-169 validator refused -- an
    # impossible state code, a bad shape. Refusing is the useful answer:
    # the guard types ten characters, instead of a wrong plate entering the
    # vehicle history with a confidence score attached.
    if not r.detected or not (r.plate_number_raw or r.plate_number):
        return None

    conf = float(r.confidence or 0.0) / 100.0
    return {
        "plate": r.plate_number_raw or r.plate_number,
        "confidence": max(0.0, min(1.0, conf)),
        "state": r.state_name or "",
        "series": r.series or "",
        "plate_label": r.plate_label or "",
    }


# NO ALIASES, deliberately. _try_ocr probes its six candidate names in
# order and MOVES ON when one returns nothing -- it cannot tell "no plate in
# this photo" from "wrong function". With read_plate also exposed as
# detect_plate and detect_plate_from_image, a photo with no readable plate
# would run the whole engine three times, several seconds each, on the one
# path where a guard is stood at a gate waiting. One name, one call.


# ── command line ────────────────────────────────────────────────────

def _one(path, show=False, save=False):
    import cv2
    engine = get_engine()
    if engine is None:
        raise SystemExit("[ERROR] ANPR engine could not be loaded")
    result = engine.process_image(path)
    print("\n" + "=" * 50)
    print("  ANPR Result")
    print("=" * 50)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    print("=" * 50 + "\n")
    if show or save:
        img = cv2.imread(str(path))
        annotated = engine.draw_result(img, result)
        if save:
            out = path.parent / f"{path.stem}_annotated{path.suffix}"
            cv2.imwrite(str(out), annotated)
            print(f"[SAVED] {out}")
        if show:
            cv2.imshow("ANPR Result", annotated)
            print("[INFO] Press any key to close window.")
            cv2.waitKey(0)
            cv2.destroyAllWindows()


def _batch(folder):
    """Read every image in a folder and report. OCT-169 / OCT-175.

    output/webcam names each capture after the plate the engine read at the
    time, so running this over that folder compares the engine against its
    own earlier output. That is reproducibility, NOT accuracy -- only a
    human reading the photographs gives ground truth.
    """
    folder = Path(folder)
    images = sorted(p for p in folder.iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not images:
        raise SystemExit(f"no images in {folder}")
    print(f"{len(images)} images in {folder}\n")
    import time
    agree = refused = differ = 0
    times = []
    for p in images:
        t0 = time.time()
        got = read_plate(p)
        times.append(time.time() - t0)
        expected = p.stem.split("_")[0].upper()
        plate = (got or {}).get("plate", "")
        if not plate:
            refused += 1
            mark = "REFUSED"
        elif plate == expected:
            agree += 1
            mark = "same"
        else:
            differ += 1
            mark = "DIFFERS"
        print(f"  {mark:8} {expected:14} -> {plate or '-':14} "
              f"{(got or {}).get('confidence', 0):.2f}  {times[-1]:.1f}s")
    n = len(images)
    print(f"\n  same as filename : {agree}/{n}")
    print(f"  differs          : {differ}/{n}")
    print(f"  refused          : {refused}/{n}")
    print(f"  seconds/image    : avg {sum(times)/n:.1f}  max {max(times):.1f}")
    print("\n  'same' means reproducible, not correct. The filenames are this")
    print("  engine's own earlier output; roughly a quarter of them are wrong")
    print("  (OCT-169). Only your eyes on the photographs give ground truth.")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Run ANPR on an image or folder")
    ap.add_argument("image", nargs="?", help="Path to a vehicle image")
    ap.add_argument("--batch", metavar="FOLDER", help="Read every image in FOLDER")
    ap.add_argument("--show", action="store_true", help="Display annotated image")
    ap.add_argument("--save", action="store_true", help="Save annotated image")
    args = ap.parse_args()

    if args.batch:
        return _batch(args.batch)
    if not args.image:
        return ap.print_help()
    path = Path(args.image)
    if not path.exists():
        raise SystemExit(f"[ERROR] File not found: {path}")
    _one(path, args.show, args.save)


if __name__ == "__main__":
    main()
