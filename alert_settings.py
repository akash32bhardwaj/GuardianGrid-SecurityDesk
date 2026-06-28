"""
alert_settings.py — Smart Alert Routing Configuration
"""

import json
import logging
from pathlib import Path
from datetime import datetime, time as dtime

logger = logging.getLogger(__name__)

SETTINGS_FILE = Path("data/alert_settings.json")
SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

URGENT_TYPES = {"BLACKLISTED", "THREAT"}

DEFAULT_SETTINGS = {
    "security_whatsapp":    "",
    "notify_unknown":       True,
    "notify_blacklisted":   True,
    "notify_known":         False,
    "notify_threats":       True,
    "quiet_hours_enabled":  False,
    "quiet_hours_start":    "23:00",
    "quiet_hours_end":      "06:00",
}


class AlertSettings:
    def __init__(self):
        self._settings = dict(DEFAULT_SETTINGS)
        self._load()

    def _load(self):
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._settings.update(saved)
            except Exception as e:
                logger.error(f"Could not load alert settings: {e}")

    def save(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._settings, f, indent=2)

    def get(self, key, default=None):
        return self._settings.get(key, default)

    def get_all(self) -> dict:
        return dict(self._settings)

    def update(self, new_settings: dict):
        for k in DEFAULT_SETTINGS:
            if k in new_settings:
                self._settings[k] = new_settings[k]
        self._settings["notify_blacklisted"] = True
        self._settings["notify_threats"]     = True
        self.save()

    def is_quiet_hours(self) -> bool:
        if not self._settings.get("quiet_hours_enabled"):
            return False
        try:
            now   = datetime.now().time()
            start = dtime.fromisoformat(self._settings["quiet_hours_start"])
            end   = dtime.fromisoformat(self._settings["quiet_hours_end"])
        except Exception:
            return False
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    def should_alert_security(self, event_type: str) -> bool:
        type_map = {
            "UNKNOWN":     "notify_unknown",
            "BLACKLISTED": "notify_blacklisted",
            "KNOWN":       "notify_known",
            "THREAT":      "notify_threats",
        }
        key = type_map.get(event_type)
        if key is None:
            return False
        if not self._settings.get(key, False):
            return False
        if event_type not in URGENT_TYPES and self.is_quiet_hours():
            return False
        return True


settings = AlertSettings()
