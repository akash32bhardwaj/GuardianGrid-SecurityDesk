from datetime import datetime

# Temporary in-memory storage
# Later this will move to PostgreSQL
INCIDENTS = []


def create_incident(
    title,
    description,
    severity,
    camera_name,
    operator=None
):

    incident_id = f"INC-{len(INCIDENTS)+1:04d}"

    now = datetime.now().isoformat()

    incident = {
        "incident_id": incident_id,

        "title": title,
        "description": description,

        "severity": severity,
        "camera_name": camera_name,

        "operator": operator,

        "status": "OPEN",

        "created_at": now,
        "updated_at": now,
        "resolved_at": None,

        "notes": []
    }

    INCIDENTS.append(incident)

    return incident


def get_all_incidents():
    return INCIDENTS


def update_incident(
    incident_id,
    updates
):

    for incident in INCIDENTS:

        if incident["incident_id"] == incident_id:

            incident.update(updates)

            incident["updated_at"] = datetime.now().isoformat()

            if updates.get("status") == "RESOLVED":
                incident["resolved_at"] = datetime.now().isoformat()

            return incident

    return None


def add_note(
    incident_id,
    operator,
    message
):

    for incident in INCIDENTS:

        if incident["incident_id"] == incident_id:

            incident["notes"].append({
                "operator": operator,
                "message": message,
                "timestamp": datetime.now().isoformat()
            })

            incident["updated_at"] = datetime.now().isoformat()

            return incident

    return None


def get_incident_by_id(
    incident_id
):

    for incident in INCIDENTS:

        if incident["incident_id"] == incident_id:
            return incident

    return None