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

# ---------------------------------------------------------------------------
# Pricing tiers
# ---------------------------------------------------------------------------
# Watch / Guard / Command are the three things a client can buy, and they are
# steps of the same loop rather than three arbitrary bundles:
#
#   Watch    detect  — the site is read and you are told what happened
#   Guard    verify  — a guard can close the loop on what was detected
#   Command  prove   — the closed loop leaves evidence someone else accepts
#
# Each tier CONTAINS the one below, so upgrading never removes anything.
#
# The resident app sits in every tier deliberately. Residents are who make a
# society renew, and a Watch site with no resident app is a camera the
# committee never sees the value of.
#
# A site_config.json may still list "features" explicitly; that wins, so a
# one-off arrangement with a client needs no code change. Tier is the
# default, not a cage.

TIER_WATCH = "watch"
TIER_GUARD = "guard"
TIER_COMMAND = "command"

_TIER_ADDS = {
    # detect, plus the resident-facing app
    TIER_WATCH: [
        "anpr",
        "whatsapp_alerts",
        "morning_brief",
        "resident_directory",
        "visitor_management",
        "flat_visitor_notifications",
        "bulk_resident_import",
    ],
    # verify: a guard can act on a detection and the action is recorded
    TIER_GUARD: [
        "guard_decision_flow",
        "dvr_recording",
        "pdf_reports",
        "contractor_passes",
        "security_score",
    ],
    # prove: the record stands up afterwards, to a committee or an auditor
    TIER_COMMAND: [
        "face_watchlist",
        "smart_replay",
        "intelligence_hub",
        "floor_heatmap",
        "voice_assistant",
        "weekly_audit",
    ],
}

_TIER_ORDER = [TIER_WATCH, TIER_GUARD, TIER_COMMAND]


def features_for_tier(tier: str) -> dict:
    """Every feature name -> True/False for one tier, cumulative."""
    tier = (tier or "").strip().lower()
    if tier not in _TIER_ORDER:
        tier = TIER_COMMAND        # unknown tier: assume the full product
    on = set()
    for t in _TIER_ORDER:
        on.update(_TIER_ADDS[t])
        if t == tier:
            break
    every = {n for names in _TIER_ADDS.values() for n in names}
    every.update(_LEGACY_DEFAULT_ON)
    return {name: (name in on) for name in sorted(every)}


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
            # A tier is the normal way to configure a site; an explicit
            # "features" object overrides it for one-off arrangements.
            tier = str(data.get("tier", "")).strip().lower()
            if not isinstance(data.get("features"), dict):
                if tier:
                    data["features"] = features_for_tier(tier)
                else:
                    raise ValueError(
                        "site_config.json needs either 'tier' "
                        "(watch|guard|command) or a 'features' object")
            data.setdefault("tier", tier or "custom")
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


def tier() -> str:
    """watch | guard | command | custom — what this site has bought."""
    return load_config().get("tier", "custom")


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
