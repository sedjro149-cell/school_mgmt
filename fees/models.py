# finance/models.py
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.db import transaction

from core.models import Student
from academics.models import Level

class FeeType(models.Model):
    """
    Notion unique de type de frais (ex: 'Première tranche').
    Un seul FeeType pour la notion ; montants par niveau dans FeeTypeAmount.
    """
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

class FeeTypeAmount(models.Model):
    """
    Liaison FeeType <-> Level avec montant pour ce couple.
    Permet d'avoir un même FeeType lié à plusieurs niveaux avec montants différents.
    """
    fee_type = models.ForeignKey(FeeType, on_delete=models.CASCADE, related_name="amounts")
    level = models.ForeignKey(Level, on_delete=models.CASCADE, related_name="fee_type_amounts")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("fee_type", "level")
        ordering = ["level__id", "fee_type__name"]

    def __str__(self):
        return f"{self.fee_type.name} - {self.level.name}: {self.amount}"

class Fee(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="fees")
    fee_type = models.ForeignKey(
        FeeType,
        null=False,
        on_delete=models.CASCADE,
        related_name="student_fees"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid = models.BooleanField(default=False)
    payment_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("student", "fee_type")
        ordering = ["fee_type__name"]

    def __str__(self):
        return f"{self.student} - {self.fee_type.name}"

    @property
    def level(self):
        """
        Retourne le Level lié à ce Fee en cherchant dans FeeTypeAmount selon l'élève.
        """
        try:
            student_level = getattr(getattr(self.student, "school_class", None), "level", None)
            if not student_level:
                return None
            fta = self.fee_type.amounts.filter(level=student_level).first()
            return fta.level if fta else student_level
        except Exception:
            return None

class Payment(models.Model):
    fee = models.ForeignKey(Fee, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    method = models.CharField(max_length=100, blank=True)
    reference = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)
    validated = models.BooleanField(default=False)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    validated_at = models.DateTimeField(null=True, blank=True)

    def validate(self, user=None):
        self.validated = True
        self.validated_by = user
        self.validated_at = timezone.now()
        self.save()
        # recalculer statut du Fee
        total_paid = sum(p.amount for p in self.fee.payments.filter(validated=True))
        if total_paid >= self.fee.amount:
            self.fee.paid = True
            self.fee.payment_date = self.validated_at.date()
        else:
            self.fee.paid = False
        self.fee.save()

    def __str__(self):
        return f"Payment {self.amount} for {self.fee}"
