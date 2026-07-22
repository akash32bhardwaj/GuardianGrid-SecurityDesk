r"""
GuardianGrid — Weekly AI Security Audit
========================================
Generates the 7-day audit:
  1. Aggregates the week from SQLite (vehicle_events + incidents)
  2. Pulls daily scores from reports/brief_*.json
  3. Renders a branded multi-section PDF -> reports/audits/audit_YYYY-MM-DD.pdf
  4. Saves a JSON summary               -> reports/audits/audit_YYYY-MM-DD.json

Audits live in reports/audits/ (a subfolder) so the daily-brief API and
dashboard pages are never polluted by weekly files.

Requires in api_server.py (imports os/json/sqlite3 already added):

    AUDITS_DIR = os.path.join(REPORTS_DIR, "audits")

    @app.route("/api/audits")
    def list_audits():
        out = []
        if os.path.isdir(AUDITS_DIR):
            for f in sorted(os.listdir(AUDITS_DIR), reverse=True):
                if f.endswith(".json"):
                    try:
                        with open(os.path.join(AUDITS_DIR, f), encoding="utf-8") as fh:
                            out.append(json.load(fh))
                    except (json.JSONDecodeError, OSError):
                        continue
        return jsonify(out[:12])

    @app.route("/api/audits/<date>/pdf")
    def audit_pdf(date):
        safe = re.sub(r"[^0-9-]", "", date)
        path = os.path.join(AUDITS_DIR, f"audit_{safe}.pdf")
        if not os.path.exists(path):
            return jsonify({"error": "audit not found"}), 404
        return send_from_directory(AUDITS_DIR, f"audit_{safe}.pdf")

Usage (from the backend folder):
  python weekly_audit.py             # last 7 days ending today
  python weekly_audit.py --days 14   # longer window

Schedule (Task Scheduler, weekly Sunday 7:30 AM):
  /c cd /d C:\Users\akash\Desktop\GuardianGrid-SecurityDesk && python weekly_audit.py
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

DB_FILE    = "guardiangrid.db"
REPORT_DIR = "reports"
AUDIT_DIR  = os.path.join(REPORT_DIR, "audits")
SITE_NAME  = os.environ.get("GG_SITE_NAME", "Demo Site")
COMPANY    = "S&N GuardianGrid Technologies"

NAVY, CYAN, BLUE  = "#0a0e1a", "#00c2ff", "#1a6bff"
GREEN, AMBER, RED = "#00c48c", "#f5a623", "#ff3b55"
MUTED, LIGHT      = "#7c8db5", "#e5ecff"

def ensure_backend_dir():
    if not os.path.exists(DB_FILE):
        print(f"[ABORT] {DB_FILE} not found in {os.getcwd()} — cd to the backend folder first.")
        sys.exit(1)

# ------------------------------------------------------------ collection ---

def day_range(days):
    today = datetime.now().date()
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]

def collect_week(days):
    import sqlite3
    dates = day_range(days)
    start = f"{dates[0]} 00:00:00"
    end   = f"{dates[-1]} 23:59:59"

    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    def rows(sql, params):
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        except sqlite3.Error:
            return []

    events = rows(
        "SELECT plate, camera, timestamp, "
        "CASE WHEN UPPER(COALESCE(access,'')) LIKE '%BLACK%' "
        "       OR UPPER(COALESCE(state,''))  LIKE '%BLACK%' THEN 'BLACKLISTED' "
        "     WHEN UPPER(COALESCE(access,'')) LIKE '%UNKNOWN%' "
        "       OR UPPER(COALESCE(access,'')) LIKE '%UNREGISTER%' "
        "       OR UPPER(COALESCE(state,''))  LIKE '%UNKNOWN%' THEN 'UNKNOWN' "
        "     ELSE 'KNOWN' END AS status "
        "FROM vehicle_events WHERE REPLACE(timestamp,'T',' ') BETWEEN ? AND ?",
        (start, end))

    incidents = rows(
        "SELECT COALESCE(incident_id,'GG-'||id) AS id, title, severity, status, "
        "created_at, camera_name FROM incidents "
        "WHERE REPLACE(created_at,'T',' ') BETWEEN ? AND ? ORDER BY created_at",
        (start, end))
    con.close()

    per_day = {d: {"date": d, "vehicles": 0, "unknown": 0, "blacklist": 0,
                   "incidents": 0, "score": None} for d in dates}
    cam_activity, cam_flagged = {}, {}

    for e in events:
        d = str(e["timestamp"] or "")[:10]
        if d not in per_day:
            continue
        per_day[d]["vehicles"] += 1
        cam = e["camera"] or "Unassigned"
        cam_activity[cam] = cam_activity.get(cam, 0) + 1
        if e["status"] == "UNKNOWN":
            per_day[d]["unknown"] += 1
            cam_flagged[cam] = cam_flagged.get(cam, 0) + 2
        elif e["status"] == "BLACKLISTED":
            per_day[d]["blacklist"] += 1
            cam_flagged[cam] = cam_flagged.get(cam, 0) + 5

    critical = 0
    for inc in incidents:
        d = str(inc["created_at"] or "")[:10]
        if d in per_day:
            per_day[d]["incidents"] += 1
        if (inc["severity"] or "").upper() in ("CRITICAL", "HIGH"):
            critical += 1

    # daily scores from brief JSONs
    for d in dates:
        p = os.path.join(REPORT_DIR, f"brief_{d}.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    per_day[d]["score"] = json.load(fh).get("score")
            except (json.JSONDecodeError, OSError):
                pass

    scores = [v["score"] for v in per_day.values() if v["score"] is not None]
    return {
        "dates": dates,
        "per_day": [per_day[d] for d in dates],
        "avg_score": round(sum(scores) / len(scores)) if scores else None,
        "best": max(scores) if scores else None,
        "worst": min(scores) if scores else None,
        "vehicles_total": sum(v["vehicles"] for v in per_day.values()),
        "unknown_total": sum(v["unknown"] for v in per_day.values()),
        "blacklist_total": sum(v["blacklist"] for v in per_day.values()),
        "incidents_total": len(incidents),
        "incidents_critical": critical,
        "incident_rows": [dict(i) for i in incidents][:12],
        "cam_activity": sorted(cam_activity.items(), key=lambda x: -x[1])[:5],
        "cam_flagged": sorted(cam_flagged.items(), key=lambda x: -x[1])[:3],
    }

# --------------------------------------------------------------- analysis --

def recommendations(w):
    recos = []
    if w["cam_flagged"]:
        cam, _ = w["cam_flagged"][0]
        recos.append(f"{cam} accumulated the most flagged activity this week — "
                     f"review its coverage, lighting and guard attention.")
    if w["blacklist_total"] > 0:
        recos.append(f"{w['blacklist_total']} blacklisted-vehicle event(s) occurred — "
                     f"confirm each incident's evidence and the guard response log.")
    if w["vehicles_total"] and w["unknown_total"] / w["vehicles_total"] > 0.25:
        recos.append("Unknown vehicles exceed 25% of traffic — consider a resident "
                     "vehicle registration drive to improve recognition coverage.")
    if w["avg_score"] is not None and w["avg_score"] >= 90:
        recos.append("Overall security posture is excellent — maintain current "
                     "patrol timings and monitoring configuration.")
    if not recos:
        recos.append("A quiet week with routine activity — no configuration changes recommended.")
    return recos[:4]

# ------------------------------------------------------------------- PDF ---

def render_pdf(w, out_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas

    W, H = A4
    c = canvas.Canvas(out_path, pagesize=A4)

    # header band
    c.setFillColor(HexColor(NAVY))
    c.rect(0, H - 40 * mm, W, 40 * mm, fill=1, stroke=0)
    c.setFillColor(HexColor(CYAN))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(18 * mm, H - 17 * mm, "Weekly AI Security Audit")
    c.setFillColor(HexColor(LIGHT))
    c.setFont("Helvetica", 10)
    c.drawString(18 * mm, H - 24 * mm,
                 f"{SITE_NAME}  \u00b7  {w['dates'][0]} to {w['dates'][-1]}")
    c.setFillColor(HexColor(MUTED))
    c.setFont("Helvetica", 8)
    c.drawString(18 * mm, H - 30 * mm, f"{COMPANY}  \u00b7  autonomous AI monitoring")

    # average score ring
    if w["avg_score"] is not None:
        s = w["avg_score"]
        col = GREEN if s >= 90 else CYAN if s >= 75 else AMBER if s >= 50 else RED
        cx, cy, r = W - 38 * mm, H - 20 * mm, 11 * mm
        c.setStrokeColor(HexColor(col)); c.setLineWidth(3)
        c.circle(cx, cy, r, stroke=1, fill=0)
        c.setFillColor(HexColor(LIGHT)); c.setFont("Helvetica-Bold", 18)
        c.drawCentredString(cx, cy - 3, str(s))
        c.setFillColor(HexColor(col)); c.setFont("Helvetica", 7)
        c.drawCentredString(cx, cy - r - 4 * mm, "avg weekly score")

    # totals row
    y = H - 56 * mm
    totals = [
        ("Vehicle movements", w["vehicles_total"], BLUE),
        ("Unknown vehicles", w["unknown_total"], AMBER),
        ("Blacklist hits", w["blacklist_total"], RED),
        ("Incidents", w["incidents_total"], GREEN),
        ("Critical", w["incidents_critical"], RED),
    ]
    bw = (W - 36 * mm - 4 * 5 * mm) / 5
    x = 18 * mm
    for label, val, col in totals:
        c.setFillColor(HexColor("#f2f4f9"))
        c.roundRect(x, y, bw, 20 * mm, 3 * mm, fill=1, stroke=0)
        c.setFillColor(HexColor(col)); c.setFont("Helvetica-Bold", 16)
        c.drawString(x + 3.5 * mm, y + 10 * mm, str(val))
        c.setFillColor(HexColor("#444")); c.setFont("Helvetica", 7)
        c.drawString(x + 3.5 * mm, y + 4.5 * mm, label)
        x += bw + 5 * mm

    # per-day table
    y -= 12 * mm
    c.setFillColor(HexColor(NAVY)); c.setFont("Helvetica-Bold", 12)
    c.drawString(18 * mm, y, "Day-by-day breakdown")
    y -= 7 * mm
    c.setFont("Helvetica-Bold", 8); c.setFillColor(HexColor(MUTED))
    headers = ["Date", "Score", "Vehicles", "Unknown", "Blacklist", "Incidents"]
    xs = [18, 55, 85, 115, 145, 175]
    for h_, x_ in zip(headers, xs):
        c.drawString(x_ * mm, y, h_)
    y -= 5.5 * mm
    c.setFont("Helvetica", 9)
    for day in w["per_day"]:
        sc = day["score"]
        sc_col = (GREEN if sc is not None and sc >= 90 else
                  CYAN if sc is not None and sc >= 75 else
                  AMBER if sc is not None and sc >= 50 else
                  RED if sc is not None else MUTED)
        c.setFillColor(HexColor("#222"))
        c.drawString(18 * mm, y, day["date"])
        c.setFillColor(HexColor(sc_col))
        c.drawString(55 * mm, y, str(sc) if sc is not None else "\u2014")
        c.setFillColor(HexColor("#222"))
        for val, x_ in zip(
            [day["vehicles"], day["unknown"], day["blacklist"], day["incidents"]],
            xs[2:],
        ):
            c.drawString(x_ * mm, y, str(val))
        y -= 5.5 * mm

    # risk analysis
    y -= 6 * mm
    c.setFillColor(HexColor(NAVY)); c.setFont("Helvetica-Bold", 12)
    c.drawString(18 * mm, y, "Risk analysis")
    y -= 7 * mm
    c.setFont("Helvetica", 9)
    if w["cam_flagged"]:
        for cam, weight in w["cam_flagged"]:
            c.setFillColor(HexColor(AMBER))
            c.drawString(18 * mm, y, "\u25b8")
            c.setFillColor(HexColor("#222"))
            c.drawString(24 * mm, y, f"{cam} \u2014 flagged-activity weight {weight}")
            y -= 5.5 * mm
    else:
        c.setFillColor(HexColor(GREEN))
        c.drawString(18 * mm, y, "No flagged vehicle activity this week.")
        y -= 5.5 * mm

    # recommendations
    y -= 6 * mm
    c.setFillColor(HexColor(NAVY)); c.setFont("Helvetica-Bold", 12)
    c.drawString(18 * mm, y, "AI recommendations")
    y -= 7 * mm
    c.setFont("Helvetica", 9)
    for reco in recommendations(w):
        c.setFillColor(HexColor(CYAN)); c.drawString(18 * mm, y, "\u25b8")
        c.setFillColor(HexColor("#222"))
        # naive wrap at ~95 chars
        line = reco
        while len(line) > 95:
            cut = line.rfind(" ", 0, 95)
            c.drawString(24 * mm, y, line[:cut]); y -= 5 * mm
            line = line[cut + 1:]
        c.drawString(24 * mm, y, line)
        y -= 6 * mm

    c.setFillColor(HexColor(MUTED)); c.setFont("Helvetica", 7)
    c.drawString(18 * mm, 14 * mm,
                 f"Generated automatically by Defender Octa \u00b7 {COMPANY} \u00b7 snguardiangrid.com")
    c.save()

# ------------------------------------------------------------------ main ---

def main():
    ap = argparse.ArgumentParser(description="GuardianGrid weekly audit")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    ensure_backend_dir()
    w = collect_week(args.days)
    os.makedirs(AUDIT_DIR, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d")
    pdf_path  = os.path.join(AUDIT_DIR, f"audit_{stamp}.pdf")
    json_path = os.path.join(AUDIT_DIR, f"audit_{stamp}.json")

    print(f"=== Weekly audit \u00b7 {SITE_NAME} \u00b7 {w['dates'][0]} \u2192 {w['dates'][-1]} ===")
    print(f"avg score={w['avg_score']} vehicles={w['vehicles_total']} "
          f"unknown={w['unknown_total']} blacklist={w['blacklist_total']} "
          f"incidents={w['incidents_total']}")

    render_pdf(w, pdf_path)
    summary = {
        "type": "weekly", "date": stamp,
        "range": [w["dates"][0], w["dates"][-1]],
        "avg_score": w["avg_score"], "best": w["best"], "worst": w["worst"],
        "vehicles_total": w["vehicles_total"], "unknown_total": w["unknown_total"],
        "blacklist_total": w["blacklist_total"],
        "incidents_total": w["incidents_total"],
        "incidents_critical": w["incidents_critical"],
        "recommendations": recommendations(w),
        "pdf": f"audit_{stamp}.pdf",
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"[OK] PDF  -> {pdf_path}")
    print(f"[OK] JSON -> {json_path}")

if __name__ == "__main__":
    main()
