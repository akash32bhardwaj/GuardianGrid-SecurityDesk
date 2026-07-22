r"""
GuardianGrid — Demo Brief Seeder
=================================
Generates a week of plausible BACKDATED morning-brief JSONs so the
Intelligence page trend chart and brief history look complete during
sales demos, before real overnight data has accumulated.

Follows the demo-mode philosophy: seeded briefs are tagged "demo": true
so they are cleanly separable from real ones, and one command purges them.

Usage (from the backend folder):
  python seed_briefs.py            # seed the last 7 days (skips today)
  python seed_briefs.py --days 14  # longer history
  python seed_briefs.py --purge    # delete ONLY seeded briefs, keep real ones

NOTE: JSON only — no PDFs are generated for seeded days. The dashboard
list still shows them; the PDF button will 404 for demo rows, so during
a pitch, download today's (real) PDF, not a seeded one.
"""

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta

REPORT_DIR = "reports"

CAMERAS = ["Main Gate", "Rear Exit", "Block C Parking", "Clubhouse"]

def ensure_backend_dir():
    if not os.path.exists("guardiangrid.db"):
        print("[ABORT] guardiangrid.db not found — cd to the backend folder first.")
        sys.exit(1)

def label_for(score):
    if score >= 90: return "Excellent"
    if score >= 75: return "Good"
    if score >= 50: return "Needs attention"
    return "At risk"

def make_brief(date, rng):
    """One plausible night. Mostly quiet, occasionally eventful."""
    eventful = rng.random() < 0.45
    vehicles = rng.randint(18, 42)
    unknown = rng.randint(1, 4) if rng.random() < 0.7 else 0
    blacklisted = 1 if (eventful and rng.random() < 0.5) else 0
    critical = blacklisted if rng.random() < 0.8 else 0
    medium = rng.randint(1, 2) if (eventful or rng.random() < 0.35) else 0
    total_inc = critical + medium

    score = 100 - critical * 15 - medium * 5 - blacklisted * 10
    if vehicles and unknown / vehicles > 0.30:
        score -= 5
    score = max(0, min(100, score))

    brief = {
        "date": date,
        "score": score,
        "label": label_for(score),
        "vehicles_total": vehicles,
        "vehicles_unknown": unknown,
        "vehicles_blacklisted": blacklisted,
        "incidents_total": total_inc,
        "incidents_critical": critical,
        "incidents_medium": medium,
        "pending_detections": 0,
        "generated_at": f"{date} 07:00",
        "pdf": f"brief_{date}.pdf",
        "risk_area": None,
        "risk_window": None,
        "recommendation": None,
        "demo": True,
    }

    if unknown or blacklisted:
        cam = rng.choice(CAMERAS)
        hour = rng.choice([23, 0, 1, 2, 3])
        def fmt(h):
            h %= 24
            return f"{h % 12 or 12}{'AM' if h < 12 else 'PM'}"
        brief["risk_area"] = cam
        brief["risk_window"] = f"{fmt(hour)}\u2013{fmt(hour + 1)}"
        if blacklisted:
            brief["recommendation"] = (
                f"Blacklisted vehicle activity at {cam} — increase night patrol "
                f"attention and verify gate barrier response between {brief['risk_window']}."
            )
        else:
            brief["recommendation"] = (
                f"Improve lighting and increase patrol attention at {cam} "
                f"between {brief['risk_window']}."
            )
    return brief

def main():
    ap = argparse.ArgumentParser(description="Seed or purge demo briefs")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--seed", type=int, default=42, help="rng seed (stable output)")
    args = ap.parse_args()

    ensure_backend_dir()
    os.makedirs(REPORT_DIR, exist_ok=True)

    if args.purge:
        removed = 0
        for f in os.listdir(REPORT_DIR):
            if not f.endswith(".json"):
                continue
            path = os.path.join(REPORT_DIR, f)
            try:
                with open(path, encoding="utf-8") as fh:
                    if json.load(fh).get("demo") is True:
                        os.remove(path)
                        removed += 1
            except (json.JSONDecodeError, OSError):
                continue
        print(f"[OK] Purged {removed} seeded brief(s). Real briefs untouched.")
        return

    rng = random.Random(args.seed)
    today = datetime.now().date()
    written, skipped = 0, 0
    for i in range(1, args.days + 1):
        date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        path = os.path.join(REPORT_DIR, f"brief_{date}.json")
        if os.path.exists(path):
            skipped += 1          # never overwrite a real brief
            continue
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(make_brief(date, rng), fh, indent=2)
        written += 1
    print(f"[OK] Seeded {written} demo brief(s), skipped {skipped} existing.")
    print("     Purge before real client reporting: python seed_briefs.py --purge")

if __name__ == "__main__":
    main()
