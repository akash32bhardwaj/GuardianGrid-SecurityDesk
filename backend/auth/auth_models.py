from werkzeug.security import generate_password_hash

USERS = [
    {
        "id": 1,
        "username": "admin",
        "password_hash": generate_password_hash("GuardianGrid@2026"),
        "role": "SUPER_ADMIN",
        "society_id": None
    }
]


def get_user_by_username(username):
    for user in USERS:
        if user["username"].lower() == username.lower():
            return user

    return None