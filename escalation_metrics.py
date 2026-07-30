"""
escalation_metrics.py — false-escalation tracking for Defender Octa
===================================================================

WHY THIS EXISTS
---------------
False-escalation rate is the number that decides whether a managed
monitoring service is profitable. Every escalation that reaches a human
costs operator minutes. If 80% of them are nothing, your operator can
watch four sites instead of twenty, and the margin model collapses.

It is also the number a client will ask about in month two: "why did you
call me at 2am about a cat?" Having the answer, with a trend line, is the
difference between a renewal conversation and a refund conversation.

WHAT IT MEASURES
----------------
Of the incidents that were escalated to a human, what fraction turned out
to need no action?

    false_escalation_rate = false / (genuine + false)

Note what is NOT in that denominator: unadjudicated and ambiguous cases.
An escalation nobody ruled on is *unknown*, not *false*. Quietly counting
unreviewed escalations as correct is the most common way this metric gets
flattered into uselessness. So this module also reports:

    adjudication_coverage = adjudicated / total_escalations

If coverage is low, the rate is not trustworthy and the module says so
out loud rather than returning a confident-looking number.

THE HONEST CAVEAT
-----------------
This metric needs a HUMAN VERDICT. Code cannot tell you whether the man
at the gate at 2am was a threat. Until something calls record_verdict(),
this table fills with escalations and zero verdicts, and every stat will
report coverage 0% with rate None. That is correct behaviour, not a bug.
The verdict capture (two buttons on the incident card) is the other half
of this feature.

DESIGN
------
Deliberately self-contained. It owns its own table, runs its own
migration on import, and does not assume anything about the schema of
`incidents` or `vehicle_events`. It links to incidents by id only, as a
loose reference. That means it cannot be broken by a change elsewhere,
and it can be removed by dropping one table.

INTEGRATION — three hooks
-------------------------
1. Where an escalation fires (tiering_brain / guardian wiring):

       from escalation_metrics import record_escalation
       record_escalation(incident_id=inc_id, tier=3, trigger_type="face",
                         camera="Main Gate", zone="gate", channel="whatsapp",
                         subject="UNKNOWN")

2. Where a guard resolves an incident (api_server incident update route):

       from escalation_metrics import record_verdict
       record_verdict(incident_id, verdict="false", by=current_user,
                      note="delivery van, resident expecting it")

3. Register the blueprint in api_server.py, BEFORE app.run():

       from escalation_metrics import escalation_bp
       app.register_blueprint(escalation_bp)

TIMESTAMPS
----------
Stored as 'YYYY-MM-DD HH:MM:SS' with a SPACE, never ISO 'T'. This is
deliberate — the T-separator has already cost this project one round of
debugging in SQL comparisons. Everything here goes through _now() and
_fmt() so the format cannot drift.

Run this file directly to self-test against a throwaway database:

    python escalation_metrics.py
"""

import os
import sqlite3
import statistics
from datetime import datetime, timedelta

try:
    from flask import Blueprint, jsonify, request
except Exception:      # allows the self-test to run without Flask
    Blueprint = None


# ══════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════

DB_PATH = os.environ.get("GUARDIANGRID_DB", "guardiangrid.db")

TS_FMT = "%Y-%m-%d %H:%M:%S"

# Verdicts a human can give
VERDICT_GENUINE   = "genuine"    # real, action was warranted
VERDICT_FALSE     = "false"      # no action needed, should not have escalated
VERDICT_AMBIGUOUS = "ambiguous"  # judgement call, excluded from the rate
VALID_VERDICTS = {VERDICT_GENUINE, VERDICT_FALSE, VERDICT_AMBIGUOUS}

# Below this adjudication coverage, the rate is reported but flagged as
# not yet trustworthy.
COVERAGE_TRUST_THRESHOLD = 0.60


def _now() -> str:
    return datetime.now().strftime(TS_FMT)


def _fmt(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # WAL lets the dashboard read while a camera thread writes.
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn


# ══════════════════════════════════════════════════════════════════
# Migration — runs on import, safe to run repeatedly
# ══════════════════════════════════════════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS escalations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id         TEXT,
    escalated_at        TEXT NOT NULL,
    tier                INTEGER,
    trigger_type        TEXT,
    camera              TEXT,
    zone                TEXT,
    hour_of_day         INTEGER,
    channel             TEXT,
    subject             TEXT,
    verdict             TEXT,
    verdict_at          TEXT,
    verdict_by          TEXT,
    verdict_note        TEXT,
    acknowledged_at     TEXT,
    ack_latency_seconds REAL,
    auto_closed         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_esc_time    ON escalations(escalated_at);
CREATE INDEX IF NOT EXISTS idx_esc_verdict ON escalations(verdict);
CREATE INDEX IF NOT EXISTS idx_esc_camera  ON escalations(camera);
CREATE INDEX IF NOT EXISTS idx_esc_inc     ON escalations(incident_id);
"""


def init_db():
    """Create the escalations table if missing. Idempotent."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


init_db()


# ══════════════════════════════════════════════════════════════════
# Write path
# ══════════════════════════════════════════════════════════════════

def record_escalation(incident_id=None, tier=None, trigger_type="other",
                      camera=None, zone=None, channel="none", subject=None,
                      at=None):
    """
    Log that an escalation reached a human. Call this at the moment the
    alert actually goes out (WhatsApp sent / voice played), NOT when the
    detection fires — a Tier 1 event that stayed silent is not an
    escalation and must not be in the denominator.

    Returns the new row id, or None on failure. Never raises: a metrics
    failure must not take down an alert path.
    """
    ts = at or _now()
    try:
        hour = datetime.strptime(ts, TS_FMT).hour
    except Exception:
        ts = _now()
        hour = datetime.now().hour

    try:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO escalations
                   (incident_id, escalated_at, tier, trigger_type, camera,
                    zone, hour_of_day, channel, subject)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (str(incident_id) if incident_id is not None else None,
                 ts, tier, trigger_type, camera, zone, hour, channel, subject)
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        print(f"[ESCALATION] log failed: {e}")
        return None


def record_acknowledgement(incident_id, at=None):
    """
    Record that a human acknowledged the escalation. Used for ack latency,
    which is the second-most useful operational number after the rate
    itself — a low false rate means nothing if nobody responds for an hour.
    """
    ts = at or _now()
    try:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT id, escalated_at FROM escalations
                   WHERE incident_id = ? AND acknowledged_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (str(incident_id),)
            ).fetchone()
            if not row:
                return False

            try:
                started = datetime.strptime(row["escalated_at"], TS_FMT)
                latency = (datetime.strptime(ts, TS_FMT) - started).total_seconds()
            except Exception:
                latency = None

            conn.execute(
                """UPDATE escalations
                   SET acknowledged_at = ?, ack_latency_seconds = ?
                   WHERE id = ?""",
                (ts, latency, row["id"])
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"[ESCALATION] ack failed: {e}")
        return False


def record_verdict(incident_id, verdict, by=None, note=None, at=None):
    """
    Record the human ruling on an escalation. This is the input the whole
    metric depends on.

    verdict: 'genuine' | 'false' | 'ambiguous'
    """
    verdict = (verdict or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        raise ValueError(
            f"verdict must be one of {sorted(VALID_VERDICTS)}, got {verdict!r}"
        )

    ts = at or _now()
    try:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT id FROM escalations
                   WHERE incident_id = ? ORDER BY id DESC LIMIT 1""",
                (str(incident_id),)
            ).fetchone()
            if not row:
                print(f"[ESCALATION] no escalation logged for incident {incident_id}")
                return False

            conn.execute(
                """UPDATE escalations
                   SET verdict = ?, verdict_at = ?, verdict_by = ?, verdict_note = ?
                   WHERE id = ?""",
                (verdict, ts, by, note, row["id"])
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"[ESCALATION] verdict failed: {e}")
        return False


def mark_auto_closed(incident_id, at=None):
    """
    Flag an escalation that timed out with no human response. These are
    tracked separately and are NOT counted as false — an unanswered alarm
    is an operational failure, a different problem from a noisy detector.
    """
    try:
        conn = _connect()
        try:
            conn.execute(
                """UPDATE escalations SET auto_closed = 1
                   WHERE incident_id = ? AND verdict IS NULL""",
                (str(incident_id),)
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        print(f"[ESCALATION] auto-close flag failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# Read path — the numbers
# ══════════════════════════════════════════════════════════════════

def _window_clause(days=None, since=None):
    if since:
        return " WHERE escalated_at >= ? ", (since,)
    if days:
        cutoff = _fmt(datetime.now() - timedelta(days=days))
        return " WHERE escalated_at >= ? ", (cutoff,)
    return "", ()


def get_stats(days=7):
    """
    Headline numbers for the last N days.

    Returns a dict. `false_escalation_rate` is None when nothing has been
    adjudicated — deliberately None rather than 0.0, so a dashboard cannot
    render "0% false escalations" from an empty table.
    """
    where, params = _window_clause(days=days)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT verdict, auto_closed, ack_latency_seconds FROM escalations{where}",
            params
        ).fetchall()
    finally:
        conn.close()

    total     = len(rows)
    genuine   = sum(1 for r in rows if r["verdict"] == VERDICT_GENUINE)
    false_    = sum(1 for r in rows if r["verdict"] == VERDICT_FALSE)
    ambiguous = sum(1 for r in rows if r["verdict"] == VERDICT_AMBIGUOUS)
    unreviewed = total - genuine - false_ - ambiguous
    auto_closed = sum(1 for r in rows if r["auto_closed"])

    decided = genuine + false_
    rate = (false_ / decided) if decided else None
    coverage = ((genuine + false_ + ambiguous) / total) if total else 0.0

    latencies = [r["ack_latency_seconds"] for r in rows
                 if r["ack_latency_seconds"] is not None]

    return {
        "window_days": days,
        "total_escalations": total,
        "genuine": genuine,
        "false": false_,
        "ambiguous": ambiguous,
        "unreviewed": unreviewed,
        "auto_closed_unanswered": auto_closed,

        "false_escalation_rate": round(rate, 4) if rate is not None else None,
        "false_escalation_pct": round(rate * 100, 1) if rate is not None else None,

        "adjudication_coverage": round(coverage, 4),
        "adjudication_coverage_pct": round(coverage * 100, 1),
        "trustworthy": coverage >= COVERAGE_TRUST_THRESHOLD and decided >= 10,
        "note": (
            "Rate is provisional — too few escalations have been reviewed."
            if not (coverage >= COVERAGE_TRUST_THRESHOLD and decided >= 10)
            else "Rate is based on a reviewed sample."
        ),

        "escalations_per_day": round(total / days, 2) if days else None,
        "median_ack_seconds": round(statistics.median(latencies), 1) if latencies else None,
    }


def get_breakdown(days=7, by="camera"):
    """
    Where the noise is coming from. `by` = camera | trigger_type | hour_of_day | tier.

    This is the actionable view: a 40% overall false rate is useless, but
    "Parking B accounts for 70% of your false escalations" tells you to go
    re-aim one camera or mask one region.
    """
    if by not in {"camera", "trigger_type", "hour_of_day", "tier", "zone"}:
        raise ValueError(f"cannot break down by {by!r}")

    where, params = _window_clause(days=days)
    conn = _connect()
    try:
        rows = conn.execute(
            f"""SELECT {by} AS bucket,
                       COUNT(*) AS total,
                       SUM(CASE WHEN verdict='{VERDICT_FALSE}'   THEN 1 ELSE 0 END) AS false_count,
                       SUM(CASE WHEN verdict='{VERDICT_GENUINE}' THEN 1 ELSE 0 END) AS genuine_count
                FROM escalations{where}
                GROUP BY {by}
                ORDER BY false_count DESC, total DESC""",
            params
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        decided = (r["false_count"] or 0) + (r["genuine_count"] or 0)
        out.append({
            "bucket": r["bucket"],
            "total": r["total"],
            "false": r["false_count"] or 0,
            "genuine": r["genuine_count"] or 0,
            "false_rate_pct": round(100 * r["false_count"] / decided, 1) if decided else None,
        })
    return out


def get_trend(days=14):
    """Daily false rate, for a sparkline. Improving noise is a sellable story."""
    cutoff = _fmt(datetime.now() - timedelta(days=days))
    conn = _connect()
    try:
        rows = conn.execute(
            f"""SELECT substr(escalated_at, 1, 10) AS day,
                       COUNT(*) AS total,
                       SUM(CASE WHEN verdict='{VERDICT_FALSE}'   THEN 1 ELSE 0 END) AS false_count,
                       SUM(CASE WHEN verdict='{VERDICT_GENUINE}' THEN 1 ELSE 0 END) AS genuine_count
                FROM escalations
                WHERE escalated_at >= ?
                GROUP BY day ORDER BY day""",
            (cutoff,)
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        decided = (r["false_count"] or 0) + (r["genuine_count"] or 0)
        out.append({
            "day": r["day"],
            "total": r["total"],
            "false_rate_pct": round(100 * r["false_count"] / decided, 1) if decided else None,
        })
    return out


def get_pending_review(limit=50):
    """Escalations still awaiting a human verdict — feeds the review queue."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM escalations
               WHERE verdict IS NULL
               ORDER BY escalated_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════
# Flask blueprint
# ══════════════════════════════════════════════════════════════════

if Blueprint is not None:
    escalation_bp = Blueprint("escalation_metrics", __name__)

    @escalation_bp.route("/api/escalations/stats", methods=["GET"])
    def _stats_route():
        days = request.args.get("days", 7, type=int)
        return jsonify(get_stats(days=days))

    @escalation_bp.route("/api/escalations/breakdown", methods=["GET"])
    def _breakdown_route():
        days = request.args.get("days", 7, type=int)
        by = request.args.get("by", "camera")
        try:
            return jsonify(get_breakdown(days=days, by=by))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    @escalation_bp.route("/api/escalations/trend", methods=["GET"])
    def _trend_route():
        days = request.args.get("days", 14, type=int)
        return jsonify(get_trend(days=days))

    @escalation_bp.route("/api/escalations/pending", methods=["GET"])
    def _pending_route():
        return jsonify(get_pending_review(
            limit=request.args.get("limit", 50, type=int)))

    @escalation_bp.route("/api/escalations/<incident_id>/verdict", methods=["POST"])
    def _verdict_route(incident_id):
        data = request.get_json(silent=True) or {}
        verdict = data.get("verdict")
        try:
            ok = record_verdict(
                incident_id,
                verdict=verdict,
                by=data.get("by"),
                note=data.get("note"),
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if not ok:
            return jsonify({"error": "no escalation found for that incident"}), 404
        return jsonify({"ok": True, "incident_id": incident_id, "verdict": verdict})
else:
    escalation_bp = None


# ══════════════════════════════════════════════════════════════════
# Self-test
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import random
    import tempfile

    DB_PATH = os.path.join(tempfile.gettempdir(), "esc_selftest.db")
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()

    print(f"Self-test DB: {DB_PATH}\n")

    cameras = ["Main Gate", "Exit Gate", "Parking A", "Parking B"]
    triggers = ["anpr", "face", "person", "weapon"]

    # Empty-table behaviour must not produce a fake 0%
    s = get_stats(days=7)
    assert s["false_escalation_rate"] is None, "empty table must give None, not 0"
    assert s["trustworthy"] is False
    print("PASS  empty table returns None, not a flattering zero")

    # Seed 120 escalations over 10 days; Parking B is deliberately noisy
    for i in range(120):
        when = datetime.now() - timedelta(days=random.uniform(0, 10))
        cam = random.choices(cameras, weights=[2, 2, 2, 5])[0]
        inc = f"INC{i:04d}"
        record_escalation(
            incident_id=inc, tier=random.choice([2, 2, 3]),
            trigger_type=random.choice(triggers), camera=cam,
            zone="gate" if "Gate" in cam else "parking",
            channel="whatsapp", subject="TEST", at=_fmt(when),
        )
        # Acknowledge most of them
        if random.random() < 0.85:
            record_acknowledgement(
                inc, at=_fmt(when + timedelta(seconds=random.uniform(20, 400))))
        else:
            mark_auto_closed(inc)

        # Adjudicate ~75%. Parking B is mostly false alarms.
        if random.random() < 0.75:
            p_false = 0.80 if cam == "Parking B" else 0.30
            v = VERDICT_FALSE if random.random() < p_false else VERDICT_GENUINE
            if random.random() < 0.08:
                v = VERDICT_AMBIGUOUS
            record_verdict(inc, v, by="guard1", note="self-test")

    s = get_stats(days=14)
    print("\n--- STATS ---")
    for k, v in s.items():
        print(f"  {k:28} {v}")

    assert s["total_escalations"] == 120
    assert s["false_escalation_rate"] is not None
    assert 0.0 <= s["false_escalation_rate"] <= 1.0
    assert s["unreviewed"] + s["genuine"] + s["false"] + s["ambiguous"] == 120
    print("\nPASS  counts reconcile, rate in range")

    print("\n--- BREAKDOWN BY CAMERA ---")
    bd = get_breakdown(days=14, by="camera")
    for row in bd:
        print(f"  {str(row['bucket']):12} total={row['total']:3}  "
              f"false={row['false']:3}  rate={row['false_rate_pct']}%")

    worst = max((r for r in bd if r["false_rate_pct"] is not None),
                key=lambda r: r["false_rate_pct"])
    assert worst["bucket"] == "Parking B", \
        f"expected Parking B to be noisiest, got {worst['bucket']}"
    print("\nPASS  breakdown correctly fingers Parking B as the noisy camera")

    print("\n--- TREND (last 14 days) ---")
    for row in get_trend(days=14):
        print(f"  {row['day']}  total={row['total']:3}  false={row['false_rate_pct']}%")

    print(f"\n--- PENDING REVIEW: {len(get_pending_review())} awaiting verdict ---")

    # Bad verdict must be rejected loudly
    try:
        record_verdict("INC0001", "probably-fine")
        raise AssertionError("should have rejected an invalid verdict")
    except ValueError:
        print("PASS  invalid verdict rejected")

    os.remove(DB_PATH)
    print("\nAll self-tests passed.")
