from django.core.management.base import BaseCommand
from notifications.models import NotificationTemplate

TEMPLATES = [
    {
        "key": "fees_due_30",
        "topic": "fees",
        "title_template": "Rappel - paiement dû le {{ due_date }}",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due }} pour {{ student_name }} est dû le {{ due_date }} (Réf {{ invoice_ref }}).",
        "default_channels": ["inapp","email"]
    },
    {
        "key": "fees_due_14",
        "topic": "fees",
        "title_template": "Rappel - paiement dû dans 14 jours",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due }} pour {{ student_name }} est dû le {{ due_date }} (Réf {{ invoice_ref }}).",
        "default_channels": ["inapp","email"]
    },
    {
        "key": "fees_due_3",
        "topic": "fees",
        "title_template": "Rappel urgent - paiement dans 3 jours",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due }} pour {{ student_name }} est dû le {{ due_date }} (Réf {{ invoice_ref }}). Merci de régulariser.",
        "default_channels": ["inapp","email","sms"]
    },
    {
        "key": "fees_due_0",
        "topic": "fees",
        "title_template": "Échéance aujourd'hui",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due }} pour {{ student_name }} est dû aujourd'hui (Réf {{ invoice_ref }}).",
        "default_channels": ["inapp","sms"]
    },
    {
        "key": "fees_due_plus3",
        "topic": "fees",
        "title_template": "Relance - paiement en souffrance",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due }} pour {{ student_name }} est toujours impayé (Réf {{ invoice_ref }}). Veuillez contacter l'administration.",
        "default_channels": ["inapp","sms","email"]
    },
    {
        "key": "fees_due_plus7",
        "topic": "fees",
        "title_template": "Relance finale - action requise",
        "body_template": "Bonjour {{ parent_name }}, malgré nos relances, le paiement de {{ amount_due }} pour {{ student_name }} reste impayé (Réf {{ invoice_ref }}). Merci de contacter l'administration.",
        "default_channels": ["inapp","sms","email"]
    }
]

class Command(BaseCommand):
    help = "Seed default notification templates (fees)."

    def handle(self, *args, **options):
        for t in TEMPLATES:
            obj, created = NotificationTemplate.objects.update_or_create(
                key=t['key'],
                defaults={
                    "topic": t['topic'],
                    "title_template": t['title_template'],
                    "body_template": t['body_template'],
                    "default_channels": t['default_channels']
                }
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Template créé: {t['key']}"))
            else:
                self.stdout.write(self.style.WARNING(f"Template mis à jour: {t['key']}"))
