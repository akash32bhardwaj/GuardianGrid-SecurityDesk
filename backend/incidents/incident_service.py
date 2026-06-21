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
        operator=data.get("operator")
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