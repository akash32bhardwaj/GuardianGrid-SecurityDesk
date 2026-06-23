from backend.incidents.incident_models import (
    create_incident,
    get_all_incidents,
    update_incident,
    add_note
)


def create_new_incident(data):

    return create_incident(
        title=data.get("title"),
        description=data.get("description"),
        severity=data.get("severity", "LOW"),
        camera_name=data.get("camera_name", "Unknown Camera"),
        operator=data.get("operator"),
        evidence_image=data.get("evidence_image")
    )


def list_incidents():
    return get_all_incidents()


def update_existing_incident(
    incident_id,
    data
):
    return update_incident(
        incident_id,
        data
    )


def add_incident_note(
    incident_id,
    operator,
    message
):
    return add_note(
        incident_id,
        operator,
        message
    )

def get_incident_stats():

    incidents = get_all_incidents()

    open_count = 0
    in_progress_count = 0
    resolved_count = 0

    for incident in incidents:

        status = incident.get("status")

        if status == "OPEN":
            open_count += 1

        elif status == "IN_PROGRESS":
            in_progress_count += 1

        elif status == "RESOLVED":
            resolved_count += 1

    return {
        "total": len(incidents),
        "open": open_count,
        "in_progress": in_progress_count,
        "resolved": resolved_count
    }
