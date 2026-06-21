from flask import request, jsonify

from backend.incidents.incident_service import (
    create_new_incident,
    list_incidents,
    update_existing_incident
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