@app.route("/api/forecast")
def threat_forecast():
    """Next-7-days risk forecast from historical weekday x hour patterns."""
    lookback_days = 60
    since = (datetime.now() - __import__("datetime").timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(os.path.join(BASE_DIR, "guardiangrid.db"))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    buckets = {}   # (weekday, hour) -> weight
    days_seen = set()

    def add(ts, w):
        t = str(ts or "").replace("T", " ")
        try:
            dt = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return
        days_seen.add(t[:10])
        key = (dt.weekday(), dt.hour)
        buckets[key] = buckets.get(key, 0) + w

    try:
        for r in cur.execute(
            "SELECT timestamp, access, state FROM vehicle_events "
            "WHERE REPLACE(timestamp,'T',' ') >= ?", (since,)):
            a = f"{r['access'] or ''} {r['state'] or ''}".upper()
            if "BLACK" in a:
                add(r["timestamp"], 5)
            elif "UNKNOWN" in a or "UNREGISTER" in a:
                add(r["timestamp"], 2)
        for r in cur.execute(
            "SELECT created_at, severity FROM incidents "
            "WHERE REPLACE(created_at,'T',' ') >= ?", (since,)):
            sev = (r["severity"] or "").upper()
            add(r["created_at"], 6 if sev in ("CRITICAL", "HIGH") else 3)
    except sqlite3.Error:
        pass
    con.close()

    n_days = len(days_seen)
    max_w = max(buckets.values()) if buckets else 1
    out_days = []
    today = datetime.now()
    for i in range(1, 8):
        d = today + __import__("datetime").timedelta(days=i)
        wd = d.weekday()
        day_buckets = {h: w for (w_, h), w in buckets.items() if w_ == wd}
        total = sum(day_buckets.values())
        risk = min(100, round((total / max_w) * 60)) if max_w else 0
        peak_hour = max(day_buckets, key=day_buckets.get) if day_buckets else None
        def fmt(h):
            return f"{h % 12 or 12}{'AM' if h < 12 else 'PM'}"
        out_days.append({
            "date": d.strftime("%Y-%m-%d"),
            "day": d.strftime("%a"),
            "risk": risk,
            "level": "High" if risk >= 60 else "Medium" if risk >= 30 else "Low",
            "peak_window": f"{fmt(peak_hour)}\u2013{fmt((peak_hour + 2) % 24)}" if peak_hour is not None else None,
        })
    confidence = ("high" if n_days >= 30 else "medium" if n_days >= 14 else "low")
    return jsonify({
        "days": out_days,
        "days_of_data": n_days,
        "confidence": confidence,
        "note": f"Prediction based on {n_days} day(s) of site history. "
                f"Accuracy improves as monitoring data accumulates.",
    })
