INCIDENTS = []


def create_incident(
    title,
    description,
    severity,
    camera_name,
    operator=None
):
    incident_id = f"INC-{len(INCIDENTS)+1:04d}"

    incident = {
        "incident_id": incident_id,
        "title": title,
        "description": description,
        "severity": severity,
        "camera_name": camera_name,
        "operator": operator,
        "status": "OPEN"
    }

    INCIDENTS.append(incident)

    return incident


def get_all_incidents():
    return INCIDENTS

def update_incident(incident_id, updates):

    for incident in INCIDENTS:

        if incident["incident_id"] == incident_id:

            incident.update(updates)

            return incident

    return None