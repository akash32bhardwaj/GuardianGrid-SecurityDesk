r"""
anomaly_score.py — DEFENDER OCTA Anomaly Score (Wow #9)
--------------------------------------------------------
Learns what "normal" looks like for this site — events per hour, split
weekday/weekend — from its own history, then flags hours that deviate.
"7 vehicle events at 2 AM on a Tuesday" means nothing in a table; "6x
the usual for this hour" is intelligence.

How it works (deliberately simple statistics, explainable to a client):
  * Baseline: last 28 days of vehicle_events, bucketed by
    (hour-of-day, weekday/weekend). Mean + population std per bucket.
  * Score: for the most recent hour, z = (observed - mean) / std.
    Score bands: z < 1 normal · 1-2 elevated · 2-3 unusual · >3 anomaly.
  * Cold start: needs >= MIN_BASELINE_DAYS days of history; before that
    it reports "learning" instead of guessing.

Surfaces:
  * GET /api/anomaly            -> current score + today's hourly view
  * hourly background check     -> WhatsApp to PATTERN_WHATSAPP when
                                   z >= ALERT_Z and count >= ALERT_MIN
                                   (cooldown shared via pattern_alerts)
  * brief_line(d)               -> one sentence for the morning brief
                                   (morning_report imports defensively)

Integration in api_server.py:
    from anomaly_score import anomaly_bp, init_anomaly
    init_anomaly(base_dir=BASE_DIR)
    app.register_blueprint(anomaly_bp)
"""

import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
anomaly_bp = Blueprint("anomaly_score", __name__)

_DB_PATH = "guardiangrid.db"
BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 7
ALERT_Z = 3.0
ALERT_MIN_EVENTS = 3          # never alert on 1 event vs a zero baseline
CHECK_EVERY_MINUTES = 60
_COOLDOWN_HOURS = 6

_TS = "REPLACE(timestamp,'T',' ')"


def init_anomaly(base_dir: str, start_thread: bool = True):
    global _DB_PATH
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")
    if start_thread:
        threading.Thread(target=_check_loop, daemon=True,
                         name="anomaly-score").start()
        logger.info("[ANOMALY] hourly check started")


def _con():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# ════════════════════════════════════════════════════════════════════
# Baseline
# ════════════════════════════════════════════════════════════════════

def _day_type(dt: datetime) -> str:
    return "weekend" if dt.weekday() >= 5 else "weekday"


def build_baseline(cur):
    """{(day_type, hour): (mean, std, n_days)} from the last 28 days,
    excluding today (today is what we're judging)."""
    end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=BASELINE_DAYS)
    rows = cur.execute(
        f"SELECT DATE({_TS}) AS d, CAST(STRFTIME('%H',{_TS}) AS INTEGER)"
        f" AS h, COUNT(*) AS n FROM vehicle_events"
        f" WHERE {_TS} >= ? AND {_TS} < ?"
        f" GROUP BY d, h",
        (start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()

    counts = {}          # (day_type, hour) -> {date: n}
    dates = set()
    for r in rows:
        try:
            dt = datetime.strptime(r["d"], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        dates.add(r["d"])
        counts.setdefault((_day_type(dt), r["h"]), {})[r["d"]] = r["n"]

    n_days_total = len(dates)
    # count how many weekday/weekend dates existed in the window,
    # so hours with zero events still average correctly over all days
    day_counts = {"weekday": 0, "weekend": 0}
    d = start
    while d < end:
        day_counts[_day_type(d)] += 1
        d += timedelta(days=1)

    baseline = {}
    for key, per_date in counts.items():
        total_days = max(day_counts[key[0]], 1)
        vals = list(per_date.values()) + [0] * (total_days - len(per_date))
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        baseline[key] = (mean, math.sqrt(var), total_days)
    return baseline, n_days_total, day_counts


def score_hour(cur, baseline, day_counts, when: datetime):
    """(observed, expected_mean, z) for the hour containing `when`."""
    h_start = when.replace(minute=0, second=0, microsecond=0)
    observed = cur.execute(
        f"SELECT COUNT(*) FROM vehicle_events"
        f" WHERE {_TS} >= ? AND {_TS} < ?",
        (h_start.strftime("%Y-%m-%d %H:%M:%S"),
         (h_start + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    ).fetchone()[0]

    key = (_day_type(when), h_start.hour)
    mean, std, _ = baseline.get(
        key, (0.0, 0.0, day_counts.get(key[0], 0)))
    # floor the std so a dead-quiet baseline hour doesn't make z explode
    eff_std = max(std, 0.7)
    z = (observed - mean) / eff_std
    return observed, round(mean, 2), round(z, 2)


def _band(z: float) -> str:
    if z >= 3:
        return "ANOMALY"
    if z >= 2:
        return "UNUSUAL"
    if z >= 1:
        return "ELEVATED"
    return "NORMAL"


# ════════════════════════════════════════════════════════════════════
# Public surfaces
# ════════════════════════════════════════════════════════════════════

def current_status():
    con = _con()
    cur = con.cursor()
    baseline, n_days, day_counts = build_baseline(cur)
    now = datetime.now()

    if n_days < MIN_BASELINE_DAYS:
        con.close()
        return {"ready": False, "learning_days": n_days,
                "needed_days": MIN_BASELINE_DAYS}

    observed, mean, z = score_hour(cur, baseline, day_counts, now)

    # last-24h hourly strip for the dashboard
    strip = []
    for i in range(23, -1, -1):
        t = now - timedelta(hours=i)
        o, m, zz = score_hour(cur, baseline, day_counts, t)
        strip.append({"hour": t.strftime("%H:00"), "observed": o,
                      "expected": m, "z": zz, "band": _band(zz)})
    con.close()
    return {"ready": True, "baseline_days": n_days,
            "now": {"observed": observed, "expected": mean,
                    "z": z, "band": _band(z)},
            "last_24h": strip}


def brief_line(_d=None):
    """One sentence for the morning brief, or None. Judges last night
    (22:00 yesterday - 06:00 today) against baseline."""
    try:
        con = _con()
        cur = con.cursor()
        baseline, n_days, day_counts = build_baseline(cur)
        if n_days < MIN_BASELINE_DAYS:
            con.close()
            return None
        worst = None
        t = datetime.now().replace(hour=22, minute=0, second=0,
                                   microsecond=0) - timedelta(days=1)
        end = datetime.now().replace(hour=6, minute=0, second=0,
                                     microsecond=0)
        while t < end:
            o, m, z = score_hour(cur, baseline, day_counts, t)
            if o >= ALERT_MIN_EVENTS and (worst is None or z > worst[2]):
                worst = (t, o, z, m)
            t += timedelta(hours=1)
        con.close()
        if worst and worst[2] >= 2:
            t, o, z, m = worst
            return (f"\U0001F4C8 Unusual: {o} events around "
                    f"{t.strftime('%H:00')} \u2014 typical for that hour "
                    f"is {m:g}.")
    except Exception as e:
        logger.warning(f"[ANOMALY] brief_line failed: {e}")
    return None


# ════════════════════════════════════════════════════════════════════
# Hourly alert check (cooldown via pattern_alerts table)
# ════════════════════════════════════════════════════════════════════

def _recently_alerted(cur, tag) -> bool:
    cutoff = (datetime.now()
              - timedelta(hours=_COOLDOWN_HOURS)).isoformat()
    try:
        r = cur.execute(
            "SELECT 1 FROM pattern_alerts WHERE plate=? AND"
            " pattern='VOLUME_ANOMALY' AND alerted_at >= ? LIMIT 1",
            (tag, cutoff)).fetchone()
        return r is not None
    except sqlite3.Error:
        return False


def run_anomaly_check():
    st = current_status()
    if not st.get("ready"):
        return {"ready": False}
    now_s = st["now"]
    if now_s["z"] < ALERT_Z or now_s["observed"] < ALERT_MIN_EVENTS:
        return {"ready": True, "alerted": False, **now_s}

    tag = datetime.now().strftime("%Y-%m-%d %H:00")
    con = _con()
    cur = con.cursor()
    if _recently_alerted(cur, tag):
        con.close()
        return {"ready": True, "alerted": False, "cooldown": True}
    ok = False
    try:
        import whatsapp_config as cfg
        to = getattr(cfg, "PATTERN_WHATSAPP", "") or \
            getattr(cfg, "SECURITY_WHATSAPP", "")
        from whatsapp_alerts import _send_whatsapp
        msg = (f"\U0001F4C8 *DEFENDER OCTA \u2014 Activity anomaly*\n\n"
               f"{now_s['observed']} vehicle events this hour \u2014 "
               f"typical for this hour is {now_s['expected']:g}.\n"
               f"Deviation: {now_s['z']}\u03c3 ({now_s['band']})\n\n"
               f"Check the live feed if unexpected.\n"
               f"_Anomaly trial \u2014 baseline "
               f"{st['baseline_days']} days_")
        ok = bool(_send_whatsapp(to, msg).get("success"))
    except Exception as e:
        logger.warning(f"[ANOMALY] alert failed: {e}")
    if ok:
        try:
            cur.execute(
                "INSERT INTO pattern_alerts (plate, pattern, detail,"
                " alerted_at) VALUES (?,?,?,?)",
                (tag, "VOLUME_ANOMALY",
                 f"{now_s['observed']} vs {now_s['expected']}",
                 datetime.now().isoformat()))
            con.commit()
        except sqlite3.Error:
            pass
    con.close()
    return {"ready": True, "alerted": ok, **now_s}


def _check_loop():
    time.sleep(180)
    while True:
        try:
            run_anomaly_check()
        except Exception as e:
            logger.error(f"[ANOMALY] check error: {e}")
        time.sleep(CHECK_EVERY_MINUTES * 60)


@anomaly_bp.route("/api/anomaly")
def api_anomaly():
    try:
        return jsonify({"success": True, **current_status()})
    except sqlite3.Error as e:
        return jsonify({"success": False, "message": f"DB error: {e}"}), 500


if __name__ == "__main__":
    init_anomaly(os.path.dirname(os.path.abspath(__file__)),
                 start_thread=False)
    import json as _j
    print(_j.dumps(current_status(), indent=2)[:1200])
