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

SAFETY DESIGN
-------------
- A missing or unusable site_config.json yields the FULL product, never a
  reduced one, and says so loudly at startup. See OCT-69 below.
- If a feature name is not in a valid file at all, it defaults to OFF.
  => New factory features are born switched-off everywhere until you
     deliberately turn them on for a client.

--------------------------------------------------------------------------
OCT-69. This file used to degrade a site silently, three ways at once.

`load_config()` derived features from `tier`, but only when a tier string
was present. With neither `tier` nor a `features` object it raised
ValueError, which the handler caught and answered with
`_fallback_config()` — a hardcoded list of 17 legacy features that OMITS
`contractor_passes`. So a config file that was present, readable and valid
JSON, but simply missing one key, quietly removed a feature from the site.

Demo ran three days that way and nobody could have known:

  1. The only signal was one console line among hundreds at startup.

  2. That line named a cause that was not true. `_fallback_config()`
     reported `_source: "fallback (no site_config.json found)"` even when
     the file existed and was perfectly readable — sending the next person
     to debug it looking for a file that was right there.

  3. The degradation was invisible over the API. `/api/site-config`
     returned `features` and nothing about where they came from, so the
     dashboard could not tell a configured site from a fallen-back one.

It was also internally inconsistent: `features_for_tier()` treats an
UNKNOWN tier as the full product, while a MISSING tier fell through to the
reduced legacy list. Same missing information, two opposite answers.

What changes:

  * The fallback is the full product (`features_for_tier(COMMAND)`),
    not the legacy 17. A site whose config cannot be read gets everything
    and a loud warning — never a quiet downgrade. Silent degradation is
    the one option that should not be on the table.

  * `_source` tells the truth, and a `_warning` says what to do about it.
    "file missing", "file unreadable", "invalid JSON" and "no tier and no
    features" are four different problems with four different fixes.

  * The startup warning is a block, not a line, and it names the path.

  * `/api/site-config` exposes `tier`, `_source` and `_warning` so the
    dashboard can show that a site is running on a fallback. A failure
    nobody can see is the OCT-15 pattern all over again.

  * OCTA_STRICT_CONFIG=1 refuses to start instead of falling back. Worth
    turning on for new client deployments, where a missing config is a
    provisioning bug you want to fail on rather than paper over.
--------------------------------------------------------------------------

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
import sys
import threading

from flask import Blueprint, jsonify

# ---------------------------------------------------------------------------
# Where to find the config file
# ---------------------------------------------------------------------------
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "site_config.json")
CONFIG_PATH = os.environ.get("OCTA_SITE_CONFIG", _DEFAULT_PATH)

# Refuse to start rather than fall back. Off by default so an existing
# deployment cannot be taken down by turning this file on.
STRICT = os.environ.get("OCTA_STRICT_CONFIG", "").strip().lower() in (
    "1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Features that existed BEFORE this switch system.
#
# Kept because features_for_tier() unions it in, so a feature listed here
# but in no tier is still a known name. It is NO LONGER the fallback: it
# omits contractor_passes, which is exactly how OCT-69 removed a feature
# from a working site.
# ---------------------------------------------------------------------------
_LEGACY_DEFAULT_ON = [
    "resident_app",
    "panic_button",
    "octa_search",
    "night_watch",
    "pattern_watch",
    "anomaly_score",
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

# OCT-08, settled 23 Sep. The split below is Akash's, and it moves two
# things from where this file had them:
#
#   * the resident app and visitor management move from Watch to GUARD.
#     The comment above still records the argument for the old placement —
#     residents are who make a society renew — and it is a real one. This
#     is a pricing decision, not a technical one, and it lives in one list:
#     move the names back if selling proves the other way round.
#   * PDF reports move from Guard to COMMAND, with the rest of the
#     "prove it afterwards" set.
#
# Names added here so the newer surfaces are sellable rather than silently
# free: resident_app, panic_button, octa_search, night_watch, pattern_watch,
# anomaly_score.
_TIER_ADDS = {
    # WATCH — detect: the site is read and you are told what happened
    TIER_WATCH: [
        "anpr",
        "whatsapp_alerts",
        "morning_brief",
        "resident_directory",
        "bulk_resident_import",
    ],
    # GUARD — verify: a guard can close the loop, and residents take part
    TIER_GUARD: [
        "guard_decision_flow",
        "visitor_management",
        "flat_visitor_notifications",
        "resident_app",
        "panic_button",
        "contractor_passes",
        "dvr_recording",
        "security_score",
    ],
    # COMMAND — prove: the closed loop leaves evidence someone else accepts
    TIER_COMMAND: [
        "face_watchlist",
        "smart_replay",
        "intelligence_hub",
        "floor_heatmap",
        "voice_assistant",
        "weekly_audit",
        "pdf_reports",
        "octa_search",
        "night_watch",
        "pattern_watch",
        "anomaly_score",
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


def _shout(reason: str, detail: str, remedy: str):
    """A startup problem a human has to notice.

    The old code printed one line into a startup log that already carries
    hundreds. Three days of running without contractor passes went by on
    the strength of it.
    """
    bar = "!" * 72
    print(
        f"\n{bar}\n"
        f"!! SITE CONFIG PROBLEM - running on a FALLBACK, not this site's config\n"
        f"!!\n"
        f"!!   problem : {reason}\n"
        f"!!   path    : {CONFIG_PATH}\n"
        f"!!   detail  : {detail}\n"
        f"!!   running : FULL product (every feature on) so nothing breaks\n"
        f"!!   fix     : {remedy}\n"
        f"!!\n"
        f"!! Set OCTA_STRICT_CONFIG=1 to make this refuse to start instead.\n"
        f"{bar}\n",
        file=sys.stderr, flush=True)


def _fallback_config(reason: str, detail: str, remedy: str):
    """Used when site_config.json cannot be used.

    Returns the FULL product, not the legacy list. The legacy list omits
    contractor_passes, so using it here silently removed a feature from a
    site whose only sin was a missing key — which is OCT-69.
    """
    if STRICT:
        raise SystemExit(
            f"[site_profile] OCTA_STRICT_CONFIG is on and the site config is "
            f"unusable: {reason} ({detail}) at {CONFIG_PATH}. {remedy}")
    _shout(reason, detail, remedy)
    return {
        "site_name": "Unnamed Site",
        "site_type": "residential",
        "tier": "fallback",
        "features": features_for_tier(TIER_COMMAND),
        "_source": f"fallback - {reason}",
        "_warning": (f"This site is running on a fallback config ({reason}). "
                     f"Every feature is switched on. {remedy}"),
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

            if not isinstance(data, dict):
                raise ValueError(
                    f"top level is {type(data).__name__}, expected an object")

            # A tier is the normal way to configure a site; an explicit
            # "features" object overrides it for one-off arrangements.
            tier = str(data.get("tier", "")).strip().lower()

            if not isinstance(data.get("features"), dict):
                if tier:
                    data["features"] = features_for_tier(tier)
                    if tier not in _TIER_ORDER:
                        # Not fatal — features_for_tier already assumes the
                        # full product — but a typo in a tier name should
                        # not pass in silence.
                        print(f"[site_profile] WARNING: tier '{tier}' is not "
                              f"one of {'/'.join(_TIER_ORDER)}; assuming the "
                              f"full product. Check {CONFIG_PATH}.",
                              file=sys.stderr, flush=True)
                else:
                    # OCT-69: this used to raise and land in a fallback that
                    # QUIETLY dropped contractor_passes. A file that exists
                    # and parses but names no tier is a provisioning gap, not
                    # a reason to sell the client less than they bought.
                    _cache = _fallback_config(
                        reason="no 'tier' and no 'features' in the file",
                        detail="the file was read and parsed fine, it just "
                               "does not say what this site has bought",
                        remedy='add "tier": "watch" | "guard" | "command" to '
                               'site_config.json, or an explicit "features" '
                               'object.')
                    return _cache

            data.setdefault("tier", tier or "custom")
            data.setdefault("site_name", "Unnamed Site")
            data.setdefault("site_type", "residential")
            data["_source"] = CONFIG_PATH
            data.pop("_warning", None)
            _cache = data

        except FileNotFoundError:
            _cache = _fallback_config(
                reason="file missing",
                detail="nothing at that path",
                remedy="create site_config.json there, or point "
                       "OCTA_SITE_CONFIG at the real one.")
        except json.JSONDecodeError as exc:
            _cache = _fallback_config(
                reason="invalid JSON",
                detail=f"{exc}",
                remedy="fix the syntax - a trailing comma or an unquoted key "
                       "is the usual cause.")
        except OSError as exc:
            # Permission denied, a directory where a file should be, an I/O
            # error on the mount. Previously uncaught, so it took the app
            # down at import with a bare traceback.
            _cache = _fallback_config(
                reason="file unreadable",
                detail=f"{exc}",
                remedy="check ownership and permissions on the mounted path.")
        except ValueError as exc:
            _cache = _fallback_config(
                reason="file unusable",
                detail=f"{exc}",
                remedy="check the structure against a working site's "
                       "site_config.json.")
        return _cache


def is_enabled(feature_name: str) -> bool:
    """The one question the rest of the code asks. Unknown feature -> False."""
    cfg = load_config()
    return bool(cfg["features"].get(feature_name, False))


def site_type() -> str:
    return load_config().get("site_type", "residential")


def tier() -> str:
    """watch | guard | command | custom | fallback — what this site has."""
    return load_config().get("tier", "custom")


def site_name() -> str:
    return load_config().get("site_name", "Unnamed Site")


def config_warning():
    """None when the site is running on its own config; a sentence when not.

    Anything that wants to surface the degradation — a banner, the morning
    brief, a health endpoint — reads this rather than guessing.
    """
    return load_config().get("_warning")


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
        # OCT-69: the response used to carry features and nothing else, so a
        # site running on a fallback looked identical to a configured one.
        # These three let the dashboard say so.
        "tier": cfg.get("tier", "custom"),
        "source": cfg.get("_source"),
        "warning": cfg.get("_warning"),
        "features": cfg["features"],
    })


@site_bp.route("/api/site-config/reload", methods=["POST"])
def reload_site_config():
    """Optional: lets you edit site_config.json and apply it WITHOUT
    restarting the container. Call it once after editing the file."""
    cfg = load_config(force_reload=True)
    return jsonify({"reloaded": True, "site_name": cfg["site_name"],
                    "site_type": cfg["site_type"],
                    "tier": cfg.get("tier"),
                    "warning": cfg.get("_warning")})
