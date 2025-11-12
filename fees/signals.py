# finance/signals.py
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db import transaction
from core.models import Student
from .models import FeeTypeAmount, Fee

@receiver(post_save, sender=Student)
def create_fees_for_new_student(sender, instance, created, **kwargs):
    """
    Quand un Student est créé : si il a une classe -> récupérer son level et créer
    les Fee correspondant aux FeeTypeAmount actifs pour ce level.
    """
    if not created:
        return
    school_class = getattr(instance, "school_class", None)
    level = getattr(school_class, "level", None) if school_class else None
    if not level:
        return
    fee_type_amounts = FeeTypeAmount.objects.filter(level=level, is_active=True).select_related("fee_type")
    with transaction.atomic():
        for fta in fee_type_amounts:
            Fee.objects.get_or_create(
                student=instance,
                fee_type=fta.fee_type,
                defaults={"amount": fta.amount}
            )

@receiver(pre_save, sender=Student)
def handle_student_level_change(sender, instance, **kwargs):
    """
    Si l'élève change de niveau (update), créer les fees du nouveau niveau s'ils manquent.
    """
    if not instance.pk:
        # nouvel enregistrement : post_save gère la création
        return
    try:
        old = Student.objects.get(pk=instance.pk)
    except Student.DoesNotExist:
        return
    old_level = getattr(getattr(old, "school_class", None), "level", None)
    new_level = getattr(getattr(instance, "school_class", None), "level", None)
    if old_level == new_level or new_level is None:
        return
    fee_type_amounts = FeeTypeAmount.objects.filter(level=new_level, is_active=True).select_related("fee_type")
    with transaction.atomic():
        for fta in fee_type_amounts:
            Fee.objects.get_or_create(
                student=instance,
                fee_type=fta.fee_type,
                defaults={"amount": fta.amount}
            )

@receiver(post_save, sender=FeeTypeAmount)
def create_fees_for_existing_students_on_new_fee_type_amount(sender, instance, created, **kwargs):
    """
    Si on ajoute (ou ré-active) un FeeTypeAmount pour un level,
    créer les Fee manquants pour tous les étudiants déjà dans ce level.
    """
    # Si record inactif et pas créé, on ne fait rien
    if not instance.is_active:
        return
    level = instance.level
    fee_type = instance.fee_type
    # Récupérer étudiants du level
    students_qs = Student.objects.filter(school_class__level=level)
    with transaction.atomic():
        for student in students_qs:
            Fee.objects.get_or_create(
                student=student,
                fee_type=fee_type,
                defaults={"amount": instance.amount}
            )
