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


def _add_viewer_from_config():
    """Optional read-only demo account, defined in site_config.json:

        "viewer": { "username": "demo", "password": "..." }

    Viewers can look at dashboards, cameras, reports and replay, but the
    API guard refuses every write and the resident directory. Safe to
    print on a QR card / hand to visitors. Absent block = no viewer.
    """
    import json
    from pathlib import Path
    try:
        with open(Path(__file__).resolve().parents[2] / "site_config.json",
                  encoding="utf-8") as f:
            v = json.load(f).get("viewer") or {}
        if v.get("username") and v.get("password"):
            USERS.append({
                "id": 2,
                "username": v["username"],
                "password_hash": generate_password_hash(v["password"]),
                "role": "VIEWER",
                "society_id": CONFIG.site_id,
            })
            print(f"[AUTH] viewer account enabled: {v['username']} (read-only)")
    except Exception as e:
        print(f"[AUTH] viewer account skipped: {e}")


_add_viewer_from_config()


def get_user_by_username(username):
    for user in USERS:
        if user["username"].lower() == username.lower():
            return user
    return None