from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from fees.models import Invoice
from notifications.models import NotificationTemplate, Notification, UserNotificationPreference
from notifications.delivery import send_notification

REMINDER_OFFSETS = [30, 14, 3, 0, -3, -7]

class Command(BaseCommand):
    help = "Génère les rappels de frais en fonction des échéances."

    def handle(self, *args, **options):
        today = timezone.now().date()
        for offset in REMINDER_OFFSETS:
            target_date = today + timedelta(days=offset)
            invoices = Invoice.objects.filter(due_date=target_date, status='pending')
            for inv in invoices:
                try:
                    template_key = f"fees_due_{abs(offset)}"
                    template = NotificationTemplate.objects.get(key=template_key)
                except NotificationTemplate.DoesNotExist:
                    self.stdout.write(self.style.WARNING(f"Template manquant: {template_key}"))
                    continue
                for parent in inv.student.parents.all():
                    existed = Notification.objects.filter(
                        topic='fees',
                        recipient_user=parent.user,
                        payload__invoice_ref=inv.reference,
                        payload__reminder_offset=offset
                    ).exists()
                    if existed:
                        continue
                    try:
                        pref = UserNotificationPreference.objects.get(user=parent.user, topic='fees')
                        if not pref.enabled:
                            continue
                        channels = pref.channels or template.default_channels
                    except UserNotificationPreference.DoesNotExist:
                        channels = template.default_channels

                    payload = {
                        "parent_name": parent.name if hasattr(parent, 'name') else getattr(parent.user, 'get_full_name', '')(),
                        "student_name": f"{inv.student.first_name} {inv.student.last_name}",
                        "amount_due": float(inv.amount_due),
                        "due_date": inv.due_date.isoformat(),
                        "invoice_ref": inv.reference,
                        "reminder_offset": offset
                    }
                    notif = Notification.objects.create(
                        template=template,
                        topic='fees',
                        recipient_user=parent.user,
                        payload=payload,
                        channels=channels
                    )
                    send_notification(notif)
