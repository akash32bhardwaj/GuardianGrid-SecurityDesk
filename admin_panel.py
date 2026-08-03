"""
admin_panel.py — Defender Octa demo fleet switchboard
------------------------------------------------------
Six on/off switches for the society demo containers, served at
admin.snguardiangrid.com. Protected by HTTP Basic Auth — credentials
come from environment variables ADMIN_PANEL_USER / ADMIN_PANEL_PASS
(set in the systemd unit).

Runs on the HOST (not in Docker) so it can call the docker CLI
directly. Reuses /opt/octa/.venv for Flask.
"""
import os
import subprocess
from functools import wraps

from flask import Flask, request, Response, redirect

app = Flask(__name__)

USER = os.environ.get("ADMIN_PANEL_USER", "akash")
PASS = os.environ.get("ADMIN_PANEL_PASS", "")

SOCIETIES = {
    f"octa-society{i}": name for i, name in enumerate([
        "Green Valley Residency", "Sunrise Enclave", "Palm Heights",
        "Silver Oak Society", "Royal Orchid Residency", "Emerald Towers",
    ], start=1)
}


def _auth_ok(a):
    return a and a.username == USER and PASS and a.password == PASS


def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _auth_ok(request.authorization):
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": 'Basic realm="Octa Admin"'})
        return f(*args, **kwargs)
    return wrapper


def _running_containers():
    out = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=15).stdout
    return set(out.split())


@app.route("/")
@requires_auth
def index():
    running = _running_containers()
    rows = ""
    for cname, society in SOCIETIES.items():
        n = cname.replace("octa-society", "")
        is_up = cname in running
        status = ("<span class='on'>&#9679; ON</span>" if is_up
                  else "<span class='off'>&#9679; OFF</span>")
        action = "stop" if is_up else "start"
        btn_cls = "btn-stop" if is_up else "btn-start"
        btn_txt = "Turn OFF" if is_up else "Turn ON"
        url = f"https://society{n}.snguardiangrid.com"
        rows += f"""
        <div class="card">
          <div class="info">
            <div class="name">{society}</div>
            <a class="url" href="{url}" target="_blank">society{n}.snguardiangrid.com</a>
            <div class="status">{status}</div>
          </div>
          <form method="post" action="/toggle/{cname}/{action}">
            <button class="{btn_cls}" type="submit">{btn_txt}</button>
          </form>
        </div>"""
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Octa Fleet Switchboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0d1626;
         color:#e2e8f0; margin:0; padding:20px; }}
  h1 {{ font-size:20px; letter-spacing:.08em; text-transform:uppercase;
        color:#94a3b8; }}
  .card {{ background:#141d2e; border:1px solid #334155; border-radius:14px;
           padding:16px 18px; margin-bottom:12px; display:flex;
           justify-content:space-between; align-items:center; gap:12px; }}
  .name {{ font-weight:600; font-size:16px; }}
  .url {{ color:#64748b; font-size:12px; text-decoration:none; }}
  .status {{ font-size:13px; margin-top:4px; }}
  .on {{ color:#34d399; }} .off {{ color:#f87171; }}
  button {{ border:0; border-radius:10px; padding:12px 18px; font-size:14px;
            font-weight:600; cursor:pointer; min-width:110px; }}
  .btn-start {{ background:#34d399; color:#052e1f; }}
  .btn-stop  {{ background:#1e293b; color:#f87171;
                border:1px solid #f87171; }}
  .foot {{ color:#475569; font-size:12px; margin-top:18px; }}
</style></head><body>
<h1>Defender Octa &mdash; Demo Switchboard</h1>
{rows}
<div class="foot">Changes take ~15 seconds to reflect on the public URL.</div>
</body></html>"""


@app.route("/toggle/<cname>/<action>", methods=["POST"])
@requires_auth
def toggle(cname, action):
    if cname not in SOCIETIES or action not in ("start", "stop"):
        return "Bad request", 400
    subprocess.run(["docker", action, cname], timeout=60)
    return redirect("/")


if __name__ == "__main__":
    if not PASS:
        raise SystemExit("Set ADMIN_PANEL_PASS before running.")
    app.run(host="127.0.0.1", port=5099)
