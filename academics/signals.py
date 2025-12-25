# -*- coding: utf-8 -*-
"""
from django.db.models.signals import post_save
from django.dispatch import receiver
from decimal import Decimal
from .models import Grade, ReportCard

# -----------------------------
# Signal pour créer ou mettre à jour un bulletin
# -----------------------------
@receiver(post_save, sender=Grade)
def create_or_update_report_card(sender, instance, **kwargs):
    """
    Crée ou met à jour automatiquement le bulletin d’un élève
    dès qu’une note est ajoutée ou modifiée.
    """
    student = instance.student
    term = instance.term

    grades = Grade.objects.filter(student=student, term=term)
    if not grades.exists():
        return  # pas de note → pas de bulletin

    # Somme pondérée de chaque matière
    total_weighted = sum([g.average_coeff or 0 for g in grades])

    # Total des coefficients de toutes les matières attribuées à la classe
    class_subjects = student.school_class.class_subjects.all() if student.school_class else []
    total_coeffs = sum([cs.coefficient for cs in class_subjects]) if class_subjects else 1

    general_average = total_weighted / total_coeffs

    # Crée ou met à jour le ReportCard
    ReportCard.objects.update_or_create(
        student=student,
        term=term,
        defaults={'average': round(Decimal(general_average), 2)}
    )
# grades/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender='grades.Grade')
def grade_post_save_notify(sender, instance, created, **kwargs):
    # respect suppression flag to avoid duplicates on bulk operations
    if getattr(instance, '_suppress_notifications', False):
        return

    try:
        from notifications.service import bulk_notify_grades
        action = 'created' if created else 'updated'
        bulk_notify_grades([(instance.id, action)])
    except Exception:
        logger.exception("Error in grade_post_save_notify for grade %s", getattr(instance, 'id', None))
