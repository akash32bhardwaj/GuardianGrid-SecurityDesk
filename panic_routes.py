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

Wiring in api_server.py (after push_alert is defined):

    from panic_routes import register_panic
    register_panic(app, push_alert)

The route expects the guard to be logged in (it rides your existing
before_request auth like every other /api route).
"""
from datetime import datetime


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
        results = {}

        # 1) HIGH alert -> SSE -> Night Watch takeover
        try:
            if push_alert:
                try:
                    push_alert(title, message, "HIGH")
                except TypeError:
                    push_alert(title=title, message=message, severity="HIGH")
                results["alert"] = "sent"
            else:
                results["alert"] = "no push_alert wired"
        except Exception as e:
            results["alert"] = f"failed: {e}"

        # 2) Incident case file
        try:
            from backend.incidents.incident_service import create_new_incident
            inc = create_new_incident({
                "title": "Guard panic activation",
                "description": message,
                "severity": "HIGH",
                "camera_name": camera,
                "operator": operator,
            })
            results["incident"] = (inc or {}).get("incident_id", "created")
        except Exception as e:
            results["incident"] = f"failed: {e}"

        # 3) Booth voice
        try:
            from booth_voice import speak
            speak(f"Emergency. Guard assistance required at {camera}.")
            results["voice"] = "spoken"
        except Exception as e:
            results["voice"] = f"unavailable: {e}"

        # 4) Ops WhatsApp
        try:
            from morning_report import send_whatsapp
            send_whatsapp(f"🔴 PANIC: {message}")
            results["whatsapp"] = "sent"
        except Exception as e:
            results["whatsapp"] = f"unavailable: {e}"

        print(f"[PANIC] {message} -> {results}")
        return jsonify({"success": True, "results": results,
                        "time": datetime.now().isoformat()})
