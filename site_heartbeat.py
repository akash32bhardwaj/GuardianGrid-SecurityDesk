#!/usr/bin/env python3
r"""
site_heartbeat.py — DEFENDER OCTA "never blind" monitor
--------------------------------------------------------
Runs on the DROPLET (host, not in a container) via cron every 15 min.
Checks each client site for two failure modes:

  1. BRIDGE DOWN : the site's Pi is unreachable over tailnet (ping)
  2. DATA SILENT : no new rows in vehicle_events for N hours
                   (cameras may be up but detection is dead)

On state change (healthy -> down, or down -> healthy) it sends a WhatsApp
alert to the founder via Twilio. It alerts on CHANGES only — no 4 AM spam
every 15 minutes while a site stays down; instead one "still down" reminder
every REMIND_HOURS.

Setup:
  1. Put this file at /opt/octa-ops/site_heartbeat.py
  2. Create /opt/octa-ops/heartbeat_config.json  (template below)
  3. Test run:   python3 /opt/octa-ops/site_heartbeat.py
  4. Cron:       sudo crontab -e
                 */15 * * * * /usr/bin/python3 /opt/octa-ops/site_heartbeat.py >> /var/log/octa_heartbeat.log 2>&1

heartbeat_config.json template (NO real credentials in git — this file
lives only on the droplet):
{
  "twilio_sid":   "ACxxxxxxxx",
  "twilio_token": "xxxxxxxx",
  "twilio_from":  "whatsapp:+14155238886",
  "alert_to":     "whatsapp:+91XXXXXXXXXX",
  "quiet_ok_hours": [1, 2, 3, 4],
  "sites": [
    {
      "name": "AGI Infra",
      "pi_ip": "100.108.120.36",
      "db":    "/opt/societies/agi-infra/guardiangrid.db",
      "max_silent_hours": 6
    }
  ]
}

"quiet_ok_hours": hours of day (0-23) when zero events is normal and the
DATA SILENT check is skipped (bridge check still runs).
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

OPS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(OPS_DIR, "heartbeat_config.json")
STATE = os.path.join(OPS_DIR, "heartbeat_state.json")
REMIND_HOURS = 6          # re-alert interval while a site stays down


# ── helpers ──────────────────────────────────────────────────────────

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def ping(ip: str) -> bool:
    """One ICMP ping, 3s timeout. Returns True if host answered."""
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "3", ip],
                           capture_output=True, timeout=8)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def last_event_age_hours(db_path: str):
    """Hours since the newest vehicle_events row, or None if unreadable."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute(
            "SELECT MAX(REPLACE(timestamp,'T',' ')) FROM vehicle_events"
        ).fetchone()
        con.close()
        if not row or not row[0]:
            return None
        last = datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last).total_seconds() / 3600.0
    except (sqlite3.Error, ValueError, OSError) as e:
        print(f"  [db] {db_path}: {e}")
        return None


def send_whatsapp(cfg, body: str):
    """Send via Twilio REST API. Prints instead if creds are placeholders."""
    sid, tok = cfg.get("twilio_sid", ""), cfg.get("twilio_token", "")
    if not sid.startswith("AC") or "xxx" in sid.lower():
        print(f"  [alert-DRYRUN] {body}")
        return
    try:
        import urllib.request
        import urllib.parse
        import base64
        url = (f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
               "/Messages.json")
        data = urllib.parse.urlencode({
            "From": cfg["twilio_from"],
            "To": cfg["alert_to"],
            "Body": body,
        }).encode()
        req = urllib.request.Request(url, data=data)
        auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  [alert] sent ({resp.status}): {body}")
    except Exception as e:
        print(f"  [alert-FAILED] {e} :: {body}")


# ── main check ───────────────────────────────────────────────────────

def check_site(site, cfg, state, now):
    name = site["name"]
    st = state.setdefault(name, {"status": "OK", "last_alert": None})
    problems = []

    # 1) bridge reachability
    if site.get("pi_ip"):
        if not ping(site["pi_ip"]):
            problems.append(f"site bridge (Pi {site['pi_ip']}) unreachable")

    # 2) event freshness (skipped during configured quiet hours)
    if now.hour not in cfg.get("quiet_ok_hours", []):
        age = last_event_age_hours(site["db"])
        limit = site.get("max_silent_hours", 6)
        if age is None:
            problems.append("event database unreadable / empty")
        elif age > limit:
            problems.append(f"no camera events for {age:.1f}h "
                            f"(threshold {limit}h)")

    new_status = "DOWN" if problems else "OK"
    old_status = st["status"]

    if new_status == "DOWN":
        due_reminder = (
            st["last_alert"] is None or
            datetime.fromisoformat(st["last_alert"])
            < now - timedelta(hours=REMIND_HOURS)
        )
        if old_status == "OK" or due_reminder:
            tag = "🔴 BLIND" if old_status == "OK" else "🔴 STILL BLIND"
            send_whatsapp(cfg, f"{tag} — {name}: " + "; ".join(problems) +
                          f" ({now:%d %b %H:%M})")
            st["last_alert"] = now.isoformat()
    elif old_status == "DOWN":
        send_whatsapp(cfg, f"🟢 RESTORED — {name}: bridge and events "
                      f"healthy again ({now:%d %b %H:%M})")
        st["last_alert"] = None

    st["status"] = new_status
    print(f"  {name}: {new_status}" +
          (f"  [{'; '.join(problems)}]" if problems else ""))


def main():
    cfg = load(CONFIG, None)
    if not cfg:
        sys.exit(f"config not found/invalid: {CONFIG}")
    state = load(STATE, {})
    now = datetime.now()
    print(f"[heartbeat] {now:%Y-%m-%d %H:%M:%S}")
    for site in cfg.get("sites", []):
        check_site(site, cfg, state, now)
    save(STATE, state)


if __name__ == "__main__":
    main()
