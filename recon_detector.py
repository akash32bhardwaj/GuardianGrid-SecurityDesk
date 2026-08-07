"""
recon_detector.py — repeat-pass / recon pattern detection
----------------------------------------------------------
Born from a real incident: snatching crews do recon before they strike —
the same bike circling the gate, repeated passes across days. Those
patterns already sit in vehicle_events; this module runs the query.

Rules (v1):
  CIRCLING     — same plate, 3+ events within a 20-minute window,
                 and the plate is not a KNOWN resident.
  REPEAT_UNKNOWN — same UNKNOWN plate seen on 3+ distinct days within
                 the last 7 days (a stranger who keeps coming back).

Each finding is flagged at most once per 24h per plate+rule (dedup via
its own recon_flags table) and pushed as a MEDIUM alert — which means
Night Watch chimes it and the tiering philosophy stays intact: this is
a "worth a look", not an alarm.

Standalone scan (from the backend folder):
    python recon_detector.py --scan          # print findings, no alerts
    python recon_detector.py --history       # show past flags

Wiring into api_server.py (2 lines, after push_alert is defined):
    from recon_detector import start_recon_watch
    start_recon_watch(push_alert)            # scans every 10 minutes

DEMO-prefixed plates (seeded data) are excluded unless include_demo=True.
"""
import argparse
import sqlite3
import threading
import time
from datetime import datetime, timedelta

DB = "guardiangrid.db"

CIRCLING_WINDOW_MIN = 20      # minutes
CIRCLING_MIN_EVENTS = 3
REPEAT_DAYS_LOOKBACK = 7      # days
REPEAT_MIN_DAYS = 3
DEDUP_HOURS = 24              # don't re-flag same plate+rule within this


def _conn(db=DB):
    c = sqlite3.connect(db)
    c.execute("""
        CREATE TABLE IF NOT EXISTS recon_flags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            plate      TEXT NOT NULL,
            rule       TEXT NOT NULL,
            camera     TEXT,
            event_count INTEGER,
            first_seen TEXT,
            last_seen  TEXT,
            flagged_at TEXT NOT NULL
        )
    """)
    return c


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("T", " ").split(".")[0])
    except ValueError:
        return None


def _recently_flagged(cur, plate, rule):
    cutoff = (datetime.now() - timedelta(hours=DEDUP_HOURS)).isoformat(sep=" ")
    row = cur.execute(
        "SELECT 1 FROM recon_flags WHERE plate=? AND rule=? AND flagged_at>=? LIMIT 1",
        (plate, rule, cutoff)).fetchone()
    return row is not None


def scan(db=DB, include_demo=False):
    """Run both rules. Returns a list of finding dicts (also records them)."""
    con = _conn(db)
    cur = con.cursor()
    now = datetime.now()
    findings = []

    demo_filter = "" if include_demo else "AND plate NOT LIKE 'DEMO%'"

    # ── Rule 1: CIRCLING — bursts in the last window ─────────────
    since = (now - timedelta(minutes=CIRCLING_WINDOW_MIN)).isoformat(sep=" ")
    rows = cur.execute(f"""
        SELECT plate, COUNT(*) n, MIN(timestamp), MAX(timestamp),
               MAX(camera)
        FROM vehicle_events
        WHERE timestamp >= ?
          AND plate IS NOT NULL AND plate != ''
          AND UPPER(COALESCE(access,'')) != 'KNOWN'
          {demo_filter}
        GROUP BY plate HAVING n >= ?
    """, (since, CIRCLING_MIN_EVENTS)).fetchall()
    for plate, n, first, last, camera in rows:
        if _recently_flagged(cur, plate, "CIRCLING"):
            continue
        findings.append({
            "rule": "CIRCLING", "plate": plate, "count": n,
            "camera": camera or "gate",
            "first_seen": first, "last_seen": last,
            "title": f"Possible recon: {plate}",
            "message": (f"{plate} passed {n} times in "
                        f"{CIRCLING_WINDOW_MIN} min near {camera or 'the gate'} "
                        f"— not a known resident."),
        })

    # ── Rule 2: REPEAT_UNKNOWN — distinct days across the week ───
    since7 = (now - timedelta(days=REPEAT_DAYS_LOOKBACK)).isoformat(sep=" ")
    rows = cur.execute(f"""
        SELECT plate, COUNT(DISTINCT DATE(timestamp)) d, COUNT(*) n,
               MIN(timestamp), MAX(timestamp), MAX(camera)
        FROM vehicle_events
        WHERE timestamp >= ?
          AND plate IS NOT NULL AND plate != ''
          AND UPPER(COALESCE(access,'')) = 'UNKNOWN'
          {demo_filter}
        GROUP BY plate HAVING d >= ?
    """, (since7, REPEAT_MIN_DAYS)).fetchall()
    for plate, d, n, first, last, camera in rows:
        if _recently_flagged(cur, plate, "REPEAT_UNKNOWN"):
            continue
        findings.append({
            "rule": "REPEAT_UNKNOWN", "plate": plate, "count": n,
            "camera": camera or "gate",
            "first_seen": first, "last_seen": last,
            "title": f"Repeat unknown vehicle: {plate}",
            "message": (f"{plate} seen on {d} different days this week "
                        f"({n} passes, latest near {camera or 'the gate'}) "
                        f"— never registered."),
        })

    # record all findings
    for f in findings:
        cur.execute(
            "INSERT INTO recon_flags (plate, rule, camera, event_count, "
            "first_seen, last_seen, flagged_at) VALUES (?,?,?,?,?,?,?)",
            (f["plate"], f["rule"], f["camera"], f["count"],
             f["first_seen"], f["last_seen"], now.isoformat(sep=" ")))
    con.commit()
    con.close()
    return findings


def _push_safe(push_fn, title, message):
    """Call the host's push_alert defensively — signature may vary."""
    for attempt in (
        lambda: push_fn(title, message, "MEDIUM"),
        lambda: push_fn(title=title, message=message, severity="MEDIUM"),
        lambda: push_fn(title, message),
    ):
        try:
            attempt()
            return True
        except TypeError:
            continue
        except Exception as e:
            print(f"[RECON] push failed: {e}")
            return False
    print("[RECON] push_alert signature not recognized — alert not sent")
    return False


def start_recon_watch(push_fn=None, interval_minutes=10, db=DB):
    """Background thread: scan periodically, push MEDIUM alerts."""
    def loop():
        print(f"[RECON] watch started (every {interval_minutes} min, "
              f"circling {CIRCLING_MIN_EVENTS}x/{CIRCLING_WINDOW_MIN}min, "
              f"repeat {REPEAT_MIN_DAYS} days/{REPEAT_DAYS_LOOKBACK})")
        while True:
            try:
                for f in scan(db=db):
                    print(f"[RECON] {f['rule']}: {f['message']}")
                    if push_fn:
                        _push_safe(push_fn, f["title"], f["message"])
            except Exception as e:
                print(f"[RECON] scan error: {e}")
            time.sleep(interval_minutes * 60)
    t = threading.Thread(target=loop, daemon=True, name="recon-watch")
    t.start()
    return t


# ── CLI ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--include-demo", action="store_true")
    args = ap.parse_args()
    if args.history:
        con = _conn()
        for r in con.execute(
                "SELECT flagged_at, rule, plate, event_count, camera "
                "FROM recon_flags ORDER BY id DESC LIMIT 25"):
            print(f"  {r[0]}  {r[1]:<15} {r[2]:<12} x{r[3]}  {r[4]}")
        con.close()
    else:
        found = scan(include_demo=args.include_demo)
        if not found:
            print("[RECON] no recon patterns in current data")
        for f in found:
            print(f"[RECON] {f['rule']}: {f['message']}")
