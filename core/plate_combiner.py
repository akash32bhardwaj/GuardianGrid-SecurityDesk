"""
plate_combiner.py — read a plate with two different kinds of model and let
them vote.  OCT-179.
----------------------------------------------------------------------------
WHY THIS EXISTS, in one measurement.  All 91 captures in output/webcam,
scored against ground truth established by looking at the photographs:

    EasyOCR alone (what shipped)        56%
    + this combiner                     93%
    + matching against the resident list 95%

EasyOCR is a GENERAL English text model.  It has never been shown a number
plate font, and it fails in one consistent way: the digit 4 reads as 1 (15
times in 91 images) and 7 reads as 2 (11 times).  No amount of preprocessing,
contrast, upscaling or decoder tuning moved those two numbers -- measured
across 16 preprocessing combinations and 5 decoder settings.

The CCT models in fast-plate-ocr are trained ON NUMBER PLATES.  They are
weaker overall here (~59%) but they fail DIFFERENTLY: 7 -> 6, 2 -> Z, 5 -> 3.
They do not make the 4 -> 1 mistake at all.

That is the whole idea.  Two readers that fail in the same way are one
reader; two that fail differently can check each other.  When they agreed on
these 91 images, they were right every time.

WHAT IS DELIBERATELY NOT DONE HERE:

  * No digit-to-digit entries were added to the CONFUSION map.  That would
    let the engine rewrite any 1 into a 4 with no evidence, trading 50 good
    reads for a chance at 26 bad ones.
  * Only STRUCTURALLY VALID plates get a vote.  A dropped character
    ("UP53DV906") is wrong in a way that agrees with other dropped
    characters -- when six models were allowed to vote freely, three weak
    ones outvoted two correct ones and accuracy fell from 93% to 73%.
    More opinions is not more accuracy.
  * Nothing is auto-corrected against the resident list in here.  That is
    the caller's job and it must be shown to the guard (OCT-171), because a
    visitor's plate can legitimately be one character from a resident's.

COST, measured (this machine; the droplet is slower):

    easy-color only                 69%   ~200ms
    easy-color + both CCT           92%   ~223ms     <- best value
    easy x2   + both CCT            93%   ~423ms     <- default
    both CCT only, no EasyOCR       61%    ~23ms

If the engine ever needs to run on live video again rather than a guard's
photo, FAST_PROFILE drops easy-sharp and costs about one point.
"""

import logging
import os
import threading

import cv2
import numpy as np

from core.anpr_engine import correct_plate_text, is_valid_indian_plate

logger = logging.getLogger(__name__)

ALLOW = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Weights: a plateau, not a peak.  Anything in w_easy 0.8-1.2 against
# w_fpo 1.0-1.6 gives the same 93%, and leave-one-vehicle-out reproduces it,
# so these are not fitted to the seven cars in the corpus.
W_EASY = 1.0
W_FPO = 1.3

FPO_MODELS = ("cct-xs-v2-global-model", "cct-s-v2-global-model")
FAST_PROFILE = os.environ.get("OCTA_ANPR_FAST") == "1"

_recs = None
_recs_failed = False
_lock = threading.Lock()


def _load_fpo():
    """The plate-trained models, loaded once.  Absent is survivable: the
    combiner degrades to EasyOCR alone, which is what shipped before."""
    global _recs, _recs_failed
    if _recs is not None or _recs_failed:
        return _recs
    with _lock:
        if _recs is None and not _recs_failed:
            try:
                from fast_plate_ocr import LicensePlateRecognizer
                _recs = {name.split("-global")[0]: LicensePlateRecognizer(name)
                         for name in FPO_MODELS}
                logger.info("[ANPR] plate models loaded: %s", list(_recs))
            except Exception as e:
                _recs_failed = True
                logger.warning(
                    "[ANPR] fast-plate-ocr unavailable (%s: %s) — falling back "
                    "to EasyOCR alone, which measured 56%% on the corpus",
                    type(e).__name__, e)
    return _recs


def _clean(s):
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def _canon(raw):
    """A valid plate, or None.  Invalid reads do not get a vote."""
    raw = _clean(raw)
    if len(raw) < 8:
        return None
    fixed, _fmt, _score = correct_plate_text(raw)
    cand = fixed or raw
    return cand if is_valid_indian_plate(cand) else None


def _easy_votes(reader, crop):
    """EasyOCR, once per preprocessing variant.

    Candidate assembly matters more than preprocessing: Indian plates carry
    IND and often a state name beside the number, so joining every detection
    gives a 13-character string the validator correctly refuses.  Trying each
    detection and each run of up to three adjacent ones, then keeping the
    best VALID candidate, is worth about 11 points on its own.
    """
    out = []
    variants = [("color", 120)] if FAST_PROFILE else [("color", 120), ("sharp", 120)]
    for mode, target_h in variants:
        img = crop
        if img.shape[0] != target_h:
            s = target_h / img.shape[0]
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
        if mode == "sharp":
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            img = cv2.addWeighted(g, 1.6, cv2.GaussianBlur(g, (0, 0), 3), -0.6, 0)
        try:
            dets = reader.readtext(img, detail=1, paragraph=False, allowlist=ALLOW,
                                   text_threshold=0.4, width_ths=0.9,
                                   decoder="beamsearch")
        except Exception as e:
            logger.debug("[ANPR] easyocr variant %s failed: %s", mode, e)
            continue
        dets = sorted(dets, key=lambda d: d[0][0][0])
        best = None
        for i in range(len(dets)):
            for j in range(i, min(i + 3, len(dets))):
                txt = _clean("".join(d[1] for d in dets[i:j + 1]))
                if len(txt) < 8:
                    continue
                conf = float(np.mean([d[2] for d in dets[i:j + 1]]))
                cand = _canon(txt)
                if cand and (best is None or conf > best[1]):
                    best = (cand, conf)
        if best:
            out.append(("easy-" + mode, best[0], best[1]))
    return out


def _fpo_votes(crop):
    recs = _load_fpo()
    if not recs:
        return []
    out = []
    for name, rec in recs.items():
        try:
            pred = rec.run(crop, return_confidence=True)[0]
        except Exception as e:
            logger.debug("[ANPR] %s failed: %s", name, e)
            continue
        cand = _canon(getattr(pred, "plate", ""))
        if not cand:
            continue
        probs = getattr(pred, "char_probs", None)
        if probs is not None and len(probs):
            mean_p, min_p = float(np.mean(probs)), float(np.min(probs))
        else:
            mean_p = min_p = 0.8
        # One weak character sinks the whole read: a plate is only as good
        # as its least certain digit, and that digit is usually the 4.
        out.append((name, cand, mean_p * (0.5 + 0.5 * min_p)))
    return out


def read_plate_crop(crop, easyocr_reader):
    """Read a plate from a tight crop.

    Returns (plate|None, confidence 0-1, votes) where votes is the list of
    (voter, plate, confidence) so a caller can show WHY, and so a disagreement
    can be surfaced instead of hidden.
    """
    if crop is None or getattr(crop, "size", 0) == 0:
        return None, 0.0, []
    votes = _easy_votes(easyocr_reader, crop) + _fpo_votes(crop)
    if not votes:
        return None, 0.0, []

    score, backers = {}, {}
    for voter, plate, conf in votes:
        w = W_EASY if voter.startswith("easy") else W_FPO
        score[plate] = score.get(plate, 0.0) + w * conf
        backers.setdefault(plate, set()).add(voter)

    best = max(score, key=score.get)
    total = sum(score.values()) or 1.0
    share = score[best] / total
    agree = len(backers[best])
    raw = min(1.0, share * (0.70 + 0.10 * agree))
    return best, _calibrate(raw), votes


# Measured on the 91 captures, raw dominance score against whether the read
# was actually right:
#
#     raw < 0.40     4 reads,   0 correct      0.0%
#     raw >= 0.40   79 reads,  77 correct     97.5%
#
# Dominance is not a probability: a correct read that two of four readers
# disputed scores ~0.52, and the UI's amber floor (OCT-177) was calibrated
# at 0.70 against EasyOCR's OWN confidence, which means something else
# entirely. Shipping the raw number would have turned almost every correct
# read amber -- and a warning that fires on everything is a warning nobody
# reads.
#
# So the scale is mapped here, where the measurement lives, rather than by
# changing the floor in the dashboard. 0.40 raw lands exactly on 0.70, the
# existing floor, and the UI needs no change. It also keeps the fallback
# honest: if the plate models are missing, detect_image returns EasyOCR's
# own confidence untouched, on the scale the floor was built for.
#
# What this is NOT: a promise. Two wrong reads scored 0.76 and 0.80 after
# calibration. Above the floor means "usually right", never "verified" --
# the same rule OCT-177 states.
CALIB_KNEE = 0.40


def _calibrate(raw):
    if raw < CALIB_KNEE:
        return round(0.35 * (raw / CALIB_KNEE), 3)
    return round(0.70 + 0.30 * min(1.0, (raw - CALIB_KNEE) / 0.50), 3)
