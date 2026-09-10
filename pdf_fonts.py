"""
pdf_fonts.py — fonts for the PDFs Defender Octa hands to clients.

WHY THIS EXISTS
    Every PDF was drawn in Helvetica, one of ReportLab's built-in base-14
    fonts. Those cover Latin and nothing else, so a resident called
    ਗੁਰਪ੍ਰੀਤ ਕੌਰ or गुरप्रीत कौर came out as a row of empty boxes — on a
    weekly security report handed to a society committee, in Punjab.

    ReportLab has no per-glyph font fallback: one string is drawn in one
    font. So the job is to pick a font that covers the script the string is
    actually written in, and to fall back gracefully when the font files
    are not installed rather than crash a report.

WHAT IT GIVES YOU
    BODY, BOLD          — default font names, safe to pass to setFont()
    font_for(text)      — the right regular font for that string's script
    bold_for(text)      — same, bold
    register_fonts()    — idempotent; called on import

If no Unicode fonts are present, everything degrades to Helvetica and the
old behaviour: Latin fine, Indic blank. Nothing raises.
"""

import glob
import os

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Debian package fonts-noto-core installs these; the Lohit families are the
# fallback if a slimmer image is used. Order matters — first match wins.
_CANDIDATES = {
    "latin": [
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ],
    "latin_bold": [
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "deva": [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    ],
    "deva_bold": [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
    ],
    "guru": [
        "/usr/share/fonts/truetype/noto/NotoSansGurmukhi-Regular.ttf",
        "/usr/share/fonts/truetype/lohit-punjabi/Lohit-Gurmukhi.ttf",
    ],
    "guru_bold": [
        "/usr/share/fonts/truetype/noto/NotoSansGurmukhi-Bold.ttf",
    ],
}

# Registered ReportLab names, or None where the file was not found.
_REGISTERED = {}
_DONE = False

BODY = "Helvetica"
BOLD = "Helvetica-Bold"


def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    # A loose glob catches a differently-versioned package path.
    for p in paths:
        hits = sorted(glob.glob(os.path.join(os.path.dirname(p), "*.ttf")))
        base = os.path.basename(p).split("-")[0].lower()
        for h in hits:
            if base in os.path.basename(h).lower():
                return h
    return None


def register_fonts():
    """Register whatever Unicode fonts this machine has. Safe to call twice."""
    global _DONE, BODY, BOLD
    if _DONE:
        return _REGISTERED
    _DONE = True

    for key, paths in _CANDIDATES.items():
        path = _first_existing(paths)
        if not path:
            _REGISTERED[key] = None
            continue
        name = f"Octa-{key}"
        try:
            pdfmetrics.registerFont(TTFont(name, path))
            _REGISTERED[key] = name
        except Exception as e:            # a corrupt or unreadable font file
            print(f"[PDF] could not register {path}: {e}")
            _REGISTERED[key] = None

    if _REGISTERED.get("latin"):
        BODY = _REGISTERED["latin"]
    if _REGISTERED.get("latin_bold"):
        BOLD = _REGISTERED["latin_bold"]

    missing = [k for k, v in _REGISTERED.items() if not v]
    if missing:
        print(f"[PDF] no font for: {', '.join(missing)} — text in those "
              f"scripts will not render. Install fonts-noto-core.")
    return _REGISTERED


def _script_of(text) -> str:
    """'guru', 'deva' or 'latin' — whichever non-Latin script appears first.

    Deliberately simple: a name is written in one script, and mixed strings
    are rare enough that picking the non-Latin one is the better bet — Noto
    Devanagari and Gurmukhi both carry Latin glyphs, so a mixed line still
    renders. The reverse is not true.
    """
    for ch in str(text or ""):
        o = ord(ch)
        if 0x0A00 <= o <= 0x0A7F:
            return "guru"
        if 0x0900 <= o <= 0x097F:
            return "deva"
    return "latin"


def font_for(text) -> str:
    register_fonts()
    key = _script_of(text)
    return _REGISTERED.get(key) or BODY


def bold_for(text) -> str:
    register_fonts()
    key = _script_of(text)
    return (_REGISTERED.get(f"{key}_bold")
            or _REGISTERED.get(key)
            or BOLD)


register_fonts()
