r"""
GuardianGrid — Defender Octa smoke test
========================================
One command, ~10 seconds, answers: "is the whole stack healthy right now?"

    cd C:\Users\akash\Desktop\GuardianGrid-SecurityDesk
    python smoke_test.py                     # test http://localhost:5000
    python smoke_test.py --base http://192.168.31.145:5000   # over LAN/Tailscale

What it does:
  1. Reads admin credentials from site_config.json (no secrets in this file)
  2. Verifies the JWT guard rejects unauthenticated requests (security check)
  3. Logs in, then exercises every API this week's features depend on,
     validating response SHAPES (keys/types), not just status codes
  4. Checks the filesystem: today's recordings, reports, PWA files
  5. Prints a PASS / WARN / FAIL checklist and exits non-zero on any FAIL

Stdlib only — no pip installs needed. Run it before every demo.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = datetime.now().strftime("%Y-%m-%d")

# ANSI colors (Windows 10+ terminals support these)
G, R, Y, C, N = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m"

results = []  # (status, name, detail)

def record(status, name, detail=""):
    results.append((status, name, detail))
    tag = {"PASS": f"{G}[PASS]{N}", "FAIL": f"{R}[FAIL]{N}", "WARN": f"{Y}[WARN]{N}"}[status]
    print(f"  {tag} {name}" + (f"  {C}·{N} {detail}" if detail else ""))

# Cloudflare's Bot Fight Mode blocks default python user-agents with 403s,
# so identify as a normal client when testing through the public domain.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) DefenderOcta-SmokeTest/1.0")

def http(base, path, token=None, timeout=8):
    req = urllib.request.Request(base + path, headers={"User-Agent": UA})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return None, str(e)

def http_post(base, path, payload, timeout=8):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return None, str(e)


def main():
    ap = argparse.ArgumentParser(description="Defender Octa smoke test")
    ap.add_argument("--base", default="http://localhost:5000")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"\n{C}Defender Octa smoke test · {base} · {TODAY}{N}\n")

    # ── 0. config & credentials ──────────────────────────────────
    print(f"{C}— Configuration —{N}")
    cfg_path = os.path.join(BASE_DIR, "site_config.json")
    cfg, cams = {}, []
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            cams = [c["name"] for c in cfg.get("rtsp_cameras", []) if c.get("name")]
            record("PASS", "site_config.json readable", f"{len(cams)} RTSP cameras configured")
        except json.JSONDecodeError as e:
            record("FAIL", "site_config.json readable", f"invalid JSON: {e}")
    else:
        record("FAIL", "site_config.json readable", "file not found — run from the backend folder")

    user = (cfg.get("admin") or {}).get("username", "admin")
    pw = (cfg.get("admin") or {}).get("password", "")
    if pw == "admin123":
        record("WARN", "admin password", "still the default admin123 — change before client deployment")

    # ── 1. server up + auth guard armed ──────────────────────────
    print(f"\n{C}— Server & security —{N}")
    st, _ = http(base, "/api/forecast")  # no token on purpose
    if st is None:
        record("FAIL", "server reachable", "connection failed — is Flask running?")
        finish(); return
    if st == 401:
        record("PASS", "JWT guard armed", "unauthenticated /api/forecast correctly rejected (401)")
    else:
        record("FAIL", "JWT guard armed", f"expected 401 without token, got {st}")

    st, d = http_post(base, "/api/auth/login", {"username": user, "password": pw})
    token = d.get("token") if isinstance(d, dict) else None
    if st == 200 and token:
        record("PASS", "login /api/auth/login", f"user '{user}', token issued")
    else:
        record("FAIL", "login /api/auth/login", f"status {st} — check credentials in site_config.json")
        finish(); return

    # ── 1b. viewer account (if configured): must be read-only ───
    v = (cfg.get("viewer") or {})
    if v.get("username") and v.get("password"):
        st_v, d_v = http_post(base, "/api/auth/login",
                              {"username": v["username"], "password": v["password"]})
        vtok = d_v.get("token") if isinstance(d_v, dict) else None
        if st_v == 200 and vtok:
            record("PASS", "viewer login", f"user '{v['username']}'")
            # a write MUST be refused with 403
            import urllib.request as _u
            req = _u.Request(base + "/gate/action",
                             data=json.dumps({"plate": "TEST", "action": "ENTRY"}).encode(),
                             headers={"Content-Type": "application/json",
                                      "Authorization": f"Bearer {vtok}",
                                      "User-Agent": UA})
            try:
                with _u.urlopen(req, timeout=8) as r:
                    record("FAIL", "viewer is read-only", f"write ACCEPTED (status {r.status})!")
            except Exception as e:
                code = getattr(e, "code", None)
                if code == 403:
                    record("PASS", "viewer is read-only", "write correctly refused (403)")
                else:
                    record("WARN", "viewer is read-only", f"write refused with {code} (expected 403)")
            # residents must be hidden even for GET
            st_r2, _ = http(base, "/residents", vtok)
            if st_r2 == 403:
                record("PASS", "viewer cannot read residents", "403 as designed")
            else:
                record("FAIL", "viewer cannot read residents", f"got {st_r2}")
        else:
            record("FAIL", "viewer login", f"status {st_v} — viewer block set but login failed")
    else:
        record("WARN", "viewer account", "not configured — add a \"viewer\" block before sharing the QR card")

    # ── 2. core data endpoints (shape-validated) ─────────────────
    print(f"\n{C}— Core data —{N}")
    def check_json(name, path, validate, warn_empty=None):
        st_, d_ = http(base, path, token)
        if st_ != 200:
            record("FAIL", name, f"status {st_}")
            return None
        try:
            msg = validate(d_)
            record("PASS", name, msg or "")
        except AssertionError as e:
            record("FAIL", name, str(e))
        except Exception as e:
            record("FAIL", name, f"shape error: {e}")
        return d_

    check_json("/vehicle_stats", "/vehicle_stats",
               lambda d: (f"entries={d.get('entries')} exits={d.get('exits')}",
                          [None for _ in ()])[0] if "entries" in d else (_ for _ in ()).throw(AssertionError("missing 'entries'")))

    check_json("/hourly_stats", "/hourly_stats", lambda d: (
        f"{len(d)} hour bucket(s) today" if isinstance(d, list) else
        (_ for _ in ()).throw(AssertionError("expected a list"))))

    heat = check_json("/camera_heat", "/camera_heat", lambda d: (
        f"{len({r['camera'] for r in d})} camera row(s)" if isinstance(d, list) else
        (_ for _ in ()).throw(AssertionError("expected a list"))))
    if isinstance(heat, list) and cams:
        present = {r.get("camera") for r in heat}
        missing = [c for c in cams if c not in present]
        if missing:
            record("FAIL", "heatmap includes all configured cameras", f"missing: {', '.join(missing)} — deploy fixed db.py?")
        else:
            record("PASS", "heatmap includes all configured cameras", ", ".join(sorted(present)))

    day = check_json(f"/api/day/{TODAY}", f"/api/day/{TODAY}", lambda d: (
        f"{len(d.get('events', []))} event(s), {len(d.get('incidents', []))} incident(s)"
        if "events" in d else (_ for _ in ()).throw(AssertionError("missing 'events'"))))

    # ── 3. AI intelligence endpoints ─────────────────────────────
    print(f"\n{C}— AI intelligence —{N}")
    check_json("/api/forecast", "/api/forecast", lambda d: (
        f"7-day forecast, confidence={d.get('confidence')}, {d.get('days_of_data')} day(s) of data"
        if isinstance(d.get("days"), list) and len(d["days"]) == 7
        else (_ for _ in ()).throw(AssertionError("expected days[7]"))))

    check_json("/api/score/live", "/api/score/live", lambda d: (
        f"score={d.get('score')}/100 ({d.get('label')})"
        if isinstance(d.get("score"), int) and 0 <= d["score"] <= 100
        else (_ for _ in ()).throw(AssertionError(f"bad score: {d.get('score')} — {d.get('error','')}"))))

    check_json("/api/score/alerts (watchdog)", "/api/score/alerts", lambda d: (
        f"{len(d.get('alerts', []))} alarm(s), {len(d.get('trend', []))} trend point(s)"
        if "thresholds" in d else (_ for _ in ()).throw(AssertionError("missing 'thresholds' — watchdog routes not deployed?"))))

    st_r, reels = http(base, "/api/replays", token)
    if st_r == 200 and isinstance(reels, list):
        if reels:
            record("PASS", "/api/replays (highlight reels)", f"{len(reels)} reel(s), latest {reels[0].get('date')}")
        else:
            record("WARN", "/api/replays (highlight reels)", "route OK but no reels yet — run: python smart_replay.py --date " + TODAY)
    else:
        record("FAIL", "/api/replays (highlight reels)", f"status {st_r} — reel routes not deployed?")

    # replay mapping — try a real event from today, else just check the route answers
    probe_cam = cams[0] if cams else "Main Gate"
    probe_ts = f"{TODAY}T12:00:00"
    from_event = None
    if isinstance(day, dict):
        for e in day.get("events", []):
            if e.get("camera") and e.get("timestamp"):
                probe_cam = e["camera"]
                probe_ts = str(e["timestamp"]).replace(" ", "T")[:19]
                from_event = e
                break
    st_m, d_m = http(base, f"/api/replay/for_event?camera={urllib.request.quote(probe_cam)}&timestamp={probe_ts}", token)
    if st_m == 200 and isinstance(d_m, dict) and d_m.get("found"):
        record("PASS", "/api/replay/for_event (segment mapping)",
               f"{probe_cam} @ {probe_ts[11:]} → {d_m.get('file')} +{d_m.get('offset_seconds')}s")
    elif st_m == 404:
        detail = "route OK; no footage covers the probed moment"
        if from_event:
            detail += f" ({probe_cam} @ {probe_ts[11:]}) — is that camera being recorded?"
        record("WARN", "/api/replay/for_event (segment mapping)", detail)
    else:
        record("FAIL", "/api/replay/for_event (segment mapping)", f"status {st_m} — replay routes not deployed?")

    # ── 4. filesystem ────────────────────────────────────────────
    print(f"\n{C}— Filesystem —{N}")
    rec_root = os.path.join(BASE_DIR, "recordings")
    seg_count, rec_cams = 0, []
    if os.path.isdir(rec_root):
        for cam in os.listdir(rec_root):
            day_dir = os.path.join(rec_root, cam, TODAY)
            if os.path.isdir(day_dir):
                n = len([f for f in os.listdir(day_dir) if f.endswith(".mp4")])
                if n:
                    rec_cams.append(f"{cam}({n})")
                    seg_count += n
    if seg_count:
        record("PASS", "recordings for today", ", ".join(rec_cams))
    else:
        record("WARN", "recordings for today", "no segments yet — recorder not running or day just started")

    brief = os.path.join(BASE_DIR, "reports", f"brief_{TODAY}.json")
    if os.path.isfile(brief):
        try:
            with open(brief, encoding="utf-8") as f:
                b = json.load(f)
            record("PASS", "today's brief exists", f"score {b.get('score')} ({b.get('label', '')})")
        except Exception:
            record("WARN", "today's brief exists", "file present but unreadable")
    else:
        record("WARN", "today's brief exists", "not generated yet — normal before 7 AM job / manual run")

    for label, rel in [("PWA manifest deployed", os.path.join("frontend", "manifest.webmanifest")),
                       ("PWA service worker deployed", os.path.join("frontend", "sw.js")),
                       ("PWA icons deployed", os.path.join("frontend", "icons", "icon-192.png"))]:
        if os.path.isfile(os.path.join(BASE_DIR, rel)):
            record("PASS", label)
        else:
            record("WARN", label, f"{rel} missing — rebuild frontend + xcopy")

    finish()


def finish():
    p = sum(1 for s, *_ in results if s == "PASS")
    w = sum(1 for s, *_ in results if s == "WARN")
    f = sum(1 for s, *_ in results if s == "FAIL")
    print(f"\n{C}{'='*56}{N}")
    verdict = (f"{G}ALL SYSTEMS GO{N}" if f == 0 and w == 0 else
               f"{Y}GO — with {w} warning(s){N}" if f == 0 else
               f"{R}NOT DEMO-READY — {f} failure(s){N}")
    print(f"  {p} passed · {w} warnings · {f} failed   →   {verdict}")
    print(f"{C}{'='*56}{N}\n")
    sys.exit(1 if f else 0)


if __name__ == "__main__":
    os.system("")  # enables ANSI colors in Windows terminals
    main()
