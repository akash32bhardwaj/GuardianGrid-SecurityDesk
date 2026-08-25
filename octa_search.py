"""
octa_search.py — DEFENDER OCTA "Google for CCTV" (Phase A: metadata search)
----------------------------------------------------------------------------
Natural-language search over vehicle_events, incidents and visitors.

  POST /api/search        {"query": "unknown vehicles last night at main gate"}
  GET  /api/search/ping   health check

Design:
  1. Parse the question into a structured filter JSON.
       - Primary parser : Claude API (Anthropic) — handles English + Hinglish.
       - Fallback parser: pure-regex rules — works offline, keeps demos alive.
  2. Run safe, parameterised SQL over guardiangrid.db.
  3. Return a human "answer line" + result cards (plate / incident / visitor),
     including confident negatives ("No entries found. Last movement: ...").

Integration (3 lines in api_server.py):
      from octa_search import search_bp, init_search
      init_search(base_dir=BASE_DIR)          # after BASE_DIR is defined
      app.register_blueprint(search_bp)       # near the other blueprints

Auth: /api/search is NOT in AUTH_EXEMPT_PREFIXES, so your existing
before_request JWT guard protects it automatically. VIEWER role can use it
(read-only GET is blocked for POST — see note in README section below if you
want viewers to search too).

Env (optional):
      ANTHROPIC_API_KEY   -> enables the LLM parser
      OCTA_SEARCH_MODEL   -> default "claude-haiku-4-5-20251001"
"""

import os
import re
import json
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

try:
    import requests as _rq
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

search_bp = Blueprint("octa_search", __name__)

_DB_PATH = "guardiangrid.db"          # overridden by init_search()
_MODEL = os.environ.get("OCTA_SEARCH_MODEL", "claude-haiku-4-5-20251001")
_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def init_search(base_dir: str):
    """Point the module at the canonical DB.

    In Docker deployments the live DB is volume-mounted at
    /data/guardiangrid.db (same path api_server.py's reports route uses);
    locally on Windows it sits next to api_server.py. Prefer /data when
    it exists so one file works in both environments.
    """
    global _DB_PATH
    docker_db = "/data/guardiangrid.db"
    if os.path.exists(docker_db):
        _DB_PATH = docker_db
    else:
        _DB_PATH = ("/data/guardiangrid.db"
                    if os.path.exists("/data/guardiangrid.db")
                    else os.path.join(base_dir, "guardiangrid.db"))


# ════════════════════════════════════════════════════════════════════
# 1) FILTER SCHEMA — the contract between parser and SQL builder
# ════════════════════════════════════════════════════════════════════
#
# {
#   "sources":   ["vehicles","incidents","visitors"],   # which tables
#   "time_from": "2026-08-19 20:00:00" | null,
#   "time_to":   "2026-08-20 06:00:00" | null,
#   "plate":     "PB10" | null,          # partial plate ok
#   "vtype":     "car" | "bike" | "truck" | null,
#   "access":    "UNKNOWN" | "BLACKLIST" | "RESIDENT" | "APPROVED" | null,
#   "event":     "ENTRY" | "EXIT" | null,
#   "camera":    "gate" | null,          # substring match
#   "severity":  "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | null,
#   "person":    "name substring" | null,   # visitors.name
#   "limit":     50
# }

_EMPTY = {
    "sources": ["vehicles"], "time_from": None, "time_to": None,
    "plate": None, "vtype": None, "access": None, "event": None,
    "camera": None, "severity": None, "person": None, "limit": 50,
}

_ALLOWED_KEYS = set(_EMPTY)
_ALLOWED_SOURCES = {"vehicles", "incidents", "visitors"}


def _sanitize(filters: dict) -> dict:
    """Whitelist keys/values so the LLM can never inject anything unsafe."""
    out = dict(_EMPTY)
    if not isinstance(filters, dict):
        return out
    for k in _ALLOWED_KEYS:
        if k in filters and filters[k] not in ("", [], {}):
            out[k] = filters[k]
    out["sources"] = [s for s in (out["sources"] or [])
                      if s in _ALLOWED_SOURCES] or ["vehicles"]
    try:
        out["limit"] = max(1, min(int(out["limit"]), 200))
    except (TypeError, ValueError):
        out["limit"] = 50
    for tk in ("time_from", "time_to"):
        v = out[tk]
        if v and not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$",
                              str(v)):
            out[tk] = None
    if out["plate"]:
        out["plate"] = re.sub(r"[^A-Z0-9]", "", str(out["plate"]).upper())[:12]
    for sk in ("vtype", "access", "event", "severity", "camera", "person"):
        if out[sk]:
            out[sk] = str(out[sk])[:40]
    return out


# ════════════════════════════════════════════════════════════════════
# 2) LLM PARSER — Claude turns Hinglish/English into filter JSON
# ════════════════════════════════════════════════════════════════════

_PARSER_PROMPT = """You convert CCTV-search questions into a JSON filter.
Site: an Indian gated society / factory monitored by DEFENDER OCTA.
Current local datetime: {now}  (weekday: {weekday})

Return ONLY a JSON object, no markdown, no explanation, with EXACTLY these keys:
sources, time_from, time_to, plate, vtype, access, event, camera, severity, person, limit

Rules:
- "sources": pick from ["vehicles","incidents","visitors"].
    * cars/bikes/plates/gate entries -> "vehicles"
    * alerts/threats/faces/watchlist/problems -> "incidents"
    * guests/deliveries/maids/people visiting a flat -> "visitors"
    * a person question that could be either -> ["visitors","incidents"]
- Times are strings "YYYY-MM-DD HH:MM:SS" in LOCAL time, or null.
    * "last night" / "kal raat" -> yesterday 20:00 to today 06:00
    * "yesterday" / "kal"       -> yesterday 00:00 to 23:59:59
    * "this morning" / "aaj subah" -> today 05:00 to 12:00
    * "today" / "aaj"           -> today 00:00 to now
    * "after 10 pm" with no day -> assume the most recent such window
    * no time mentioned         -> both null (means: all time, newest first)
- "plate": uppercase alphanumerics only; partial plates are fine ("PB10").
- "vtype": car | bike | truck | null. ("scooter","two wheeler","activa"->bike)
- "access": UNKNOWN | BLACKLIST | RESIDENT | APPROVED | null.
    ("unregistered","stranger","anjaan","suspicious vehicle" -> UNKNOWN;
     "blacklisted","banned" -> BLACKLIST)
- "event": ENTRY | EXIT | null. ("aaya","came","entered"->ENTRY; "gaya","left"->EXIT)
- "camera": short substring like "gate","main gate","block c", else null.
- "severity": CRITICAL|HIGH|MEDIUM|LOW or null (incidents only).
- "person": a name substring for visitor search, else null.
- "limit": 50 unless the user asks for more/less.

Examples:
Q: "show all unknown vehicles last night"
{{"sources":["vehicles"],"time_from":"{yday} 20:00:00","time_to":"{today} 06:00:00","plate":null,"vtype":null,"access":"UNKNOWN","event":null,"camera":null,"severity":null,"person":null,"limit":50}}

Q: "kal raat 2 baje ke baad kaun aaya tha?"
{{"sources":["vehicles","visitors"],"time_from":"{today} 02:00:00","time_to":"{today} 06:00:00","plate":null,"vtype":null,"access":null,"event":"ENTRY","camera":null,"severity":null,"person":null,"limit":50}}

Q: "PB10 wali gaadi kab aayi thi last time"
{{"sources":["vehicles"],"time_from":null,"time_to":null,"plate":"PB10","vtype":null,"access":null,"event":"ENTRY","camera":null,"severity":null,"person":null,"limit":10}}

Q: "any high severity alerts this week?"
{{"sources":["incidents"],"time_from":"{week_ago} 00:00:00","time_to":null,"plate":null,"vtype":null,"access":null,"event":null,"camera":null,"severity":"HIGH","person":null,"limit":50}}

User question: {q}"""


def _llm_parse(q: str):
    if not (_API_KEY and _REQUESTS_OK):
        return None
    now = datetime.now()
    prompt = _PARSER_PROMPT.format(
        now=now.strftime("%Y-%m-%d %H:%M:%S"),
        weekday=now.strftime("%A"),
        today=now.strftime("%Y-%m-%d"),
        yday=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
        week_ago=(now - timedelta(days=7)).strftime("%Y-%m-%d"),
        q=q,
    )
    try:
        r = _rq.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=8,
        )
        r.raise_for_status()
        txt = "".join(b.get("text", "")
                      for b in r.json().get("content", [])
                      if b.get("type") == "text")
        txt = re.sub(r"```(json)?", "", txt).strip()
        return _sanitize(json.loads(txt))
    except Exception as e:  # network, JSON, anything → fall back silently
        print(f"[OCTA-SEARCH] LLM parse failed, using fallback: {e}")
        return None


# ════════════════════════════════════════════════════════════════════
# 3) RULE-BASED FALLBACK PARSER — offline, demo-proof
# ════════════════════════════════════════════════════════════════════

def _rule_parse(q: str) -> dict:
    ql = q.lower()
    f = dict(_EMPTY)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # --- sources -----------------------------------------------------
    src = set()
    if re.search(r"alert|incident|threat|watchlist|face|chehra|problem", ql):
        src.add("incidents")
    if re.search(r"visitor|guest|delivery|maid|mehmaan|milkman|courier|"
                 r"kaam ?wali|servant", ql):
        src.add("visitors")
    if re.search(r"vehicle|car|gaadi|gadi|bike|scooter|truck|plate|number|"
                 r"activa|swift", ql):
        src.add("vehicles")
    if re.search(r"\bkaun\b|who came|kon aaya", ql) and not src:
        src.update(("vehicles", "visitors"))
    f["sources"] = list(src) or ["vehicles"]

    # --- time --------------------------------------------------------
    if re.search(r"last night|kal raat|kal rat", ql):
        f["time_from"], f["time_to"] = f"{yday} 20:00:00", f"{today} 06:00:00"
    elif re.search(r"\byesterday\b|\bkal\b", ql):
        f["time_from"], f["time_to"] = f"{yday} 00:00:00", f"{yday} 23:59:59"
    elif re.search(r"this morning|aaj subah|subah", ql):
        f["time_from"], f["time_to"] = f"{today} 05:00:00", f"{today} 12:00:00"
    elif re.search(r"\btoday\b|\baaj\b", ql):
        f["time_from"] = f"{today} 00:00:00"
    elif re.search(r"this week|is hafte", ql):
        f["time_from"] = (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    m = re.search(r"after (\d{1,2})\s*(am|pm|baje)?", ql)
    if m:
        h = int(m.group(1)) % 12
        if (m.group(2) or "") == "pm" or (m.group(2) == "baje" and h < 6):
            h += 12
        base = f["time_from"][:10] if f["time_from"] else today
        f["time_from"] = f"{base} {h:02d}:00:00"

    # --- attributes ---------------------------------------------------
    m = re.search(r"\b([A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,3}\s?\d{0,4})\b", q.upper())
    if m and len(re.sub(r"[^A-Z0-9]", "", m.group(1))) >= 4:
        f["plate"] = re.sub(r"[^A-Z0-9]", "", m.group(1))
    if re.search(r"bike|scooter|two.?wheeler|activa", ql):
        f["vtype"] = "bike"
    elif re.search(r"truck|tempo|lorry", ql):
        f["vtype"] = "truck"
    elif re.search(r"\bcar\b|gaadi|swift|sedan", ql):
        f["vtype"] = "car"
    if re.search(r"unknown|unregister|stranger|anjaan|suspicious", ql):
        f["access"] = "UNKNOWN"
    elif re.search(r"blacklist|banned", ql):
        f["access"] = "BLACKLIST"
    if re.search(r"enter|entered|came|aaya|aayi|\bin\b", ql):
        f["event"] = "ENTRY"
    elif re.search(r"exit|left|gaya|gayi|nikla", ql):
        f["event"] = "EXIT"
    m = re.search(r"(main gate|back gate|gate ?\d|block ?[a-z])", ql)
    if m:
        f["camera"] = m.group(1)
    for sev in ("critical", "high", "medium", "low"):
        if sev in ql:
            f["severity"] = sev.upper()
            if "incidents" not in f["sources"]:
                f["sources"].append("incidents")
            break
    return f


# ════════════════════════════════════════════════════════════════════
# 4) SQL EXECUTION — parameterised, read-only
# ════════════════════════════════════════════════════════════════════

def _norm_ts(col: str) -> str:
    return f"REPLACE({col},'T',' ')"


def _search_vehicles(cur, f):
    sql = ("SELECT id, plate, vtype, state, event, confidence, image, "
           f"access, camera, {_norm_ts('timestamp')} AS ts "
           "FROM vehicle_events WHERE 1=1")
    args = []
    if f["time_from"]:
        sql += f" AND {_norm_ts('timestamp')} >= ?"; args.append(f["time_from"])
    if f["time_to"]:
        sql += f" AND {_norm_ts('timestamp')} <= ?"; args.append(f["time_to"])
    if f["plate"]:
        sql += " AND REPLACE(UPPER(plate),' ','') LIKE ?"
        args.append(f"%{f['plate']}%")
    if f["vtype"]:
        sql += " AND LOWER(vtype) LIKE ?"; args.append(f"%{f['vtype'].lower()}%")
    if f["access"]:
        sql += " AND (UPPER(access) LIKE ? OR UPPER(state) LIKE ?)"
        args += [f"%{f['access']}%"] * 2
    if f["event"]:
        sql += " AND UPPER(event) LIKE ?"; args.append(f"%{f['event']}%")
    if f["camera"]:
        sql += " AND LOWER(camera) LIKE ?"; args.append(f"%{f['camera'].lower()}%")
    sql += " ORDER BY ts DESC LIMIT ?"; args.append(f["limit"])
    return [
        {"kind": "vehicle", "id": r["id"], "plate": r["plate"],
         "vtype": r["vtype"] or "Vehicle", "event": r["event"] or "—",
         "access": r["access"] or r["state"] or "—",
         "camera": r["camera"] or "—", "timestamp": r["ts"],
         "confidence": r["confidence"] or 0,
         "image": f"/vehicle_image/{os.path.basename(r['image'])}"
                  if r["image"] else None}
        for r in cur.execute(sql, args)
    ]


def _search_incidents(cur, f):
    sql = ("SELECT COALESCE(incident_id,'GG-'||id) AS iid, title, severity, "
           f"status, camera_name, description, {_norm_ts('created_at')} AS ts "
           "FROM incidents WHERE 1=1")
    args = []
    if f["time_from"]:
        sql += f" AND {_norm_ts('created_at')} >= ?"; args.append(f["time_from"])
    if f["time_to"]:
        sql += f" AND {_norm_ts('created_at')} <= ?"; args.append(f["time_to"])
    if f["severity"]:
        sql += " AND UPPER(severity) = ?"; args.append(f["severity"].upper())
    if f["camera"]:
        sql += " AND LOWER(camera_name) LIKE ?"
        args.append(f"%{f['camera'].lower()}%")
    if f["plate"]:
        sql += " AND (UPPER(title) LIKE ? OR UPPER(description) LIKE ?)"
        args += [f"%{f['plate']}%"] * 2
    if f["person"]:
        sql += " AND (title LIKE ? OR description LIKE ?)"
        args += [f"%{f['person']}%"] * 2
    sql += " ORDER BY ts DESC LIMIT ?"; args.append(f["limit"])
    return [
        {"kind": "incident", "id": r["iid"], "title": r["title"],
         "severity": r["severity"], "status": r["status"],
         "camera": r["camera_name"] or "—",
         "description": (r["description"] or "")[:200],
         "timestamp": r["ts"]}
        for r in cur.execute(sql, args)
    ]


def _search_visitors(cur, f):
    # visitors table (actual schema): id, name, flat, phone, purpose,
    # in_time, out_time
    try:
        sql = ("SELECT id, name, flat, purpose, "
               f"{_norm_ts('in_time')} AS tin, "
               f"{_norm_ts('out_time')} AS tout "
               "FROM visitors WHERE 1=1")
        args = []
        if f["time_from"]:
            sql += f" AND {_norm_ts('in_time')} >= ?"
            args.append(f["time_from"])
        if f["time_to"]:
            sql += f" AND {_norm_ts('in_time')} <= ?"
            args.append(f["time_to"])
        if f["person"]:
            sql += " AND name LIKE ?"; args.append(f"%{f['person']}%")
        sql += " ORDER BY tin DESC LIMIT ?"; args.append(f["limit"])
        return [
            {"kind": "visitor", "id": r["id"], "name": r["name"],
             "flat": r["flat"], "purpose": r["purpose"] or "—",
             "timestamp": r["tin"], "exit_time": r["tout"]}
            for r in cur.execute(sql, args)
        ]
    except sqlite3.Error as e:
        print(f"[OCTA-SEARCH] visitors table mismatch: {e}")
        return []


def _last_movement(cur):
    """For confident negatives: when was the site last active?"""
    try:
        r = cur.execute(
            f"SELECT plate, camera, {_norm_ts('timestamp')} AS ts "
            "FROM vehicle_events ORDER BY ts DESC LIMIT 1").fetchone()
        if r:
            return f"Last movement: {r['plate']} at {r['camera']} — {r['ts'][:16]}"
    except sqlite3.Error:
        pass
    return None


# ════════════════════════════════════════════════════════════════════
# 5) ANSWER LINE — turn counts into a human sentence
# ════════════════════════════════════════════════════════════════════

def _answer_line(f, n_veh, n_inc, n_vis, last_move):
    when = ""
    if f["time_from"] and f["time_to"]:
        when = f" between {f['time_from'][5:16]} and {f['time_to'][5:16]}"
    elif f["time_from"]:
        when = f" since {f['time_from'][5:16]}"
    total = n_veh + n_inc + n_vis
    if total == 0:
        base = "No matching activity found" + when + "."
        return base + (f" {last_move}." if last_move else "")
    parts = []
    if n_veh:
        d = []
        if f["access"]:
            d.append(f["access"].lower())
        if f["vtype"]:
            d.append(f["vtype"])
        label = " ".join(d + ["vehicle event" + ("s" if n_veh != 1 else "")])
        parts.append(f"{n_veh} {label}")
    if n_inc:
        parts.append(f"{n_inc} incident{'s' if n_inc != 1 else ''}")
    if n_vis:
        parts.append(f"{n_vis} visitor{'s' if n_vis != 1 else ''}")
    return "Found " + ", ".join(parts) + when + "."


# ════════════════════════════════════════════════════════════════════
# 6) ROUTES
# ════════════════════════════════════════════════════════════════════

@search_bp.route("/api/search/ping")
def search_ping():
    return jsonify({"ok": True, "llm": bool(_API_KEY), "model": _MODEL})


@search_bp.route("/api/search", methods=["POST"])
def octa_search():
    data = request.get_json(silent=True) or {}
    q = (data.get("query") or "").strip()
    if not q:
        return jsonify({"success": False, "message": "query required"}), 400
    if len(q) > 300:
        q = q[:300]

    filters = _llm_parse(q) or _sanitize(_rule_parse(q))
    parser = "llm" if (_API_KEY and filters is not None
                       and filters is not _EMPTY) else "rules"
    # (parser label is approximate; the important thing is the result)

    vehicles, incidents, visitors = [], [], []
    try:
        con = sqlite3.connect(_DB_PATH)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        if "vehicles" in filters["sources"]:
            vehicles = _search_vehicles(cur, filters)
        if "incidents" in filters["sources"]:
            incidents = _search_incidents(cur, filters)
        if "visitors" in filters["sources"]:
            visitors = _search_visitors(cur, filters)
        last_move = None
        if not (vehicles or incidents or visitors):
            last_move = _last_movement(cur)
        con.close()
    except sqlite3.Error as e:
        return jsonify({"success": False, "message": f"DB error: {e}"}), 500

    return jsonify({
        "success": True,
        "query": q,
        "parser": parser,
        "filters": filters,          # shown in UI dev-mode; great for debugging
        "answer": _answer_line(filters, len(vehicles), len(incidents),
                               len(visitors), last_move),
        "results": {
            "vehicles": vehicles,
            "incidents": incidents,
            "visitors": visitors,
        },
        "count": len(vehicles) + len(incidents) + len(visitors),
    })
