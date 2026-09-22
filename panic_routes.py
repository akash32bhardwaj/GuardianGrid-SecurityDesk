"""
panic_routes.py — guard panic button (manual Tier 3)
-----------------------------------------------------
One POST from the booth screen fires the full Tier-3 response chain:

  1. HIGH alert onto the SSE stream  -> Night Watch fullscreen takeover
  2. Incident case file created      -> evidence trail starts immediately
  3. Booth voice announcement        -> audible on site
  4. Ops WhatsApp                    -> founder/ops phone buzzes

Every integration is defensive: whatever exists fires, whatever is
missing is skipped with a console note — the button must NEVER fail
just because one channel is down.

--------------------------------------------------------------------------
OCT-70. The defensive design above is right and is kept exactly as it was.
What was wrong was the verdict at the end.

Each channel caught its own exception, wrote the failure into a string in
the `results` dict, and then the function ended with an unconditional

    return jsonify({"success": True, "results": results, ...})

So if all four channels failed, the guard got `success: true`. Pressing the
panic button would show a confirmation and raise nobody. On a control that
exists for an emergency, a false confirmation is worse than an error — the
guard stops escalating because the screen told them help is coming.

Tested 17 Sep 2026 and all four channels fired: alert sent, incident
GG-0082 created, booth voice spoken in Hindi and English, ops WhatsApp
delivered. The button works today. This is about the day one of them stops,
and the realistic path there is the Twilio credential in OCT-42 — an
expired token turns WhatsApp into a caught exception and a green tick.

Two changes:

  1. `success` is now computed from what actually fired, not asserted.

  2. Firing is judged against the channels that REACH A PERSON. Creating an
     incident case file is an evidence record, not a call for help. If the
     only thing that worked was writing a row to the database, nobody has
     been told, and the honest answer is that the alarm did not go out.
     Reporting success on the strength of a database write would be the
     same bug in a smaller costume.

The response keeps the old `results` dict of strings so any existing caller
is unaffected, and adds `status`, `notified`, `failed` and a plain-language
`message` the booth screen can put in front of the guard.
--------------------------------------------------------------------------

Wiring in api_server.py (after push_alert is defined):

    from panic_routes import register_panic
    register_panic(app, push_alert)

The route expects the guard to be logged in (it rides your existing
before_request auth like every other /api route).
"""
import sys
import threading
import time
from datetime import datetime


# Channels that put the emergency in front of a human being. "incident" is
# deliberately not here: it is the evidence trail, and a case file nobody
# has been told about is not a response.
NOTIFY_CHANNELS = ("alert", "voice", "whatsapp")


# ── Rate limiting (OCT-88) ────────────────────────────────────────
# /api/panic is now exempt from the VIEWER read-only rule, so a demo or
# QR visitor can raise an alarm. That is deliberate: a blocked alarm
# costs more than a false one. But it means the button is reachable by
# anyone who can open the dashboard, so it needs a bound.
#
# Two different limits, because they answer two different problems.
#
# COOLDOWN is for the honest case. A guard who is frightened presses the
# button twice. The second press must NOT be refused — being told "no"
# during an emergency is exactly the failure this whole finding is about.
# It is acknowledged instead: the alarm is already out, and the response
# says so.
#
# HOURLY_CAP is for the dishonest case: a prospect holding the button
# down, or a script. That one does refuse, because past a certain volume
# the presses are no longer information.

COOLDOWN_SECONDS = 60
HOURLY_CAP = 10

_recent = {}            # identity -> [epoch, ...]
_recent_lock = threading.Lock()


def _identity(request):
    """Who is pressing. Falls back to IP for an unauthenticated caller."""
    user = getattr(request, "auth_user", None) or {}
    return (user.get("username") or user.get("sub")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "unknown")


def _check_rate(who):
    """(allowed, seconds_since_last, presses_this_hour)."""
    now = time.time()
    with _recent_lock:
        hits = [t for t in _recent.get(who, []) if now - t < 3600]
        since = (now - hits[-1]) if hits else None
        if len(hits) >= HOURLY_CAP:
            _recent[who] = hits
            return False, since, len(hits)
        hits.append(now)
        _recent[who] = hits
        return True, since, len(hits)


# ── Does this site have a booth speaker at all? ─────────────────────
# OCT-97 made the voice channel honest: it only counts as reached when the
# speech engine actually started. On a site hosted in the cloud that is
# never true - the droplet has no speaker; the speaker (if any) lives on
# the on-site box - so every panic press reported AMBER "partly sent" even
# when every channel that exists had fired. Correct, but misleading, and it
# is what a prospect sees on the demo.
#
# site_config.json can now say so:   "booth_voice": false
# Then the voice channel is "not applicable": it is reported, but it is
# neither a success nor a failure, and the status reflects the channels the
# site really has. Missing key = true, so an on-site install with a speaker
# behaves exactly as before. Read on every press, so editing the file takes
# effect without a restart.

def _booth_voice_enabled():
    try:
        import json
        try:
            from site_config import resolve_site_config_path
            path = resolve_site_config_path()
        except Exception:
            path = "site_config.json"
        with open(path, encoding="utf-8") as f:
            v = json.load(f).get("booth_voice", True)
    except Exception:
        return True               # unknown -> behave as before
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "off", "no", "0", "none")
    return bool(v)


def register_panic(app, push_alert=None):

    # OCT-97: probe the speech engine at startup, so the first real panic
    # press already knows whether this host can speak instead of waiting to
    # find out. Failure here is information, not an error.
    for _mod in ("booth_voice_ml", "booth_voice"):
        try:
            __import__(_mod).warmup()
        except Exception:
            pass

    @app.route("/api/panic", methods=["POST"])
    def guard_panic():
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        note = (data.get("note") or "").strip()
        camera = (data.get("camera") or "Main Gate").strip()
        operator = (data.get("operator") or "guard").strip()
        stamp = datetime.now().strftime("%I:%M %p")

        who = _identity(request)
        allowed, since_last, this_hour = _check_rate(who)

        if not allowed:
            # Refusing here is safe: HOURLY_CAP presses have already gone
            # out in the last hour, so an alarm HAS been raised.
            print(f"[PANIC] rate limit hit by {who} ({this_hour}/h)",
                  file=sys.stderr, flush=True)
            return jsonify({
                "success": False, "status": "rate_limited",
                "message": (f"The alarm has already been raised {this_hour} "
                            f"times in the last hour. If this is a real "
                            f"emergency, call your supervisor and the police "
                            f"directly now."),
                "notified": [], "failed": [], "results": {},
                "time": datetime.now().isoformat(),
            }), 429

        if since_last is not None and since_last < COOLDOWN_SECONDS:
            # NOT an error. The alarm is out; say so and stop doing the
            # work again. A guard pressing twice must never be told "no".
            print(f"[PANIC] duplicate within {int(since_last)}s from {who}",
                  file=sys.stderr, flush=True)
            return jsonify({
                "success": True, "status": "already_raised",
                "message": (f"Alarm already raised {int(since_last)} seconds "
                            f"ago — help is on the way. Stay on the line "
                            f"with your supervisor."),
                "notified": [], "failed": [], "results": {},
                "time": datetime.now().isoformat(),
            })

        title = "GUARD PANIC"
        message = (f"Panic button pressed at {camera} ({stamp})"
                   + (f" — {note}" if note else ""))

        results = {}     # channel -> human string (unchanged shape)
        ok = {}          # channel -> True/False, the thing `success` reads
        skipped = []     # channels this site does not have (not a failure)

        def record(channel, fired, detail):
            ok[channel] = bool(fired)
            results[channel] = detail

        # 1) HIGH alert -> SSE -> Night Watch takeover
        try:
            if push_alert:
                try:
                    push_alert(title, message, "HIGH")
                except TypeError:
                    push_alert(title=title, message=message, severity="HIGH")
                record("alert", True, "sent")
            else:
                # Not an exception, but nothing was sent either. This used to
                # land in `results` looking like an explanation and counted
                # towards nothing; now it counts as the failure it is.
                record("alert", False, "no push_alert wired")
        except Exception as e:
            record("alert", False, f"failed: {e}")

        # 2) Incident case file (evidence, not notification — see above)
        try:
            from backend.incidents.incident_service import create_new_incident
            inc = create_new_incident({
                "title": "Guard panic activation",
                "description": message,
                "severity": "HIGH",
                "camera_name": camera,
                "operator": operator,
            })
            record("incident", True, (inc or {}).get("incident_id", "created"))
        except Exception as e:
            record("incident", False, f"failed: {e}")

        # 3) Booth voice (multilingual if available, legacy fallback)
        #
        # OCT-97. This used to record voice as "spoken (hi+en)" the moment
        # announce() returned. announce() only QUEUES — it returns at once
        # whether or not the host can make a sound — and the worker failed
        # silently on its own thread when there was no speech engine. So on
        # every droplet this channel reported success while the log said
        # "Could not start TTS engine". Same mistake as the WhatsApp branch
        # in the OCT-70 addendum below: "the call did not throw" recorded
        # as "a person was reached".
        #
        # Now the module is asked whether its engine actually started, and
        # the channel only counts as reached when it did. Even then it says
        # "announced", not "heard": the engine being ready does not prove a
        # speaker is plugged in, and this record should claim no more than
        # the system knows.
        if not _booth_voice_enabled():
            results["voice"] = "not applicable: no booth speaker at this site"
            skipped.append("voice")
        else:
            try:
                try:
                    import booth_voice_ml as _v
                    can, why = _v.available(wait=2.0)
                    if can:
                        _v.announce("panic", camera=camera)
                        langs = "+".join(_v.languages())
                        record("voice", True, f"announced ({langs}), engine ready")
                    else:
                        record("voice", False, f"not spoken: {why}")
                except ImportError:
                    import booth_voice as _v
                    can, why = _v.available(wait=2.0)
                    if can:
                        _v.speak(f"Emergency. Guard assistance required at {camera}.")
                        record("voice", True, "announced (en), engine ready")
                    else:
                        record("voice", False, f"not spoken: {why}")
            except Exception as e:
                record("voice", False, f"unavailable: {e}")

        # 4) Ops WhatsApp
        #
        # OCT-70 ADDENDUM, 19 Sep. My own fix had the bug it was fixing.
        # `send_whatsapp()` RETURNS False when the Twilio env vars are
        # missing — it prints "[WARN] Twilio env vars missing" and returns,
        # it does not raise. So `except Exception` never fired and the
        # channel was recorded as "sent" on the strength of the call not
        # throwing. Exactly the assumption this whole finding was about,
        # one level further down.
        #
        # This is also why the register said all four channels fired on
        # 17 Sep. Measured on the droplet on 19 Sep, neither demo.env nor
        # primera.env contains ANY TWILIO_* variable, so that send could
        # not have gone anywhere. The test observed a function returning
        # quietly, not a message arriving.
        #
        # The return value is now the evidence.
        try:
            from morning_report import send_whatsapp
            delivered = send_whatsapp(f"🔴 PANIC: {message}")
            if delivered:
                record("whatsapp", True, "sent")
            else:
                record("whatsapp", False,
                       "not sent (Twilio not configured for this site)")
        except Exception as e:
            record("whatsapp", False, f"unavailable: {e}")

        notified = [c for c in NOTIFY_CHANNELS if ok.get(c)]
        failed = [c for c, good in ok.items() if not good]
        success = bool(notified)

        if not success:
            status = "none"
            human = ("ALARM NOT SENT — no channel reached anyone. "
                     "Call your supervisor and the police directly now.")
        elif failed:
            status = "partial"
            human = ("Alarm raised via " + ", ".join(notified) +
                     ". These did not go through: " + ", ".join(failed) + ".")
        else:
            status = "all"
            human = "Alarm raised on every channel."

        # Loud, and on stderr when it matters, so this is greppable in the
        # container log rather than buried in request noise.
        line = f"[PANIC] {message} -> status={status} results={results}"
        print(line, file=sys.stderr if not success else sys.stdout, flush=True)

        return jsonify({
            "success": success,          # now earned, not asserted
            "status": status,            # "all" | "partial" | "none"
            "message": human,            # show this to the guard verbatim
            "notified": notified,        # channels that reached a person
            "failed": failed,            # everything that did not fire
            "not_applicable": skipped,   # channels this site does not have
            "results": results,          # unchanged: channel -> detail string
            "incident": results.get("incident") if ok.get("incident") else None,
            "time": datetime.now().isoformat(),
        })
