from django.core.management.base import BaseCommand
from academics.models import LevelFee, Level
from fees.models import FeeType, Fee
from core.models import Student
from django.db import transaction

class Command(BaseCommand):
    help = "Migrer LevelFee vers FeeType + créer les Fee par étudiant"

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("⚠️ Faites une sauvegarde avant !"))
        created_ft = 0
        created_fees = 0

        for lf in LevelFee.objects.select_related("level").all():
            level = lf.level
            with transaction.atomic():
                ft, ft_created = FeeType.objects.get_or_create(
                    name="Frais scolaires (par défaut)",
                    level=level,
                    defaults={"default_amount": lf.total_amount}
                )
                if ft_created:
                    created_ft += 1

                students = Student.objects.filter(school_class__level=level)
                for s in students:
                    fee_obj, fee_created = Fee.objects.get_or_create(
                        student=s,
                        fee_type=ft,
                        defaults={"amount": lf.total_amount}
                    )
                    if fee_created:
                        created_fees += 1

        self.stdout.write(self.style.SUCCESS(f"FeeTypes créés: {created_ft} | Fees créés: {created_fees}"))
