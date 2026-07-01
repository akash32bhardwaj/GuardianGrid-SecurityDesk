from whatsapp_alerts import send_vehicle_alert

result = send_vehicle_alert(
    plate="DL3CAB5678",
    event="ENTRY",
    resident_info={
        "found": True,
        "status": "BLACKLISTED",
        "resident_name": "Rajesh Kumar",
        "flat_number": "201",
        "block": "C",
        "phone": "99999-88888",
        "notes": "Unpaid dues"
    }
)
print(result)