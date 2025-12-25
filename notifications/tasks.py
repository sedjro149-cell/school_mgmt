from django.utils import timezone
from datetime import timedelta
from fees.models import Fee  # chemin correct
from .models import NotificationTemplate, Notification, UserNotificationPreference
from .delivery import send_notification

REMINDER_OFFSETS = [30, 14, 3, 0, -3, -7]

def generate_fee_reminders_once():
    today = timezone.now().date()
    for offset in REMINDER_OFFSETS:
        target_date = today + timedelta(days=offset)
        # chercher Fees dont due_date == target_date et non payés
        fees = Fee.objects.filter(due_date=target_date, paid=False)
        for fee in fees:
            try:
                template_key = f"fees_due_{abs(offset)}"
                template = NotificationTemplate.objects.get(key=template_key)
            except NotificationTemplate.DoesNotExist:
                continue

            # notify each parent
            for parent in fee.student.parents.all():
                user_obj = getattr(parent, 'user', None)
                if not user_obj:
                    continue

                # prevent duplicates: check if notif for this fee + offset already exists
                existed = Notification.objects.filter(
                    topic='fees',
                    recipient_user=user_obj,
                    payload__fee_id=fee.id,
                    payload__reminder_offset=offset
                ).exists()
                if existed:
                    continue

                # user preference
                try:
                    pref = UserNotificationPreference.objects.get(user=user_obj, topic='fees')
                    if not pref.enabled:
                        continue
                    channels = pref.channels or template.default_channels
                except UserNotificationPreference.DoesNotExist:
                    channels = template.default_channels

                payload = {
                    "fee_id": fee.id,
                    "fee_type": fee.fee_type.name,
                    "student_name": getattr(getattr(fee.student,'user',None),'get_full_name', lambda: '')() or f"{getattr(fee.student,'first_name','')} {getattr(fee.student,'last_name','')}",
                    "amount_due": float(fee.amount),
                    "due_date": fee.due_date.isoformat() if fee.due_date else None,
                    "reminder_offset": offset
                }

                notif = Notification.objects.create(
                    template=template,
                    topic='fees',
                    recipient_user=user_obj,
                    payload=payload,
                    channels=channels
                )
                # Optionnel: envoyer tout de suite
                send_notification(notif)
