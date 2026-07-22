# Defender Octa — Client Demo Deployment Guide

Goal: take a spare Windows laptop from blank to a working demo where the client double-clicks one icon and the login page appears.

This is a **native Windows** deployment (no Docker) because the app uses a **live USB webcam** via DirectShow, which a container on Windows can't access.

---

## What I added to your project

Three files, placed directly in the project folder (next to `api_server.py`):

| File | What it does |
|------|--------------|
| `SETUP DEFENDER OCTA (run once).bat` | Installs Python 3.11, creates a `.venv`, installs **all** dependencies, drops the login page into place, adds a Desktop shortcut. Run once. |
| `Start Defender Octa (Demo).bat` | The one-click launcher. Starts the server (auto-restarts on crash) and opens the login page in the browser. |
| `requirements-demo.txt` | The **complete** dependency list (your `requirements.txt` was missing 7 packages the code imports). |
| `Add RTSP Cameras.bat` + `add_cameras.py` | The one on-site step: an interactive tool to enter the client's RTSP camera links (no JSON editing). |

Two problems I fixed along the way, because they would have broken the demo:

1. **Missing dependencies.** `requirements.txt` did not list `python-dotenv`, `reportlab`, `insightface`, `onnxruntime`, `ultralytics`, `openpyxl`, or `twilio` — all imported by the code. The setup installs the full set.
2. **Login page would 404.** The app serves the UI from `frontend\`, but that folder had the JS/CSS assets and **no `index.html`** — the real one lives in `indian_anpr\frontend\`. The setup and the launcher copy it into `frontend\` automatically.

---

## Part A — On the demo laptop (blank Windows machine)

1. Copy the **whole project folder** onto the laptop (USB stick or download). Keep the folder intact — the scripts run from inside it.
2. Double-click **`SETUP DEFENDER OCTA (run once).bat`**.
   - It needs internet and takes ~10–25 minutes (the AI libraries are large).
   - If Windows says Python was just installed but the window can't see it, close the window and run SETUP again — it will finish.
3. Double-click **`Add RTSP Cameras`** (Desktop) and paste the client's camera links — see Part B. **This is the only thing you configure on-site.**
4. Double-click **`Start Defender Octa`** on the Desktop.
   - The **first** launch downloads the easyOCR + face models (~400 MB), so give it a minute or two.
   - The login page opens automatically at **http://localhost:5000**.
5. Log in with **`admin` / `admin123`**.

That's the whole demo flow. To stop, just close the black launcher window.

---

## Part B — The ONLY on-site step: add the RTSP camera links

Everything else is baked in during Part A. At the client, the single manual step is entering their camera URLs, because those depend on the client's cameras and network — you can't know them in advance.

Do **not** hand-edit `site_config.json`. Use the tool:

1. Double-click **`Add RTSP Cameras`** (Desktop shortcut, or `Add RTSP Cameras.bat` in the folder).
2. Choose **R** (replace all), then paste each RTSP URL and give it a name. Press ENTER on a blank URL when done.
3. It backs up the old config, writes the new cameras, and tells you to start.
4. Double-click **`Start Defender Octa`**.

The tool handles escaped passwords (e.g. `%40` for `@`), makes a timestamped backup (`site_config.backup-…json`), and leaves every other setting untouched.

The four demo cameras currently in the config point at your office LAN (`192.168.31.x`) — the tool's **Replace all** option clears them and swaps in the client's, so nothing stale shows as offline.

> Optional (only if you also want the laptop's built-in webcam as a feed): the `camera.index` in `site_config.json` is `1`; most laptops use `0`. Not needed if the demo runs off the client's RTSP cameras.

---

## Part C — Demo data (optional, recommended)

Your project already has a demo-data system. To make charts and KPIs look populated:

```
:: from inside the project folder
.venv\Scripts\python seed_demo.py --days 7      :: a week of history
```
Then set `"demo": { "demo_mode": true }` in `site_config.json` (see `DEMO_MODE_WIRING.txt`) and restart. Turn it off with `demo_mode: false`; remove seeded rows with `python seed_demo.py --clear`.

---

## Troubleshooting

- **Login page didn't open / 404** — confirm `frontend\index.html` exists. Re-running the launcher recreates it from `indian_anpr\frontend\`.
- **Black camera feed** — wrong camera index; set it to `0` in `site_config.json` and restart.
- **"Optional AI libraries did not fully install" during setup** — `insightface` needs Microsoft C++ Build Tools. Login, ANPR, and the dashboard still work; only live face/person detection is off. Install Build Tools from https://visualstudio.microsoft.com/visual-cpp-build-tools/ and re-run setup to enable it.
- **Port 5000 already in use** — change `server.port` in `site_config.json` and update the URL in the launcher (`http://localhost:5000` → your port), or just close whatever else is using 5000.
- **App keeps restarting** — the launcher auto-restarts on crash by design; read the error printed just above the "Restarting in 5 seconds" line to see the real cause.

---

## Why not Docker

You initially wanted a Docker one-click. For this app it's the wrong fit: it's a single Flask process with SQLite (nothing to orchestrate), and its live camera uses `cv2.VideoCapture(index, cv2.CAP_DSHOW)` — a Windows DirectShow API. Docker Desktop on Windows runs inside a Linux VM with no access to a USB webcam, so the live feed — the core of the demo — would be dead. The native launcher above is simpler and keeps the camera working.
