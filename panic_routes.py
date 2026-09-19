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
from datetime import datetime


# Channels that put the emergency in front of a human being. "incident" is
# deliberately not here: it is the evidence trail, and a case file nobody
# has been told about is not a response.
NOTIFY_CHANNELS = ("alert", "voice", "whatsapp")


def register_panic(app, push_alert=None):

    @app.route("/api/panic", methods=["POST"])
    def guard_panic():
        from flask import request, jsonify
        data = request.get_json(silent=True) or {}
        note = (data.get("note") or "").strip()
        camera = (data.get("camera") or "Main Gate").strip()
        operator = (data.get("operator") or "guard").strip()
        stamp = datetime.now().strftime("%I:%M %p")

        title = "GUARD PANIC"
        message = (f"Panic button pressed at {camera} ({stamp})"
                   + (f" — {note}" if note else ""))

        results = {}     # channel -> human string (unchanged shape)
        ok = {}          # channel -> True/False, the thing `success` reads

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
        try:
            try:
                from booth_voice_ml import announce
                announce("panic", camera=camera)
                record("voice", True, "spoken (hi+en)")
            except ImportError:
                from booth_voice import speak
                speak(f"Emergency. Guard assistance required at {camera}.")
                record("voice", True, "spoken (en)")
        except Exception as e:
            record("voice", False, f"unavailable: {e}")

        # 4) Ops WhatsApp
        try:
            from morning_report import send_whatsapp
            send_whatsapp(f"🔴 PANIC: {message}")
            record("whatsapp", True, "sent")
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
            "results": results,          # unchanged: channel -> detail string
            "incident": results.get("incident") if ok.get("incident") else None,
            "time": datetime.now().isoformat(),
        })
