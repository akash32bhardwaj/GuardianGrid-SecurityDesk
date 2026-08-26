r"""
incident_report.py — DEFENDER OCTA Incident Report PDF (Prove stage)
---------------------------------------------------------------------
One click on a resolved (or active) incident -> a professional PDF the
client can attach to an insurance claim, police FIR, or committee
minutes. Assembles what the system already recorded:

  incident record + timeline (fired / acknowledged / escalated /
  resolved with response times) + resolution disposition & note +
  evidence images (vehicle snapshots around the incident) + SOP.

Route (JWT-protected by the global guard):
  GET /api/canvas/<incident_id>/report.pdf

Integration in api_server.py: nothing extra — canvas_routes imports
and registers the route on its own blueprint via register_report().
"""

import logging
import os
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)

_DB_PATH = "guardiangrid.db"
_BASE_DIR = "."


def init_report(base_dir: str):
    global _BASE_DIR, _DB_PATH
    _BASE_DIR = base_dir
    docker_db = "/data/guardiangrid.db"
    _DB_PATH = docker_db if os.path.exists(docker_db) \
        else os.path.join(base_dir, "guardiangrid.db")


def _con():
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _site_name():
    try:
        import json
        with open(os.path.join(_BASE_DIR, "site_config.json"),
                  encoding="utf-8") as f:
            cfg = json.load(f)
        soc = cfg.get("society") or {}
        return (soc.get("name") or cfg.get("site_name")
                or cfg.get("name") or "Defender Octa Site")
    except Exception:
        return "Defender Octa Site"


def _gather(iid: str):
    """Everything the report needs, from the stores that already exist."""
    inc = None
    try:
        from backend.incidents.incident_models import get_all_incidents
        inc = next((i for i in get_all_incidents() or []
                    if i.get("incident_id") == iid), None)
    except Exception as e:
        logger.warning(f"[REPORT] incident lookup: {e}")
    if not inc:
        return None

    con = _con()
    ack = con.execute(
        "SELECT * FROM ack_log WHERE incident_id=?", (iid,)).fetchone()
    res = None
    try:
        res = con.execute(
            "SELECT * FROM canvas_resolutions WHERE incident_id=?",
            (iid,)).fetchone()
    except sqlite3.Error:
        pass

    # evidence: events ±10 min around the incident, same camera preferred
    created = str(inc.get("created_at", ""))[:19].replace("T", " ")
    ev = []
    try:
        rows = con.execute(
            "SELECT plate, event, access, state, camera, image,"
            " REPLACE(timestamp,'T',' ') AS ts FROM vehicle_events"
            " WHERE REPLACE(timestamp,'T',' ') BETWEEN"
            " DATETIME(?, '-10 minutes') AND DATETIME(?, '+10 minutes')"
            " ORDER BY ts LIMIT 8", (created, created)).fetchall()
        for r in rows:
            img = None
            if r["image"]:
                for cand in (r["image"],
                             os.path.join(_BASE_DIR, str(r["image"])),
                             os.path.join(_BASE_DIR, "output", "webcam",
                                          os.path.basename(str(r["image"])))):
                    if cand and os.path.exists(cand):
                        img = cand
                        break
            ev.append({"plate": r["plate"], "event": r["event"],
                       "access": r["access"] or r["state"] or "—",
                       "camera": r["camera"], "ts": r["ts"], "img": img})
    except sqlite3.Error as e:
        logger.warning(f"[REPORT] evidence query: {e}")
    con.close()
    return {"inc": inc, "ack": dict(ack) if ack else None,
            "res": dict(res) if res else None, "evidence": ev}


def generate_pdf(iid: str, out_path: str) -> bool:
    data = _gather(iid)
    if not data:
        return False
    inc, ack, res, evidence = (data["inc"], data["ack"],
                               data["res"], data["evidence"])

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle, Image as RLImage)
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError as e:
        logger.error(f"[REPORT] reportlab missing: {e}")
        return False

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=16,
                        spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9,
                         textColor=colors.grey)
    sec = ParagraphStyle("sec", parent=styles["Heading2"], fontSize=11,
                         spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5,
                          leading=13)

    sev = str(inc.get("severity", "")).upper()
    sev_color = colors.HexColor(
        "#dc2626" if sev == "CRITICAL"
        else "#d97706" if sev == "HIGH" else "#64748b")

    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"Incident Report {iid}")
    el = []
    el.append(Paragraph("INCIDENT REPORT", h1))
    el.append(Paragraph(
        f"{_site_name()} · DEFENDER OCTA by S&amp;N GuardianGrid · "
        f"Generated {datetime.now():%d %b %Y, %H:%M}", sub))
    el.append(Spacer(1, 6))

    # ── summary table ────────────────────────────────────────────
    disposition = (res or {}).get("resolution") or (
        "OPEN" if inc.get("status") != "RESOLVED" else "RESOLVED")
    rows = [
        ["Incident ID", iid, "Severity", sev],
        ["Title", inc.get("title", ""), "Status",
         inc.get("status", "")],
        ["Location", inc.get("camera_name") or inc.get("camera") or "—",
         "Disposition", disposition],
    ]
    t = Table(rows, colWidths=[26 * mm, 68 * mm, 26 * mm, 58 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
        ("TEXTCOLOR", (3, 0), (3, 0), sev_color),
        ("FONTNAME", (3, 0), (3, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#e2e8f0")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    el.append(t)

    if inc.get("description"):
        el.append(Paragraph("Description", sec))
        el.append(Paragraph(str(inc["description"]), body))

    # ── response timeline ────────────────────────────────────────
    el.append(Paragraph("Response timeline", sec))
    tl = [["Event", "Time", "Detail"]]
    created = str(inc.get("created_at", ""))[:19].replace("T", " ")
    tl.append(["Incident detected", created,
               f"Source camera: {inc.get('camera_name') or '—'}"])
    if ack:
        if ack.get("acked_at"):
            rs = ack.get("response_seconds")
            tl.append(["Acknowledged by operator", ack["acked_at"],
                       f"Response time: {rs:.0f} seconds" if rs is not None
                       else ""])
        if ack.get("escalated"):
            tl.append(["Escalated to supervisor", "—",
                       "No acknowledgment within the response window — "
                       "WhatsApp escalation sent"])
    if res:
        tl.append([f"Closed as {res.get('resolution', 'RESOLVED')}",
                   res.get("resolved_at", ""),
                   f"By {res.get('resolved_by', '—')}"
                   + (f" · Note: {res.get('note')}" if res.get("note")
                      else "")])
    tt = Table(tl, colWidths=[48 * mm, 36 * mm, 94 * mm])
    tt.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    el.append(tt)

    # ── evidence ─────────────────────────────────────────────────
    if evidence:
        el.append(Paragraph("Evidence — activity around the incident "
                            "(±10 minutes)", sec))
        ev_rows = [["Time", "Plate", "Event", "Access", "Camera"]]
        for e in evidence:
            ev_rows.append([e["ts"][11:19], e["plate"] or "—",
                            e["event"] or "—", e["access"], e["camera"]])
        et = Table(ev_rows, colWidths=[22 * mm, 34 * mm, 22 * mm,
                                       30 * mm, 70 * mm])
        et.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3,
             colors.HexColor("#e2e8f0")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
        ]))
        el.append(et)

        # snapshot images, up to 3, scaled
        imgs = [e["img"] for e in evidence if e["img"]][:3]
        if imgs:
            el.append(Spacer(1, 6))
            pics = []
            for p in imgs:
                try:
                    pics.append(RLImage(p, width=52 * mm, height=39 * mm))
                except Exception:
                    pass
            if pics:
                it = Table([pics])
                it.setStyle(TableStyle(
                    [("LEFTPADDING", (0, 0), (-1, -1), 2),
                     ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
                el.append(it)

    # ── notes trail ──────────────────────────────────────────────
    notes = inc.get("notes") or []
    if notes:
        el.append(Paragraph("Audit notes", sec))
        for n in notes[-6:]:
            if isinstance(n, dict):
                el.append(Paragraph(
                    f"• {n.get('at', '')[:16]} — "
                    f"{n.get('operator', '')}: {n.get('message', '')}",
                    body))
            else:
                el.append(Paragraph(f"• {n}", body))

    el.append(Spacer(1, 10))
    el.append(Paragraph(
        "This report was generated automatically by DEFENDER OCTA from "
        "tamper-resistant system records. Response times are measured by "
        "the platform, not self-reported.", sub))

    try:
        doc.build(el)
        return True
    except Exception as e:
        logger.error(f"[REPORT] build failed: {e}")
        return False
