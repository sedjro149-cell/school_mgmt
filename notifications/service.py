# notifications/service.py
import logging
from typing import List, Tuple
from django.db import transaction
from django.utils import timezone
from django.apps import apps  # <--- INDISPENSABLE pour éviter l'erreur

from .models import Notification, NotificationTemplate, UserNotificationPreference
from .utils import render_django_template
from .delivery import send_notification

logger = logging.getLogger(__name__)

def _resolve_parents_from_student(student):
    """
    Try several common relations to return a list of User instances that represent parents/guardians.
    Adapt to your Student model: checks student.parent, student.parents, student.guardians, student.user fallback.
    """
    users = []
    try:
        # single FK parent
        if hasattr(student, 'parent') and student.parent:
            p = student.parent
            users.append(getattr(p, 'user', p))
            return users

        # many-to-many parents
        if hasattr(student, 'parents'):
            try:
                for p in student.parents.all():
                    users.append(getattr(p, 'user', p))
                if users:
                    return users
            except Exception:
                pass

        # guardians
        if hasattr(student, 'guardians'):
            try:
                for g in student.guardians.all():
                    users.append(getattr(g, 'user', g))
                if users:
                    return users
            except Exception:
                pass

        # maybe student is a proxy to user
        if hasattr(student, 'user'):
            users.append(student.user)
    except Exception:
        logger.exception("Error resolving parents for student %s", getattr(student, 'id', student))
    return users

def _determine_channels_for_user(user, topic, explicit_channels=None, tpl: NotificationTemplate = None):
    """
    Priority:
      explicit_channels > user preference > template.default_channels > ['inapp']
    """
    if explicit_channels:
        return explicit_channels
    pref = UserNotificationPreference.objects.filter(user=user, topic=topic, enabled=True).first()
    if pref and pref.channels:
        return pref.channels
    if tpl and tpl.default_channels:
        return tpl.default_channels
    return ['inapp']

@transaction.atomic
def create_notification_for_user(recipient_user, topic: str, payload: dict = None, template_key: str = None,
                                 channels: list = None, auto_send: bool = True) -> Notification:
    """
    Create a Notification and (optionally) send it immediately through send_notification.
    """
    payload = payload or {}
    tpl = None
    if template_key:
        tpl = NotificationTemplate.objects.filter(key=template_key).first()
    if not tpl:
        tpl = NotificationTemplate.objects.filter(topic=topic).first()

    chosen_channels = _determine_channels_for_user(recipient_user, topic, explicit_channels=channels, tpl=tpl)

    notif = Notification.objects.create(
        template=tpl,
        topic=topic,
        recipient_user=recipient_user,
        payload=payload,
        channels=chosen_channels,
    )

    # optional pre-render caching (not required)
    # notif.render_title(); notif.render_body()

    if auto_send:
        try:
            send_notification(notif)
        except Exception:
            logger.exception("send_notification failed for notif %s", notif.id)

    return notif

def bulk_notify_grades(grade_id_action_pairs: List[Tuple[int, str]]):
    """
    grade_id_action_pairs: list of tuples (grade_id, action) where action in {'created', 'updated'}.
    Loads related Grade rows and creates notifications for each student's parents.
    This function is safe to call inside transaction.on_commit.
    """
    
    # --- CORRECTION MAJEURE ICI ---
    # On utilise apps.get_model pour récupérer Grade sans faire un "import" direct
    # qui échoue à cause des dépendances circulaires.
    try:
        Grade = apps.get_model('academics', 'Grade')
    except LookupError:
        logger.error("Le modèle 'academics.Grade' est introuvable via apps.get_model.")
        return
    # ------------------------------

    grade_ids = [gid for gid, _ in grade_id_action_pairs]
    
    # Maintenant "Grade" est bien défini
    grades = Grade.objects.select_related('student', 'student__user', 'subject').filter(id__in=grade_ids)
    grades_by_id = {g.id: g for g in grades}

    for gid, action in grade_id_action_pairs:
        g = grades_by_id.get(gid)
        if not g:
            logger.warning("Grade %s not found for notification", gid)
            continue

        student = g.student
        parents = _resolve_parents_from_student(student)
        if not parents:
            logger.warning("No parents resolved for student %s (grade %s)", getattr(student, 'id', student), gid)
            continue

        # safe payload
        student_name = None
        if hasattr(student, 'user'):
            u = student.user
            student_name = f"{getattr(u, 'first_name','').strip()} {getattr(u, 'last_name','').strip()}".strip()
        else:
            student_name = getattr(student, 'full_name', str(student))

        teacher_name = ''
        if hasattr(g, 'teacher') and g.teacher is not None:
            teacher = g.teacher
            teacher_name = getattr(teacher, 'get_full_name', lambda: str(teacher))()

        payload = {
            'student_id': str(getattr(student, 'id', '')),
            'student_name': student_name,
            # NE PAS convertir en str ici si tu veux utiliser |floatformat dans le template
            'grade': float(g.average_subject) if g.average_subject is not None else 0, 
            'subject': getattr(g.subject, 'name', str(g.subject)),
            'term': g.term,
            'teacher_name': teacher_name,
            'grade_id': g.id,
            'created_at': g.created_at.isoformat() if getattr(g, 'created_at', None) else None,
            'action': action,
        }

        tpl_key = 'grade_added' if action == 'created' else 'grade_updated'

        for parent_user in parents:
            try:
                create_notification_for_user(recipient_user=parent_user, topic='grades', payload=payload,
                                             template_key=tpl_key, auto_send=True)
            except Exception:
                logger.exception("Failed to create notification for parent %s for grade %s",
                                 getattr(parent_user, 'id', parent_user), g.id)