from werkzeug.security import generate_password_hash
from site_config import CONFIG

USERS = [
    {
        "id": 1,
        "username": CONFIG.admin_username,
        "password_hash": generate_password_hash(CONFIG.admin_password),
        "role": "SUPER_ADMIN",
        "society_id": CONFIG.site_id
    }
]


def _site_config_path():
    """site_config.json, wherever this deployment keeps it.

    /app in the container (this file is /app/backend/auth/auth_models.py, so
    parents[2] is /app), and the entrypoint also links it into /data, which
    is the working directory the app runs from.
    """
    from pathlib import Path
    candidates = [
        Path(__file__).resolve().parents[2] / "site_config.json",
        Path.cwd() / "site_config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _add_viewers_from_config():
    """Read-only demo accounts, defined in site_config.json.

    Two supported shapes (both may be present; every valid entry is added):

        "viewer":  { "username": "demo", "password": "..." }           # legacy single
        "viewers": [ { "username": "demo-escon", "password": "..." },
                     { "username": "demo-aura",  "password": "..." } ] # per-prospect

    Viewers can look at dashboards, cameras, reports and replay, but the API
    guard refuses every write and the resident directory, and personal data
    is redacted from their responses. Safe to print on a card or send to a
    prospect over WhatsApp. No block = no viewers.

    NOTE ON HISTORY: the multi-viewer version of this lived for weeks in an
    untracked auth_models.py at the repo root — a file nothing imported and
    git never saw. This module, the one actually imported, only ever
    understood the single "viewer" block, so a site_config.json carrying a
    "viewers" list silently produced no accounts at all. If per-prospect
    demo logins ever seemed not to exist, that is why.
    """
    import json
    try:
        with open(_site_config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"[AUTH] viewer accounts skipped: {e}")
        return

    entries = []
    single = cfg.get("viewer") or {}
    if single.get("username") and single.get("password"):
        entries.append(single)
    for v in cfg.get("viewers") or []:
        if v.get("username") and v.get("password"):
            entries.append(v)

    next_id = 2
    seen = {USERS[0]["username"].lower()}
    for v in entries:
        uname = v["username"]
        if uname.lower() in seen:
            print(f"[AUTH] duplicate viewer '{uname}' skipped")
            continue
        seen.add(uname.lower())
        USERS.append({
            "id": next_id,
            "username": uname,
            "password_hash": generate_password_hash(v["password"]),
            "role": "VIEWER",
            "society_id": CONFIG.site_id,
        })
        print(f"[AUTH] viewer account enabled: {uname} (read-only)")
        next_id += 1

    if not entries:
        print("[AUTH] no viewer accounts configured for this site")


_add_viewers_from_config()


def get_user_by_username(username):
    for user in USERS:
        if user["username"].lower() == username.lower():
            return user
    return None
