from django.core.mail import send_mail
from .models import Notification, NotificationAttempt, Channel

def send_notification(notification: Notification):
    results = {}
    title = notification.render_title()
    body = notification.render_body()
    channels = notification.channels or (notification.template.default_channels if notification.template else ['inapp'])

    for ch in channels:
        if ch == Channel.INAPP:
            NotificationAttempt.objects.create(notification=notification, channel=ch, success=True, response='stored')
            results[ch] = True
        elif ch == Channel.EMAIL:
            try:
                if notification.recipient_user.email:
                    send_mail(subject=title, message=body, from_email='no-reply@school.local', recipient_list=[notification.recipient_user.email])
                    NotificationAttempt.objects.create(notification=notification, channel=ch, success=True)
                    results[ch] = True
                else:
                    NotificationAttempt.objects.create(notification=notification, channel=ch, success=False, response='no email')
                    results[ch] = False
            except Exception as e:
                NotificationAttempt.objects.create(notification=notification, channel=ch, success=False, response=str(e))
                results[ch] = False
        elif ch == Channel.SMS:
            try:
                phone = getattr(notification.recipient_user, 'phone', None)
                if phone:
                    # TODO: integrate your sms_send(phone, body)
                    # sms_send(phone, body)
                    NotificationAttempt.objects.create(notification=notification, channel=ch, success=True, response='sms_sent_placeholder')
                    results[ch] = True
                else:
                    NotificationAttempt.objects.create(notification=notification, channel=ch, success=False, response='no phone')
                    results[ch] = False
            except Exception as e:
                NotificationAttempt.objects.create(notification=notification, channel=ch, success=False, response=str(e))
                results[ch] = False
        elif ch == Channel.PUSH:
            try:
                devices = notification.recipient_user.devices.all()
                if not devices.exists():
                    NotificationAttempt.objects.create(notification=notification, channel=ch, success=False, response='no devices')
                    results[ch] = False
                    continue
                for dev in devices:
                    # TODO: push_send(dev.provider, dev.token, title, body)
                    # push_send(dev.provider, dev.token, title, body)
                    NotificationAttempt.objects.create(notification=notification, channel=ch, success=True, response='push_sent_placeholder')
                results[ch] = True
            except Exception as e:
                NotificationAttempt.objects.create(notification=notification, channel=ch, success=False, response=str(e))
                results[ch] = False

    if any(results.values()):
        notification.mark_sent()
    else:
        notification.error = "all channels failed"
        notification.save(update_fields=['error'])
    return results
