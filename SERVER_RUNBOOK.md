# Defender Octa — Cloud Server Runbook (octa-server)

Goal: DigitalOcean droplet (Ubuntu 24.04, 2 GB) running the Defender Octa
dashboard + API with demo data, 24×7, at https://demo.snguardiangrid.com —
auto-starting, auto-restarting, no open ports (Cloudflare tunnel).

Work top to bottom. Every command is paste-ready. Lines starting with # are
comments — don't paste those. Expected time: ~90 min happy path.

─────────────────────────────────────────────────────────────────
STAGE 0 — Log in from your Windows CMD
─────────────────────────────────────────────────────────────────
ssh root@YOUR_DROPLET_IP
# type yes → then the root password from your notes.
# You should see:  root@octa-server:~#

─────────────────────────────────────────────────────────────────
STAGE 1 — Swap memory FIRST (protects the 2 GB box during installs)
─────────────────────────────────────────────────────────────────
fallocate -l 3G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h
# CHECK: the "Swap:" line shows 3.0Gi. Without this, installing torch
# (pulled by easyocr) can kill the box mid-install.

─────────────────────────────────────────────────────────────────
STAGE 2 — System packages
─────────────────────────────────────────────────────────────────
apt update && apt -y upgrade
apt -y install python3-venv python3-pip git ffmpeg libgl1 libglib2.0-0 espeak-ng build-essential
# libgl1/libglib2.0-0 → opencv needs them on servers (no display)
# espeak-ng → pyttsx3's Linux voice backend (prevents import errors)
# build-essential → insightface may compile from source
# ffmpeg → segment/replay tooling (harmless to have even in demo mode)

─────────────────────────────────────────────────────────────────
STAGE 3 — Clone the private repo (uses your GitHub token)
─────────────────────────────────────────────────────────────────
cd /opt
git clone https://YOUR_GITHUB_TOKEN@github.com/akash32bhardwaj/GuardianGrid-SecurityDesk.git octa
cd /opt/octa
ls
# CHECK: you see api_server.py, serve.py, frontend/, requirements-demo.txt …
# (Paste the token in place of YOUR_GITHUB_TOKEN — it stays on the server,
#  never in this chat.)

─────────────────────────────────────────────────────────────────
STAGE 4 — Python environment + dependencies (the long stage: 15–30 min)
─────────────────────────────────────────────────────────────────
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel

# Phase 1 — CORE (torch via easyocr makes this the big download):
pip install flask flask-cors Werkzeug waitress PyJWT python-dotenv \
    "numpy<2" opencv-python-headless easyocr reportlab openpyxl requests \
    twilio tqdm Pillow pyttsx3 psycopg2-binary
# NOTE: opencv-python-HEADLESS on purpose — the server has no display;
# headless avoids GUI library problems. Same cv2 API, code unchanged.

# Phase 2 — HEAVY (needed because api_server imports the detectors at startup):
pip install onnxruntime ultralytics insightface
# If ONLY insightface fails after a long compile: tell me — we'll stub it.
# ultralytics + onnxruntime should install cleanly from wheels.

─────────────────────────────────────────────────────────────────
STAGE 5 — Configuration (camera OFF, strong password, demo mode)
─────────────────────────────────────────────────────────────────
cp site_config.example.json site_config.json
nano site_config.json
# In nano make EXACTLY these edits (arrow keys; Ctrl+O Enter to save; Ctrl+X to exit):
#   "society.name"      → "Defender Octa Demo"
#   "camera.enabled"    → false          ← no webcam on a server
#   "rtsp_cameras"      → []             ← empty list: [] (delete the two examples)
#   "recording.auto_start" → false
#   "admin.password"    → a STRONG password (this box is public 24×7 —
#                          admin123 is not an option here)
#   add demo flag if instructed by DEMO_MODE_WIRING: "demo": { "demo_mode": true }

# Seed a week of demo data + a brief so the dashboard looks alive:
python seed_demo.py --days 7
python seed_briefs.py --days 7
python morning_report.py --hours 12

─────────────────────────────────────────────────────────────────
STAGE 6 — First manual run (prove it serves before automating)
─────────────────────────────────────────────────────────────────
python serve.py
# CHECK: waitress banner, no tracebacks. From a SECOND CMD on your laptop:
#   curl http://DROPLET_IP:5000/api/auth/test    ← if that route 404s, just
#   open http://DROPLET_IP:5000/frontend/ in your browser → login page.
# Then back on the server: Ctrl+C to stop.
# If serve.py listens only on 127.0.0.1 (page unreachable), tell me —
# one-line fix; the tunnel makes it moot anyway.

─────────────────────────────────────────────────────────────────
STAGE 7 — systemd service (auto-start, auto-restart: the "real server" part)
─────────────────────────────────────────────────────────────────
cat > /etc/systemd/system/octa.service << 'EOF'
[Unit]
Description=Defender Octa dashboard
After=network-online.target

[Service]
WorkingDirectory=/opt/octa
ExecStart=/opt/octa/.venv/bin/python serve.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now octa
systemctl status octa --no-pager
# CHECK: "active (running)". Logs any time with:  journalctl -u octa -f

─────────────────────────────────────────────────────────────────
STAGE 8 — Cloudflare tunnel ON THE SERVER
─────────────────────────────────────────────────────────────────
cd /tmp
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
dpkg -i cloudflared-linux-amd64.deb

cloudflared tunnel login
# It prints a URL. Copy it into YOUR laptop's browser, log into Cloudflare,
# click snguardiangrid.com. Terminal then says the cert was saved.

cloudflared tunnel create octa-server

# Move the DNS name from the laptop tunnel to this one:
#   Cloudflare dashboard → snguardiangrid.com → DNS → Records →
#   DELETE the existing "demo" CNAME (it points at the old octa-demo tunnel).
# Then:
cloudflared tunnel route dns octa-server demo.snguardiangrid.com

# Install as a service (auto-start, auto-restart):
cloudflared service install
mkdir -p /etc/cloudflared
cat > /etc/cloudflared/config.yml << 'EOF'
tunnel: octa-server
credentials-file: /root/.cloudflared/TUNNEL_ID.json
ingress:
  - hostname: demo.snguardiangrid.com
    service: http://localhost:5000
  - service: http_status:404
EOF
# Replace TUNNEL_ID.json with the real filename:  ls /root/.cloudflared/
systemctl restart cloudflared
systemctl status cloudflared --no-pager
# CHECK: active (running), log shows "Registered tunnel connection".

─────────────────────────────────────────────────────────────────
STAGE 9 — Acceptance gate (from your Windows laptop)
─────────────────────────────────────────────────────────────────
:: Laptop's Flask and old tunnel can be OFF — that's the whole point.
python smoke_test.py --base https://demo.snguardiangrid.com
:: Expect: green board. The filesystem section will WARN (it checks your
:: laptop's disk, not the server's) — those warnings are expected here.
:: Then the human test: open https://demo.snguardiangrid.com/frontend/ on
:: your phone, log in with the NEW password, walk the tabs.

─────────────────────────────────────────────────────────────────
DONE — what you now own
─────────────────────────────────────────────────────────────────
• 24×7 dashboard at demo.snguardiangrid.com, surviving reboots
  (systemd restarts Flask; cloudflared service restarts the tunnel)
• No open ports on the droplet (tunnel dials out) — no firewall wrangling
• Your laptop is free: dev, travel, shutdown — demo stays up
• Reboot test when curious:  reboot  → wait 2 min → smoke test again

Later upgrades (ask when wanted):
• live.snguardiangrid.com → laptop tunnel for real-camera showings
• deploy updates:  cd /opt/octa && git pull && systemctl restart octa
• DigitalOcean weekly backups toggle (+$2.40/mo) before Singhania week
