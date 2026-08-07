"""
nightwatch_routes.py — Night Watch configuration endpoint
----------------------------------------------------------
Exposes the per-site night window so the frontend Night Watch mode knows
when to arm. The window lives in site_config.json (per-site, per your
architecture) with defaults matching tiering_brain.py's deep-night hours:

    "nightwatch": { "enabled": true, "start": 14, "end": 16 }

If the block is absent, defaults apply — a site is never accidentally
unprotected because someone forgot a config stanza (fail-upward, same
philosophy as the brain).

Wiring (two lines in api_server.py, near the other route registrations):

    from nightwatch_routes import register_nightwatch
    register_nightwatch(app)
"""
import json
import os

DEFAULTS = {"enabled": True, "start": 23, "end": 5}


def _load_window():
    cfg = dict(DEFAULTS)
    try:
        if os.path.exists("site_config.json"):
            with open("site_config.json", "r", encoding="utf-8") as f:
                block = (json.load(f) or {}).get("nightwatch") or {}
            for k in ("enabled", "start", "end"):
                if k in block:
                    cfg[k] = block[k]
    except Exception:
        pass  # unreadable config -> defaults (fail-upward)
    # sanity clamp
    try:
        cfg["start"] = int(cfg["start"]) % 24
        cfg["end"] = int(cfg["end"]) % 24
        cfg["enabled"] = bool(cfg["enabled"])
    except Exception:
        cfg = dict(DEFAULTS)
    return cfg


def register_nightwatch(app):
    @app.route("/api/nightwatch/config")
    def nightwatch_config():
        from flask import jsonify
        return jsonify(_load_window())
