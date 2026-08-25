"""
site_profile.py — Defender Octa site-type & feature-switch system
==================================================================
Drop this file into your Flask backend folder
(C:\\GuardianGrid\\GuardianGrid-SecurityDesk, next to your main app file).

WHAT IT DOES
------------
1. Loads a per-client file called site_config.json.
2. Lets any part of the backend ask: is_enabled("contractor_passes")?
3. Adds one API endpoint:  GET /api/site-config
   -> the dashboard calls this on load to know which menus to show.
4. Gives you @feature_required("...") to lock an entire API route
   so a residential deployment physically refuses factory requests.

SAFETY DESIGN (important)
-------------------------
- If site_config.json is MISSING (e.g. AGI Infra before you add one),
  every existing feature defaults to ON and site_type = "residential".
  => Deploying this file changes NOTHING for current clients.
- If a feature name is not in the file at all, it defaults to OFF.
  => New factory features are born switched-off everywhere until you
     deliberately turn them on for a client.

HOW TO WIRE IT (2 lines in your main app file)
----------------------------------------------
    from site_profile import site_bp, is_enabled, feature_required
    app.register_blueprint(site_bp)

WHERE THE FILE LIVES
--------------------
By default it looks for site_config.json in the same folder as your app.
Inside Docker, mount each client's copy the same way you already mount
/opt/societies/<clientname>. You can also set an environment variable
OCTA_SITE_CONFIG to point anywhere, e.g.:
    -e OCTA_SITE_CONFIG=/app/data/site_config.json
"""

import json
import os
import threading

from flask import Blueprint, jsonify

# ---------------------------------------------------------------------------
# Where to find the config file
# ---------------------------------------------------------------------------
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "site_config.json")
CONFIG_PATH = os.environ.get("OCTA_SITE_CONFIG", _DEFAULT_PATH)

# ---------------------------------------------------------------------------
# Features that existed BEFORE this switch system.
# If site_config.json is missing, these are assumed ON so nothing breaks.
# ---------------------------------------------------------------------------
_LEGACY_DEFAULT_ON = [
    "resident_directory",
    "flat_visitor_notifications",
    "bulk_resident_import",
    "visitor_management",
    "guard_decision_flow",
    "anpr",
    "face_watchlist",
    "smart_replay",
    "morning_brief",
    "weekly_audit",
    "security_score",
    "intelligence_hub",
    "floor_heatmap",
    "pdf_reports",
    "voice_assistant",
    "dvr_recording",
    "whatsapp_alerts",
]

_lock = threading.Lock()
_cache = None  # loaded config lives here after first read


def _fallback_config():
    """Used when no site_config.json exists — behaves exactly like today."""
    return {
        "site_name": "Unnamed Site",
        "site_type": "residential",
        "features": {name: True for name in _LEGACY_DEFAULT_ON},
        "_source": "fallback (no site_config.json found)",
    }


def load_config(force_reload=False):
    """Read site_config.json once and keep it in memory."""
    global _cache
    with _lock:
        if _cache is not None and not force_reload:
            return _cache
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data.get("features"), dict):
                raise ValueError("'features' must be an object of true/false")
            data.setdefault("site_name", "Unnamed Site")
            data.setdefault("site_type", "residential")
            data["_source"] = CONFIG_PATH
            _cache = data
        except FileNotFoundError:
            _cache = _fallback_config()
        except (ValueError, json.JSONDecodeError) as exc:
            # A broken file should never take the whole system down.
            print(f"[site_profile] WARNING: bad site_config.json ({exc}); "
                  f"using safe fallback (everything legacy ON).")
            _cache = _fallback_config()
        return _cache


def is_enabled(feature_name: str) -> bool:
    """The one question the rest of the code asks. Unknown feature -> False."""
    cfg = load_config()
    return bool(cfg["features"].get(feature_name, False))


def site_type() -> str:
    return load_config().get("site_type", "residential")


def site_name() -> str:
    return load_config().get("site_name", "Unnamed Site")


# ---------------------------------------------------------------------------
# Route guard: put on top of any API route that belongs to one feature.
#
#   @app.route("/api/contractors")
#   @feature_required("contractor_passes")
#   def list_contractors():
#       ...
# ---------------------------------------------------------------------------
from functools import wraps


def feature_required(feature_name: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if not is_enabled(feature_name):
                return jsonify({
                    "error": "feature_disabled",
                    "feature": feature_name,
                    "message": f"'{feature_name}' is not enabled for this site.",
                }), 403
            return view_func(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# The API endpoint the dashboard calls on load.
# NOTE: registered like your other blueprints. If your JWT before_request
# guard exempts some public routes, decide whether this one needs auth —
# it only reveals feature names, so either way is acceptable.
# ---------------------------------------------------------------------------
site_bp = Blueprint("site_profile", __name__)


@site_bp.route("/api/site-config", methods=["GET"])
def get_site_config():
    cfg = load_config()
    return jsonify({
        "site_name": cfg["site_name"],
        "site_type": cfg["site_type"],
        "features": cfg["features"],
    })


@site_bp.route("/api/site-config/reload", methods=["POST"])
def reload_site_config():
    """Optional: lets you edit site_config.json and apply it WITHOUT
    restarting the container. Call it once after editing the file."""
    cfg = load_config(force_reload=True)
    return jsonify({"reloaded": True, "site_name": cfg["site_name"],
                    "site_type": cfg["site_type"]})
