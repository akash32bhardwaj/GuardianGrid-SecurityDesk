from flask import request, jsonify
import jwt
import threading
import time

from backend.auth.auth_models import get_user_by_username
from backend.auth.auth_service import (
    generate_token,
    verify_password,
    decode_token
)

# ── Login throttling ────────────────────────────────────────────────────────
# The dashboard login had no limit of any kind: unlimited password guesses
# against the account with the most privilege on the site. The resident OTP
# and flat-PIN logins have had attempt counters and lockouts all along, so
# this brings the admin login up to the same standard.
#
# Locked per client IP, not per username, deliberately: locking by username
# would let anyone freeze a society's admin out of their own dashboard just
# by guessing at their login. An attacker only ever locks themselves out.
# A distributed attempt from many IPs is out of scope here and belongs at
# the edge — Cloudflare rate limiting or fail2ban.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCK_SECONDS = 15 * 60
_MAX_TRACKED_IPS = 5000

_login_state = {}                 # ip -> {"n": failures, "until": epoch}
_login_mutex = threading.Lock()


def _client_ip() -> str:
    """Real client address behind the Cloudflare tunnel."""
    for header in ("CF-Connecting-IP", "X-Forwarded-For"):
        val = request.headers.get(header, "")
        if val:
            return val.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _lock_seconds_left(ip: str) -> int:
    now = time.time()
    with _login_mutex:
        rec = _login_state.get(ip)
        if not rec:
            return 0
        if rec["until"] > now:
            return int(rec["until"] - now) + 1
        if rec["until"]:
            _login_state.pop(ip, None)
    return 0


def _record_failure(ip: str) -> None:
    now = time.time()
    with _login_mutex:
        if len(_login_state) > _MAX_TRACKED_IPS:
            for stale, r in list(_login_state.items()):
                if r["until"] < now:
                    _login_state.pop(stale, None)
        rec = _login_state.setdefault(ip, {"n": 0, "until": 0.0})
        rec["n"] += 1
        if rec["n"] >= LOGIN_MAX_ATTEMPTS:
            rec["until"] = now + LOGIN_LOCK_SECONDS
            rec["n"] = 0


def _clear_failures(ip: str) -> None:
    with _login_mutex:
        _login_state.pop(ip, None)


def register_auth_routes(app):

    @app.route("/api/auth/login", methods=["POST"])
    def login_user():

        ip = _client_ip()
        wait = _lock_seconds_left(ip)
        if wait:
            mins = wait // 60 + 1
            return jsonify({
                "success": False,
                "message": f"Too many failed sign-ins. Try again in "
                           f"{mins} minute{'s' if mins != 1 else ''}."
            }), 429, {"Retry-After": str(wait)}

        data = request.get_json(silent=True) or {}

        username = data.get("username", "")
        password = data.get("password", "")

        user = get_user_by_username(username)

        # One message for "no such user" and "wrong password", so the response
        # never reveals which usernames exist.
        if not user or not verify_password(password, user["password_hash"]):
            _record_failure(ip)
            return jsonify({
                "success": False,
                "message": "Invalid credentials"
            }), 401

        _clear_failures(ip)
        token = generate_token(user)

        return jsonify({
            "success": True,
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"]
            }
        })
    @app.route("/api/auth/test")
    def auth_test():
        return jsonify({
            "success": True,
            "message": "Auth module loaded"
        })

    @app.route("/api/auth/me", methods=["GET"])
    def current_user():

        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return jsonify({
                "success": False,
                "message": "Missing token"
            }), 401

        try:
            token = auth_header.replace("Bearer ", "")

            payload = decode_token(token)

            return jsonify({
                "success": True,
                "user": {
                    "id": payload["user_id"],
                    "username": payload["username"],
                    "role": payload["role"],
                    "society_id": payload["society_id"]
                }
            })

        except jwt.ExpiredSignatureError:
            return jsonify({
                "success": False,
                "message": "Token expired"
            }), 401

        except Exception:
            return jsonify({
                "success": False,
                "message": "Invalid token"
            }), 401