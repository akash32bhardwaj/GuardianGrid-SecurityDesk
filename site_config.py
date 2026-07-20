"""
site_config.py — loads per-site settings from site_config.json
----------------------------------------------------------------
Place this next to api_server.py. It reads site_config.json once at
startup and exposes settings as a simple object, with safe defaults
if the file or any key is missing.

Usage in api_server.py:
    from site_config import CONFIG
    port  = CONFIG.port
    cams  = CONFIG.rtsp_cameras
    ...

Editing settings for a new society = edit site_config.json only.
No code changes, ever.
"""

import json
import os
from pathlib import Path

CONFIG_PATH = Path("site_config.json")

# Safe defaults — used if the file is missing or a key is absent.
_DEFAULTS = {
    "society":   {"name": "Defender Octa", "site_id": "site-000", "location": ""},
    "server":    {"port": 5000, "host": "0.0.0.0"},
    "camera":    {"enabled": True, "index": 0},
    "rtsp_cameras": [],
    "detection": {
        "min_confidence": 30, "debounce_seconds": 30, "exit_minutes": 5,
        "vote_window_seconds": 8.0, "vote_min_samples": 3,
        "require_guard_decision": True,
    },
    "admin":  {"username": "admin", "password": "change-me-now"},
    "backup": {"enabled": True, "keep_days": 14},
}


def _deep_merge(defaults: dict, loaded: dict) -> dict:
    """Fill any missing keys in loaded with defaults."""
    out = dict(defaults)
    for k, v in (loaded or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class _Config:
    def __init__(self):
        raw = {}
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[CONFIG] WARNING: could not parse site_config.json ({e}). Using defaults.")
        else:
            print("[CONFIG] WARNING: site_config.json not found. Using defaults.")

        cfg = _deep_merge(_DEFAULTS, raw)

        # Society
        self.society_name = cfg["society"]["name"]
        self.site_id      = cfg["society"]["site_id"]
        self.location     = cfg["society"].get("location", "")

        # Server
        self.port = int(cfg["server"]["port"])
        self.host = cfg["server"]["host"]

        # Camera
        self.camera_enabled = bool(cfg["camera"]["enabled"])
        idx = cfg["camera"]["index"]
        self.camera_index   = int(idx) if str(idx).isdigit() else str(idx)

        # RTSP cameras (skip commented/empty entries)
        self.rtsp_cameras = [
            c for c in cfg.get("rtsp_cameras", [])
            if isinstance(c, dict) and c.get("url") and "username:password" not in c.get("url", "")
        ]

        # Detection
        d = cfg["detection"]
        self.min_confidence        = int(d["min_confidence"])
        self.debounce_seconds      = int(d["debounce_seconds"])
        self.exit_minutes          = int(d["exit_minutes"])
        self.vote_window_seconds   = float(d["vote_window_seconds"])
        self.vote_min_samples      = int(d["vote_min_samples"])
        self.require_guard_decision = bool(d["require_guard_decision"])

        # Admin
        self.admin_username = os.environ.get("ADMIN_USERNAME", cfg["admin"]["username"])
        self.admin_password = os.environ.get("ADMIN_PASSWORD", cfg["admin"]["password"])

        # Backup
        self.backup_enabled  = bool(cfg["backup"]["enabled"])
        self.backup_keep_days = int(cfg["backup"]["keep_days"])

    def warn_if_insecure(self):
        """Print loud warnings for unsafe defaults left in production."""
        warnings = []
        if self.admin_password in ("change-me-now", "admin", "password", ""):
            warnings.append("Admin password is still a default — CHANGE IT in site_config.json.")
        if self.site_id in ("site-000", "demo-001"):
            warnings.append("site_id is still the demo value — set a real one per society.")
        if warnings:
            print("\n" + "!" * 55)
            print("  SECURITY WARNINGS:")
            for w in warnings:
                print(f"   - {w}")
            print("!" * 55 + "\n")


CONFIG = _Config()
