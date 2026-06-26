"""
Indian ANPR Engine — Accuracy Enhanced Edition
-------------------------------------------------
Improvements over previous version:
  1. Comprehensive character-confusion correction (0/O/D/Q, 1/I/L, B/8, S/5, etc.)
  2. State-code validation — corrects ambiguous chars to match real Indian state codes
  3. Multiple preprocessing variants per plate crop — picks BEST scoring result
  4. Combined scoring: OCR confidence + format match + valid state code bonus
  5. PlateVoter — temporal majority voting for live video (smooths out per-frame errors)
"""

import cv2
import numpy as np
import easyocr
import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime
from collections import Counter, defaultdict
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Indian state/UT lookup
# ---------------------------------------------------------------------------
STATE_CODES = {
    "AN": "Andaman & Nicobar Islands", "AP": "Andhra Pradesh",  "AR": "Arunachal Pradesh",
    "AS": "Assam",           "BR": "Bihar",              "CH": "Chandigarh",
    "CG": "Chhattisgarh",   "DN": "Dadra & Nagar Haveli","DD": "Daman & Diu",
    "DL": "Delhi",           "GA": "Goa",                "GJ": "Gujarat",
    "HR": "Haryana",         "HP": "Himachal Pradesh",   "JK": "Jammu & Kashmir",
    "JH": "Jharkhand",       "KA": "Karnataka",          "KL": "Kerala",
    "LD": "Lakshadweep",     "MP": "Madhya Pradesh",     "MH": "Maharashtra",
    "MN": "Manipur",         "ML": "Meghalaya",          "MZ": "Mizoram",
    "NL": "Nagaland",        "OD": "Odisha",             "PY": "Puducherry",
    "PB": "Punjab",          "RJ": "Rajasthan",          "SK": "Sikkim",
    "TN": "Tamil Nadu",      "TS": "Telangana",          "TR": "Tripura",
    "UP": "Uttar Pradesh",   "UK": "Uttarakhand",        "WB": "West Bengal",
}
VALID_STATE_CODES = set(STATE_CODES.keys())

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
BH_PATTERN  = re.compile(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$")
OLD_PATTERN = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$")

# ---------------------------------------------------------------------------
# Character confusion map — characters commonly mixed up by OCR
# Each entry: char -> list of visually similar alternates (incl. itself)
# ---------------------------------------------------------------------------
CONFUSION = {
    "0": ["0", "O", "D", "Q"],
    "O": ["O", "0", "D", "Q"],
    "D": ["D", "0", "O"],
    "Q": ["Q", "0", "O"],
    "1": ["1", "I", "L", "J"],
    "I": ["I", "1", "L"],
    "L": ["L", "1", "I"],
    "2": ["2", "Z"],
    "Z": ["Z", "2"],
    "5": ["5", "S"],
    "S": ["S", "5"],
    "6": ["6", "G", "C"],
    "G": ["G", "6", "C"],
    "8": ["8", "B"],
    "B": ["B", "8"],
    "4": ["4", "A"],
    "A": ["A", "4"],
    "9": ["9", "g", "P"],
    "7": ["7", "T"],
    "T": ["T", "7"],
    "V": ["V", "Y"],
    "Y": ["Y", "V"],
    "U": ["U", "V"],
    "M": ["M", "N", "H"],
    "N": ["N", "M"],
    "H": ["H", "M"],
    "K": ["K", "X"],
    "X": ["X", "K"],
    "E": ["E", "F"],
    "F": ["F", "E"],
    "R": ["R", "P"],
    "P": ["P", "R"],
}

LETTERS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGITS  = set("0123456789")


def _alternates(ch: str, want: str) -> list:
    """
    Return possible corrected characters for `ch` that satisfy `want`
    ("L" for letter, "D" for digit). Always includes original char.
    """
    options = CONFUSION.get(ch, [ch])
    if want == "L":
        filtered = [c for c in options if c in LETTERS]
    else:
        filtered = [c for c in options if c in DIGITS]
    return filtered or [ch]


# ---------------------------------------------------------------------------
# Plate format templates: list of "L"/"D" per position, by total length
# ---------------------------------------------------------------------------
def _format_template(length: int, is_bh_hint: bool = False):
    """
    Returns a list like ['L','L','D','D','L','L','D','D','D','D']
    describing expected char type at each position, or None if
    no known template matches this length.
    """
    if is_bh_hint and length == 10:
        # YY BH NNNN XX  -> D D L L D D D D L L
        return list("DDLLDDDDLL")
    if length == 10:
        # XX DD LLL DDDD  (e.g. PB10ABC1234) -> but 3-letter series is rarer
        return list("LLDDLLLDDD" + "D")[:10]  # fallback, rarely used
    if length == 9:
        # XX DD LL DDDD  (e.g. PB10AB1234 minus... actually 9 = XXDLDDDD?)
        # Most common 9-char: XX D LL DDDD  (single digit district)
        return list("LDLLDDDD" + "D")[:9]
    return None


def _best_template_for(cleaned: str) -> Optional[list]:
    """Pick the most sensible template based on length + BH hint."""
    length = len(cleaned)
    if length == 10:
        # Check if looks like BH series (digits at 0,1 and 'BH' at 2,3 region)
        if cleaned[2:4] == "BH" or (cleaned[0:2].isdigit() and "BH" in cleaned[2:4]):
            return list("DDLLDDDDLL")
        return list("LLDDLLDDDD")  # standard XX DD LL DDDD
    if length == 9:
        return list("LLDLLDDDD")   # XX D LL DDDD (single digit district)
    return None


def correct_plate_text(raw: str) -> tuple:
    """
    Apply confusion-aware correction using format templates and
    state-code validation.

    Returns (corrected_text, format_type, format_score)
    """
    cleaned = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if len(cleaned) < 6:
        return cleaned, "Unknown", 0.0

    best_result = cleaned
    best_score  = -1.0
    best_format = "Unknown"

    template = _best_template_for(cleaned)
    templates_to_try = [t for t in [template] if t]

    for tmpl in templates_to_try:
        chars = list(cleaned)
        for i, want in enumerate(tmpl):
            if i >= len(chars):
                break
            ch = chars[i]
            is_letter = ch in LETTERS
            is_digit  = ch in DIGITS
            matches   = (want == "L" and is_letter) or (want == "D" and is_digit)
            if not matches:
                alts = _alternates(ch, want)
                chars[i] = alts[0]
        candidate = "".join(chars)

        # Score this candidate
        score = 0.0
        is_bh = tmpl == list("DDLLDDDDLL")

        if is_bh and BH_PATTERN.match(candidate):
            score += 0.5
            fmt = "BH Series"
        elif not is_bh and OLD_PATTERN.match(candidate):
            score += 0.5
            fmt = "Old format"
        else:
            fmt = "Old format" if not is_bh else "BH Series"

        # State code validation bonus (only for non-BH)
        if not is_bh:
            state_code = candidate[:2]
            if state_code in VALID_STATE_CODES:
                score += 0.3
            else:
                # Try correcting first 2 chars to find a valid state code
                c0_opts = CONFUSION.get(candidate[0], [candidate[0]])
                c1_opts = CONFUSION.get(candidate[1], [candidate[1]])
                found = False
                for c0 in c0_opts:
                    if c0 not in LETTERS:
                        continue
                    for c1 in c1_opts:
                        if c1 not in LETTERS:
                            continue
                        if (c0 + c1) in VALID_STATE_CODES:
                            candidate = c0 + c1 + candidate[2:]
                            score += 0.3
                            found = True
                            break
                    if found:
                        break

        if score > best_score:
            best_score  = score
            best_result = candidate
            best_format = fmt

    if best_score < 0:
        # No template matched — return cleaned as-is
        if BH_PATTERN.match(cleaned):
            return cleaned, "BH Series", 0.5
        if OLD_PATTERN.match(cleaned):
            return cleaned, "Old format", 0.5
        return cleaned, "Unknown", 0.0

    return best_result, best_format, best_score

def is_valid_indian_plate(text):

    text = re.sub(r"[^A-Z0-9]", "", text.upper())

    if len(text) < 8:
        return False

    if len(text) > 12:
        return False

    # Must contain at least 2 digits
    digit_count = sum(c.isdigit() for c in text)

    if digit_count < 2:
        return False

    # First 2 chars should be letters
    if not text[:2].isalpha():
        return False

    return True


def classify_plate_color(roi: np.ndarray):
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    yellow_mask = cv2.inRange(hsv, (18, 80, 80),  (38, 255, 255))
    green_mask  = cv2.inRange(hsv, (38, 50, 40),  (90, 255, 255))
    black_mask  = cv2.inRange(hsv, (0,  0,  0),   (180, 255, 60))
    total = roi.shape[0] * roi.shape[1]
    counts = {
        "yellow": cv2.countNonZero(yellow_mask),
        "green":  cv2.countNonZero(green_mask),
        "black":  cv2.countNonZero(black_mask),
    }
    dominant = max(counts, key=counts.get)
    labels = {
        "yellow": ("yellow", "Commercial"),
        "green":  ("green",  "Electric Vehicle"),
        "black":  ("black",  "Taxi / Rental"),
    }
    if counts[dominant] >= total * 0.15:
        return labels[dominant]
    return ("white", "Private")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class PlateResult:
    detected: bool = False
    plate_number: str = ""
    plate_number_raw: str = ""
    plate_type: str = "unknown"
    plate_label: str = "Unknown"
    state_code: str = ""
    state_name: str = ""
    series: str = "Old format"
    confidence: float = 0.0
    bbox: Optional[tuple] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "detected":     self.detected,
            "plate_number": self.plate_number,
            "plate_raw":    self.plate_number_raw,
            "plate_type":   self.plate_type,
            "plate_label":  self.plate_label,
            "state_code":   self.state_code,
            "state_name":   self.state_name,
            "series":       self.series,
            "confidence":   round(self.confidence, 2),
            "bbox":         list(self.bbox) if self.bbox else None,
            "timestamp":    self.timestamp,
            "notes":        self.notes,
        }


# ---------------------------------------------------------------------------
# Preprocessing variants — generate several enhanced versions of a crop
# ---------------------------------------------------------------------------
def generate_variants(roi: np.ndarray) -> list:
    """Return list of preprocessed images (numpy arrays) to try OCR on.
    Reduced to 2 fast variants — speed matters for live video."""
    target_h = 120
    if roi.shape[0] > 0 and roi.shape[0] != target_h:
        scale = target_h / roi.shape[0]
        roi = cv2.resize(roi, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

    variants = []

    # 1. Plain upscaled color — EasyOCR often does best directly on this
    variants.append(roi)

    # 2. CLAHE contrast enhancement — helps uneven lighting, cheap to compute
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    variants.append(enhanced)

    return variants


# ---------------------------------------------------------------------------
# Main Engine
# ---------------------------------------------------------------------------
class ANPREngine:
    def __init__(self, use_gpu: bool = False):
        logger.info("Loading EasyOCR model...")
        self.reader = easyocr.Reader(["en"], gpu=use_gpu, verbose=False)
        self._cache: dict = {}
        self._cache_ttl = 2.0
        logger.info("ANPR engine ready ⚡ (Accuracy-enhanced)")

    # ----------------------------------------------------------------
    def process_image(self, source) -> PlateResult:
        if isinstance(source, (str, Path)):
            img = cv2.imread(str(source))
            if img is None:
                raise FileNotFoundError(f"Cannot read: {source}")
        else:
            img = source.copy()

        h, w = img.shape[:2]
        if w > 800:
            scale = 800 / w
            img   = cv2.resize(img, (800, int(h * scale)),
                               interpolation=cv2.INTER_LINEAR)
        return self._detect_and_read(img)

    def draw_result(self, source, result: PlateResult) -> np.ndarray:
        if isinstance(source, (str, Path)):
            img = cv2.imread(str(source))
        else:
            img = source.copy()
        if not result.detected or result.bbox is None:
            return img
        x, y, w, h = result.bbox
        color_map = {
            "yellow":(0,215,255),"green":(0,180,0),
            "black":(60,60,60),  "white":(0,200,100)
        }
        color = color_map.get(result.plate_type, (0,200,100))
        cv2.rectangle(img, (x,y), (x+w,y+h), color, 2)
        label = f"{result.plate_number}  {result.confidence:.0f}%"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(img, (x, y-th-10), (x+tw+8, y), color, -1)
        cv2.putText(img, label, (x+4, y-6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,0,0), 2)
        return img

    # ----------------------------------------------------------------
    def _detect_and_read(self, img: np.ndarray) -> PlateResult:
        h_img, w_img = img.shape[:2]
        bottom_start = int(h_img * 0.40)
        search_area  = img[bottom_start:, :]

        # Color-based candidates first — most accurate for Indian
        # white/yellow plates, lets us exit early before trying
        # slower contour-based candidates.
        candidates = []
        for roi, (x, y, w, h) in self._find_by_color(search_area):
            candidates.append((roi, (x, y + bottom_start, w, h)))
        for roi, (x, y, w, h) in self._find_by_contours(search_area):
            candidates.append((roi, (x, y + bottom_start, w, h)))
        candidates.append((search_area, (0, bottom_start, w_img, h_img - bottom_start)))

        # Cap candidates to keep per-frame time bounded on CPU
        candidates = candidates[:4]

        best: Optional[PlateResult] = None
        best_total_score = -1.0

        # Score threshold: confidence (0-1) + format/state bonus (up to 0.8).
        # 1.1+ means high-confidence + valid format + valid state code —
        # good enough to stop searching further candidates/variants.
        EARLY_EXIT_SCORE = 1.1

        for roi, bbox in candidates:
            cache_key = self._roi_hash(roi)
            if cache_key in self._cache:
                cached, t = self._cache[cache_key]
                if time.time() - t < self._cache_ttl:
                    return cached

            plate_type, plate_label = classify_plate_color(roi)

            for variant in generate_variants(roi):
                try:
                    detections = self.reader.readtext(
                        variant,
                        detail=1,
                        paragraph=False,
                        allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                        text_threshold=0.4,
                        width_ths=0.9,
                    )
                except Exception:
                    continue

                for (_, text, conf) in detections:
                    text = text.strip()
                    if not text:
                        continue

                    corrected, fmt, fmt_score = correct_plate_text(text)
                    if not is_valid_indian_plate(corrected):
                        continue

                    total_score = conf + fmt_score

                    if total_score > best_total_score:
                        state_code = corrected[:2] if fmt != "BH Series" else "BH"
                        best_total_score = total_score
                        best = PlateResult(
                            detected        = True,
                            plate_number    = self._format_plate(corrected, fmt),
                            plate_number_raw= corrected,
                            plate_type      = plate_type,
                            plate_label     = plate_label,
                            state_code      = state_code,
                            state_name      = STATE_CODES.get(state_code, "Unknown")
                                               if fmt != "BH Series"
                                               else "All India (BH)",
                            series          = fmt,
                            confidence      = conf * 100,
                            bbox            = bbox,
                            notes           = f"fmt_score={fmt_score:.1f}",
                        )

                if best_total_score >= EARLY_EXIT_SCORE:
                    break   # good enough — skip remaining variants

            if best_total_score >= EARLY_EXIT_SCORE:
                break   # good enough — skip remaining candidates

        if best:
            self._cache[self._roi_hash(candidates[0][0])] = (best, time.time())

        return best if best else PlateResult()

    # ----------------------------------------------------------------
    def _find_by_contours(self, img: np.ndarray):
        gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur   = cv2.bilateralFilter(gray, 9, 17, 17)
        edges  = cv2.Canny(blur, 20, 180)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
        edges  = cv2.dilate(edges, kernel, iterations=1)

        contours, _ = cv2.findContours(edges, cv2.RETR_TREE,
                                        cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:20]

        candidates = []
        h_img, w_img = img.shape[:2]
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            aspect = w / h if h else 0
            if not (1.5 <= aspect <= 7.0):
                continue
            if not (0.005 <= (w*h)/(w_img*h_img) <= 0.50):
                continue
            candidates.append((img[y:y+h, x:x+w], (x, y, w, h)))
        return candidates

    def _find_by_color(self, img: np.ndarray):
        hsv     = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        h_img, w_img = img.shape[:2]
        white_mask  = cv2.inRange(hsv, (0,  0, 180), (180, 40, 255))
        yellow_mask = cv2.inRange(hsv, (18, 80,  80), (38, 255, 255))

        candidates = []
        for mask in [white_mask, yellow_mask]:
            kernel  = cv2.getStructuringElement(cv2.MORPH_RECT, (5,5))
            cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
            cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN,  kernel)
            contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL,
                                            cv2.CHAIN_APPROX_SIMPLE)
            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                aspect = w / h if h else 0
                if not (1.5 <= aspect <= 7.0):
                    continue
                if not (0.005 <= (w*h)/(w_img*h_img) <= 0.50):
                    continue
                candidates.append((img[y:y+h, x:x+w], (x, y, w, h)))
        return candidates

    @staticmethod
    def _roi_hash(roi: np.ndarray) -> int:
        small = cv2.resize(roi, (16,8), interpolation=cv2.INTER_LINEAR)
        return hash(small.tobytes())

    @staticmethod
    def _format_plate(cleaned: str, series: str) -> str:
        if series == "BH Series" and len(cleaned) >= 8:
            suffix = " " + cleaned[8:] if len(cleaned) > 8 else ""
            return f"{cleaned[:2]} BH {cleaned[4:8]}{suffix}"
        if len(cleaned) >= 8:
            return f"{cleaned[:2]} {cleaned[2:4]} {cleaned[4:6]} {cleaned[6:]}"
        return cleaned


# ---------------------------------------------------------------------------
# PlateVoter — temporal majority voting for live video
# ---------------------------------------------------------------------------
class PlateVoter:
    """
    Accumulates plate readings over a time window and returns the
    most likely "consensus" plate using per-character majority voting.

    Usage:
        voter = PlateVoter(window_seconds=2.0, min_samples=3)
        voter.add(result)            # call on every detection
        consensus = voter.get_consensus()   # returns PlateResult or None
    """
    def __init__(self, window_seconds: float = 2.0, min_samples: int = 3):
        self.window_seconds = window_seconds
        self.min_samples    = min_samples
        self._samples: list = []   # list of (PlateResult, timestamp)

    def add(self, result: PlateResult):
        if not result.detected or not result.plate_number_raw:
            return
        now = time.time()
        self._samples.append((result, now))
        # Drop old samples
        self._samples = [(r, t) for (r, t) in self._samples
                         if now - t <= self.window_seconds]

    def get_consensus(self) -> Optional[PlateResult]:
        if len(self._samples) < self.min_samples:
            return None

        # Group by length — only vote among same-length readings
        by_length = defaultdict(list)
        for r, _ in self._samples:
            by_length[len(r.plate_number_raw)].append(r)

        # Pick the most common length
        best_length = max(by_length, key=lambda k: len(by_length[k]))
        group = by_length[best_length]

        if len(group) < self.min_samples:
            return None

        # Per-character majority vote
        consensus_chars = []
        for i in range(best_length):
            chars_at_i = [r.plate_number_raw[i] for r in group
                          if i < len(r.plate_number_raw)]
            most_common = Counter(chars_at_i).most_common(1)[0][0]
            consensus_chars.append(most_common)
        consensus_plate = "".join(consensus_chars)

        # Re-validate consensus through correction
        corrected, fmt, fmt_score = correct_plate_text(consensus_plate)
        state_code = corrected[:2] if fmt != "BH Series" else "BH"

        # Use the highest-confidence sample as template for other fields
        best_sample = max(group, key=lambda r: r.confidence)

        avg_conf = sum(r.confidence for r in group) / len(group)

        return PlateResult(
            detected         = True,
            plate_number     = ANPREngine._format_plate(corrected, fmt),
            plate_number_raw = corrected,
            plate_type       = best_sample.plate_type,
            plate_label      = best_sample.plate_label,
            state_code       = state_code,
            state_name       = STATE_CODES.get(state_code, "Unknown")
                                if fmt != "BH Series" else "All India (BH)",
            series           = fmt,
            confidence       = min(99.0, avg_conf + fmt_score * 20),
            bbox             = best_sample.bbox,
            notes            = f"voted from {len(group)} samples",
        )

    def reset(self):
        self._samples = []
