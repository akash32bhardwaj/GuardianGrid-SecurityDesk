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


def get_user_by_username(username):
    for user in USERS:
        if user["username"].lower() == username.lower():
            return user
    return None