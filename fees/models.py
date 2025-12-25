# fees/models.py
from decimal import Decimal
import logging

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.core.exceptions import PermissionDenied, ValidationError
from django.apps import apps

from core.models import Student
from django.db.models import Sum

from academics.models import Level

logger = logging.getLogger(__name__)


class FeeType(models.Model):
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    # date limite par défaut pour ce type de frais (peut être surchargée au niveau du Fee)
    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date limite par défaut pour ce type de frais (peut être surchargée au niveau du Fee)."
    )

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

    # date d'échéance spécifique au fee (pré-remplie depuis fee_type)
    due_date = models.DateField(null=True, blank=True)

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
    def total_paid(self):
        """
        Somme des paiements VALIDÉS pour ce fee.
        Retourne Decimal('0') si aucun paiement validé.
        """
        s = self.payments.filter(validated=True).aggregate(total=Sum('amount'))['total']
        return Decimal(s) if s is not None else Decimal('0')

    @property
    def total_paid_all(self):
        """
        Somme de tous les paiements (validés ou non).
        Utile si tu veux afficher les paiements en attente.
        """
        s = self.payments.aggregate(total=Sum('amount'))['total']
        return Decimal(s) if s is not None else Decimal('0')

    @property
    def total_remaining(self):
        """
        Montant restant basé sur les paiements validés (amount - total_paid).
        """
        try:
            return Decimal(self.amount) - self.total_paid
        except Exception:
            return Decimal('0')


    def save(self, *args, **kwargs):
        # si pas de due_date sur le Fee, récupérer depuis FeeType (si présent)
        if not self.due_date and self.fee_type and getattr(self.fee_type, "due_date", None):
            self.due_date = self.fee_type.due_date
        super().save(*args, **kwargs)

    @property
    def level(self):
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
        """
        Valide le paiement, met à jour le Fee (paid/payment_date) et déclenche notification de validation.

        - Vérifie si l'utilisateur a la permission (si fourni).
        - Protège les mises à jour d'état avec transaction.atomic().
        - Empêche les erreurs de notifications de casser la validation.
        - Lève PermissionDenied ou ValidationError en cas de problème sérieux.
        """
        # permission check (si l'appelant fournit un user, on le vérifie ; sinon on autorise l'exécution)
        if user is not None:
            if not (getattr(user, "is_superuser", False) or getattr(user, "is_staff", False) or user.has_perm("fees.can_validate_payment")):
                raise PermissionDenied("Vous n'avez pas la permission de valider ce paiement.")

        # validations basiques
        fee = getattr(self, "fee", None)
        if fee is None:
            raise ValidationError("Le paiement n'est associé à aucun 'fee'.")

        # empêcher double validation
        if self.validated:
            raise ValidationError("Paiement déjà validé.")

        with transaction.atomic():
            # marquer le paiement comme validé
            self.validated = True
            self.validated_by = user
            self.validated_at = timezone.now()
            self.save(update_fields=['validated', 'validated_by', 'validated_at'])

            # recalculer statut du Fee (somme des paiements validés)
            total_paid = Decimal('0')
            # utilise Decimal pour éviter surprises
            for p in self.fee.payments.filter(validated=True):
                if p.amount is not None:
                    total_paid += Decimal(p.amount)

            # mettre à jour le Fee selon le total validé
            try:
                fee_amount = Decimal(fee.amount) if fee.amount is not None else Decimal('0')
            except Exception:
                fee_amount = Decimal('0')

            if total_paid >= fee_amount:
                fee.paid = True
                fee.payment_date = self.validated_at.date() if self.validated_at else None
            else:
                fee.paid = False
                fee.payment_date = None
            fee.save(update_fields=['paid', 'payment_date'])

        # Notifications : la partie notification est secondaire — on la protège pour ne pas casser la validation
        try:
            Notification = apps.get_model('notifications', 'Notification')
            NotificationTemplate = apps.get_model('notifications', 'NotificationTemplate')
            UserNotificationPreference = apps.get_model('notifications', 'UserNotificationPreference')
            try:
                from notifications.delivery import send_notification as send_notification_fn
            except Exception:
                send_notification_fn = None
        except Exception:
            Notification = None
            send_notification_fn = None

        if not Notification:
            # notifications app non installée : on quitte proprement
            return

        # Préparer données communes à la payload
        student = getattr(fee, "student", None)
        student_user = getattr(student, "user", None) if student is not None else None

        if student_user:
            student_name = getattr(student_user, "get_full_name", lambda: "")() or f"{getattr(student, 'first_name', '')} {getattr(student, 'last_name', '')}"
        else:
            student_name = f"{getattr(student, 'first_name', '')} {getattr(student, 'last_name', '')}" if student is not None else None

        payload = {
            "student_id": getattr(student, 'id', None),
            "student_name": student_name,
            "fee_id": fee.id,
            "fee_type": fee.fee_type.name if getattr(fee, 'fee_type', None) else None,
            "amount": float(self.amount) if self.amount is not None else None,
            "payment_id": self.id,
            "validated_at": self.validated_at.isoformat() if self.validated_at else None,
        }

        # récupérer template (silencieux en cas d'erreur)
        tpl = None
        try:
            tpl = NotificationTemplate.objects.filter(key='payment_validated').first()
        except Exception:
            tpl = None

        default_channels = getattr(tpl, 'default_channels', None) or ['inapp']

        # notifier parents
        try:
            parents_qs = fee.student.parents.all()
        except Exception:
            parents_qs = []

        for parent in parents_qs:
            user_obj = getattr(parent, 'user', None)
            if not user_obj:
                continue

            # empêcher doublon (protégé)
            try:
                existed = Notification.objects.filter(
                    topic='fees',
                    recipient_user=user_obj,
                    payload__payment_id=self.id
                ).exists()
            except Exception:
                existed = False

            if existed:
                continue

            channels = list(default_channels)
            try:
                pref = UserNotificationPreference.objects.get(user=user_obj, topic='fees')
                if not pref.enabled:
                    continue
                channels = pref.channels or channels
            except Exception:
                # si pb sur preference -> on laisse channels par défaut
                pass

            try:
                notif = Notification.objects.create(
                    template=tpl,
                    topic='fees',
                    recipient_user=user_obj,
                    payload={**payload, "parent_name": getattr(parent, 'name', '')},
                    channels=channels
                )
            except Exception:
                # si création de notification échoue on skip ce destinataire
                logger.exception("Échec création notification parent for payment_id=%s parent=%r", self.id, parent)
                continue

            if send_notification_fn:
                try:
                    send_notification_fn(notif)
                except Exception:
                    logger.exception("Échec envoi notification parent for payment_id=%s notif_id=%r", self.id, getattr(notif, 'id', None))

        # notifier l'élève (s'il a user)
        if student_user:
            try:
                existed = Notification.objects.filter(
                    topic='fees',
                    recipient_user=student_user,
                    payload__payment_id=self.id
                ).exists()
            except Exception:
                existed = False

            if not existed:
                channels = list(default_channels)
                try:
                    pref = UserNotificationPreference.objects.get(user=student_user, topic='fees')
                    if not pref.enabled:
                        channels = []
                    else:
                        channels = pref.channels or channels
                except Exception:
                    pass

                notif = None
                try:
                    notif = Notification.objects.create(
                        template=tpl,
                        topic='fees',
                        recipient_user=student_user,
                        payload=payload,
                        channels=channels
                    )
                except Exception:
                    logger.exception("Échec création notification student for payment_id=%s student=%r", self.id, student_user)

                if notif and send_notification_fn:
                    try:
                        send_notification_fn(notif)
                    except Exception:
                        logger.exception("Échec envoi notification student for payment_id=%s notif_id=%r", self.id, getattr(notif, 'id', None))

    def __str__(self):
        return f"Payment {self.amount} for {self.fee}"
