# academics/views.py
import json
import logging
import random
import math
import time
from collections import defaultdict
from datetime import datetime
from django.apps import apps
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from django.db import IntegrityError
from django.db import transaction
from django.contrib.auth.models import User
from academics.models import DraftGrade, Grade, Subject


from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, NotFound
from rest_framework.parsers import MultiPartParser, FormParser


from django_filters.rest_framework import DjangoFilterBackend

from academics.models import (
    ClassScheduleEntry,
    Level,
    SchoolClass,
    Subject,
    ClassSubject,
    Grade,
    SubjectComment,
    TimeSlot,
)
from academics.serializers import (
    ClassScheduleEntrySerializer,
    ReportCardSerializer,
    SubjectCommentSerializer,
    TimeSlotSerializer,
    DraftGradeSerializer,
)
from .serializers import (
    UserSerializer,
    ParentSerializer,
    StudentSerializer,
    LevelSerializer,
    SchoolClassSerializer,
    SubjectSerializer,
    ClassSubjectSerializer,
    GradeSerializer,
    GradeBulkLineSerializer,
    GroupedClassSubjectSerializer,
)
from core.models import Student, Parent
from core.permissions import IsTeacherOrAdminCanEditComment
from .permissions import IsAdminOrParentReadOnly, IsAdminOrReadOnly
from django.db import connection
from academics.models import ClassScheduleEntry
from .filters import GradeFilter
from academics.services.report_cards import compute_report_cards_from_grades
from academics.timetable_by_level import run_timetable_pipeline
def reset_timetable_table():
    """
    Supprime toutes les entrées de ClassScheduleEntry
    et réinitialise l'auto-incrément PostgreSQL à 1.
    """
    print("🧹 Réinitialisation complète de la table des emplois du temps...")

    # Suppression de toutes les lignes
    ClassScheduleEntry.objects.all().delete()

    # Réinitialisation de la séquence PostgreSQL
    with connection.cursor() as cursor:
        cursor.execute("ALTER SEQUENCE academics_classscheduleentry_id_seq RESTART WITH 1;")

    print("✅ Table vidée et ID réinitialisés à 1.")

logger = logging.getLogger(__name__)


# ----------------------------
# USERS
# ----------------------------
class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return User.objects.all()
        # Parents/Students ne peuvent pas voir tous les utilisateurs
        return User.objects.filter(id=user.id)


# ----------------------------
# PARENTS
# ----------------------------
class ParentViewSet(viewsets.ModelViewSet):
    queryset = Parent.objects.all()
    serializer_class = ParentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return Parent.objects.all()
        if hasattr(user, "parent"):
            return Parent.objects.filter(user=user)
        return Parent.objects.none()


# ----------------------------
# STUDENTS
# ----------------------------
class StudentViewSet(viewsets.ModelViewSet):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user

        # --- Admin / Superuser : tout
        if user.is_staff or user.is_superuser:
            return Student.objects.all()

        # --- Parent : uniquement ses enfants
        if hasattr(user, "parent"):
            parent = getattr(user, "parent", None)
            if parent is None:
                return Student.objects.none()
            return Student.objects.filter(parent=parent)

        # --- Élève : uniquement lui-même
        if hasattr(user, "student"):
            return Student.objects.filter(user=user)

        # --- Enseignant : élèves de ses classes
        if hasattr(user, "teacher"):
            teacher = getattr(user, "teacher", None)
            if teacher is None:
                return Student.objects.none()
            # suppose que tu as un modèle ClassSubject qui relie teacher <-> school_class
            return Student.objects.filter(
                school_class__classsubject__teacher=teacher
            ).distinct()

        # --- Autres rôles : rien
        return Student.objects.none()


# ----------------------------
# LEVELS
# ----------------------------
class LevelViewSet(viewsets.ModelViewSet):
    queryset = Level.objects.all()
    serializer_class = LevelSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from .models import SchoolClass
from .serializers import SchoolClassSerializer, SchoolClassListSerializer
from .permissions import IsAdminOrReadOnly

class SchoolClassViewSet(viewsets.ModelViewSet):
    queryset = SchoolClass.objects.all()
    serializer_class = SchoolClassSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    # --- DÉSACTIVATION DE LA PAGINATION POUR CE VIEWSET (comme demandé) ---
    pagination_class = None

    def get_queryset(self):
        user = self.request.user

        # LIST : réponse légère (pas de prefetch students/teachers)
        if self.action == "list":
            # select_related level pour infos basiques, pas de prefetch des relations lourdes
            return SchoolClass.objects.select_related("level").all()

        # Autres actions (retrieve, update...) : précharge relations pour éviter N+1
        qs = SchoolClass.objects.select_related("level").prefetch_related(
            "students__user",
            "teachers__user"
        )

        if user.is_staff or user.is_superuser:
            return qs

        if hasattr(user, "teacher"):
            return qs.filter(teachers=user.teacher)

        if hasattr(user, "parent"):
            return qs.filter(students__parent=user.parent).distinct()

        if hasattr(user, "student"):
            return qs.filter(students=user.student)

        return qs.none()

    def get_serializer_class(self):
        # list -> serializer léger ; sinon -> serializer détaillé (nom existant conservé)
        if self.action == "list":
            return SchoolClassListSerializer
        return SchoolClassSerializer
# ----------------------------
# SUBJECTS
# ----------------------------
class SubjectViewSet(viewsets.ModelViewSet):
    queryset = Subject.objects.all()
    serializer_class = SubjectSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    pagination_class = None


# ----------------------------
# CLASS-SUBJECT (liaisons)
# ----------------------------
class ClassSubjectViewSet(viewsets.ModelViewSet):
    """
    Gère les associations entre classes et matières :
    - Liste complète
    - Liste des matières d'une classe
    - Détail d'une matière spécifique d'une classe
    - Création, modification, suppression
    """
    queryset = ClassSubject.objects.all()
    serializer_class = ClassSubjectSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    # FILTRAGE DE BASE
    def get_queryset(self):
        user = self.request.user

        if user.is_staff or user.is_superuser:
            return ClassSubject.objects.all()

        if hasattr(user, "teacher"):
            return ClassSubject.objects.filter(
                school_class__in=user.teacher.classes.all()
            )

        if hasattr(user, "parent"):
            parent = user.parent
            return ClassSubject.objects.filter(
                school_class__in=parent.students.values_list("school_class", flat=True)
            ).distinct()

        if hasattr(user, "student"):
            student = user.student
            if not student or not student.school_class:
                return ClassSubject.objects.none()
            return ClassSubject.objects.filter(school_class=student.school_class)

        return ClassSubject.objects.none()

    # CRUD ADMIN
    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_staff and not user.is_superuser:
            raise PermissionDenied("Seuls les administrateurs peuvent créer des liaisons de matières.")
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        if not user.is_staff and not user.is_superuser:
            raise PermissionDenied("Seuls les administrateurs peuvent modifier des liaisons de matières.")
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_staff and not user.is_superuser:
            raise PermissionDenied("Seuls les administrateurs peuvent supprimer des liaisons de matières.")
        instance.delete()

    # LISTE PAR CLASSE
    @action(detail=False, methods=["get"], url_path=r'by-class/(?P<class_id>\d+)')
    def by_class(self, request, class_id=None):
        try:
            school_class = SchoolClass.objects.get(id=class_id)
        except SchoolClass.DoesNotExist:
            raise NotFound("Classe introuvable.")

        class_subjects = ClassSubject.objects.filter(school_class=school_class)
        serializer = self.get_serializer(class_subjects, many=True)
        return Response(serializer.data)

    # LISTE PAR MATIÈRE
    @action(detail=False, methods=["get"], url_path=r'by-subject/(?P<subject_id>\d+)')
    def by_subject(self, request, subject_id=None):
        try:
            subject = Subject.objects.get(id=subject_id)
        except Subject.DoesNotExist:
            raise NotFound("Matière introuvable.")

        class_subjects = ClassSubject.objects.filter(subject=subject)
        serializer = self.get_serializer(class_subjects, many=True)
        return Response(serializer.data)

    # PAR CLASSE + MATIÈRE
    @action(detail=False, methods=["get", "patch", "delete"], url_path=r'by-class-subject/(?P<class_id>\d+)/(?P<subject_id>\d+)')
    def by_class_subject(self, request, class_id=None, subject_id=None):
        try:
            class_subject = ClassSubject.objects.get(
                school_class_id=class_id, subject_id=subject_id
            )
        except ClassSubject.DoesNotExist:
            raise NotFound("Association classe-matière introuvable.")

        # GET
        if request.method == "GET":
            serializer = self.get_serializer(class_subject)
            return Response(serializer.data)

        # PATCH
        elif request.method == "PATCH":
            if not (request.user.is_staff or request.user.is_superuser):
                raise PermissionDenied("Seuls les administrateurs peuvent modifier cette matière.")
            serializer = self.get_serializer(class_subject, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

        # DELETE
        elif request.method == "DELETE":
            if not (request.user.is_staff or request.user.is_superuser):
                raise PermissionDenied("Seuls les administrateurs peuvent supprimer cette matière.")
            class_subject.delete()
            return Response({"detail": "Liaison supprimée avec succès."}, status=status.HTTP_204_NO_CONTENT)

class DraftGradeViewSet(viewsets.ModelViewSet):
    """
    CRUD sur les brouillons de notes (DraftGrade).
    - Les profs peuvent CRUD uniquement leurs brouillons (pour leurs classes et leur matière).
    - Les admins peuvent tout.
    - Endpoint additionnel `POST /api/draft-grades/submit/` pour soumettre définitivement les brouillons.
    """
    queryset = DraftGrade.objects.all()
    serializer_class = DraftGradeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["student", "subject", "term"]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return DraftGrade.objects.all()
        if hasattr(user, "teacher"):
            teacher = user.teacher
            # drafts created by this teacher, limited to his classes & subject for safety
            return DraftGrade.objects.filter(teacher=teacher, student__school_class__in=teacher.classes.all(), subject=teacher.subject)
        if hasattr(user, "parent"):
            # parent can view drafts for their children (read-only)
            return DraftGrade.objects.filter(student__parent=user.parent)
        if hasattr(user, "student"):
            # student can view their drafts (rare), mostly not used
            return DraftGrade.objects.filter(student=user.student)
        return DraftGrade.objects.none()

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "teacher") and not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Vous devez être professeur pour créer des brouillons de notes.")

        teacher = user.teacher if hasattr(user, "teacher") else None
        student = serializer.validated_data["student"]
        subject = serializer.validated_data["subject"]
        term = serializer.validated_data["term"]

        # permission checks
        if not (user.is_staff or user.is_superuser):
            # la matière doit être la matière du prof
            if subject != teacher.subject:
                raise PermissionDenied("Vous ne pouvez saisir des notes que pour votre matière.")
            # l'élève doit être dans les classes du prof
            if student.school_class not in teacher.classes.all():
                raise PermissionDenied("Vous ne pouvez saisir des notes que pour vos élèves.")

        # require at least one numeric field when creating
        note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]
        if not any(serializer.validated_data.get(f) is not None for f in note_fields):
            raise serializers.ValidationError("Au moins une note doit être fournie dans le brouillon.")

        # Upsert behaviour: si le même draft existe pour (teacher,student,subject,term) -> update
        existing = DraftGrade.objects.filter(teacher=teacher, student=student, subject=subject, term=term).first() if teacher else None
        if existing:
            # update existing fields
            for k, v in serializer.validated_data.items():
                setattr(existing, k, v)
            existing.save()
            # bind instance so DRF returns it
            serializer.instance = existing
            return

        # else create
        serializer.save(teacher=teacher)

    def perform_update(self, serializer):
        user = self.request.user
        instance = serializer.instance

        if not (user.is_staff or user.is_superuser):
            if not hasattr(user, "teacher"):
                raise PermissionDenied("Permission refusée.")
            teacher = user.teacher
            if instance.teacher != teacher:
                raise PermissionDenied("Vous ne pouvez modifier que vos propres brouillons.")
            if instance.student.school_class not in teacher.classes.all():
                raise PermissionDenied("Vous ne pouvez modifier que vos propres élèves.")
            if instance.subject != teacher.subject:
                raise PermissionDenied("Vous ne pouvez modifier que votre matière.")

        # require at least one numeric field on update as well (unless you want to allow clearing)
        note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]
        if not any((serializer.validated_data.get(f) is not None) or (getattr(instance, f) is not None) for f in note_fields):
            raise serializers.ValidationError("Au moins une note doit être présente dans le brouillon.")

        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not (user.is_staff or user.is_superuser):
            if not hasattr(user, "teacher") or instance.teacher != user.teacher:
                raise PermissionDenied("Vous ne pouvez supprimer que vos brouillons.")
        instance.delete()

    @action(detail=False, methods=["post"], url_path="submit")
    def submit(self, request):
        """
        Soumet définitivement les brouillons du prof.
        Corps attendu: { "term": "T1", "school_class": <id optional> }
        - Crée les enregistrements Grade de façon atomique.
        - Si un Grade final existe déjà pour un étudiant+matière+term, la soumission échoue (retourne 400 avec détails).
        - Supprime les DraftGrade créés.
        """
        user = request.user
        if not hasattr(user, "teacher") and not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Seuls les profs peuvent soumettre des brouillons.")

        teacher = user.teacher
        term = request.data.get("term")
        school_class_id = request.data.get("school_class", None)

        if term not in dict(DraftGrade.TERM_CHOICES).keys():
            return Response({"detail": "Champ 'term' manquant ou invalide."}, status=status.HTTP_400_BAD_REQUEST)

        # collect drafts to submit
        drafts_qs = DraftGrade.objects.filter(teacher=teacher, term=term, subject=teacher.subject)
        if school_class_id:
            drafts_qs = drafts_qs.filter(student__school_class_id=school_class_id)

        drafts = list(drafts_qs.select_related("student", "subject"))
        if not drafts:
            return Response({"detail": "Aucun brouillon à soumettre pour les critères fournis."}, status=status.HTTP_400_BAD_REQUEST)

        # check for existing final grades that would collide
        collisions = []
        for d in drafts:
            if Grade.objects.filter(student=d.student, subject=d.subject, term=d.term).exists():
                collisions.append({
                    "student_id": d.student.id,
                    "student_name": d.student.user.get_full_name(),
                    "subject_id": d.subject.id,
                    "term": d.term
                })
        if collisions:
            return Response({"detail": "Certains élèves ont déjà des notes finales pour ce (subject, term).", "collisions": collisions}, status=status.HTTP_400_BAD_REQUEST)

        created = []
        errors = []
        # atomic creation: create Grade objects (use save() to compute averages)
        try:
            with transaction.atomic():
                for d in drafts:
                    # double-check permissions per draft
                    if d.student.school_class not in teacher.classes.all():
                        errors.append({"student_id": d.student.id, "error": "Élève hors de vos classes."})
                        continue
                    if d.subject != teacher.subject:
                        errors.append({"student_id": d.student.id, "error": "Matière différente de la vôtre."})
                        continue

                    # require at least one note
                    if not any(getattr(d, f) is not None for f in ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]):
                        errors.append({"student_id": d.student.id, "error": "Aucune note fournie dans le brouillon."})
                        continue

                    g = Grade(
                        student=d.student,
                        subject=d.subject,
                        term=d.term,
                        interrogation1=d.interrogation1,
                        interrogation2=d.interrogation2,
                        interrogation3=d.interrogation3,
                        devoir1=d.devoir1,
                        devoir2=d.devoir2,
                    )
                    # save triggers calculate_averages()
                    g.save()
                    created.append({"grade_id": g.id, "student_id": d.student.id})
                # if any errors collected, rollback entire transaction to avoid partial submits
                if errors:
                    raise IntegrityError("Validation errors in drafts; abort transaction.")
                # delete drafts after successful creation
                DraftGrade.objects.filter(id__in=[d.id for d in drafts]).delete()
        except IntegrityError as e:
            return Response({"detail": "Soumission annulée.", "errors": errors or str(e)}, status=status.HTTP_400_BAD_REQUEST)
        # success: optionally notify admin or run post-commit tasks
        # ex: transaction.on_commit(lambda: notif_service.notify_submission(teacher.id, created))
        return Response({"created": created}, status=status.HTTP_201_CREATED)
# ----------------------------
# GRADE
# ----------------------------
# imports utiles (si pas déjà présents en haut du fichier)
from django.db import transaction
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action

# grades/views.py (updated GradeViewSet - only show modified class)
from django.db import transaction
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action
from django.utils import timezone

from .models import Grade
from .serializers import GradeSerializer, GradeBulkLineSerializer
from rest_framework.permissions import IsAuthenticated
from notifications import service as notif_service

class GradeViewSet(viewsets.ModelViewSet):
    queryset = Grade.objects.all()
    serializer_class = GradeSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_class = GradeFilter
    pagination_class = None

    # ... get_queryset stays the same ...

    @action(detail=False, methods=["post"], url_path="bulk_upsert")
    def bulk_upsert(self, request):
        payload = request.data
        if not isinstance(payload, list):
            return Response({"detail": "Payload must be a list of objects."}, status=status.HTTP_400_BAD_REQUEST)

        MAX_LINES = 1000
        if len(payload) > MAX_LINES:
            return Response({"detail": f"Too many items (max {MAX_LINES})."}, status=status.HTTP_400_BAD_REQUEST)

        results = []
        created = updated = errors = 0
        user = request.user

        notify_list = []  # collect (grade_id, 'created'|'updated') to notify after commit

        with transaction.atomic():
            for idx, item in enumerate(payload):
                serializer = GradeBulkLineSerializer(data=item)
                if not serializer.is_valid():
                    errors += 1
                    results.append({
                        "index": idx,
                        "input": item,
                        "status": "error",
                        "errors": serializer.errors
                    })
                    continue

                valid = serializer.validated_data
                student = valid.get("student")
                subject = valid.get("subject")
                term = valid.get("term")
                line_id = valid.get("id", None)

                # detect provided fields
                provided_keys = set(item.keys())
                note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]

                defaults = {}
                for f in note_fields:
                    if f in provided_keys:
                        defaults[f] = valid.get(f)

                if "term" in provided_keys:
                    defaults["term"] = term

                if not defaults and not line_id:
                    errors += 1
                    results.append({
                        "index": idx,
                        "input": item,
                        "status": "error",
                        "errors": "No updatable fields provided (nothing to create/update)."
                    })
                    continue

                # Permission checks
                if not (user.is_staff or user.is_superuser):
                    if hasattr(user, "teacher"):
                        allowed_classes = user.teacher.classes.all()
                        if student.school_class not in allowed_classes:
                            errors += 1
                            results.append({
                                "index": idx,
                                "student_id": getattr(student, "id", None),
                                "status": "error",
                                "errors": "Permission denied for this student.",
                            })
                            continue
                    else:
                        errors += 1
                        results.append({
                            "index": idx,
                            "student_id": getattr(student, "id", None),
                            "status": "error",
                            "errors": "Permission denied.",
                        })
                        continue

                try:
                    if line_id:
                        try:
                            g = Grade.objects.select_for_update().get(id=line_id)
                        except Grade.DoesNotExist:
                            errors += 1
                            results.append({
                                "index": idx,
                                "student_id": student.id,
                                "status": "error",
                                "errors": "Grade id not found.",
                            })
                            continue

                        if str(g.student.id) != str(student.id):
                            errors += 1
                            results.append({
                                "index": idx,
                                "student_id": student.id,
                                "status": "error",
                                "errors": "Mismatched student for grade id.",
                            })
                            continue

                        for k, v in defaults.items():
                            setattr(g, k, v)
                        # to avoid double-signal: if you have signals, you can set _suppress_notifications
                        setattr(g, '_suppress_notifications', True)
                        g.save()
                        updated += 1
                        results.append({
                            "index": idx,
                            "student_id": student.id,
                            "subject_id": subject.id,
                            "status": "updated",
                            "id": g.id,
                            "average_interro": g.average_interro,
                            "average_subject": g.average_subject,
                            "average_coeff": g.average_coeff,
                        })
                        notify_list.append((g.id, 'updated'))

                    else:
                        g, created_flag = Grade.objects.select_for_update().update_or_create(
                            student=student,
                            subject=subject,
                            term=term,
                            defaults=defaults
                        )
                        # suppress signals if used
                        setattr(g, '_suppress_notifications', True)
                        g.save()
                        if created_flag:
                            created += 1
                            results.append({
                                "index": idx,
                                "student_id": student.id,
                                "subject_id": subject.id,
                                "status": "created",
                                "id": g.id,
                                "average_interro": g.average_interro,
                                "average_subject": g.average_subject,
                                "average_coeff": g.average_coeff,
                            })
                            notify_list.append((g.id, 'created'))
                        else:
                            updated += 1
                            results.append({
                                "index": idx,
                                "student_id": student.id,
                                "subject_id": subject.id,
                                "status": "updated",
                                "id": g.id,
                                "average_interro": g.average_interro,
                                "average_subject": g.average_subject,
                                "average_coeff": g.average_coeff,
                            })
                            notify_list.append((g.id, 'updated'))

                except Exception as e:
                    errors += 1
                    results.append({
                        "index": idx,
                        "student_id": getattr(student, "id", None),
                        "subject_id": getattr(subject, "id", None),
                        "status": "error",
                        "errors": str(e)
                    })

        # after commit: run notifications (non-blocking relative to DB commit)
        if notify_list:
            # import and call in on_commit to ensure DB transaction is committed
            transaction.on_commit(lambda: notif_service.bulk_notify_grades(notify_list))

        return Response({"created": created, "updated": updated, "errors": errors, "results": results})


# ----------------------------
# CLASS SCHEDULE (CRUD)
# ----------------------------
from rest_framework import viewsets, filters, status
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from .models import ClassScheduleEntry
from .serializers import ClassScheduleEntrySerializer

# views.py
from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q

from django_filters.rest_framework import DjangoFilterBackend

from academics.models import ClassScheduleEntry
from .serializers import ClassScheduleEntrySerializer

class ClassScheduleEntryViewSet(viewsets.ModelViewSet):
    """
    CRUD pour les créneaux (utilisé par admin/staff).
    - queryset optimisé avec select_related pour éviter N+1.
    - permissions : IsAuthenticated (restreindre à admin si besoin).
    """
    queryset = ClassScheduleEntry.objects.select_related(
        "school_class",
        "subject",
        "teacher__user",
    ).all()
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user

        # Admin / staff : Tout voir
        if user.is_staff or user.is_superuser:
            return self.queryset

        # Enseignant : Voir ses propres cours
        if hasattr(user, "teacher"):
            return self.queryset.filter(teacher=user.teacher)

        # Les autres (élèves/parents) n'ont pas accès en écriture via ce ViewSet
        return ClassScheduleEntry.objects.none()


class TimetableViewSet(viewsets.ReadOnlyModelViewSet):
    """
    End-point read-only optimisé pour l'affichage (Calendrier).
    - pagination_class = None (retourne un array)
    - queryset pré-optimisé (select_related)
    - filtres : school_class, teacher, weekday
    - protection : n'autorise pas une liste massive non filtrée pour les staff
    """
    pagination_class = None

    queryset = ClassScheduleEntry.objects.select_related(
        "school_class",
        "subject",
        "teacher__user",
    ).all()
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["school_class", "teacher", "weekday"]
    search_fields = [
        "school_class__name",
        "subject__name",
        "teacher__user__last_name",
        "teacher__user__first_name",
    ]
    ordering_fields = ["weekday", "starts_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        params = self.request.query_params

        # --- PROTECTION contre réponses massives non filtrées ---
        # Si action=list et aucun filtre significatif n'est fourni, on renvoie rien
        # pour les comptes staff (évite timeout/oom). Les parents/élèves/enseignants
        # sont déjà restreints plus bas.
        if self.action == "list":
            class_param = params.get("class_id") or params.get("school_class")
            teacher_param = params.get("teacher_id") or params.get("teacher")
            weekday = params.get("weekday")

            if (user.is_staff or user.is_superuser) and not any([class_param, teacher_param, weekday]):
                return qs.none()

        # --- A. LOGIQUE D'ACCES ---
        if user.is_staff or user.is_superuser:
            has_full_access = True
        else:
            has_full_access = False

        allowed_class_ids = set()
        if not has_full_access:
            if hasattr(user, "student") and getattr(user.student, "school_class", None):
                allowed_class_ids.add(user.student.school_class.id)

            if hasattr(user, "parent"):
                child_classes = list(user.parent.students.values_list("school_class_id", flat=True))
                allowed_class_ids.update([c for c in child_classes if c is not None])

        if not has_full_access:
            if hasattr(user, "teacher"):
                teacher = user.teacher
                teacher_class_ids = list(teacher.classes.values_list("id", flat=True))
                qs = qs.filter(Q(teacher=teacher) | Q(school_class__id__in=teacher_class_ids)).distinct()
            else:
                if not allowed_class_ids:
                    return qs.none()
                qs = qs.filter(school_class__id__in=allowed_class_ids)

        # --- B. FILTRES MANUELS (Query Params) ---
        # Filtre par classe (id ou name)
        class_param = params.get("class_id") or params.get("school_class")
        if class_param:
            if str(class_param).isdigit():
                qs = qs.filter(school_class__id=int(class_param))
            else:
                qs = qs.filter(school_class__name__iexact=class_param)

        # Filtre par prof (id ou username/nom)
        teacher_param = params.get("teacher_id") or params.get("teacher")
        if teacher_param:
            if str(teacher_param).isdigit():
                qs = qs.filter(teacher__id=int(teacher_param))
            else:
                qs = qs.filter(
                    Q(teacher__user__username__iexact=teacher_param) |
                    Q(teacher__user__last_name__iexact=teacher_param)
                )

        # Filtre par weekday
        weekday = params.get("weekday")
        if weekday is not None and weekday != "":
            try:
                qs = qs.filter(weekday=int(weekday))
            except (ValueError, TypeError):
                pass

        # Order final
        return qs.order_by("weekday", "starts_at")
# ----------------------------
# REPORT CARDS
# ----------------------------
def _parse_bool(val: str) -> bool:
    if val is None:
        return False
    return str(val).lower() in ("1", "true", "yes", "y", "on")


from django.db.models import Q

# academics/views.py (ou le fichier où est défini ReportCardViewSet)
import time
import logging
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)

# academics/views.py

# 1. On renomme 'time' en 'std_time' pour éviter TOUT conflit avec datetime.time
import time as std_time 
import logging

from django.core.cache import cache
from django.apps import apps  # <--- INDISPENSABLE pour apps.get_model
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

# Assurez-vous que ces imports existent dans votre projet :
# from .utils import compute_report_cards_from_grades 
# from .serializers import ReportCardSerializer

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60  # ajustez si tu veux — 0 pour désactiver

def _parse_bool(val: str) -> bool:
    if val is None:
        return False
    return str(val).lower() in ("1", "true", "yes", "y", "on")

# views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from datetime import datetime

# Assure-toi d'importer tes modèles
from .models import ClassScheduleEntry, StudentAttendance

class DailyAttendanceSheetView(APIView):
    """
    Récupère la structure pour la prise de présence d'une classe à une date donnée.
    GET /api/attendance/sheet/?class_id=X&date=YYYY-MM-DD
    """
    
    def get(self, request):
        class_id = request.query_params.get('class_id')
        date_str = request.query_params.get('date')

        if not class_id or not date_str:
            return Response({"error": "class_id and date are required"}, status=400)

        # 1. Analyser la date
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            # Attention : En Python .weekday() donne 0=Lundi, 6=Dimanche.
            # Vérifie si ton modèle 'weekday' utilise 0 ou 1 pour Lundi.
            # Ici je suppose que ton modèle suit python (0=Lundi).
            weekday = target_date.weekday() 
        except ValueError:
            return Response({"error": "Invalid date format"}, status=400)

        # 2. Récupérer les cours prévus ce jour-là (Les colonnes du tableau)
        schedule_entries = ClassScheduleEntry.objects.filter(
            school_class_id=class_id,
            weekday=weekday
        ).select_related('subject', 'teacher__user').order_by('starts_at')

        # S'il n'y a pas cours ce jour-là
        if not schedule_entries.exists():
             return Response({
                "message": "Aucun cours prévu ce jour pour cette classe",
                "schedule": [],
                "students": [],
                "absences": []
            })

        # 3. Récupérer les élèves de la classe (Les lignes du tableau)
        students = Student.objects.filter(school_class_id=class_id).order_by('user__last_name')

        # 4. Récupérer les absences DÉJÀ enregistrées ce jour-là (Les cases cochées)
        existing_absences = StudentAttendance.objects.filter(
            date=target_date,
            schedule_entry__in=schedule_entries
        )

        # --- Construction de la réponse JSON optimisée ---

        # A. Liste des cours (Colonnes)
        schedule_data = [
            {
                "id": entry.id,
                "subject": entry.subject.name,
                "starts_at": entry.starts_at,
                "ends_at": entry.ends_at,
                "teacher": f"{entry.teacher.user.last_name}" if entry.teacher else "N/A"
            }
            for entry in schedule_entries
        ]

        # B. Liste des élèves (Lignes)
        student_data = [
            {
                "id": s.id,
                "name": f"{s.user.last_name} {s.user.first_name}", # Adapte selon ton modèle Student
            }
            for s in students
        ]

        # C. Map des absences existantes (Pour pré-remplir le tableau)
        # On renvoie une liste simple pour que le front puisse matcher facilement
        attendance_data = [
            {
                "id": att.id, # ID de l'absence (utile pour DELETE)
                "student_id": att.student_id,
                "schedule_entry_id": att.schedule_entry_id,
                "status": att.status
            }
            for att in existing_absences
        ]

        return Response({
            "date": date_str,
            "weekday": weekday,
            "schedule": schedule_data,
            "students": student_data,
            "absences": attendance_data
        })
class ReportCardViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class = None

    # --- Helpers pour récupérer les modèles proprement ---
    @property
    def Grade(self):
        return apps.get_model('academics', 'Grade')

    @property
    def Student(self):
        return apps.get_model('core', 'Student') 

    def _get_teacher_students_qs(self, teacher):
        """
        Récupère les élèves liés à un teacher de façon robuste.
        """
        # 1. Via la relation directe students
        if hasattr(teacher, 'students'):
            return teacher.students.all()

        # 2. Via les classes enseignées
        if hasattr(teacher, 'classes'):
            classes_qs = teacher.classes.all()
            if classes_qs.exists():
                return self.Student.objects.filter(school_class__in=classes_qs)

        # 3. Via les notes (Grade) existantes
        try:
            student_ids = self.Grade.objects.filter(teacher=teacher).values_list("student_id", flat=True).distinct()
            return self.Student.objects.filter(pk__in=student_ids)
        except Exception:
            pass

        return None

    def _determine_class_ids_for_ranking(self, request, user, class_id_param, student_id_param):
        """
        Retourne un set d'IDs de classes pour le calcul des rangs.
        """
        # 1) class_id param explicite
        if class_id_param:
            return {int(class_id_param)}

        # 2) si student_id fourni -> trouver sa classe
        if student_id_param:
            try:
                s = self.Student.objects.select_related("school_class").get(pk=student_id_param)
                if s.school_class_id:
                    return {s.school_class_id}
            except Exception: # Catch large pour éviter crash si DoesNotExist ou autre
                pass 

        # 3) Rôle utilisateur (Student)
        if hasattr(user, "student") and getattr(user.student, 'school_class_id', None):
            return {user.student.school_class_id}

        # 4) Rôle utilisateur (Parent)
        if hasattr(user, "parent"):
            # On récupère toutes les classes de tous les enfants du parent
            classes = user.parent.students.values_list("school_class_id", flat=True).distinct()
            return set(int(cid) for cid in classes if cid is not None)

        # 5) Rôle utilisateur (Teacher)
        if hasattr(user, "teacher"):
            # Priorité aux classes assignées
            if hasattr(user.teacher, 'classes'):
                classes_qs = user.teacher.classes.all()
                if classes_qs.exists():
                    return set(int(c.pk) for c in classes_qs)
            
            # Fallback via les élèves qu'il note
            teacher_students = self._get_teacher_students_qs(user.teacher)
            if teacher_students is not None:
                return set(int(cid) for cid in teacher_students.values_list("school_class_id", flat=True).distinct() if cid is not None)

        # 6) Pas de contrainte (Admin ou cas non géré) -> None signifie "Toutes les classes"
        return None

    def list(self, request):
        user = request.user

        # Récupération des paramètres
        student_id = request.query_params.get("student_id")
        class_id = request.query_params.get("class_id")
        term = request.query_params.get("term")
        
        include_missing_subjects = _parse_bool(request.query_params.get("include_missing_subjects"))
        full_weighting = _parse_bool(request.query_params.get("full_weighting"))

        # ---------- 1. Déterminer le contexte (Classes) ----------
        class_ids_for_ranking = self._determine_class_ids_for_ranking(request, user, class_id, student_id)

        # ---------- 2. Construire le queryset des Notes ----------
        ranking_grades_qs = self.Grade.objects.select_related("student", "student__school_class", "subject")
        
        if term:
            ranking_grades_qs = ranking_grades_qs.filter(term__iexact=term)

        if class_ids_for_ranking is not None:
            ranking_grades_qs = ranking_grades_qs.filter(student__school_class__id__in=class_ids_for_ranking)

        # Tri pour stabilité du calcul
        ranking_grades_qs = ranking_grades_qs.order_by("student_id", "term")

        # ---------- 3. Caching (Optionnel) ----------
        cache_key = None
        ranking_report_cards = None
        
        # (Logique de cache simplifiée...)
        # try:
        #     if CACHE_TTL_SECONDS > 0:
        #         ids_key = "ALL" if class_ids_for_ranking is None else ",".join(str(x) for x in sorted(class_ids_for_ranking))
        #         cache_key = f"report_cards:{ids_key}:{term}:{include_missing_subjects}"
        #         ranking_report_cards = cache.get(cache_key)
        # except Exception:
        #     pass

        # ---------- 4. Calcul (Lourd) ----------
        if ranking_report_cards is None:
            # UTILISATION DU NOM SÉCURISÉ std_time
            t0 = std_time.time()
            
            # IMPORTANT: Assurez-vous que cette fonction est importée en haut du fichier
            # from .utils import compute_report_cards_from_grades
            ranking_report_cards = compute_report_cards_from_grades(
                ranking_grades_qs,
                include_missing_subjects=include_missing_subjects,
                full_weighting=full_weighting,
            )
            
            duration = std_time.time() - t0
            logger.info(f"compute_report_cards (ranking) took {duration:.2f}s")

            # if cache_key and CACHE_TTL_SECONDS > 0:
            #     cache.set(cache_key, ranking_report_cards, CACHE_TTL_SECONDS)

        # ---------- 5. Filtrage final (Qui voit quoi ?) ----------
        filtered_report_cards = ranking_report_cards

        if user.is_staff or user.is_superuser:
            pass 
            
        elif hasattr(user, "student"):
            s_pk = str(user.student.pk)
            filtered_report_cards = [r for r in ranking_report_cards if str(r["student"].pk) == s_pk]
            
        elif hasattr(user, "parent"):
            child_ids = set(str(pk) for pk in user.parent.students.values_list("pk", flat=True))
            filtered_report_cards = [r for r in ranking_report_cards if str(r["student"].pk) in child_ids]
            
        elif hasattr(user, "teacher"):
            teacher_students = self._get_teacher_students_qs(user.teacher)
            if teacher_students:
                t_ids = set(str(pk) for pk in teacher_students.values_list("pk", flat=True))
                filtered_report_cards = [r for r in ranking_report_cards if str(r["student"].pk) in t_ids]
            else:
                filtered_report_cards = []

        if student_id:
            filtered_report_cards = [r for r in filtered_report_cards if str(r["student"].pk) == str(student_id)]

        filtered_report_cards.sort(key=lambda x: (str(x["student"]).lower(), x.get("term", "")))

        # IMPORTANT: Assurez-vous que le serializer est importé
        # from .serializers import ReportCardSerializer
        serializer = ReportCardSerializer(filtered_report_cards, many=True, context={"request": request})
        return Response(serializer.data)
# academics/views.py

from rest_framework import viewsets
from .models import StudentAttendance
from .serializers import StudentAttendanceSerializer

from django.apps import apps
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from .models import StudentAttendance
from .serializers import StudentAttendanceSerializer

# attendance/viewsets.py
import logging
from django.apps import apps
from django.db import transaction
from rest_framework import viewsets, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import StudentAttendance
from .serializers import StudentAttendanceSerializer

logger = logging.getLogger(__name__)


class StudentAttendanceViewSet(viewsets.ModelViewSet):
    """
    CRUD pour StudentAttendance. Lors de la création d'une absence,
    on notifie les parents / contact(s) de l'élève.
    """
    queryset = StudentAttendance.objects.all()
    serializer_class = StudentAttendanceSerializer
    permission_classes = [IsAuthenticated]

    def _get_notifications_models(self):
        """
        Return Notification models + send_notification function if available,
        otherwise return (None, None, None, None).
        """
        try:
            Notification = apps.get_model("notifications", "Notification")
            NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
            UserNotificationPreference = apps.get_model("notifications", "UserNotificationPreference")
            from notifications.delivery import send_notification as _send
            send_notification = _send
            return Notification, NotificationTemplate, UserNotificationPreference, send_notification
        except Exception as e:
            logger.debug("Notifications app not available / import failed: %s", e)
            return None, None, None, None

    def _iter_student_parents(self, student):
        """
        Retourne un itérable de 'parent objects' compatibles, en essayant :
        - student.parent (FK unique)
        - student.parents (m2m / related manager)
        - fallback : empty list
        Chaque parent retourné doit avoir un attribut .user (sinon on le skipera).
        """
        # 1) FK single parent
        if hasattr(student, "parent") and getattr(student, "parent") is not None:
            yield student.parent
            return

        # 2) common m2m / related manager
        if hasattr(student, "parents"):
            try:
                qs = student.parents
                if hasattr(qs, "all"):
                    for p in qs.all():
                        yield p
                    return
                else:
                    # maybe it's a single object
                    yield qs
                    return
            except Exception:
                pass

        # 3) try other common names if ever present
        for attr in ("guardians", "guardian", "contacts", "family"):
            if hasattr(student, attr):
                val = getattr(student, attr)
                if hasattr(val, "all"):
                    for p in val.all():
                        yield p
                else:
                    yield val
                return

        # nothing found
        return

    def create(self, request, *args, **kwargs):
        """
        Save attendance record, then create notifications for parent(s).
        Best-effort: notifications will not block the main request on failure.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        absence = serializer.save()

        logger.info("Attendance created id=%s student=%s date=%s by=%s",
                    getattr(absence, "id", None),
                    getattr(absence, "student_id", None),
                    getattr(absence, "date", None),
                    getattr(request.user, "username", None))

        Notification, NotificationTemplate, UserNotificationPreference, send_notification = self._get_notifications_models()
        if not Notification:
            # notifications app absente -> on renvoie juste la ressource créée
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

        # ensure template exists (dev fallback)
        try:
            template, created = NotificationTemplate.objects.get_or_create(
                key="absence_reported",
                defaults={
                    "topic": "attendance",
                    "title_template": "Absence signalée — {{ student_name }}",
                    "body_template": "Bonjour {{ parent_name }}, {{ student_name }} a été signalé absent le {{ date }}{% if subject %} pour {{ subject }}{% endif %}.{% if reason %} Motif : {{ reason }}.{% endif %}",
                    "default_channels": ["inapp"]
                }
            )
            if created:
                logger.info("NotificationTemplate 'absence_reported' créé (fallback).")
        except Exception as e:
            template = None
            logger.exception("Erreur lors de get_or_create template absence_reported: %s", e)

        # student info
        student = absence.student
        try:
            student_name = student.user.get_full_name() if getattr(student, "user", None) else f"{getattr(student, 'first_name','')} {getattr(student, 'last_name','')}".strip()
        except Exception:
            student_name = str(getattr(student, "id", None))

        schedule_entry = getattr(absence, "schedule_entry", None)
        subject = getattr(getattr(schedule_entry, "subject", None), "name", None) if schedule_entry else None
        starts_at = getattr(schedule_entry, "starts_at", None) if schedule_entry else None

        # iterate parents discovered by helper
        parent_found = False
        for parent in self._iter_student_parents(student):
            parent_found = True
            user_obj = getattr(parent, "user", None)
            if not user_obj:
                logger.debug("Parent object %s has no linked user; skipping", getattr(parent, "id", None))
                continue

            # avoid duplicates
            q = Notification.objects.filter(
                topic="attendance",
                recipient_user=user_obj,
                payload__student_id=student.id,
                payload__date=str(absence.date),
            )
            if schedule_entry:
                q = q.filter(payload__schedule_entry_id=schedule_entry.id)

            if q.exists():
                logger.debug("Notif already exists for recipient %s student %s date %s", user_obj.id, student.id, absence.date)
                continue

            # default channels
            channels = (template.default_channels if template and getattr(template, "default_channels", None) else ["inapp"])

            # respect user preferences
            try:
                pref = UserNotificationPreference.objects.filter(user=user_obj, topic="attendance").first()
                if pref and not pref.enabled:
                    logger.debug("Parent user %s disabled attendance notifications", user_obj.id)
                    continue
                if pref and pref.channels:
                    channels = pref.channels
            except Exception:
                logger.exception("Erreur en lisant UserNotificationPreference pour user %s", getattr(user_obj, "id", None))

            payload = {
                "student_id": student.id,
                "student_name": student_name,
                "date": str(absence.date),
                "schedule_entry_id": schedule_entry.id if schedule_entry else None,
                "subject": subject,
                "starts_at": str(starts_at) if starts_at else None,
                "status": absence.status,
                "reason": absence.reason or None,
                "marked_by": (request.user.get_full_name() if getattr(request.user, "get_full_name", None) else getattr(request.user, "username", None)),
                "parent_id": getattr(user_obj, "id", None),
                "parent_name": (user_obj.get_full_name() if getattr(user_obj, "get_full_name", None) else getattr(user_obj, "username", None))
            }

            try:
                notif = Notification.objects.create(
                    template=template,
                    topic="attendance",
                    recipient_user=user_obj,
                    payload=payload,
                    channels=channels
                )
                logger.info("Notification created id=%s recipient=%s student=%s", getattr(notif, "id", None), getattr(user_obj, "id", None), getattr(student, "id", None))

                # schedule delivery after commit
                if send_notification:
                    try:
                        transaction.on_commit(lambda n=notif: send_notification(n))
                        logger.debug("Scheduled send_notification for notif %s", getattr(notif, "id", None))
                    except Exception:
                        # fallback synchronous send (best-effort)
                        try:
                            send_notification(notif)
                        except Exception as e:
                            logger.exception("Fallback send_notification failed for notif %s: %s", getattr(notif, "id", None), e)
            except Exception:
                logger.exception("Failed to create Notification for parent=%s student=%s", getattr(user_obj, "id", None), getattr(student, "id", None))

        # fallback: si aucun parent trouvé, on notifie l'utilisateur de l'élève (optionnel)
        if not parent_found:
            student_user = getattr(student, "user", None)
            if student_user:
                try:
                    channels = (template.default_channels if template and getattr(template, "default_channels", None) else ["inapp"])
                    payload = {
                        "student_id": student.id,
                        "student_name": student_name,
                        "date": str(absence.date),
                        "schedule_entry_id": schedule_entry.id if schedule_entry else None,
                        "subject": subject,
                        "status": absence.status,
                        "reason": absence.reason or None,
                        "marked_by": (request.user.get_full_name() if getattr(request.user, "get_full_name", None) else getattr(request.user, "username", None)),
                    }
                    notif = Notification.objects.create(
                        template=template,
                        topic="attendance",
                        recipient_user=student_user,
                        payload=payload,
                        channels=channels
                    )
                    if send_notification:
                        try:
                            transaction.on_commit(lambda n=notif: send_notification(n))
                        except Exception:
                            try:
                                send_notification(notif)
                            except Exception:
                                pass
                    logger.info("Fallback notification created for student user %s (no parents)", getattr(student_user, "id", None))
                except Exception:
                    logger.exception("Impossible de créer la fallback notification pour student user %s", getattr(student_user, "id", None))

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    # Tu peux ajouter des permissions ici si nécessaire
# ----------------------------
# SUBJECT COMMENTS
# ----------------------------
class SubjectCommentViewSet(viewsets.ModelViewSet):
    queryset = SubjectComment.objects.all()
    serializer_class = SubjectCommentSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["student", "subject", "term"]

    def get_queryset(self):
        user = self.request.user

        if user.is_staff or user.is_superuser:
            return SubjectComment.objects.all()

        if hasattr(user, "teacher"):
            teacher = user.teacher
            return SubjectComment.objects.filter(
                student__school_class__in=teacher.classes.all(),
                subject=teacher.subject
            )

        if hasattr(user, "parent"):
            return SubjectComment.objects.filter(student__parent=user.parent)

        if hasattr(user, "student"):
            return SubjectComment.objects.filter(student=user.student)

        return SubjectComment.objects.none()

    def perform_create(self, serializer):
        teacher = self.request.user.teacher

        student = serializer.validated_data["student"]
        subject = serializer.validated_data["subject"]
        term = serializer.validated_data["term"]

        if student.school_class not in teacher.classes.all():
            raise PermissionDenied("Vous ne pouvez commenter que vos propres élèves.")
        if subject != teacher.subject:
            raise PermissionDenied("Vous ne pouvez commenter que votre matière.")

        if SubjectComment.objects.filter(student=student, subject=subject, term=term).exists():
            raise serializers.ValidationError("Un commentaire pour cet élève, cette matière et ce trimestre existe déjà.")

        serializer.save(teacher=teacher)

    def perform_update(self, serializer):
        teacher = self.request.user.teacher
        instance = serializer.instance

        if instance.student.school_class not in teacher.classes.all():
            raise PermissionDenied("Vous ne pouvez modifier que les commentaires de vos propres élèves.")
        if instance.subject != teacher.subject:
            raise PermissionDenied("Vous ne pouvez modifier que votre matière.")

        serializer.save()


# ----------------------------
# TIMESLOTS
# ----------------------------
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

class TimeSlotViewSet(viewsets.ModelViewSet):
    """
    Gestion des créneaux horaires.
    Pagination désactivée (retourne toujours un tableau complet).
    """
    pagination_class = None  # 🔥 Désactive la pagination pour cet endpoint

    queryset = TimeSlot.objects.all().order_by("start_time")
    serializer_class = TimeSlotSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        user = self.request.user

        if user.is_staff or user.is_superuser:
            return self.queryset

        # Si plus tard tu veux filtrer par école / classe,
        # ajoute la logique ici.
        return TimeSlot.objects.none()

# ----------------------------
# GENERATE TIMETABLE (API)
# ----------------------------
class GenerateTimetableView(APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request, *args, **kwargs):
        reset_timetable_table()
        dry_run = request.data.get("dry_run", False)
        persist = request.data.get("persist", True)

        try:
            result = run_timetable_pipeline(dry_run=dry_run, persist=persist)
            return Response(result, status=status.HTTP_200_OK)
        except Exception as e:
            return Response(
                {"detail": f"Erreur lors de la génération : {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ----------------------------
# TIMETABLE VIEWSET (READ-ONLY) WITH ROBUST FILTERS + DEBUG
# ----------------------------
class TimetableViewSet(viewsets.ReadOnlyModelViewSet):
    """
    End-point read-only pour les emplois du temps.
    Supporte:
      - ?class_id= / ?school_class= / ?school_class_id=
      - ?teacher_id= / ?teacher=
      - ?level_id=
      - ?weekday=
    + search (SearchFilter) et ordering (OrderingFilter)
    + filterset_fields pour usage de django-filter côté client.
    """
    queryset = ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher")
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]

    # backends
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["school_class", "teacher", "weekday", "school_class__level"]
    search_fields = ["school_class__name", "subject__name", "teacher__user__last_name", "teacher__user__first_name"]
    ordering_fields = ["weekday", "starts_at"]

    from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend

from .models import ClassScheduleEntry
from .serializers import ClassScheduleEntrySerializer


from rest_framework import viewsets, filters
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from .models import ClassScheduleEntry
from .serializers import ClassScheduleEntrySerializer

class TimetableViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet pour les emplois du temps.
    Désactivation explicite de la pagination pour garantir que le frontend 
    reçoive la liste complète des cours sans avoir à gérer les pages.
    """
    queryset = ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher")
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]
    
    # --- CRITIQUE : Désactive la pagination globale pour cet endpoint ---
    pagination_class = None 

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["school_class", "teacher", "weekday", "school_class__level"]
    search_fields = ["school_class__name", "subject__name", "teacher__user__last_name", "teacher__user__first_name"]
    ordering_fields = ["weekday", "starts_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        user = self.request.user

        def clean_val(v):
            if v is None or v == "undefined" or v == "":
                return None
            s = str(v).strip()
            return s.rstrip('/')

        # Paramètres acceptés
        class_id = clean_val(params.get("class_id") or params.get("school_class") or params.get("school_class_id"))
        teacher_id = clean_val(params.get("teacher_id") or params.get("teacher"))
        level_id = clean_val(params.get("level_id") or params.get("school_class__level"))
        weekday = clean_val(params.get("weekday"))

        # --- Gestion des droits d'accès ---
        allowed_class_ids = None

        if user.is_staff or user.is_superuser:
            allowed_class_ids = None # Accès total
        elif hasattr(user, "student") and getattr(user.student, "school_class_id", None):
            allowed_class_ids = {user.student.school_class_id}
        elif hasattr(user, "parent"):
            allowed_class_ids = set(user.parent.students.values_list('school_class_id', flat=True).distinct())
        elif hasattr(user, "teacher"):
            teacher_obj = user.teacher
            allowed_class_ids = set()
            try:
                allowed_class_ids.update(teacher_obj.classes.values_list("pk", flat=True))
            except:
                pass
            # Fallback sur les entrées de cours existantes
            allowed_class_ids.update(
                ClassScheduleEntry.objects.filter(teacher=teacher_obj).values_list("school_class_id", flat=True).distinct()
            )
        else:
            allowed_class_ids = set()

        # Application du périmètre de sécurité
        if allowed_class_ids is not None:
            if not allowed_class_ids:
                return qs.none()
            qs = qs.filter(school_class_id__in=list(allowed_class_ids))

        # Application des filtres utilisateur dynamiques
        if class_id:
            if class_id.isdigit():
                qs = qs.filter(school_class_id=int(class_id))
            else:
                qs = qs.filter(school_class__name__icontains=class_id)

        if teacher_id:
            if teacher_id.isdigit():
                qs = qs.filter(teacher_id=int(teacher_id))
            else:
                qs = qs.filter(teacher__user__username=teacher_id)

        if level_id:
            if level_id.isdigit():
                qs = qs.filter(school_class__level_id=int(level_id))
            else:
                qs = qs.filter(school_class__level__name__icontains=level_id)

        if weekday and weekday.isdigit():
            qs = qs.filter(weekday=int(weekday))

        return qs.order_by("weekday", "starts_at") 
# Ajoute en haut de academics/views.py
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .timetable_conflicts import detect_teacher_conflicts, detect_and_resolve

class TimetableConflictsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        result = detect_teacher_conflicts()
        return Response(result, status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        dry_run = bool(request.data.get("dry_run", True))
        persist = bool(request.data.get("persist", False))
        # Safety: if persist True require staff
        if persist and not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les admins peuvent appliquer les résolutions."}, status=status.HTTP_403_FORBIDDEN)

        report = detect_and_resolve(dry_run=dry_run, persist=persist)
        return Response(report, status=status.HTTP_200_OK)
# academics/views.py (ou academics/api_views.py)
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from academics.services.schedule_checker import run_check

class ScheduleCheckView(APIView):
    """
    GET  /academics/schedule-check/?class_id=5&limit=20&verbose=1
    Returns JSON report produced by run_check().
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        params = request.query_params
        class_id = params.get("class_id")
        limit = params.get("limit")
        verbose = params.get("verbose")

        try:
            class_id_val = int(class_id) if class_id is not None else None
        except Exception:
            return Response({"detail": "class_id must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            limit_val = int(limit) if limit is not None else 10
        except Exception:
            return Response({"detail": "limit must be an integer"}, status=status.HTTP_400_BAD_REQUEST)

        verbose_val = str(verbose).lower() in ("1", "true", "yes", "on")

        try:
            report = run_check(class_id=class_id_val, limit=limit_val, verbose=verbose_val)
            return Response(report, status=status.HTTP_200_OK)
        except Exception as e:
            # Log if you want. Return 500 with message.
            return Response({"detail": f"Erreur lors de l'analyse: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
# academics/views_batch_timetable.py  (copier/coller dans academics/views.py si tu veux)
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, time
from collections import defaultdict

from django.db import transaction
from django.utils.dateparse import parse_time

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from academics.models import ClassScheduleEntry, TimeSlot
from .serializers import ClassScheduleEntrySerializer  # facultatif pour réponse détaillée


def _to_minutes_from_timeobj(t: time) -> int:
    return t.hour * 60 + t.minute


def _load_slots_ordered() -> List[Dict[str, Any]]:
    """
    Charge tous les TimeSlot ordonnés (par day, start_time) et renvoie une liste d'objets:
      [{ "idx": 0, "db_obj": <TimeSlot>, "weekday": ..., "start": minutes, "end": minutes, "dur": ... }, ...]
    L'index 'idx' correspond simplement à l'index dans la liste (utile pour target_slot_idx).
    """
    qs = list(TimeSlot.objects.all().order_by("day", "start_time", "end_time"))
    slots = []
    for idx, s in enumerate(qs):
        st = s.start_time
        et = s.end_time
        if st is None or et is None:
            continue
        start_min = _to_minutes_from_timeobj(st)
        end_min = _to_minutes_from_timeobj(et)
        if end_min <= start_min:
            continue
        slots.append({
            "idx": idx,
            "db_obj": s,
            "weekday": s.day,
            "start": start_min,
            "end": end_min,
            "dur": end_min - start_min,
        })
    return slots


def _parse_time_str_or_obj(s: Optional[str]) -> Optional[time]:
    if s is None:
        return None
    if isinstance(s, time):
        return s
    # accept "HH:MM" or "HH:MM:SS"
    t = parse_time(s)
    return t


def _overlaps(a_weekday: int, a_start: int, a_end: int, b_weekday: int, b_start: int, b_end: int) -> bool:
    if a_weekday != b_weekday:
        return False
    return (a_start < b_end) and (b_start < a_end)


class TimetableBatchValidateView(APIView):
    """
    POST /academics/timetable-batch-validate/
    Body:
      {
        "operations": [
          { "entry_id": int, "target_slot_idx": int } OR
          { "entry_id": int, "target_weekday": int, "target_start": "HH:MM", "target_end": "HH:MM" }
        ]
      }

    Retour:
      {
        "valid": bool,
        "errors": [...],
        "conflicts": { "teacher_conflicts": [...], "class_conflicts": [...] },
        "preview": { "entry_id": { "from": {...}, "to": {...} }, ... }
      }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        payload = request.data or {}
        ops = payload.get("operations")
        if not isinstance(ops, list):
            return Response({"detail": "operations doit être une liste d'opérations."}, status=status.HTTP_400_BAD_REQUEST)

        # load current entries implicated and all entries for conflict detection
        entry_ids = {int(op.get("entry_id")) for op in ops if op.get("entry_id") is not None}
        if not entry_ids:
            return Response({"detail": "Aucun entry_id fourni dans operations."}, status=status.HTTP_400_BAD_REQUEST)

        # fetch DB entries (we'll require they all exist)
        db_entries = list(ClassScheduleEntry.objects.select_related("school_class", "teacher", "subject").filter(id__in=entry_ids))
        found_ids = {e.id for e in db_entries}
        missing = list(entry_ids - found_ids)
        if missing:
            return Response({"detail": "Entries non trouvées", "missing_entry_ids": missing}, status=status.HTTP_400_BAD_REQUEST)

        # Fetch all schedule entries (we need global view to detect conflicts)
        all_entries = list(ClassScheduleEntry.objects.select_related("school_class", "teacher").all())

        # Build in-memory map entry_id -> dict with current values
        sim_entries = {}
        for e in all_entries:
            sim_entries[e.id] = {
                "id": e.id,
                "school_class_id": e.school_class_id,
                "teacher_id": e.teacher_id,
                "weekday": e.weekday,
                "starts_at": e.starts_at,
                "ends_at": e.ends_at,
                "start_min": _to_minutes_from_timeobj(e.starts_at) if e.starts_at else None,
                "end_min": _to_minutes_from_timeobj(e.ends_at) if e.ends_at else None,
            }

        # load timeslots for slot_idx map
        slots = _load_slots_ordered()
        idx_to_slot = {s["idx"]: s for s in slots}

        errors = []
        preview = {}  # entry_id -> {from:..., to:...}

        # Apply operations in-memory (sequentially)
        for op in ops:
            eid = op.get("entry_id")
            if eid is None:
                errors.append({"op": op, "error": "entry_id requis"})
                continue
            if eid not in sim_entries:
                errors.append({"entry_id": eid, "error": "entry introuvable dans la planning actuel"})
                continue

            curr = sim_entries[eid]
            orig = {"weekday": curr["weekday"], "starts_at": curr["starts_at"], "ends_at": curr["ends_at"]}

            # determine target
            target_slot_idx = op.get("target_slot_idx")
            target_weekday = op.get("target_weekday")
            target_start = op.get("target_start")
            target_end = op.get("target_end")

            # prefer slot_idx if provided
            if target_slot_idx is not None:
                try:
                    target_slot_idx = int(target_slot_idx)
                except Exception:
                    errors.append({"entry_id": eid, "error": "target_slot_idx invalide"})
                    continue
                slot = idx_to_slot.get(target_slot_idx)
                if not slot:
                    errors.append({"entry_id": eid, "error": f"slot_idx {target_slot_idx} introuvable"})
                    continue
                new_weekday = slot["weekday"]
                new_start_min = slot["start"]
                new_end_min = slot["end"]
                new_st_time = slot["db_obj"].start_time
                new_end_time = slot["db_obj"].end_time
            else:
                # require weekday + start + end
                if target_weekday is None or target_start is None or target_end is None:
                    errors.append({"entry_id": eid, "error": "soit target_slot_idx soit (target_weekday + target_start + target_end) requis"})
                    continue
                try:
                    new_weekday = int(target_weekday)
                except Exception:
                    errors.append({"entry_id": eid, "error": "target_weekday invalide"})
                    continue
                st_obj = _parse_time_str_or_obj(target_start)
                en_obj = _parse_time_str_or_obj(target_end)
                if st_obj is None or en_obj is None:
                    errors.append({"entry_id": eid, "error": "format horaire invalide (HH:MM)"})
                    continue
                new_start_min = _to_minutes_from_timeobj(st_obj)
                new_end_min = _to_minutes_from_timeobj(en_obj)
                if new_end_min <= new_start_min:
                    errors.append({"entry_id": eid, "error": "target_end doit être > target_start"})
                    continue
                new_st_time = st_obj
                new_end_time = en_obj

            # write proposed values into sim_entries
            curr["weekday"] = new_weekday
            curr["starts_at"] = new_st_time
            curr["ends_at"] = new_end_time
            curr["start_min"] = new_start_min
            curr["end_min"] = new_end_min

            preview[eid] = {
                "from": {
                    "weekday": orig["weekday"],
                    "starts_at": str(orig["starts_at"]),
                    "ends_at": str(orig["ends_at"]),
                },
                "to": {
                    "weekday": curr["weekday"],
                    "starts_at": str(curr["starts_at"]),
                    "ends_at": str(curr["ends_at"]),
                }
            }

        # After applying all ops in-memory, detect conflicts across sim_entries
        teacher_conflicts = []
        class_conflicts = []

        # group entries per teacher/day and per class/day to find overlaps
        per_teacher_day = defaultdict(list)
        per_class_day = defaultdict(list)
        for e in sim_entries.values():
            if e["teacher_id"] is not None and e["start_min"] is not None:
                per_teacher_day[(e["teacher_id"], e["weekday"])].append(e)
            if e["school_class_id"] is not None and e["start_min"] is not None:
                per_class_day[(e["school_class_id"], e["weekday"])].append(e)

        # helper to build overlap report
        def find_overlaps(list_entries):
            overlaps = []
            ents_sorted = sorted(list_entries, key=lambda x: x["start_min"] or 0)
            for i in range(len(ents_sorted) - 1):
                a = ents_sorted[i]; b = ents_sorted[i + 1]
                if a["start_min"] is None or b["start_min"] is None:
                    continue
                if _overlaps(a["weekday"], a["start_min"], a["end_min"], b["weekday"], b["start_min"], b["end_min"]):
                    overlaps.append((a, b))
            return overlaps

        # teacher overlaps
        for (tid, day), ents in per_teacher_day.items():
            ov = find_overlaps(ents)
            if ov:
                teacher_conflicts.append({
                    "teacher_id": tid,
                    "weekday": day,
                    "overlaps": [
                        {
                            "entry_ids": [a["id"], b["id"]],
                            "class_ids": [a["school_class_id"], b["school_class_id"]],
                            "times": [f"{a['starts_at']} - {a['ends_at']}", f"{b['starts_at']} - {b['ends_at']}"]
                        } for a, b in ov
                    ]
                })

        # class overlaps
        for (cid, day), ents in per_class_day.items():
            ov = find_overlaps(ents)
            if ov:
                class_conflicts.append({
                    "class_id": cid,
                    "weekday": day,
                    "overlaps": [
                        {
                            "entry_ids": [a["id"], b["id"]],
                            "teacher_ids": [a["teacher_id"], b["teacher_id"]],
                            "times": [f"{a['starts_at']} - {a['ends_at']}", f"{b['starts_at']} - {b['ends_at']}"]
                        } for a, b in ov
                    ]
                })

        valid = (len(errors) == 0) and (len(teacher_conflicts) == 0) and (len(class_conflicts) == 0)

        result = {
            "valid": valid,
            "errors": errors,
            "conflicts": {
                "teacher_conflicts": teacher_conflicts,
                "class_conflicts": class_conflicts,
            },
            "preview": preview,
        }
        return Response(result, status=status.HTTP_200_OK)


class TimetableBatchApplyView(APIView):
    """
    POST /academics/timetable-batch-apply/
    Même payload que validate. Si persist=True on applique les changements en DB (transaction).
    Seuls les staff/superuser peuvent persist=True.
    Retourne rapport similaire à la validation + liste applied_ids + errors.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        payload = request.data or {}
        ops = payload.get("operations")
        persist = bool(payload.get("persist", False))

        if not isinstance(ops, list):
            return Response({"detail": "operations doit être une liste d'opérations."}, status=status.HTTP_400_BAD_REQUEST)

        if persist and not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les admins peuvent appliquer (persist=True)."}, status=status.HTTP_403_FORBIDDEN)

        # Reuse the validator logic to simulate and detect conflicts
        # For clarity we call the validation view internally (duplicate logic would be repeated otherwise).
        # Simpler: perform same in-memory simulation here (we could refactor to avoid duplication).
        # For brevity we will call TimetableBatchValidateView.post directly.
        validator = TimetableBatchValidateView()
        # attach request to validator so permission checks pass if needed (not strictly necessary here)
        # build a fake request-like object: but easiest is to call the same logic: we'll reuse code by calling validator.post
        # However DRF APIs expect self and request -> we can just call its post with the same request
        # But be careful: validator expects request with .data; we already have payload; create a shallow Request clone is unnecessary.
        # Instead, call the same simulation by invoking validator.post with the same request payload.
        # To avoid complexity, we'll directly reuse the implementation by calling its method.

        # Construct a new request-like object is more involved; easiest path: duplicate minimal simulation logic by calling TimetableBatchValidateView.post
        # We'll create a shallow DRF Request wrapper is overkill; instead, reuse helper by instantiating and calling its post with current request.
        # NOTE: This works in practice because validator.post uses request.data only.
        validation_response = TimetableBatchValidateView().post(request)
        if validation_response.status_code != 200:
            return validation_response
        validation_data = validation_response.data

        if not validation_data.get("valid", False):
            # If not valid, return validation report and refuse to apply
            return Response({
                "applied": [],
                "errors": ["Validation failed: conflicts or errors detected. See 'validation' field."],
                "validation": validation_data
            }, status=status.HTTP_400_BAD_REQUEST)

        # Now actually apply
        applied = []
        db_errors = []
        try:
            with transaction.atomic():
                # For each operation, perform the DB update
                for op in ops:
                    eid = op.get("entry_id")
                    # fetch entry again (fresh)
                    try:
                        entry = ClassScheduleEntry.objects.select_for_update().get(pk=eid)
                    except ClassScheduleEntry.DoesNotExist:
                        db_errors.append({"entry_id": eid, "error": "entry introuvable au moment de l'application"})
                        continue

                    # determine target as in validator
                    target_slot_idx = op.get("target_slot_idx")
                    target_weekday = op.get("target_weekday")
                    target_start = op.get("target_start")
                    target_end = op.get("target_end")

                    if target_slot_idx is not None:
                        # map via TimeSlot ordering (same as validator)
                        slots = _load_slots_ordered()
                        if target_slot_idx < 0 or target_slot_idx >= len(slots):
                            db_errors.append({"entry_id": eid, "error": f"slot_idx {target_slot_idx} introuvable"})
                            continue
                        s = slots[target_slot_idx]["db_obj"]
                        entry.weekday = s.day
                        entry.starts_at = s.start_time
                        entry.ends_at = s.end_time
                        entry.save(update_fields=["weekday", "starts_at", "ends_at"])
                        applied.append(entry.id)
                    else:
                        # require explicit times
                        if target_weekday is None or target_start is None or target_end is None:
                            db_errors.append({"entry_id": eid, "error": "target invalide pour application"})
                            continue
                        st_obj = _parse_time_str_or_obj(target_start)
                        en_obj = _parse_time_str_or_obj(target_end)
                        if st_obj is None or en_obj is None:
                            db_errors.append({"entry_id": eid, "error": "format horaire invalide (HH:MM)"})
                            continue
                        entry.weekday = int(target_weekday)
                        entry.starts_at = st_obj
                        entry.ends_at = en_obj
                        entry.save(update_fields=["weekday", "starts_at", "ends_at"])
                        applied.append(entry.id)
        except Exception as exc:
            # rollback occurs automatically; return error
            return Response({"detail": "Erreur lors de l'application, transaction annulée", "exception": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # success
        return Response({
            "applied": applied,
            "errors": db_errors,
            "validation": validation_data,
        }, status=status.HTTP_200_OK)

# ... tes imports
from academics.models import Announcement
from academics.serializers import AnnouncementSerializer
# from .permissions import IsAdminOrReadOnly (déjà présent dans tes imports)

# =======================
# Annonces ViewSet
# =======================
class AnnouncementViewSet(viewsets.ModelViewSet):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]  # si IsAdminOrReadOnly existe
    parser_classes = [MultiPartParser, FormParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['title', 'content']
    ordering_fields = ['created_at']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)