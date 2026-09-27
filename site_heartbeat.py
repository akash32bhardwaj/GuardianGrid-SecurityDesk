#!/usr/bin/env python3
r"""
site_heartbeat.py — DEFENDER OCTA "never blind" monitor
--------------------------------------------------------
Runs on the DROPLET (host, not in a container) via cron every 15 min.
Checks each client site for three failure modes:

  1. BRIDGE DOWN : the site's Pi is unreachable over tailnet (ping)
  2. SERVICE DOWN: the site's container does not answer /api/health
  3. DATA SILENT : no new rows in vehicle_events for N hours
                   (cameras may be up but detection is dead)

Every check is OPTIONAL and driven by which keys a site carries. A cloud
site with no cameras gets "url" and no "db", so it is uptime-monitored
without a permanent false alarm about silent cameras. A site with all
three keys gets all three checks. A site with NONE is reported as a
problem, because a watched site that nothing actually watches is the
worst of the three states and the hardest to notice.

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
      "name":  "Escon Primera",
      "url":   "http://127.0.0.1:5009/api/health"
    },
    {
      "name":  "Defender Octa Demo",
      "url":   "http://127.0.0.1:5008/api/health",
      "pi_ip": "100.x.x.x",
      "db":    "/opt/societies/demo/guardiangrid.db",
      "max_silent_hours": 6
    }
  ]
}

"url": optional. Any site that answers it gets an uptime check. 200 and
401 both count as UP -- /api/health returns 401 unauthenticated, which is
what deploy.sh step [4/5] already treats as proof the container is alive.
"url_timeout": optional, seconds, default 10.

"quiet_ok_hours": hours of day (0-23) when zero events is normal and the
DATA SILENT check is skipped (bridge check still runs).
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# The droplet runs UTC; every site container runs with TZ=Asia/Kolkata and
# writes its timestamps in that zone. This script compared one against the
# other, so `datetime.now() - last_event` came out NEGATIVE by about five
# and a half hours -- and a negative age can never exceed max_silent_hours.
#
# The freshness check therefore could not fire until a real stoppage was old
# enough for the skew to wash out, which made "max_silent_hours: 6" mean
# roughly eleven and a half. quiet_ok_hours was read in host time too, so
# "quiet night hours" [1,2,3,4] were 06:30-10:30 IST -- the morning rush.
#
# Everything here now happens in SITE time. OCT-24b, one layer out: that
# entry fixed the containers and nobody asked what compared against them.
SITE_TZ = ZoneInfo(os.environ.get("OCTA_TZ", "Asia/Kolkata"))

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


def http_ok(url: str, timeout: float = 10.0):
    """GET `url`. Returns (ok, detail).

    200 AND 401 both count as UP. /api/health answers 401 when called
    without a token, and deploy.sh step [4/5] already accepts exactly that
    as proof the container is serving. Anything else -- 502 from nginx,
    connection refused, a timeout -- means nobody is home.

    This is a liveness check, not a correctness check. It proves a process
    is accepting connections on that port; it does not prove the product
    works. Said plainly here so nobody later mistakes a green light for one.
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            code = getattr(r, "status", None) or r.getcode()
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return (code in (200, 401)), f"HTTP {code}"


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
        last = datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=SITE_TZ)
        return (datetime.now(SITE_TZ) - last).total_seconds() / 3600.0
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

    # 0) a site nothing checks. Not a warning in a log -- an alert, because
    #    it looks identical to a healthy site from every other angle.
    if not (site.get("pi_ip") or site.get("url") or site.get("db")):
        problems.append("no checks configured (needs pi_ip, url or db)")

    # 1) bridge reachability
    if site.get("pi_ip"):
        if not ping(site["pi_ip"]):
            problems.append(f"site bridge (Pi {site['pi_ip']}) unreachable")

    # 2) service uptime. Runs at every hour, including quiet_ok_hours: a
    #    container being down at 3 AM is not "normal quiet", it is down.
    if site.get("url"):
        ok, detail = http_ok(site["url"], site.get("url_timeout", 10))
        if not ok:
            problems.append(f"service not answering at {site['url']} "
                            f"({detail})")

    # 3) event freshness (skipped during configured quiet hours).
    #    Gated on "db" being present: a cloud site with no cameras has no
    #    event database, and checking one would mean a standing false alarm
    #    for the life of the account.
    if site.get("db") and now.hour not in cfg.get("quiet_ok_hours", []):
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
        send_whatsapp(cfg, f"🟢 RESTORED — {name}: all checks "
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
    now = datetime.now(SITE_TZ)      # site time, so quiet_ok_hours means
                                     # what an Indian committee would read
    print(f"[heartbeat] {now:%Y-%m-%d %H:%M:%S}")
    for site in cfg.get("sites", []):
        check_site(site, cfg, state, now)
    save(STATE, state)


if __name__ == "__main__":
    main()
