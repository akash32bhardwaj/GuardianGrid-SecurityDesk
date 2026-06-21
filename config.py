import os
from dotenv import load_dotenv

load_dotenv()

APP_NAME = "GuardianGrid"
APP_VERSION = "1.1"

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "GuardianGrid@2026")

JWT_SECRET = os.getenv(
    "JWT_SECRET",
    "GuardianGrid-Change-This-Later"
)

DATABASE_URL = os.getenv("DATABASE_URL", "")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")