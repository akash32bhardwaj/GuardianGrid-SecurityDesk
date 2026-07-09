"""
correction_routes.py — Manual Plate Correction API v2
-------------------------------------------------------
On correction, updates ALL shared state:
  - vehicle_log        → 🕒 AI Timeline
  - vehicle_db         → 🔍 Vehicle Search
  - latest_detection   → polling loop → 🛡️ Security Gate
  - latest_resident    → 🗑️ Vehicle Intelligence sidebar
  - activity_feed      → 📜 Live Activity + 🕒 AI Timeline
  - notification_feed  → 🔔 Notification Center
  + fires WhatsApp alert with corrected plate
"""

from flask import Blueprint, request, jsonify
from datetime import datetime
from pathlib import Path
import threading

correction_bp = Blueprint("correction", __name__)


@correction_bp.route("/api/correct_plate", methods=["POST"])
def correct_plate():
    data = request.get_json() or {}

    original  = data.get("original_plate",  "").strip().upper().replace(" ", "")
    corrected = data.get("corrected_plate", "").strip().upper().replace(" ", "")
    event     = data.get("event", "ENTRY")
    snapshot  = data.get("snapshot_path", "")

    if not corrected:
        return jsonify({"error": "corrected_plate is required"}), 400

    from api_server import (
        vehicle_log, vehicle_db, lock,
        latest_detection, latest_resident,
        activity_feed, notification_feed
    )
    from resident_db import db as resident_db

    now = datetime.now()

    # ── Format plate nicely ───────────────────────────────────
    def fmt(p):
        if len(p) >= 8:
            return f"{p[:2]} {p[2:4]} {p[4:6]} {p[6:]}"
        return p

    formatted = fmt(corrected)

    # ── Resident lookup ───────────────────────────────────────
    resident_info = resident_db.lookup(corrected)
    if resident_info:
        resident_data = {
            "found":         True,
            "status":        resident_info.status,
            "resident_name": resident_info.resident_name,
            "flat_number":   resident_info.flat_number,
            "block":         resident_info.block,
            "phone":         resident_info.phone,
            "vehicle_model": resident_info.vehicle_model,
            "vehicle_color": resident_info.vehicle_color,
            "notes":         resident_info.notes,
            "display_name":  resident_info.display_name,
        }
    else:
        resident_data = {"found": False, "status": "UNKNOWN"}

    with lock:
        # ── 1. Update vehicle_log (→ Timeline, Vehicle Log) ──
        updated = False
        for record in vehicle_log:
            orig = record.get("plate", "").replace(" ", "").upper()
            if orig == original or orig == corrected:
                record["plate"]          = formatted
                record["corrected"]      = True
                record["original_plate"] = original
                record["corrected_at"]   = now.isoformat()
                if resident_info:
                    record["resident_name"] = resident_info.resident_name
                    record["flat_number"]   = resident_info.flat_number
                updated = True
                break

        if not updated:
            new_record = {
                "vehicle_id":     f"VH{len(vehicle_db)+1:04d}",
                "plate":          formatted,
                "original_plate": original,
                "corrected":      True,
                "corrected_at":   now.isoformat(),
                "event":          event,
                "time":           now.strftime("%H:%M:%S"),
                "timestamp":      now.isoformat(),
                "image":          Path(snapshot).name if snapshot else "",
                "resident_name":  resident_info.resident_name if resident_info else "",
                "flat_number":    resident_info.flat_number   if resident_info else "",
            }
            vehicle_log.appendleft(new_record)
            vehicle_db[corrected] = new_record

        # ── 2. Update latest_detection (→ Security Gate polling) ──
        latest_detection.update({
            "plate":     formatted,
            "timestamp": now.isoformat(),
            "event":     event,
            "alert":     f"CORRECTED: {formatted}",
            "state":     resident_info.state_name if hasattr(resident_info, "state_name") and resident_info else "",
            "confidence": latest_detection.get("confidence", 0),
        })

        # ── 3. Update latest_resident (→ Vehicle Intelligence sidebar) ──
        if resident_info:
            latest_resident.update({
                "plate":  formatted,
                "name":   resident_info.resident_name,
                "flat":   resident_info.flat_number,
                "phone":  resident_info.phone,
                "status": resident_info.status,
            })
        else:
            latest_resident.update({
                "plate":  formatted,
                "name":   "Unknown Vehicle",
                "flat":   "-",
                "phone":  "-",
                "status": "UNKNOWN",
            })

        # ── 4. Push to activity_feed (→ Live Activity + Timeline) ──
        activity_feed.appendleft({
            "time":  now.isoformat(),
            "event": f"✏️ PLATE CORRECTED: {original} → {corrected}"
                     + (f" | {resident_info.resident_name} · Flat {resident_info.flat_number}" if resident_info else ""),
            "type":  "correction"
        })

        # ── 5. Push to notification_feed (→ Notification Center) ──
        notification_feed.appendleft({
            "time":     now.isoformat(),
            "title":    "Plate Correction Made",
            "message":  f"{original} corrected to {formatted}"
                        + (f" — {resident_info.resident_name}, Flat {resident_info.flat_number}" if resident_info else ""),
            "severity": "LOW",
        })

    # ── 6. Fire WhatsApp with corrected plate ─────────────────
    try:
        from whatsapp_alerts import send_vehicle_alert
        WHATSAPP_AVAILABLE = True
    except ImportError:
        WHATSAPP_AVAILABLE = False

    if WHATSAPP_AVAILABLE:
        threading.Thread(
            target=send_vehicle_alert,
            kwargs={
                "plate":         formatted,
                "event":         event,
                "resident_info": resident_data,
                "snapshot_path": snapshot,
            },
            daemon=True
        ).start()

    print(f"[CORRECTION] {original} → {formatted} | "
          f"{resident_info.resident_name if resident_info else 'Unknown'}")

    return jsonify({
        "success":        True,
        "original_plate": original,
        "corrected_plate": formatted,
        "event":          event,
        "resident":       resident_data,
        "timestamp":      now.isoformat(),
    })


@correction_bp.route("/api/lookup_plate", methods=["GET"])
def lookup_plate():
    plate = request.args.get("plate", "").strip().upper().replace(" ", "")
    if not plate:
        return jsonify({"found": False})

    from resident_db import db as resident_db
    resident = resident_db.lookup(plate)

    if resident:
        return jsonify({
            "found":         True,
            "status":        resident.status,
            "resident_name": resident.resident_name,
            "flat_number":   resident.flat_number,
            "block":         resident.block,
            "phone":         resident.phone,
            "vehicle_model": resident.vehicle_model,
            "vehicle_color": resident.vehicle_color,
            "display_name":  resident.display_name,
            "notes":         resident.notes,
        })

    return jsonify({"found": False, "status": "UNKNOWN"})
