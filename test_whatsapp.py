python -c "
from alert_settings import settings
print('notify_blacklisted:', settings.get('notify_blacklisted'))
print('should alert BLACKLISTED:', settings.should_alert_security('BLACKLISTED'))
print('security number:', settings.get('security_whatsapp'))
"