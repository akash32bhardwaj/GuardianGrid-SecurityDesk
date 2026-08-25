r"""
GuardianGrid — Morning Intelligence Brief
==========================================
Generates the overnight security report:
  1. Queries SQLite for the last N hours (default 12)
  2. Computes the GuardianGrid Security Score (0-100)
  3. Renders a branded PDF  ->  reports/brief_YYYY-MM-DD.pdf
  4. Saves a JSON summary   ->  reports/brief_YYYY-MM-DD.json  (dashboard reads this)
  5. Sends a WhatsApp text summary via Twilio (optional, --send)

Usage (always run from the backend folder):
  python morning_report.py                 # generate PDF + JSON only
  python morning_report.py --send          # also send WhatsApp summary
  python morning_report.py --hours 10      # custom window
  python morning_report.py --dry-run       # print summary, write nothing

Schedule (Windows Task Scheduler, daily 7:00 AM):
  Program:   C:\Windows\System32\cmd.exe
  Arguments: /c cd /d C:\Users\akash\Desktop\GuardianGrid-SecurityDesk && python morning_report.py --send
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

# ---------------------------------------------------------------- config ---

DB_FILE      = "/data/guardiangrid.db" \
    if os.path.exists("/data/guardiangrid.db") else "guardiangrid.db"


def _site_name():
    env = os.environ.get("GG_SITE_NAME", "")
    if env:
        return env
    try:
        with open("site_config.json", encoding="utf-8") as f:
            cfg = json.load(f)
        soc = cfg.get("society") or {}
        return (soc.get("name") or cfg.get("site_name")
                or cfg.get("name") or "Defender Octa Site")
    except (OSError, json.JSONDecodeError):
        return "Defender Octa Site"


SITE_NAME    = _site_name()
REPORT_DIR   = "reports"
COMPANY      = "S&N GuardianGrid Technologies"
TAGLINE      = "AI Night Patrol · 0 human hours required"

# Brand palette
NAVY   = "#0a0e1a"
CARD   = "#101828"
BLUE   = "#1a6bff"
CYAN   = "#00c2ff"
GREEN  = "#00c48c"
AMBER  = "#f5a623"
RED    = "#ff3b55"
MUTED  = "#7c8db5"
LIGHT  = "#e5ecff"

# Twilio (same env vars your whatsapp_alerts.py uses — adjust names if needed)
TWILIO_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
WA_FROM      = os.environ.get("TWILIO_WHATSAPP_FROM", "")   # e.g. whatsapp:+14155238886
WA_TO        = os.environ.get("GG_REPORT_WHATSAPP_TO", "")  # e.g. whatsapp:+91XXXXXXXXXX

# Fall back to whatsapp_config.py (the container's config source) when the
# env vars are absent — same credentials the alert pipeline already uses.
try:
    import whatsapp_config as _wcfg
    TWILIO_SID   = TWILIO_SID or getattr(_wcfg, "TWILIO_ACCOUNT_SID", "")
    TWILIO_TOKEN = TWILIO_TOKEN or getattr(_wcfg, "TWILIO_AUTH_TOKEN", "")
    WA_FROM      = WA_FROM or getattr(_wcfg, "TWILIO_WHATSAPP_FROM", "")
    WA_TO        = WA_TO or getattr(_wcfg, "REPORT_WHATSAPP", "") \
        or getattr(_wcfg, "SECURITY_WHATSAPP", "")
except ImportError:
    pass

# ------------------------------------------------- directory enforcement ---

def ensure_backend_dir():
    """Same guard as migrate.py — prevents ghost guardiangrid.db files."""
    if not os.path.exists(DB_FILE):
        print(f"[ABORT] {DB_FILE} not found in {os.getcwd()}")
        print("        cd to the backend folder first, e.g.:")
        print(r"        cd C:\Users\akash\Desktop\GuardianGrid-SecurityDesk")
        sys.exit(1)

# ------------------------------------------------------------ db helpers ---

def table_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None

def safe_count(cur, sql, params=()):
    """Run a COUNT query; return 0 on any schema mismatch instead of crashing."""
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0

def safe_rows(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except sqlite3.Error:
        return []

# --------------------------------------------------------- data gathering ---

def collect(hours):
    """
    Pulls overnight stats. Table/column names follow the Defender Octa schema;
    every query is wrapped so a missing table degrades to 0 instead of a crash.
    Adjust the SQL constants below if your column names differ.
    """
    since = datetime.now() - timedelta(hours=hours)
    since_iso = since.strftime("%Y-%m-%d %H:%M:%S")

    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    d = {
        "site": SITE_NAME,
        "window_hours": hours,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "since": since_iso,
    }

    # --- vehicle movements (vehicle_events table from the ANPR pipeline) ---
    d["vehicles_total"] = safe_count(cur,
        "SELECT COUNT(*) FROM vehicle_events WHERE REPLACE(timestamp,'T',' ') >= ?", (since_iso,))
    d["vehicles_unknown"] = safe_count(cur,
        "SELECT COUNT(*) FROM vehicle_events WHERE REPLACE(timestamp,'T',' ') >= ? AND ("
        "UPPER(COALESCE(access,'')) LIKE '%UNKNOWN%' OR "
        "UPPER(COALESCE(access,'')) LIKE '%UNREGISTER%' OR "
        "UPPER(COALESCE(state,''))  LIKE '%UNKNOWN%')",
        (since_iso,))
    d["vehicles_blacklisted"] = safe_count(cur,
        "SELECT COUNT(*) FROM vehicle_events WHERE REPLACE(timestamp,'T',' ') >= ? AND ("
        "UPPER(COALESCE(access,'')) LIKE '%BLACK%' OR "
        "UPPER(COALESCE(state,''))  LIKE '%BLACK%')",
        (since_iso,))

    # --- pending guard decisions (table may not exist in this deployment) --
    d["pending_detections"] = safe_count(cur,
        "SELECT COUNT(*) FROM pending_detections WHERE REPLACE(created_at,'T',' ') >= ?",
        (since_iso,))

    # --- incidents, by severity --------------------------------------------
    d["incidents_total"] = safe_count(cur,
        "SELECT COUNT(*) FROM incidents WHERE REPLACE(created_at,'T',' ') >= ?", (since_iso,))
    d["incidents_critical"] = safe_count(cur,
        "SELECT COUNT(*) FROM incidents WHERE REPLACE(created_at,'T',' ') >= ? "
        "AND UPPER(severity) IN ('CRITICAL','HIGH')", (since_iso,))
    d["incidents_medium"] = safe_count(cur,
        "SELECT COUNT(*) FROM incidents WHERE REPLACE(created_at,'T',' ') >= ? "
        "AND UPPER(severity) = 'MEDIUM'", (since_iso,))
    d["incidents_open"] = safe_count(cur,
        "SELECT COUNT(*) FROM incidents WHERE REPLACE(created_at,'T',' ') >= ? "
        "AND UPPER(status) = 'OPEN'", (since_iso,))

    # --- top incidents for the report body ---------------------------------
    d["incident_rows"] = [
        dict(r) for r in safe_rows(cur,
            "SELECT COALESCE(incident_id, 'GG-' || id) AS id, title, severity, "
            "status, created_at FROM incidents WHERE REPLACE(created_at,'T',' ') >= ? "
            "ORDER BY created_at DESC LIMIT 8", (since_iso,))
    ]

    # --- unknown-vehicle plate list (max 6, for the PDF) --------------------
    d["unknown_plates"] = [
        dict(r) for r in safe_rows(cur,
            "SELECT plate, timestamp, camera FROM vehicle_events "
            "WHERE REPLACE(timestamp,'T',' ') >= ? AND ("
            "UPPER(COALESCE(access,'')) LIKE '%UNKNOWN%' OR "
            "UPPER(COALESCE(access,'')) LIKE '%UNREGISTER%' OR "
            "UPPER(COALESCE(state,''))  LIKE '%UNKNOWN%') "
            "ORDER BY timestamp DESC LIMIT 6", (since_iso,))
    ]

    # --- overnight stayers (Virtual Supervisor rule #1) ---------------------
    # Latest event per plate over a 24h lookback == ENTRY, entry happened at
    # or after 18:00, and the vehicle is not a registered resident (KNOWN).
    lookback = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    stay_rows = safe_rows(cur,
        "SELECT plate, event, access, camera, timestamp FROM vehicle_events "
        "WHERE REPLACE(timestamp,'T',' ') >= ? ORDER BY timestamp ASC", (lookback,))
    last_by_plate = {}
    for r in stay_rows:
        p = (r["plate"] or "").strip()
        if p:
            last_by_plate[p] = r
    overnight = []
    for p, r in last_by_plate.items():
        if (r["event"] or "").upper() != "ENTRY":
            continue
        acc = (r["access"] or "UNKNOWN").upper()
        if "KNOWN" == acc or "RESIDENT" in acc:
            continue                      # residents park overnight, that's normal
        ts = str(r["timestamp"] or "").replace("T", " ")
        try:
            hh = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        if hh >= 18 or hh <= 4:           # evening/night arrival still inside
            overnight.append({
                "plate": p, "access": acc,
                "camera": r["camera"] or "", "since": ts[:16],
            })
    overnight.sort(key=lambda o: o["since"])
    d["overnight_vehicles"] = overnight[:10]
    d["overnight_count"] = len(overnight)

    # --- raw rows for risk-area analysis (camera x hour buckets) -----------
    d["risk_rows"] = [
        dict(r) for r in safe_rows(cur,
            "SELECT camera, timestamp, "
            "CASE WHEN UPPER(COALESCE(access,'')) LIKE '%BLACK%' "
            "       OR UPPER(COALESCE(state,''))  LIKE '%BLACK%' THEN 'BLACKLISTED' "
            "     WHEN UPPER(COALESCE(access,'')) LIKE '%UNKNOWN%' "
            "       OR UPPER(COALESCE(access,'')) LIKE '%UNREGISTER%' "
            "       OR UPPER(COALESCE(state,''))  LIKE '%UNKNOWN%' THEN 'UNKNOWN' "
            "     ELSE 'KNOWN' END AS status "
            "FROM vehicle_events "
            "WHERE REPLACE(timestamp,'T',' ') >= ? AND camera IS NOT NULL", (since_iso,))
    ]

    # --- visitors (gate console / visitor register) -------------------------
    d["visitors_total"] = safe_count(cur,
        "SELECT COUNT(*) FROM visitors WHERE REPLACE(in_time,'T',' ') >= ?",
        (since_iso,))
    d["visitors_inside"] = safe_count(cur,
        "SELECT COUNT(*) FROM visitors WHERE REPLACE(in_time,'T',' ') >= ? "
        "AND (out_time IS NULL OR out_time = '')", (since_iso,))

    # --- overnight quiet window: longest gap between events ----------------
    ev_ts = [str(r["timestamp"]).replace("T", " ")[:19]
             for r in safe_rows(cur,
                 "SELECT timestamp FROM vehicle_events "
                 "WHERE REPLACE(timestamp,'T',' ') >= ? ORDER BY timestamp",
                 (since_iso,))]
    d["quiet_from"], d["quiet_to"], d["quiet_minutes"] = None, None, 0
    try:
        pts = ([since_iso] + ev_ts +
               [datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        best = (0, None, None)
        for a, b in zip(pts, pts[1:]):
            ta = datetime.strptime(a[:19], "%Y-%m-%d %H:%M:%S")
            tb = datetime.strptime(b[:19], "%Y-%m-%d %H:%M:%S")
            gap = (tb - ta).total_seconds() / 60
            if gap > best[0]:
                best = (gap, ta, tb)
        if best[1]:
            d["quiet_minutes"] = int(best[0])
            d["quiet_from"] = best[1].strftime("%H:%M")
            d["quiet_to"] = best[2].strftime("%H:%M")
    except ValueError:
        pass

    con.close()

    # --- watch items from Pattern Watch (defensive) -------------------------
    d["watch_items"] = []
    try:
        import pattern_watch as _pw
        _pw.init_pattern_watch(os.path.dirname(os.path.abspath(__file__)),
                               start_thread=False)
        d["watch_items"] = _pw.run_all_detectors()[:3]
    except Exception:
        pass

    return d

# --------------------------------------------------------- security score ---

def compute_score(d):
    """
    GuardianGrid Security Score, 0-100.
      Start at 100, subtract weighted penalties, floor at 0.
    Tune the weights freely — keep them documented for clients.
    """
    score = 100
    score -= d["incidents_critical"] * 15
    score -= d["incidents_medium"]   * 5
    score -= d["vehicles_blacklisted"] * 10
    score -= min(d["pending_detections"], 5) * 3     # unresolved guard queue
    score -= min(d["incidents_open"], 5) * 2          # open at report time
    unknown_overnight = sum(1 for o in d.get("overnight_vehicles", [])
                            if o["access"] not in ("VISITOR",))
    score -= min(unknown_overnight, 3) * 3             # unknown vehicles overnight

    # small penalty if unknown traffic ratio is unusually high (> 30 %)
    if d["vehicles_total"] > 0:
        ratio = d["vehicles_unknown"] / d["vehicles_total"]
        if ratio > 0.30:
            score -= 5

    score = max(0, min(100, score))
    if score >= 90:
        label, color = "Excellent", GREEN
    elif score >= 75:
        label, color = "Good", CYAN
    elif score >= 50:
        label, color = "Needs attention", AMBER
    else:
        label, color = "At risk", RED
    return score, label, color


# ------------------------------------------------------------- risk area ---

def compute_risk(d):
    """
    Finds the camera with the highest weighted risk activity and its peak
    hour window. Weights: blacklist=5, unknown=2, everything else=0.
    Returns (area, window, recommendation) or (None, None, None) when the
    night was fully quiet — the dashboard hides the banner in that case.
    """
    buckets = {}   # (camera, hour) -> weight
    for row in d.get("risk_rows", []):
        status = (row.get("status") or "").upper()
        w = 5 if status == "BLACKLISTED" else 2 if status in ("UNKNOWN", "UNREGISTERED") else 0
        if w == 0:
            continue
        ts = str(row.get("timestamp") or "")
        try:
            hour = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        key = (row.get("camera") or "Unknown camera", hour)
        buckets[key] = buckets.get(key, 0) + w

    if not buckets:
        return None, None, None

    # top camera overall
    cam_totals = {}
    for (cam, _), w in buckets.items():
        cam_totals[cam] = cam_totals.get(cam, 0) + w
    top_cam = max(cam_totals, key=cam_totals.get)

    # peak hour for that camera
    hours = {h: w for (c, h), w in buckets.items() if c == top_cam}
    peak = max(hours, key=hours.get)

    def fmt_h(h):
        h = h % 24
        suffix = "AM" if h < 12 else "PM"
        display = h % 12 or 12
        return f"{display}{suffix}"

    window = f"{fmt_h(peak)}–{fmt_h(peak + 1)}"

    # dominant cause at the top camera: blacklist vs unknown traffic
    cause_w = {"BLACKLISTED": 0, "UNKNOWN": 0}
    for row in d.get("risk_rows", []):
        if (row.get("camera") or "Unknown camera") != top_cam:
            continue
        s = (row.get("status") or "").upper()
        if s in cause_w:
            cause_w[s] += 5 if s == "BLACKLISTED" else 2
    blacklist_led = cause_w["BLACKLISTED"] >= cause_w["UNKNOWN"]

    night = 22 <= peak or peak <= 5
    if blacklist_led and night:
        reco = (f"Blacklisted vehicle activity at {top_cam} — increase night "
                f"patrol attention and verify gate barrier response between {window}.")
    elif blacklist_led:
        reco = (f"Blacklisted vehicle activity at {top_cam} — review the "
                f"incident evidence and confirm guard response around {window}.")
    elif night:
        reco = (f"Improve lighting and increase patrol attention at "
                f"{top_cam} between {window}.")
    else:
        reco = (f"Brief the day guard to verify unknown vehicles at "
                f"{top_cam} around {window}.")
    return top_cam, window, reco

# ----------------------------------------------------------------- PDF -----

def render_pdf(d, score, label, color, out_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas

    W, H = A4
    c = canvas.Canvas(out_path, pagesize=A4)

    # dark header band
    c.setFillColor(HexColor(NAVY))
    c.rect(0, H - 42 * mm, W, 42 * mm, fill=1, stroke=0)

    c.setFillColor(HexColor(CYAN))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(18 * mm, H - 18 * mm, "Morning Intelligence Brief")

    c.setFillColor(HexColor(LIGHT))
    c.setFont("Helvetica", 10)
    c.drawString(18 * mm, H - 25 * mm,
                 f"{d['site']}  ·  {d['generated_at']}  ·  "
                 f"last {d['window_hours']} hours")
    c.setFillColor(HexColor(MUTED))
    c.setFont("Helvetica", 8)
    c.drawString(18 * mm, H - 31 * mm, f"{COMPANY}  ·  {TAGLINE}")

    # score circle (simple ring)
    cx, cy, r = W - 40 * mm, H - 21 * mm, 12 * mm
    c.setStrokeColor(HexColor(color))
    c.setLineWidth(3)
    c.circle(cx, cy, r, stroke=1, fill=0)
    c.setFillColor(HexColor(LIGHT))
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(cx, cy - 3, str(score))
    c.setFont("Helvetica", 7)
    c.setFillColor(HexColor(color))
    c.drawCentredString(cx, cy - r - 4 * mm, f"Security score · {label}")

    # KPI row
    y = H - 58 * mm
    kpis = [
        ("Vehicle movements", d["vehicles_total"], BLUE),
        ("Unknown vehicles",  d["vehicles_unknown"], AMBER),
        ("Blacklist hits",    d["vehicles_blacklisted"], RED),
        ("Incidents",         d["incidents_total"], GREEN),
    ]
    box_w = (W - 36 * mm - 3 * 6 * mm) / 4
    x = 18 * mm
    for title, val, col in kpis:
        c.setFillColor(HexColor("#f2f4f9"))
        c.roundRect(x, y, box_w, 22 * mm, 3 * mm, fill=1, stroke=0)
        c.setFillColor(HexColor(col))
        c.setFont("Helvetica-Bold", 18)
        c.drawString(x + 4 * mm, y + 11 * mm, str(val))
        c.setFillColor(HexColor("#444"))
        c.setFont("Helvetica", 8)
        c.drawString(x + 4 * mm, y + 5 * mm, title)
        x += box_w + 6 * mm

    # incident table
    y -= 14 * mm
    c.setFillColor(HexColor(NAVY))
    c.setFont("Helvetica-Bold", 12)
    c.drawString(18 * mm, y, "Overnight incidents")
    y -= 7 * mm
    c.setFont("Helvetica", 9)
    if not d["incident_rows"]:
        c.setFillColor(HexColor(GREEN))
        c.drawString(18 * mm, y, "No incidents recorded. AI patrol reported a quiet night.")
        y -= 7 * mm
    else:
        for row in d["incident_rows"]:
            sev = (row.get("severity") or "LOW").upper()
            sev_color = RED if sev in ("CRITICAL", "HIGH") else \
                        AMBER if sev == "MEDIUM" else MUTED
            c.setFillColor(HexColor(sev_color))
            c.drawString(18 * mm, y, f"[{sev}]")
            c.setFillColor(HexColor("#222"))
            title = str(row.get("title") or "")[:70]
            c.drawString(40 * mm, y, f"{row.get('id','')}  {title}")
            c.setFillColor(HexColor(MUTED))
            c.drawRightString(W - 18 * mm, y, str(row.get("created_at") or ""))
            y -= 6 * mm
            if y < 40 * mm:
                break

    # unknown plates
    if d["unknown_plates"]:
        y -= 6 * mm
        c.setFillColor(HexColor(NAVY))
        c.setFont("Helvetica-Bold", 12)
        c.drawString(18 * mm, y, "Unknown vehicles")
        y -= 7 * mm
        c.setFont("Helvetica", 9)
        for p in d["unknown_plates"]:
            c.setFillColor(HexColor("#222"))
            c.drawString(18 * mm, y,
                         f"{p.get('plate','?')}   ·   "
                         f"{p.get('camera') or 'camera n/a'}")
            c.setFillColor(HexColor(MUTED))
            c.drawRightString(W - 18 * mm, y, str(p.get("timestamp") or ""))
            y -= 6 * mm
            if y < 30 * mm:
                break

    # overnight stayers (Virtual Supervisor)
    if d.get("overnight_vehicles"):
        y -= 6 * mm
        c.setFillColor(HexColor(NAVY))
        c.setFont("Helvetica-Bold", 12)
        c.drawString(18 * mm, y, "Vehicles inside overnight (Virtual Supervisor)")
        y -= 7 * mm
        c.setFont("Helvetica", 9)
        for o in d["overnight_vehicles"]:
            col = AMBER if o["access"] == "VISITOR" else RED
            c.setFillColor(HexColor(col))
            c.drawString(18 * mm, y, o["access"])
            c.setFillColor(HexColor("#222"))
            c.drawString(42 * mm, y, f"{o['plate']}   ·   entered {o['since']}"
                         + (f"   ·   {o['camera']}" if o['camera'] else ""))
            y -= 5.5 * mm
            if y < 30 * mm:
                break

    # risk recommendation strip
    if d.get("recommendation"):
        y -= 8 * mm
        c.setFillColor(HexColor("#fdf0e3"))
        c.roundRect(18 * mm, y - 4 * mm, W - 36 * mm, 12 * mm, 2 * mm, fill=1, stroke=0)
        c.setFillColor(HexColor("#8a4b0f"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(21 * mm, y + 2.5 * mm, f"Highest risk area: {d['risk_area']} ({d['risk_window']})")
        c.setFont("Helvetica", 8)
        c.drawString(21 * mm, y - 1.5 * mm, d["recommendation"])

    # footer
    c.setFillColor(HexColor(MUTED))
    c.setFont("Helvetica", 7)
    c.drawString(18 * mm, 15 * mm,
                 f"Generated automatically by Defender Octa · {COMPANY} · snguardiangrid.com")
    c.save()

# ------------------------------------------------------------- WhatsApp ----

def whatsapp_summary_text(d, score, label):
    """Narrated brief — sentences a person reads, not a table they parse."""
    now = datetime.now()
    day = now.strftime("%A, %d %b")
    greeting = "\u2600\ufe0f *Good morning" if now.hour < 12 else "\U0001F6E1 *Site brief"

    v, u, b = d["vehicles_total"], d["vehicles_unknown"], d["vehicles_blacklisted"]
    inc, crit = d["incidents_total"], d["incidents_critical"]
    vis = d.get("visitors_total", 0)

    # --- opening verdict ---
    if b or crit:
        mood = "\u26a0\ufe0f An eventful night \u2014 details below."
    elif inc or u > 2:
        mood = "Mostly calm, a few things worth a look."
    else:
        mood = "Quiet night overall."

    # --- activity sentence ---
    bits = []
    if v:
        bits.append(f"{v} vehicle movement{'s' if v != 1 else ''}"
                    + (f" ({u} unknown)" if u else ""))
    if vis:
        bits.append(f"{vis} visitor{'s' if vis != 1 else ''}"
                    + (f", {d.get('visitors_inside', 0)} still inside"
                       if d.get("visitors_inside") else ""))
    if inc:
        bits.append(f"{inc} incident{'s' if inc != 1 else ''}"
                    + (f" ({crit} high severity)" if crit else ""))
    activity = ("No recorded activity in the window."
                if not bits else " \u00b7 ".join(bits) + ".")

    lines = [
        f"{greeting} \u2014 {d['site']}, {day}*",
        "",
        mood,
        activity,
    ]

    # --- blacklist / critical callouts first, they matter most ---
    if b:
        lines.append(f"\u26d4 {b} blacklist hit{'s' if b != 1 else ''} \u2014 "
                     f"check Incident Management.")
    for row in d.get("incident_rows", [])[:2]:
        if str(row.get("severity", "")).upper() in ("CRITICAL", "HIGH"):
            lines.append(f"\U0001F6A8 {row.get('title', 'Incident')} "
                         f"({row.get('status', '')})")

    # --- overnight texture ---
    if d.get("quiet_minutes", 0) >= 120 and d.get("quiet_from"):
        lines.append(f"\U0001F319 Longest quiet stretch: "
                     f"{d['quiet_from']}\u2013{d['quiet_to']} "
                     f"({d['quiet_minutes'] // 60}h {d['quiet_minutes'] % 60}m "
                     f"with no movement).")
    if d.get("overnight_count"):
        lines.append(f"\U0001F697 {d['overnight_count']} non-resident "
                     f"vehicle{'s' if d['overnight_count'] != 1 else ''} "
                     f"stayed overnight.")

    # --- pattern watch items ---
    icons = {"REPEAT_UNKNOWN": "\U0001F501", "ODD_HOURS": "\U0001F319",
             "SHORT_VISITS": "\u23f1\ufe0f"}
    for w in d.get("watch_items", [])[:2]:
        lines.append(f"{icons.get(w['pattern'], '\u26a0\ufe0f')} Watch: "
                     f"{w['plate']} \u2014 {w['detail']}")

    # --- risk recommendation (existing analysis) ---
    if d.get("recommendation"):
        lines.append(f"\u26a0 Risk focus: {d.get('risk_area', '')} "
                     f"({d.get('risk_window', '')}). {d['recommendation']}")

    lines += [
        "",
        f"*Security score: {score}/100 ({label})*",
        "Reply *status* anytime for a live summary.",
    ]
    return "\n".join(lines)

def send_whatsapp(body):
    if not (TWILIO_SID and TWILIO_TOKEN and WA_FROM and WA_TO):
        print("[WARN] Twilio env vars missing — skipping WhatsApp send.")
        print("       Need: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
              "TWILIO_WHATSAPP_FROM, GG_REPORT_WHATSAPP_TO")
        return False
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    msg = client.messages.create(from_=WA_FROM, to=WA_TO, body=body)
    print(f"[OK] WhatsApp sent, SID {msg.sid}")
    return True

# ----------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(description="GuardianGrid morning report")
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--send", action="store_true", help="send WhatsApp summary")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ensure_backend_dir()
    d = collect(args.hours)
    score, label, color = compute_score(d)
    risk_area, risk_window, recommendation = compute_risk(d)
    d["risk_area"], d["risk_window"], d["recommendation"] = risk_area, risk_window, recommendation

    print(f"=== Morning brief · {d['site']} · score {score} ({label}) ===")
    print(f"vehicles={d['vehicles_total']} unknown={d['vehicles_unknown']} "
          f"blacklist={d['vehicles_blacklisted']} incidents={d['incidents_total']}")

    if args.dry_run:
        print(whatsapp_summary_text(d, score, label))
        return

    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    pdf_path  = os.path.join(REPORT_DIR, f"brief_{stamp}.pdf")
    json_path = os.path.join(REPORT_DIR, f"brief_{stamp}.json")

    render_pdf(d, score, label, color, pdf_path)
    print(f"[OK] PDF  -> {pdf_path}")

    summary = {
        "date": stamp, "score": score, "label": label,
        "vehicles_total": d["vehicles_total"],
        "vehicles_unknown": d["vehicles_unknown"],
        "vehicles_blacklisted": d["vehicles_blacklisted"],
        "incidents_total": d["incidents_total"],
        "incidents_critical": d["incidents_critical"],
        "incidents_medium": d["incidents_medium"],
        "pending_detections": d["pending_detections"],
        "generated_at": d["generated_at"],
        "pdf": f"brief_{stamp}.pdf",
        "overnight_count": d["overnight_count"],
        "overnight_vehicles": d["overnight_vehicles"],
        "risk_area": risk_area,
        "risk_window": risk_window,
        "recommendation": recommendation,
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"[OK] JSON -> {json_path}")

    if args.send:
        send_whatsapp(whatsapp_summary_text(d, score, label))


if __name__ == "__main__":
    main()
