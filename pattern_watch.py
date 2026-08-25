r"""
pattern_watch.py — DEFENDER OCTA Pattern & Loitering Alerts (Wow #5)
---------------------------------------------------------------------
Turns the event log into intelligence: instead of "a vehicle entered",
the system notices "this vehicle KEEPS entering".

Detectors (all SQL over vehicle_events — no new data collection):
  1. REPEAT_UNKNOWN : unknown plate seen >=3 times across >=2 distinct
                      days within the last 7 days  (recon pattern)
  2. ODD_HOURS      : unknown plate with >=3 events between 00:00-05:00
                      in the last 7 days
  3. SHORT_VISITS   : plate with >=3 entry->exit stays under 10 minutes
                      in the last 14 days ("drove in, looked, left")

Alerting:
  * WhatsApp to PATTERN_WHATSAPP (whatsapp_config.py) — the founder's
    number during the tuning trial. Falls back to SECURITY_WHATSAPP.
  * 3-day cooldown per (plate, pattern) via the pattern_alerts table,
    so the same pattern doesn't re-fire every scan.
  * Background scan every 6 hours inside the app process; plus
    GET /api/patterns for the dashboard (live compute, JWT-protected).

Integration in api_server.py:
    from pattern_watch import pattern_bp, init_pattern_watch
    init_pattern_watch(base_dir=BASE_DIR)     # starts the 6h scan thread
    app.register_blueprint(pattern_bp)

Config (optional, whatsapp_config.py):
    PATTERN_WHATSAPP = "whatsapp:+91XXXXXXXXXX"   # defaults to security
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
pattern_bp = Blueprint("pattern_watch", __name__)

_DB_PATH = "guardiangrid.db"
_SCAN_EVERY_HOURS = 6
_COOLDOWN_DAYS = 3

# Thresholds — agreed calibration, adjust after the 2-week trial
REPEAT_MIN_SIGHTINGS = 3
REPEAT_MIN_DAYS = 2
REPEAT_WINDOW_DAYS = 7
ODD_MIN_EVENTS = 3
ODD_WINDOW_DAYS = 7
ODD_HOUR_FROM, ODD_HOUR_TO = 0, 5          # 00:00–05:00
SHORT_MAX_MINUTES = 10
SHORT_MIN_VISITS = 3
SHORT_WINDOW_DAYS = 14


def init_pattern_watch(base_dir: str, start_thread: bool = True):
    global _DB_PATH
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")
    _ensure_table()
    if start_thread:
        t = threading.Thread(target=_scan_loop, daemon=True,
                             name="pattern-watch")
        t.start()
        logger.info("[PATTERNS] background scan every "
                    f"{_SCAN_EVERY_HOURS}h started")


def _con():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ensure_table():
    try:
        con = _con()
        con.execute(
            "CREATE TABLE IF NOT EXISTS pattern_alerts ("
            " id INTEGER PRIMARY KEY, plate TEXT, pattern TEXT,"
            " detail TEXT, alerted_at TEXT)")
        con.commit()
        con.close()
    except sqlite3.Error as e:
        logger.error(f"[PATTERNS] table init failed: {e}")


_TS = "REPLACE(timestamp,'T',' ')"
_UNKNOWN = ("(UPPER(COALESCE(access,'')) LIKE '%UNKNOWN%' "
            "OR (COALESCE(access,'')='' AND "
            "UPPER(COALESCE(state,'')) LIKE '%UNKNOWN%'))")


# ════════════════════════════════════════════════════════════════════
# Detectors — each returns [{plate, pattern, detail, evidence...}]
# ════════════════════════════════════════════════════════════════════

def detect_repeat_unknown(cur):
    since = (datetime.now() - timedelta(days=REPEAT_WINDOW_DAYS)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    rows = cur.execute(
        f"SELECT plate, COUNT(*) AS n,"
        f" COUNT(DISTINCT DATE({_TS})) AS days,"
        f" MIN({_TS}) AS first_seen, MAX({_TS}) AS last_seen,"
        f" GROUP_CONCAT(DISTINCT camera) AS cameras"
        f" FROM vehicle_events"
        f" WHERE {_TS} >= ? AND {_UNKNOWN} AND plate IS NOT NULL"
        f"   AND LENGTH(plate) >= 4"
        f" GROUP BY plate"
        f" HAVING n >= ? AND days >= ?",
        (since, REPEAT_MIN_SIGHTINGS, REPEAT_MIN_DAYS)).fetchall()
    return [{
        "plate": r["plate"], "pattern": "REPEAT_UNKNOWN",
        "detail": (f"{r['n']} sightings across {r['days']} days "
                   f"(last {REPEAT_WINDOW_DAYS}d) at {r['cameras']}"),
        "count": r["n"], "days": r["days"],
        "first_seen": r["first_seen"], "last_seen": r["last_seen"],
    } for r in rows]


def detect_odd_hours(cur):
    since = (datetime.now() - timedelta(days=ODD_WINDOW_DAYS)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    rows = cur.execute(
        f"SELECT plate, COUNT(*) AS n, MAX({_TS}) AS last_seen"
        f" FROM vehicle_events"
        f" WHERE {_TS} >= ? AND {_UNKNOWN}"
        f"   AND CAST(STRFTIME('%H', {_TS}) AS INTEGER) >= ?"
        f"   AND CAST(STRFTIME('%H', {_TS}) AS INTEGER) < ?"
        f"   AND plate IS NOT NULL AND LENGTH(plate) >= 4"
        f" GROUP BY plate HAVING n >= ?",
        (since, ODD_HOUR_FROM, ODD_HOUR_TO, ODD_MIN_EVENTS)).fetchall()
    return [{
        "plate": r["plate"], "pattern": "ODD_HOURS",
        "detail": (f"{r['n']} events between "
                   f"{ODD_HOUR_FROM:02d}:00-{ODD_HOUR_TO:02d}:00 "
                   f"in the last {ODD_WINDOW_DAYS}d"),
        "count": r["n"], "last_seen": r["last_seen"],
    } for r in rows]


def detect_short_visits(cur):
    """Entry->next Exit for the same plate under SHORT_MAX_MINUTES."""
    since = (datetime.now() - timedelta(days=SHORT_WINDOW_DAYS)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    events = cur.execute(
        f"SELECT plate, UPPER(COALESCE(event,'')) AS ev, {_TS} AS ts"
        f" FROM vehicle_events"
        f" WHERE {_TS} >= ? AND plate IS NOT NULL AND LENGTH(plate) >= 4"
        f" ORDER BY plate, ts",
        (since,)).fetchall()
    short = {}
    open_entry = {}
    for r in events:
        p = r["plate"]
        if "ENTRY" in r["ev"]:
            open_entry[p] = r["ts"]
        elif "EXIT" in r["ev"] and p in open_entry:
            try:
                t_in = datetime.strptime(open_entry.pop(p)[:19],
                                         "%Y-%m-%d %H:%M:%S")
                t_out = datetime.strptime(r["ts"][:19],
                                          "%Y-%m-%d %H:%M:%S")
                mins = (t_out - t_in).total_seconds() / 60.0
                if 0 <= mins <= SHORT_MAX_MINUTES:
                    short.setdefault(p, []).append(round(mins, 1))
            except ValueError:
                pass
    return [{
        "plate": p, "pattern": "SHORT_VISITS",
        "detail": (f"{len(v)} visits under {SHORT_MAX_MINUTES} min in "
                   f"{SHORT_WINDOW_DAYS}d (durations: "
                   + ", ".join(f"{m}m" for m in v[:5]) + ")"),
        "count": len(v),
    } for p, v in short.items() if len(v) >= SHORT_MIN_VISITS]


def run_all_detectors():
    con = _con()
    cur = con.cursor()
    findings = (detect_repeat_unknown(cur)
                + detect_odd_hours(cur)
                + detect_short_visits(cur))
    con.close()
    return findings


# ════════════════════════════════════════════════════════════════════
# Alerting with cooldown
# ════════════════════════════════════════════════════════════════════

def _recently_alerted(cur, plate, pattern) -> bool:
    cutoff = (datetime.now() - timedelta(days=_COOLDOWN_DAYS)).isoformat()
    r = cur.execute(
        "SELECT 1 FROM pattern_alerts WHERE plate=? AND pattern=?"
        " AND alerted_at >= ? LIMIT 1", (plate, pattern, cutoff)).fetchone()
    return r is not None


def _pattern_recipient():
    try:
        import whatsapp_config as cfg
        to = getattr(cfg, "PATTERN_WHATSAPP", "") or \
            getattr(cfg, "SECURITY_WHATSAPP", "")
        return to
    except ImportError:
        return ""


_ICONS = {"REPEAT_UNKNOWN": "🔁", "ODD_HOURS": "🌙", "SHORT_VISITS": "⏱️"}
_TITLES = {
    "REPEAT_UNKNOWN": "Repeat unknown vehicle",
    "ODD_HOURS": "Odd-hours pattern",
    "SHORT_VISITS": "Short-visit pattern",
}


def _send_pattern_alert(finding) -> bool:
    to = _pattern_recipient()
    if not to:
        return False
    msg = (f"{_ICONS.get(finding['pattern'], '⚠️')} "
           f"*DEFENDER OCTA — Pattern detected*\n\n"
           f"*{_TITLES.get(finding['pattern'], finding['pattern'])}*\n"
           f"🚗 Plate: {finding['plate']}\n"
           f"📊 {finding['detail']}\n\n"
           f"↩️ Reply *show* for the latest snapshot.\n"
           f"_Pattern trial — thresholds under calibration_")
    try:
        from whatsapp_alerts import _send_whatsapp
        r = _send_whatsapp(to, msg)
        ok = bool(r.get("success"))
        if ok:
            # let "show" replies resolve to this plate's latest snapshot
            try:
                from whatsapp_inbound import record_alert_context
                record_alert_context(to, plate=finding["plate"],
                                     camera="", snapshot="")
            except Exception:
                pass
        return ok
    except Exception as e:
        logger.warning(f"[PATTERNS] alert send failed: {e}")
        return False


def run_pattern_scan() -> dict:
    """One full scan: detect -> filter by cooldown -> alert -> record."""
    findings = run_all_detectors()
    sent = 0
    con = _con()
    cur = con.cursor()
    for f in findings:
        if _recently_alerted(cur, f["plate"], f["pattern"]):
            continue
        if _send_pattern_alert(f):
            cur.execute(
                "INSERT INTO pattern_alerts (plate, pattern, detail,"
                " alerted_at) VALUES (?,?,?,?)",
                (f["plate"], f["pattern"], f["detail"],
                 datetime.now().isoformat()))
            sent += 1
    con.commit()
    con.close()
    logger.info(f"[PATTERNS] scan: {len(findings)} findings, "
                f"{sent} new alerts")
    return {"findings": len(findings), "alerted": sent}


def _scan_loop():
    time.sleep(120)          # let the app finish booting first
    while True:
        try:
            run_pattern_scan()
        except Exception as e:
            logger.error(f"[PATTERNS] scan error: {e}")
        time.sleep(_SCAN_EVERY_HOURS * 3600)


# ════════════════════════════════════════════════════════════════════
# Dashboard endpoint (JWT-protected by the global guard)
# ════════════════════════════════════════════════════════════════════

@pattern_bp.route("/api/patterns")
def api_patterns():
    try:
        findings = run_all_detectors()
    except sqlite3.Error as e:
        return jsonify({"success": False, "message": f"DB error: {e}"}), 500
    return jsonify({
        "success": True,
        "count": len(findings),
        "patterns": findings,
        "thresholds": {
            "repeat_unknown": f"{REPEAT_MIN_SIGHTINGS}+ sightings, "
                              f"{REPEAT_MIN_DAYS}+ days, "
                              f"{REPEAT_WINDOW_DAYS}d window",
            "odd_hours": f"{ODD_MIN_EVENTS}+ events "
                         f"{ODD_HOUR_FROM:02d}:00-{ODD_HOUR_TO:02d}:00, "
                         f"{ODD_WINDOW_DAYS}d window",
            "short_visits": f"{SHORT_MIN_VISITS}+ visits under "
                            f"{SHORT_MAX_MINUTES}min, "
                            f"{SHORT_WINDOW_DAYS}d window",
        },
    })


if __name__ == "__main__":
    # manual scan:  python pattern_watch.py
    init_pattern_watch(os.path.dirname(os.path.abspath(__file__)),
                       start_thread=False)
    out = run_pattern_scan()
    print(out)
