# fees/signals.py
import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db import transaction
from django.apps import apps

logger = logging.getLogger(__name__)


# ---------- Fees creation hooks (student / fee type amount) ----------

@receiver(post_save, sender=apps.get_model('core', 'Student'))
def create_fees_for_new_student(sender, instance, created, **kwargs):
    """
    Quand un Student est créé : si il a une classe -> récupérer son level
    et créer les Fee correspondant aux FeeTypeAmount actifs pour ce level.
    """
    if not created:
        return

    school_class = getattr(instance, "school_class", None)
    level = getattr(school_class, "level", None) if school_class else None
    if not level:
        return

    FeeTypeAmount = apps.get_model('fees', 'FeeTypeAmount')
    Fee = apps.get_model('fees', 'Fee')

    fee_type_amounts = FeeTypeAmount.objects.filter(level=level, is_active=True).select_related("fee_type")
    with transaction.atomic():
        for fta in fee_type_amounts:
            try:
                Fee.objects.get_or_create(
                    student=instance,
                    fee_type=fta.fee_type,
                    defaults={"amount": fta.amount}
                )
            except Exception as e:
                logger.exception(
                    "Erreur lors de la création de Fee pour student %s et fee_type %s: %s",
                    instance.pk, getattr(fta.fee_type, 'pk', None), e
                )


@receiver(pre_save, sender=apps.get_model('core', 'Student'))
def handle_student_level_change(sender, instance, **kwargs):
    """
    Si l'élève change de niveau (update), créer les fees du nouveau niveau s'ils manquent.
    (Pré-save : on compare l'ancien en base et le nouvel état)
    """
    if not instance.pk:
        # nouvel enregistrement : post_save gère la création
        return

    Student = apps.get_model('core', 'Student')
    try:
        old = Student.objects.get(pk=instance.pk)
    except Student.DoesNotExist:
        return

    old_level = getattr(getattr(old, "school_class", None), "level", None)
    new_level = getattr(getattr(instance, "school_class", None), "level", None)
    if old_level == new_level or new_level is None:
        return

    FeeTypeAmount = apps.get_model('fees', 'FeeTypeAmount')
    Fee = apps.get_model('fees', 'Fee')

    fee_type_amounts = FeeTypeAmount.objects.filter(level=new_level, is_active=True).select_related("fee_type")
    with transaction.atomic():
        for fta in fee_type_amounts:
            try:
                Fee.objects.get_or_create(
                    student=instance,
                    fee_type=fta.fee_type,
                    defaults={"amount": fta.amount}
                )
            except Exception as e:
                logger.exception(
                    "Erreur lors de la création de Fee après changement de niveau pour student %s: %s",
                    instance.pk, e
                )


@receiver(post_save, sender=apps.get_model('fees', 'FeeTypeAmount'))
def create_fees_for_existing_students_on_new_fee_type_amount(sender, instance, created, **kwargs):
    """
    Si on ajoute (ou ré-active) un FeeTypeAmount pour un level,
    créer les Fee manquants pour tous les étudiants déjà dans ce level.
    """
    # ne rien faire si le record est inactif
    if not instance.is_active:
        return

    Student = apps.get_model('core', 'Student')
    Fee = apps.get_model('fees', 'Fee')

    level = instance.level
    fee_type = instance.fee_type

    students_qs = Student.objects.filter(school_class__level=level)
    with transaction.atomic():
        for student in students_qs:
            try:
                Fee.objects.get_or_create(
                    student=student,
                    fee_type=fee_type,
                    defaults={"amount": instance.amount}
                )
            except Exception as e:
                logger.exception(
                    "Erreur lors de la création de Fee pour student %s suite à FeeTypeAmount %s: %s",
                    student.pk, getattr(instance, 'pk', None), e
                )


# ---------- Payment notifications (parent unique) ----------

@receiver(post_save, sender=apps.get_model('fees', 'Payment'))
def payment_created_signal(sender, instance, created, **kwargs):
    """
    Lorsque un Payment est créé (même non validé), on génère une notification 'payment_received'
    pour le parent unique (champ 'parent' sur Student) et pour l'élève (si user).
    """
    if not created:
        return

    # Récupère les modèles notifications via apps.get_model pour éviter circular imports
    try:
        Notification = apps.get_model('notifications', 'Notification')
        NotificationTemplate = apps.get_model('notifications', 'NotificationTemplate')
        UserNotificationPreference = apps.get_model('notifications', 'UserNotificationPreference')
        # tentative d'import de la fonction d'envoi (peut être absent en dev)
        try:
            from notifications.delivery import send_notification as send_notification_fn
        except Exception:
            send_notification_fn = None
    except LookupError:
        # notifications app absente -> on sort proprement
        logger.debug("notifications app non disponible — pas de notif envoyée pour payment %s", instance.pk)
        return

    fee = instance.fee
    tpl = NotificationTemplate.objects.filter(key='payment_received').first()
    default_channels = tpl.default_channels if tpl else ['inapp']

    # payload commun
    student_user = getattr(fee.student, 'user', None)
    if student_user:
        student_name = getattr(student_user, 'get_full_name', lambda: '')() or f"{getattr(fee.student, 'first_name','')} {getattr(fee.student,'last_name','')}"
    else:
        student_name = f"{getattr(fee.student,'first_name','')} {getattr(fee.student,'last_name','')}"

    payload = {
        "payment_id": instance.id,
        "fee_id": getattr(fee, 'id', None),
        "fee_type": fee.fee_type.name if getattr(fee, 'fee_type', None) else None,
        "amount": float(instance.amount) if instance.amount is not None else None,
        "method": instance.method,
        "reference": instance.reference,
        "paid_at": getattr(instance, 'paid_at', None).isoformat() if getattr(instance, 'paid_at', None) else None,
        "student_name": student_name,
    }

    # notifier le parent unique (champ 'parent' sur Student)
    parent = getattr(fee.student, 'parent', None)
    if parent:
        user_obj = getattr(parent, 'user', None)
        if user_obj:
            # éviter doublon pour ce payment
            existed = Notification.objects.filter(
                topic='fees',
                recipient_user=user_obj,
                payload__payment_id=instance.id
            ).exists()
            if not existed:
                channels = list(default_channels)
                try:
                    pref = UserNotificationPreference.objects.get(user=user_obj, topic='fees')
                    if not pref.enabled:
                        channels = []
                    else:
                        channels = pref.channels or channels
                except UserNotificationPreference.DoesNotExist:
                    pass

                try:
                    notif = Notification.objects.create(
                        template=tpl,
                        topic='fees',
                        recipient_user=user_obj,
                        payload={**payload, "parent_name": getattr(parent, 'name', '')},
                        channels=channels
                    )
                except Exception as e:
                    logger.exception(
                        "Impossible de créer Notification pour parent user %s sur payment %s: %s",
                        getattr(user_obj, 'pk', None), instance.pk, e
                    )
                else:
                    # tenter d'envoyer immédiatement (si send_notification disponible)
                    if send_notification_fn:
                        try:
                            send_notification_fn(notif)
                        except Exception as e:
                            logger.exception(
                                "Erreur d'envoi de notif initiale pour payment %s -> parent %s: %s",
                                instance.pk, getattr(user_obj, 'pk', None), e
                            )

    # notifier l'élève si user attaché
    if student_user:
        existed = Notification.objects.filter(
            topic='fees',
            recipient_user=student_user,
            payload__payment_id=instance.id
        ).exists()
        if not existed:
            channels = list(default_channels)
            try:
                pref = UserNotificationPreference.objects.get(user=student_user, topic='fees')
                if not pref.enabled:
                    channels = []
                else:
                    channels = pref.channels or channels
            except UserNotificationPreference.DoesNotExist:
                pass

            try:
                notif = Notification.objects.create(
                    template=tpl,
                    topic='fees',
                    recipient_user=student_user,
                    payload=payload,
                    channels=channels
                )
            except Exception as e:
                logger.exception(
                    "Impossible de créer Notification pour student user %s sur payment %s: %s",
                    getattr(student_user, 'pk', None), instance.pk, e
                )
            else:
                if send_notification_fn:
                    try:
                        send_notification_fn(notif)
                    except Exception as e:
                        logger.exception(
                            "Erreur d'envoi de notif initiale pour payment %s -> student %s: %s",
                            instance.pk, getattr(student_user, 'pk', None), e
                        )
