from flask import request, jsonify
import jwt

from backend.auth.auth_models import get_user_by_username
from backend.auth.auth_service import (
    generate_token,
    verify_password,
    decode_token
)


def register_auth_routes(app):

    @app.route("/api/auth/login", methods=["POST"])
    def login_user():

        data = request.get_json()

        username = data.get("username", "")
        password = data.get("password", "")

        user = get_user_by_username(username)

        if not user:
            return jsonify({
                "success": False,
                "message": "Invalid credentials"
            }), 401

        if not verify_password(password, user["password_hash"]):
            return jsonify({
                "success": False,
                "message": "Invalid credentials"
            }), 401

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