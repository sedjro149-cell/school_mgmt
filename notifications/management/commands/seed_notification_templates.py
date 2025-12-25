from django.core.management.base import BaseCommand
from notifications.models import NotificationTemplate

# Liste des templates avec la syntaxe correcte ({{ variable }})
TEMPLATES = [
     # management command seeder: ajouter ces éléments dans TEMPLATES
    {
        "key": "grade_added",
        "topic": "grades",
        "title_template": "Nouvelle note pour {{ student_name }}",
        "body_template": "Une nouvelle note a été ajoutée : {{ subject }} — {{ grade }} ({{ term }}).",
        "default_channels": ["inapp", "email"]
    },
    {
    "key": "absence_reported",
    "topic": "attendance",
    "title_template": "Absence signalée — {{ student_name }}",
    "body_template": "Bonjour {{ parent_name }}, {{ student_name }} a été signalé absent le {{ date }}{% if subject %} pour {{ subject }} ({{ starts_at }}){% endif %}.{% if reason %} Motif : {{ reason }}.{% endif %}",
    "default_channels": ["inapp", "sms"]
    },

    {
        "key": "grade_updated",
        "topic": "grades",
        "title_template": "Note mise à jour pour {{ student_name }}",
        "body_template": "La note de {{ subject }} a été modifiée : {{ grade }} ({{ term }}).",
        "default_channels": ["inapp"]
    },

    {
        "key": "payment_received",
        "topic": "fees",
        "title_template": "Paiement reçu pour {{ student_name }}",
        "body_template": "Nous avons enregistré un paiement de {{ amount|floatformat:0 }} pour {{ student_name }}. Réf: {{ reference }}. En attente de validation.",
        "default_channels": ["inapp", "email"]
    },
    {
        "key": "payment_validated",
        "topic": "fees",
        "title_template": "Paiement validé pour {{ student_name }}", 
        "body_template": "Le paiement de {{ amount|floatformat:0 }} pour {{ student_name }} a été validé le {{ validated_at }}. Merci.",
        "default_channels": ["inapp", "email", "sms"]
    },
    {
        "key": "fees_due_30",
        "topic": "fees",
        "title_template": "Rappel - paiement dû le {{ due_date }}",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due|floatformat:0 }} pour {{ student_name }} est dû le {{ due_date }} (Réf {{ invoice_ref }}).",
        "default_channels": ["inapp", "email"]
    },
    {
        "key": "fees_due_14",
        "topic": "fees",
        "title_template": "Rappel - paiement dû dans 14 jours",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due|floatformat:0 }} pour {{ student_name }} est dû le {{ due_date }} (Réf {{ invoice_ref }}).",
        "default_channels": ["inapp", "email"]
    },
    {
        "key": "fees_due_3",
        "topic": "fees",
        "title_template": "Rappel urgent - paiement dans 3 jours",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due|floatformat:0 }} pour {{ student_name }} est dû le {{ due_date }} (Réf {{ invoice_ref }}). Merci de régulariser.",
        "default_channels": ["inapp", "email", "sms"]
    },
    {
        "key": "fees_due_0",
        "topic": "fees",
        "title_template": "Échéance aujourd'hui",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due|floatformat:0 }} pour {{ student_name }} est dû aujourd'hui (Réf {{ invoice_ref }}).",
        "default_channels": ["inapp", "sms"]
    },
    {
        "key": "fees_overdue_3",
        "topic": "fees",
        "title_template": "Relance - paiement en souffrance (3 jours)",
        "body_template": "Bonjour {{ parent_name }}, le paiement de {{ amount_due|floatformat:0 }} pour {{ student_name }} est toujours impayé (Réf {{ invoice_ref }}). Veuillez contacter l'administration.",
        "default_channels": ["inapp", "sms", "email"]
    },
    {
        "key": "fees_overdue_7",
        "topic": "fees",
        "title_template": "Relance finale - action requise",
        "body_template": "Bonjour {{ parent_name }}, malgré nos relances, le paiement de {{ amount_due|floatformat:0 }} pour {{ student_name }} reste impayé (Réf {{ invoice_ref }}). Merci de contacter l'administration.",
        "default_channels": ["inapp", "sms", "email"]
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