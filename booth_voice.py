"""
booth_voice.py — GuardianGrid Local Booth Voice
=================================================
Speaks Guardian alerts directly out of the SERVER's own speaker, offline.
No browser, no LiveKit room, no internet, no API key.

Uses pyttsx3 (Windows SAPI5 voices) so it works even if the network is down —
the right tradeoff for a security booth where a mute 3 AM alert is the worst
outcome.

Wiring: this becomes the voice_fn for guardian_wiring. In api_server.py startup:

    from booth_voice import speak
    from guardian_wiring import wire_guardian
    wire_guardian(voice_fn=speak)

That's it — Guardian's "Hey boss..." lines now come out of the booth speaker.

------------------------------------------------------------------------------
DESIGN NOTES (honest):
- Speech runs on a BACKGROUND QUEUE + THREAD. TTS is blocking (it takes seconds
  to speak), and we must never block the Flask request/detection thread. So
  speak() just drops text on a queue and returns instantly; a worker thread
  speaks them one at a time, in order.
- If two alerts fire close together, they QUEUE (spoken one after another)
  rather than overlap into garble. For security that's correct — you want to
  hear both clearly.
- pyttsx3 on Windows must run its engine on ONE thread for its whole life;
  creating a new engine per call leaks and eventually hangs. So we hold one
  engine on the worker thread.
- Voice selection: tries to pick a female English voice if one is installed;
  otherwise uses the default. Robotic but reliable. Upgrade to cloud TTS later
  for nicer audio once the core is proven.
------------------------------------------------------------------------------
"""

import queue
import logging
import threading

logger = logging.getLogger("booth-voice")

try:
    import pyttsx3
    _TTS_OK = True
except Exception as e:            # pragma: no cover
    _TTS_OK = False
    logger.warning("pyttsx3 not available (%s) — booth voice disabled", e)

_speech_q: "queue.Queue[str]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()

# Tuning
SPEECH_RATE = 165     # words per minute; lower = clearer, higher = faster
VOLUME      = 1.0     # 0.0–1.0
PREFER_FEMALE = True  # pick a female voice if the machine has one


def _pick_voice(engine):
    """Choose a female English voice if available, else default."""
    try:
        voices = engine.getProperty("voices")
    except Exception:
        return
    if not PREFER_FEMALE:
        return
    for v in voices:
        name = (getattr(v, "name", "") or "").lower()
        # Windows commonly ships "Zira" (female US). Fall back to any 'female'.
        if "zira" in name or "female" in name:
            engine.setProperty("voice", v.id)
            logger.info("Booth voice: using '%s'", v.name)
            return
    logger.info("Booth voice: using default voice")


def _worker():
    """Single long-lived engine speaking queued lines one at a time."""
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", SPEECH_RATE)
        engine.setProperty("volume", VOLUME)
        _pick_voice(engine)
    except Exception as e:
        logger.error("Could not start TTS engine: %s", e)
        return

    logger.info("Booth voice worker ready.")
    while True:
        text = _speech_q.get()      # blocks until there's something to say
        if text is None:            # shutdown signal
            break
        try:
            engine.say(text)
            engine.runAndWait()     # blocks HERE, on the worker thread only
        except Exception as e:
            logger.error("Speech failed: %s", e)
        finally:
            _speech_q.task_done()


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        if not _TTS_OK:
            return
        th = threading.Thread(target=_worker, daemon=True, name="booth-voice")
        th.start()
        _worker_started = True


def speak(text: str):
    """
    Queue a line for the booth speaker. Returns immediately (non-blocking).
    This is the function you pass as voice_fn to wire_guardian().
    """
    if not text:
        return
    if not _TTS_OK:
        logger.info("[voice unavailable] would say: %s", text)
        return
    _ensure_worker()
    _speech_q.put(text)
    logger.info("[voice queued] %s", text)


# ── Standalone test — actually speaks out loud on a machine with audio ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import time
    print("Speaking a test alert through the local speaker...")
    speak("Hey boss, a blacklisted vehicle has entered. "
          "Plate D L 3 C A B 5 6 7 8. Please respond.")
    speak("This is a second alert, spoken after the first.")
    # keep the process alive long enough to finish speaking
    time.sleep(12)
    print("Done.")
