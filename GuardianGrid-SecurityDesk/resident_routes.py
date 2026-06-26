"""
resident_routes.py
------------------
Flask routes for resident database.
Import and register in api_server.py:

  from resident_routes import resident_bp
  app.register_blueprint(resident_bp)
"""

import os
from flask import Blueprint, request, jsonify, send_file
from werkzeug.utils import secure_filename
from pathlib import Path
from resident_db import db, Resident

resident_bp = Blueprint("residents", __name__)
UPLOAD_DIR  = Path("data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@resident_bp.route("/residents", methods=["GET"])
def get_all_residents():
    """Get all residents."""
    return jsonify({
        "count":     db.count(),
        "residents": db.get_all()
    })


@resident_bp.route("/residents/lookup/<plate>", methods=["GET"])
def lookup_resident(plate):
    """Look up a plate number — returns resident info or unknown."""
    resident = db.lookup(plate)
    if resident:
        return jsonify({
            "found":        True,
            "status":       resident.status,
            "plate_number": resident.plate_number,
            "resident_name":resident.resident_name,
            "flat_number":  resident.flat_number,
            "block":        resident.block,
            "phone":        resident.phone,
            "vehicle_type": resident.vehicle_type,
            "vehicle_model":resident.vehicle_model,
            "vehicle_color":resident.vehicle_color,
            "display_name": resident.display_name,
            "notes":        resident.notes,
        })
    return jsonify({
        "found":   False,
        "status":  "UNKNOWN",
        "message": f"Plate {plate} not registered in system"
    })


@resident_bp.route("/residents/add", methods=["POST"])
def add_resident():
    """Add a single resident manually."""
    data = request.get_json()
    if not data or not data.get("plate_number"):
        return jsonify({"error": "plate_number is required"}), 400
    try:
        resident = Resident(
            plate_number  = data["plate_number"],
            resident_name = data.get("resident_name", "Unknown"),
            flat_number   = data.get("flat_number", ""),
            block         = data.get("block", ""),
            phone         = data.get("phone", ""),
            vehicle_type  = data.get("vehicle_type", "Car"),
            vehicle_model = data.get("vehicle_model", ""),
            vehicle_color = data.get("vehicle_color", ""),
            notes         = data.get("notes", ""),
            status        = data.get("status", "KNOWN"),
        )
        db.add(resident)
        return jsonify({"success": True, "message": f"Added {resident.plate_number}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@resident_bp.route("/residents/remove/<plate>", methods=["DELETE"])
def remove_resident(plate):
    """Remove a resident by plate number."""
    if db.remove(plate):
        return jsonify({"success": True})
    return jsonify({"error": "Plate not found"}), 404


@resident_bp.route("/residents/blacklist/<plate>", methods=["POST"])
def blacklist_plate(plate):
    """Blacklist a vehicle."""
    data   = request.get_json() or {}
    reason = data.get("reason", "")
    db.blacklist(plate, reason)
    return jsonify({"success": True, "message": f"{plate} blacklisted"})


@resident_bp.route("/residents/import", methods=["POST"])
def import_residents():
    """Import residents from uploaded Excel or CSV file."""
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file     = request.files["file"]
    filename = secure_filename(file.filename)
    filepath = UPLOAD_DIR / filename
    file.save(str(filepath))

    ext = filename.lower().rsplit(".", 1)[-1]
    if ext in ("xlsx", "xls"):
        result = db.import_from_excel(str(filepath))
    elif ext == "csv":
        result = db.import_from_csv(str(filepath))
    else:
        return jsonify({"error": "Only .xlsx or .csv files supported"}), 415

    return jsonify(result)


@resident_bp.route("/residents/export", methods=["GET"])
def export_residents():
    """Export all residents to Excel."""
    result = db.export_to_excel("data/residents_export.xlsx")
    if "error" in result:
        return jsonify(result), 500
    return send_file("data/residents_export.xlsx",
                     as_attachment=True,
                     download_name="residents_export.xlsx")


@resident_bp.route("/residents/template", methods=["GET"])
def download_template():
    """Download the Excel import template."""
    template = Path("RESIDENT_TEMPLATE.xlsx")
    if template.exists():
        return send_file(str(template), as_attachment=True,
                         download_name="GuardianGrid_Resident_Template.xlsx")
    return jsonify({"error": "Template not found"}), 404
