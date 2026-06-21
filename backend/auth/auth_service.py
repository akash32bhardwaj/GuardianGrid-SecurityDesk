import jwt
from datetime import datetime, timedelta
from werkzeug.security import check_password_hash

from config import JWT_SECRET

TOKEN_EXPIRY_HOURS = 12


def generate_token(user):
    payload = {
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "society_id": user.get("society_id"),
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS)
    }

    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def decode_token(token):
    return jwt.decode(
        token,
        JWT_SECRET,
        algorithms=["HS256"]
    )

def verify_password(password, password_hash):
    return check_password_hash(password_hash, password)