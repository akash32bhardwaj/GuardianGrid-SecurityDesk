"""
booth_voice_ml.py — multilingual booth announcements (Hindi + English v1)
--------------------------------------------------------------------------
Speaks security announcements at the guard booth in Hindi first, then
English, using Windows' offline SAPI voices via pyttsx3. A guard
half-asleep at 2 AM responds to words in his own language, not beeps —
and a talk-down line in Hindi lands harder on an intruder than English.

PUNJABI: Windows ships no Punjabi SAPI voice, so offline Punjabi TTS is
not possible today. The full Punjabi (Gurmukhi) phrase catalog is
included below, ready for the day we wire an online neural voice
(edge-tts / Google). Until then hi+en covers every guard in Punjab.

Setup on the booth PC (one time):
  Windows Settings -> Time & Language -> Language & region ->
  Add a language -> हिन्दी (Hindi) -> tick "Text-to-speech" -> install.
  Then verify:   python booth_voice_ml.py --voices
  Test:          python booth_voice_ml.py --test panic

Usage from code:
    from booth_voice_ml import announce
    announce("talkdown", camera="Main Gate")   # speaks hi then en
    announce("panic", camera="Gate 2")

Every call is queued on a worker thread — never blocks the caller,
never raises to the caller. If no Hindi voice is installed, English
alone is spoken and a one-time console hint is printed.
"""
import argparse
import queue
import threading

# ── Phrase catalog ───────────────────────────────────────────────
# {key: {"hi": Devanagari, "pa": Gurmukhi (future), "en": English}}
PHRASES = {
    "panic": {
        "hi": "आपातकाल। {camera} पर तुरंत सहायता भेजें। सुरक्षा दल सतर्क हो जाएं।",
        "pa": "ਐਮਰਜੈਂਸੀ। {camera} ਤੇ ਤੁਰੰਤ ਮਦਦ ਭੇਜੋ। ਸੁਰੱਖਿਆ ਟੀਮ ਸਾਵਧਾਨ ਹੋ ਜਾਵੇ।",
        "en": "Emergency. Guard assistance required at {camera} immediately.",
    },
    "talkdown": {
        "hi": ("ध्यान दें। आप कैमरे की निगरानी में हैं और आपकी रिकॉर्डिंग हो रही है। "
               "सुरक्षा को सूचित कर दिया गया है। तुरंत परिसर छोड़ दें।"),
        "pa": ("ਧਿਆਨ ਦਿਓ। ਤੁਸੀਂ ਕੈਮਰੇ ਦੀ ਨਿਗਰਾਨੀ ਵਿੱਚ ਹੋ ਅਤੇ ਤੁਹਾਡੀ ਰਿਕਾਰਡਿੰਗ ਹੋ ਰਹੀ ਹੈ। "
               "ਸੁਰੱਖਿਆ ਨੂੰ ਸੂਚਿਤ ਕਰ ਦਿੱਤਾ ਗਿਆ ਹੈ। ਤੁਰੰਤ ਇਲਾਕਾ ਛੱਡ ਦਿਓ।"),
        "en": ("Attention. You are under camera surveillance and being "
               "recorded. Security has been notified. Leave the premises "
               "immediately."),
    },
    "weapon": {
        "hi": "चेतावनी। {camera} पर हथियार का संदेह। सभी गार्ड तुरंत सतर्क हों।",
        "pa": "ਚੇਤਾਵਨੀ। {camera} ਤੇ ਹਥਿਆਰ ਦਾ ਸ਼ੱਕ। ਸਾਰੇ ਗਾਰਡ ਤੁਰੰਤ ਸਾਵਧਾਨ ਹੋਣ।",
        "en": "Warning. Possible weapon detected at {camera}. All guards alert.",
    },
    "unknown_vehicle": {
        "hi": "{camera} पर अज्ञात वाहन {plate}। कृपया जाँच करें।",
        "pa": "{camera} ਤੇ ਅਣਪਛਾਤਾ ਵਾਹਨ {plate}। ਕਿਰਪਾ ਕਰਕੇ ਜਾਂਚ ਕਰੋ।",
        "en": "Unknown vehicle {plate} at {camera}. Please verify.",
    },
    "recon": {
        "hi": "सावधान। वाहन {plate} बार-बार गेट के पास देखा गया है। नज़र रखें।",
        "pa": "ਸਾਵਧਾਨ। ਵਾਹਨ {plate} ਵਾਰ-ਵਾਰ ਗੇਟ ਕੋਲ ਦੇਖਿਆ ਗਿਆ ਹੈ। ਨਜ਼ਰ ਰੱਖੋ।",
        "en": "Caution. Vehicle {plate} has passed the gate repeatedly. Keep watch.",
    },
    "visitor": {
        "hi": "{flat} के लिए आगंतुक गेट पर है।",
        "pa": "{flat} ਲਈ ਮਹਿਮਾਨ ਗੇਟ ਤੇ ਹੈ।",
        "en": "Visitor at the gate for {flat}.",
    },
    "night_armed": {
        "hi": "रात्रि निगरानी सक्रिय। डिफेंडर ऑक्टा आपकी सुरक्षा में है।",
        "pa": "ਰਾਤ ਦੀ ਨਿਗਰਾਨੀ ਸਰਗਰਮ। ਡਿਫੈਂਡਰ ਔਕਟਾ ਤੁਹਾਡੀ ਸੁਰੱਖਿਆ ਵਿੱਚ ਹੈ।",
        "en": "Night watch armed. Defender Octa is protecting this site.",
    },
}

LANGS = ["hi", "en"]        # spoken order; "pa" joins when a voice exists
_RATE = 155                 # slightly slower than default for clarity

# ── Engine (worker thread + queue; import-safe on any OS) ────────
_q = queue.Queue()
_started = False
_hindi_hint_shown = False


def _find_voice(engine, want):
    """Return a SAPI voice id matching the language, or None."""
    needles = {
        "hi": ("hindi", "kalpana", "hemant", "hi-in", "hi_in"),
        "en": ("zira", "david", "english", "en-us", "en-gb"),
        "pa": ("punjabi", "pa-in"),
    }[want]
    try:
        for v in engine.getProperty("voices"):
            blob = f"{v.id} {v.name} {getattr(v, 'languages', '')}".lower()
            if any(n in blob for n in needles):
                return v.id
    except Exception:
        pass
    return None


def _worker():
    global _hindi_hint_shown
    try:
        import pyttsx3
    except ImportError:
        print("[VOICE-ML] pyttsx3 not installed — announcements disabled "
              "(pip install pyttsx3)")
        while True:
            _q.get()  # drain silently
    while True:
        lang, text = _q.get()
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", _RATE)
            vid = _find_voice(engine, lang)
            if vid:
                engine.setProperty("voice", vid)
            elif lang == "hi":
                if not _hindi_hint_shown:
                    print("[VOICE-ML] No Hindi voice found. Install: Settings "
                          "> Time & Language > Language & region > Add "
                          "Hindi > Text-to-speech. Speaking English only.")
                    _hindi_hint_shown = True
                engine.stop()
                continue
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception as e:
            print(f"[VOICE-ML] speak error ({lang}): {e}")


def _ensure_worker():
    global _started
    if not _started:
        threading.Thread(target=_worker, daemon=True,
                         name="booth-voice-ml").start()
        _started = True


# ── Public API ───────────────────────────────────────────────────
def announce(key, **fmt):
    """Queue a catalogued announcement in each configured language.
    Unknown keys fall back to speaking the key text itself in English."""
    _ensure_worker()
    entry = PHRASES.get(key)
    if not entry:
        _q.put(("en", str(key)))
        return
    for lang in LANGS:
        text = entry.get(lang)
        if not text:
            continue
        try:
            text = text.format(**fmt)
        except (KeyError, IndexError):
            pass  # missing placeholder -> speak with braces, still useful
        _q.put((lang, text))


def speak(text, lang="en"):
    """Free-text speech (compat with booth_voice.speak signature)."""
    _ensure_worker()
    _q.put((lang, str(text)))


# ── CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", action="store_true",
                    help="list installed SAPI voices")
    ap.add_argument("--test", metavar="KEY",
                    help=f"speak a phrase: {', '.join(PHRASES)}")
    args = ap.parse_args()
    if args.voices:
        import pyttsx3
        e = pyttsx3.init()
        for v in e.getProperty("voices"):
            print(f"  {v.name}  ->  {v.id}")
    elif args.test:
        announce(args.test, camera="Main Gate", plate="PB10AB1234",
                 flat="B-302")
        import time
        time.sleep(12)  # let the queue speak before the process exits
    else:
        ap.print_help()
