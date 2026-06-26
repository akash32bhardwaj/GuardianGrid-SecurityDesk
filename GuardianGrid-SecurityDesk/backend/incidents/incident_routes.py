from flask import request, jsonify
from backend.incidents.incident_service import (
    create_new_incident,
    list_incidents,
    update_existing_incident,
    add_incident_note,  # ← added missing comma
    get_incident_stats
)
def register_incident_routes(app):
    @app.route("/api/incidents", methods=["GET"])
    def get_incidents():
        return jsonify({
            "success": True,
            "incidents": list_incidents()
        })
    @app.route("/api/incidents", methods=["POST"])
    def create_incident():
        data = request.get_json()
        incident = create_new_incident(data)
        return jsonify({
            "success": True,
            "incident": incident
        })
    @app.route("/api/incidents/<incident_id>", methods=["PATCH"])
    def update_incident_route(incident_id):
        data = request.get_json()
        incident = update_existing_incident(
            incident_id,
            data
        )
        if not incident:
            return jsonify({
                "success": False,
                "message": "Incident not found"
            }), 404
        return jsonify({
            "success": True,
            "incident": incident
        })
    @app.route(
        "/api/incidents/<incident_id>/notes",
        methods=["POST"]
    )
    def add_note_route(incident_id):
        data = request.get_json()
        incident = add_incident_note(
            incident_id,
            data.get("operator"),
            data.get("message")
        )
        if not incident:
            return jsonify({
                "success": False,
                "message": "Incident not found"
            }), 404
        return jsonify({
            "success": True,
            "incident": incident
        })
    @app.route(          # ← removed extra leading space
        "/api/incidents/stats",
        methods=["GET"]
    )
    def incident_stats():  # ← removed extra leading space
        return jsonify({
            "success": True,
            "stats": get_incident_stats()
        }) 
