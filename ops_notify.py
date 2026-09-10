#!/usr/bin/env python3
"""
ops_notify.py — tell a human when a background job fails.

Run INSIDE a site container, which is where the WhatsApp credentials and the
existing sender already live:

    docker exec -w /data octa-demo python /app/ops_notify.py "message"

Written because the nightly demo reseed failed six nights running and nothing
said so. Its log was faithfully recorded and never read, which is what logs
are for and also why they are not enough on their own. Anything that runs
unattended and matters should be able to reach a person.

Exits 0 whether or not the message got through: a notifier that fails a job
because it could not send a notification has made the situation worse.
"""
import sys


def notify(message: str) -> bool:
    """Send `message` to the operations number. True if it went out."""
    try:
        from whatsapp_config import DEFAULT_OWNER_WHATSAPP as to
    except Exception:
        try:
            from whatsapp_config import SECURITY_WHATSAPP as to
        except Exception:
            print("[OPS] no operations number configured — not sent")
            return False

    try:
        from whatsapp_alerts import _send_whatsapp
    except Exception as e:
        print(f"[OPS] sender unavailable: {e}")
        return False

    try:
        site = "this site"
        try:
            from site_config import CONFIG
            site = f"{CONFIG.society_name} ({CONFIG.site_id})"
        except Exception:
            pass
        res = _send_whatsapp(to, f"⚠️ Defender Octa — {site}\n\n{message}")
        ok = bool((res or {}).get("success", res))
        print("[OPS] notification sent" if ok else f"[OPS] send failed: {res}")
        return ok
    except Exception as e:
        print(f"[OPS] send failed: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: ops_notify.py <message>")
        sys.exit(0)
    notify(" ".join(sys.argv[1:]))
    sys.exit(0)
