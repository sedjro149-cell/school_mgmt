import json
import logging
import time as std_time
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_time

from rest_framework import filters, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django_filters.rest_framework import DjangoFilterBackend

from core.models import Parent, Student
from core.permissions import IsTeacherOrAdminCanEditComment

from notifications import service as notif_service

from academics.models import (
    Announcement,
    AttendanceSession,
    ClassScheduleEntry,
    ClassSubject,
    DraftGrade,
    Grade,
    Level,
    SchoolClass,
    SchoolYearConfig,
    StudentAttendance,
    Subject,
    SubjectComment,
    TermStatus,
    TermSubjectConfig,
    TimeSlot,
)
from academics.serializers import (
    AnnouncementSerializer,
    AttendanceSessionSerializer,
    ClassScheduleEntrySerializer,
    ClassSubjectSerializer,
    DraftGradeSerializer,
    GradeBulkLineSerializer,
    GradeSerializer,
    GroupedClassSubjectSerializer,
    LevelSerializer,
    ParentSerializer,
    ReportCardSerializer,
    SchoolClassListSerializer,
    SchoolClassSerializer,
    SchoolYearConfigSerializer,
    StudentAttendanceSerializer,
    StudentSerializer,
    SubjectCommentSerializer,
    SubjectSerializer,
    TermStatusSerializer,
    TermSubjectConfigSerializer,
    TimeSlotSerializer,
    UserSerializer,
)
from academics.services.report_cards import compute_report_cards_from_grades
from academics.timetable_by_level import run_timetable_pipeline
from academics.timetable_conflicts import detect_and_resolve, detect_teacher_conflicts

from .filters import GradeFilter
from .permissions import IsAdminOrParentReadOnly, IsAdminOrReadOnly
from academics.services.schedule_checker import run_check

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_bool(val: str) -> bool:
    if val is None:
        return False
    return str(val).lower() in ("1", "true", "yes", "y", "on")


def reset_timetable_table():
    ClassScheduleEntry.objects.all().delete()
    with connection.cursor() as cursor:
        cursor.execute("ALTER SEQUENCE academics_classscheduleentry_id_seq RESTART WITH 1;")


def _valid_terms():
    # Trimestres actifs selon SchoolYearConfig.nb_terms (2 -> T1,T2 ; 3 -> T1,T2,T3)
    nb = SchoolYearConfig.get_solo().nb_terms
    return [f"T{n}" for n in range(1, nb + 1)]


# ─────────────────────────────────────────────────────────────────────────────
#  USERS
# ─────────────────────────────────────────────────────────────────────────────

class UserViewSet(viewsets.ModelViewSet):
    queryset           = User.objects.all()
    serializer_class   = UserSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return User.objects.all()
        return User.objects.filter(id=user.id)


# ─────────────────────────────────────────────────────────────────────────────
#  PARENTS
# ─────────────────────────────────────────────────────────────────────────────

class ParentViewSet(viewsets.ModelViewSet):
    queryset           = Parent.objects.all()
    serializer_class   = ParentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return Parent.objects.all()
        if hasattr(user, "parent"):
            return Parent.objects.filter(user=user)
        return Parent.objects.none()


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENTS
# ─────────────────────────────────────────────────────────────────────────────

class StudentViewSet(viewsets.ModelViewSet):
    queryset           = Student.objects.all()
    serializer_class   = StudentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return Student.objects.all()
        if hasattr(user, "parent"):
            return Student.objects.filter(parent=user.parent).select_related("user", "school_class")
        if hasattr(user, "student"):
            return Student.objects.filter(user=user).select_related("user", "school_class")
        if hasattr(user, "teacher"):
            return (
                Student.objects
                .filter(school_class__teachers=user.teacher)
                .distinct()
                .select_related("user", "school_class")
            )
        return Student.objects.none()


# ─────────────────────────────────────────────────────────────────────────────
#  LEVELS
# ─────────────────────────────────────────────────────────────────────────────

class LevelViewSet(viewsets.ModelViewSet):
    queryset           = Level.objects.all()
    serializer_class   = LevelSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL CLASSES
# ─────────────────────────────────────────────────────────────────────────────

class SchoolClassViewSet(viewsets.ModelViewSet):
    queryset           = SchoolClass.objects.all()
    serializer_class   = SchoolClassSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    pagination_class   = None

    def get_queryset(self):
        user = self.request.user
        if self.action == "list":
            return SchoolClass.objects.select_related("level").all()
        qs = SchoolClass.objects.select_related("level").prefetch_related("students__user", "teachers__user")
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
        if self.action == "list":
            return SchoolClassListSerializer
        return SchoolClassSerializer


# ─────────────────────────────────────────────────────────────────────────────
#  SUBJECTS
# ─────────────────────────────────────────────────────────────────────────────

class SubjectViewSet(viewsets.ModelViewSet):
    queryset           = Subject.objects.all()
    serializer_class   = SubjectSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    pagination_class   = None


# ─────────────────────────────────────────────────────────────────────────────
#  CLASS-SUBJECT
# ─────────────────────────────────────────────────────────────────────────────

class ClassSubjectViewSet(viewsets.ModelViewSet):
    queryset           = ClassSubject.objects.all()
    serializer_class   = ClassSubjectSerializer
    permission_classes = [IsAuthenticated]
    pagination_class   = None

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return ClassSubject.objects.all()
        if hasattr(user, "teacher"):
            return ClassSubject.objects.filter(school_class__in=user.teacher.classes.all())
        if hasattr(user, "parent"):
            return ClassSubject.objects.filter(
                school_class__in=user.parent.students.values_list("school_class", flat=True)
            ).distinct()
        if hasattr(user, "student") and user.student and user.student.school_class:
            return ClassSubject.objects.filter(school_class=user.student.school_class)
        return ClassSubject.objects.none()

    def _require_admin(self):
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            raise PermissionDenied("Seuls les administrateurs peuvent effectuer cette action.")

    def perform_create(self, serializer):
        self._require_admin()
        serializer.save()

    def perform_update(self, serializer):
        self._require_admin()
        serializer.save()

    def perform_destroy(self, instance):
        self._require_admin()
        instance.delete()

    @action(detail=False, methods=["get"], url_path=r"by-class/(?P<class_id>\d+)")
    def by_class(self, request, class_id=None):
        school_class = get_object_or_404(SchoolClass, id=class_id)
        serializer = self.get_serializer(
            ClassSubject.objects.filter(school_class=school_class), many=True
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path=r"by-subject/(?P<subject_id>\d+)")
    def by_subject(self, request, subject_id=None):
        subject = get_object_or_404(Subject, id=subject_id)
        serializer = self.get_serializer(ClassSubject.objects.filter(subject=subject), many=True)
        return Response(serializer.data)

    @action(
        detail=False, methods=["get", "patch", "delete"],
        url_path=r"by-class-subject/(?P<class_id>\d+)/(?P<subject_id>\d+)",
    )
    def by_class_subject(self, request, class_id=None, subject_id=None):
        try:
            cs = ClassSubject.objects.get(school_class_id=class_id, subject_id=subject_id)
        except ClassSubject.DoesNotExist:
            raise NotFound("Association classe-matière introuvable.")

        if request.method == "GET":
            return Response(self.get_serializer(cs).data)

        self._require_admin()

        if request.method == "PATCH":
            s = self.get_serializer(cs, data=request.data, partial=True)
            s.is_valid(raise_exception=True)
            s.save()
            return Response(s.data)

        if request.method == "DELETE":
            cs.delete()
            return Response({"detail": "Liaison supprimée."}, status=status.HTTP_204_NO_CONTENT)


# ─────────────────────────────────────────────────────────────────────────────
#  COPY CLASS CONFIG
# ─────────────────────────────────────────────────────────────────────────────

class CopyClassConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les administrateurs peuvent copier une configuration."},
                            status=status.HTTP_403_FORBIDDEN)

        payload          = request.data or {}
        source_class_id  = payload.get("source_class_id")
        target_class_ids = payload.get("target_class_ids", [])
        overwrite        = bool(payload.get("overwrite", False))

        if not source_class_id:
            return Response({"detail": "source_class_id est obligatoire."}, status=400)
        if not isinstance(target_class_ids, list) or not target_class_ids:
            return Response({"detail": "target_class_ids doit être une liste non-vide."}, status=400)

        source_class = get_object_or_404(SchoolClass, id=source_class_id)
        source_configs = list(ClassSubject.objects.filter(school_class=source_class).select_related("subject"))
        if not source_configs:
            return Response({"detail": f"La classe source '{source_class}' n'a aucune matière."}, status=400)

        results = []
        total_created = total_skipped = total_errors = 0

        for target_id in target_class_ids:
            result = {"target_class_id": target_id, "target_class_name": None,
                      "created": 0, "skipped": 0, "overwritten": 0, "errors": []}

            if target_id == source_class_id:
                result["errors"].append("Classe cible identique à la source.")
                results.append(result)
                continue

            try:
                target_class = SchoolClass.objects.get(id=target_id)
            except SchoolClass.DoesNotExist:
                result["errors"].append(f"Classe cible (id={target_id}) introuvable.")
                total_errors += 1
                results.append(result)
                continue

            result["target_class_name"] = str(target_class)

            try:
                with transaction.atomic():
                    if overwrite:
                        deleted, _ = ClassSubject.objects.filter(school_class=target_class).delete()
                        result["overwritten"] = deleted

                    existing = set(
                        ClassSubject.objects.filter(school_class=target_class)
                        .values_list("subject_id", flat=True)
                    )
                    to_create = [
                        ClassSubject(
                            school_class=target_class, subject=cs.subject,
                            coefficient=cs.coefficient, hours_per_week=cs.hours_per_week,
                            is_optional=cs.is_optional,
                        )
                        for cs in source_configs if cs.subject_id not in existing
                    ]
                    skipped = len(source_configs) - len(to_create)
                    if to_create:
                        ClassSubject.objects.bulk_create(to_create)
                        result["created"] = len(to_create)
                        total_created    += len(to_create)
                    result["skipped"]  = skipped
                    total_skipped     += skipped
            except Exception as exc:
                result["errors"].append(str(exc))
                total_errors += 1

            results.append(result)

        return Response({
            "source_class_id":        source_class_id,
            "source_class_name":      str(source_class),
            "source_subjects_count":  len(source_configs),
            "overwrite":              overwrite,
            "results":                results,
            "summary": {"total_created": total_created, "total_skipped": total_skipped, "total_errors": total_errors},
        })


# ─────────────────────────────────────────────────────────────────────────────
#  DRAFT GRADES
# ─────────────────────────────────────────────────────────────────────────────

class DraftGradeViewSet(viewsets.ModelViewSet):
    queryset           = DraftGrade.objects.all()
    serializer_class   = DraftGradeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend]
    pagination_class   = None
    filterset_fields   = ["student", "subject", "term"]

    def get_queryset(self):
        user = self.request.user
        vt   = _valid_terms()
        if user.is_staff or user.is_superuser:
            return DraftGrade.objects.filter(term__in=vt)
        if hasattr(user, "teacher"):
            teacher = user.teacher
            return DraftGrade.objects.filter(
                teacher=teacher,
                student__school_class__in=teacher.classes.all(),
                subject=teacher.subject,
                term__in=vt,
            )
        if hasattr(user, "parent"):
            return DraftGrade.objects.filter(student__parent=user.parent, term__in=vt)
        if hasattr(user, "student"):
            return DraftGrade.objects.filter(student=user.student, term__in=vt)
        return DraftGrade.objects.none()

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "teacher") and not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Vous devez être professeur pour créer des brouillons.")

        teacher = user.teacher if hasattr(user, "teacher") else None
        student = serializer.validated_data["student"]
        subject = serializer.validated_data["subject"]
        term    = serializer.validated_data["term"]

        if not (user.is_staff or user.is_superuser):
            if subject != teacher.subject:
                raise PermissionDenied("Vous ne pouvez saisir des notes que pour votre matière.")
            if student.school_class not in teacher.classes.all():
                raise PermissionDenied("Vous ne pouvez saisir des notes que pour vos élèves.")

        note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]
        if not any(serializer.validated_data.get(f) is not None for f in note_fields):
            raise serializers.ValidationError("Au moins une note doit être fournie.")

        if teacher:
            existing = DraftGrade.objects.filter(
                teacher=teacher, student=student, subject=subject, term=term
            ).first()
            if existing:
                for k, v in serializer.validated_data.items():
                    setattr(existing, k, v)
                existing.save()
                serializer.instance = existing
                return

        serializer.save(teacher=teacher)

    def perform_update(self, serializer):
        user     = self.request.user
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

        note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]
        if not any(
            serializer.validated_data.get(f) is not None or getattr(instance, f) is not None
            for f in note_fields
        ):
            raise serializers.ValidationError("Au moins une note doit être présente.")

        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not (user.is_staff or user.is_superuser):
            if not hasattr(user, "teacher") or instance.teacher != user.teacher:
                raise PermissionDenied("Vous ne pouvez supprimer que vos brouillons.")
        instance.delete()

    @action(detail=False, methods=["post"], url_path="submit")
    def submit(self, request):
        user = request.user
        if not hasattr(user, "teacher") and not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Seuls les profs peuvent soumettre des brouillons.")

        teacher         = user.teacher
        term            = request.data.get("term")
        school_class_id = request.data.get("school_class", None)

        if term not in dict(DraftGrade.TERM_CHOICES).keys():
            return Response({"detail": "Champ 'term' manquant ou invalide."}, status=400)

        # Vérifier que le terme est valide selon SchoolYearConfig
        year_config = SchoolYearConfig.get_solo()
        valid_terms = [f"T{n}" for n in range(1, year_config.nb_terms + 1)]
        if term not in valid_terms:
            return Response(
                {"detail": f"Le trimestre '{term}' n'existe pas (l'école a {year_config.nb_terms} trimestres)."},
                status=400,
            )

        drafts_qs = DraftGrade.objects.filter(teacher=teacher, term=term, subject=teacher.subject)
        if school_class_id:
            drafts_qs = drafts_qs.filter(student__school_class_id=school_class_id)

        drafts = list(drafts_qs.select_related("student", "student__school_class", "subject"))
        if not drafts:
            return Response({"detail": "Aucun brouillon à soumettre."}, status=400)

        # Vérifier que le TermStatus n'est pas verrouillé
        class_ids = {d.student.school_class_id for d in drafts}
        locked_classes = [
            cid for cid in class_ids
            if (ts := TermStatus.objects.filter(school_class_id=cid, term=term).first())
            and not ts.is_editable
        ]
        if locked_classes:
            return Response(
                {"detail": f"Le trimestre {term} est verrouillé pour {len(locked_classes)} classe(s).",
                 "locked_class_ids": locked_classes},
                status=status.HTTP_423_LOCKED,
            )

        collisions = [
            {"student_id": d.student.id, "student_name": d.student.user.get_full_name(),
             "subject_id": d.subject.id, "term": d.term}
            for d in drafts
            if Grade.objects.filter(student=d.student, subject=d.subject, term=d.term).exists()
        ]
        if collisions:
            return Response(
                {"detail": "Certains élèves ont déjà des notes finales.", "collisions": collisions},
                status=400,
            )

        created = []
        errors  = []
        note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]

        try:
            with transaction.atomic():
                for d in drafts:
                    if d.student.school_class not in teacher.classes.all():
                        errors.append({"student_id": d.student.id, "error": "Élève hors de vos classes."})
                        continue
                    if d.subject != teacher.subject:
                        errors.append({"student_id": d.student.id, "error": "Matière différente de la vôtre."})
                        continue
                    if not any(getattr(d, f) is not None for f in note_fields):
                        errors.append({"student_id": d.student.id, "error": "Aucune note dans le brouillon."})
                        continue

                    g = Grade(
                        student=d.student, subject=d.subject, term=d.term,
                        interrogation1=d.interrogation1, interrogation2=d.interrogation2,
                        interrogation3=d.interrogation3, devoir1=d.devoir1, devoir2=d.devoir2,
                    )
                    g.save()
                    created.append({"grade_id": g.id, "student_id": d.student.id})

                if errors:
                    raise IntegrityError("Validation errors; abort.")

                DraftGrade.objects.filter(id__in=[d.id for d in drafts]).delete()

        except IntegrityError as e:
            return Response({"detail": "Soumission annulée.", "errors": errors or str(e)}, status=400)

        return Response({"created": created}, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────────────────
#  GRADES
# ─────────────────────────────────────────────────────────────────────────────

class GradeViewSet(viewsets.ModelViewSet):
    serializer_class   = GradeSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    filter_backends    = [DjangoFilterBackend]
    filterset_class    = GradeFilter
    pagination_class   = None

    def get_queryset(self):
        user = self.request.user
        vt   = _valid_terms()
        qs   = Grade.objects.select_related(
            "student", "student__school_class", "subject"
        ).filter(term__in=vt)
        if user.is_staff or user.is_superuser:
            return qs
        if hasattr(user, "teacher"):
            return qs.filter(student__school_class__in=user.teacher.classes.all())
        if hasattr(user, "parent"):
            return qs.filter(student_id__in=user.parent.students.values_list("id", flat=True))
        if hasattr(user, "student"):
            return qs.filter(student=user.student)
        return qs.none()


    def get_serializer_context(self):
        """
        Pour parents et élèves : injecte published_pairs dans le contexte
        pour que GradeSerializer.to_representation() masque les moyennes
        des trimestres non publiés.
        """
        ctx  = super().get_serializer_context()
        user = self.request.user

        is_restricted = (
            not user.is_staff
            and not user.is_superuser
            and not hasattr(user, "teacher")
        )
        ctx["is_restricted_role"] = is_restricted

        if is_restricted:
            ctx["published_pairs"] = set(
                TermStatus.objects.filter(
                    status=TermStatus.Status.PUBLISHED
                ).values_list("school_class_id", "term")
            )

        return ctx

    @action(detail=False, methods=["post"], url_path="bulk_upsert")
    def bulk_upsert(self, request):
        payload = request.data
        if not isinstance(payload, list):
            return Response({"detail": "Payload must be a list."}, status=400)
        if len(payload) > 1000:
            return Response({"detail": "Too many items (max 1000)."}, status=400)

        results     = []
        created     = updated = errors = 0
        user        = request.user
        notify_list = []
        note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]

        _ts_cache = {}

        def _get_ts(school_class_id, term):
            key = (school_class_id, term)
            if key not in _ts_cache:
                _ts_cache[key] = TermStatus.objects.filter(
                    school_class_id=school_class_id, term=term
                ).first()
            return _ts_cache[key]

        with transaction.atomic():
            for idx, item in enumerate(payload):
                ser = GradeBulkLineSerializer(data=item)
                if not ser.is_valid():
                    errors += 1
                    results.append({"index": idx, "input": item, "status": "error", "errors": ser.errors})
                    continue

                valid         = ser.validated_data
                student       = valid.get("student")
                subject       = valid.get("subject")
                term          = valid.get("term")
                line_id       = valid.get("id", None)
                provided_keys = set(item.keys())

                defaults = {f: valid.get(f) for f in note_fields if f in provided_keys}
                if "term" in provided_keys:
                    defaults["term"] = term

                if not defaults and not line_id:
                    errors += 1
                    results.append({"index": idx, "status": "error", "errors": "No updatable fields."})
                    continue

                # Vérifier le TermStatus (verrou)
                ts = _get_ts(student.school_class_id, term)
                if ts and not ts.is_editable:
                    errors += 1
                    results.append({
                        "index": idx, "student_id": getattr(student, "id", None), "status": "error",
                        "errors": f"Le trimestre {term} est verrouillé pour '{student.school_class.name}'.",
                    })
                    continue

                if not (user.is_staff or user.is_superuser):
                    if hasattr(user, "teacher"):
                        if student.school_class not in user.teacher.classes.all():
                            errors += 1
                            results.append({"index": idx, "status": "error", "errors": "Permission denied."})
                            continue
                    else:
                        errors += 1
                        results.append({"index": idx, "status": "error", "errors": "Permission denied."})
                        continue

                try:
                    if line_id:
                        try:
                            g = Grade.objects.select_for_update().get(id=line_id)
                        except Grade.DoesNotExist:
                            errors += 1
                            results.append({"index": idx, "status": "error", "errors": "Grade id not found."})
                            continue

                        if str(g.student.id) != str(student.id):
                            errors += 1
                            results.append({"index": idx, "status": "error", "errors": "Mismatched student."})
                            continue

                        for k, v in defaults.items():
                            setattr(g, k, v)
                        g._suppress_notifications = True
                        g.save()
                        updated += 1
                        results.append({
                            "index": idx, "student_id": student.id, "subject_id": subject.id,
                            "status": "updated", "id": g.id,
                            "average_interro": g.average_interro,
                            "average_subject": g.average_subject,
                            "average_coeff":   g.average_coeff,
                        })
                        notify_list.append((g.id, "updated"))

                    else:
                        g, created_flag = Grade.objects.select_for_update().update_or_create(
                            student=student, subject=subject, term=term, defaults=defaults
                        )
                        g._suppress_notifications = True
                        g.save()
                        op = "created" if created_flag else "updated"
                        if created_flag:
                            created += 1
                        else:
                            updated += 1
                        results.append({
                            "index": idx, "student_id": student.id, "subject_id": subject.id,
                            "status": op, "id": g.id,
                            "average_interro": g.average_interro,
                            "average_subject": g.average_subject,
                            "average_coeff":   g.average_coeff,
                        })
                        notify_list.append((g.id, op))

                except Exception as e:
                    errors += 1
                    results.append({"index": idx, "status": "error", "errors": str(e)})

        if notify_list:
            transaction.on_commit(lambda: notif_service.bulk_notify_grades(notify_list))

        return Response({"created": created, "updated": updated, "errors": errors, "results": results})


# ─────────────────────────────────────────────────────────────────────────────
#  CLASS SCHEDULE
# ─────────────────────────────────────────────────────────────────────────────

class ClassScheduleEntryViewSet(viewsets.ModelViewSet):
    queryset = ClassScheduleEntry.objects.select_related(
        "school_class", "subject", "teacher__user"
    ).all()
    serializer_class   = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return self.queryset
        if hasattr(user, "teacher"):
            return self.queryset.filter(teacher=user.teacher)
        return ClassScheduleEntry.objects.none()


# ─────────────────────────────────────────────────────────────────────────────
#  TIMETABLE (read-only)
# ─────────────────────────────────────────────────────────────────────────────

class TimetableViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ClassScheduleEntry.objects.select_related(
        "school_class", "subject", "teacher"
    ).all()
    serializer_class   = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]
    pagination_class   = None
    filter_backends    = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields   = ["school_class", "teacher", "weekday", "school_class__level"]
    search_fields      = ["school_class__name", "subject__name",
                          "teacher__user__last_name", "teacher__user__first_name"]
    ordering_fields    = ["weekday", "starts_at"]

    def get_queryset(self):
        qs     = super().get_queryset()
        params = self.request.query_params
        user   = self.request.user

        def clean(v):
            if v is None or v in ("undefined", ""):
                return None
            return str(v).strip().rstrip("/")

        class_id   = clean(params.get("class_id") or params.get("school_class") or params.get("school_class_id"))
        teacher_id = clean(params.get("teacher_id") or params.get("teacher"))
        level_id   = clean(params.get("level_id") or params.get("school_class__level"))
        weekday    = clean(params.get("weekday"))

        allowed = None
        if user.is_staff or user.is_superuser:
            allowed = None
        elif hasattr(user, "student") and getattr(user.student, "school_class_id", None):
            allowed = {user.student.school_class_id}
        elif hasattr(user, "parent"):
            allowed = set(user.parent.students.values_list("school_class_id", flat=True).distinct())
        elif hasattr(user, "teacher"):
            t = user.teacher
            allowed = set()
            try:
                allowed.update(t.classes.values_list("pk", flat=True))
            except Exception:
                pass
            allowed.update(
                ClassScheduleEntry.objects.filter(teacher=t)
                .values_list("school_class_id", flat=True).distinct()
            )
        else:
            allowed = set()

        if allowed is not None:
            if not allowed:
                return qs.none()
            qs = qs.filter(school_class_id__in=list(allowed))

        if class_id:
            qs = qs.filter(school_class_id=int(class_id)) if class_id.isdigit() else qs.filter(school_class__name__icontains=class_id)
        if teacher_id:
            qs = qs.filter(teacher_id=int(teacher_id)) if teacher_id.isdigit() else qs.filter(teacher__user__username=teacher_id)
        if level_id:
            qs = qs.filter(school_class__level_id=int(level_id)) if level_id.isdigit() else qs.filter(school_class__level__name__icontains=level_id)
        if weekday and weekday.isdigit():
            qs = qs.filter(weekday=int(weekday))

        return qs.order_by("weekday", "starts_at")


# ─────────────────────────────────────────────────────────────────────────────
#  REPORT CARDS
# ─────────────────────────────────────────────────────────────────────────────

class ReportCardViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class   = None

    @property
    def Grade(self):
        return apps.get_model("academics", "Grade")

    @property
    def Student(self):
        return apps.get_model("core", "Student")

    def _get_teacher_students_qs(self, teacher):
        if hasattr(teacher, "students"):
            return teacher.students.all()
        if hasattr(teacher, "classes"):
            qs = teacher.classes.all()
            if qs.exists():
                return self.Student.objects.filter(school_class__in=qs)
        try:
            ids = (
                self.Grade.objects.filter(teacher=teacher)
                .values_list("student_id", flat=True).distinct()
            )
            return self.Student.objects.filter(pk__in=ids)
        except Exception:
            pass
        return None

    def _determine_class_ids_for_ranking(self, request, user, class_id_param, student_id_param):
        if class_id_param:
            return {int(class_id_param)}
        if student_id_param:
            try:
                s = self.Student.objects.select_related("school_class").get(pk=student_id_param)
                if s.school_class_id:
                    return {s.school_class_id}
            except Exception:
                pass
        if hasattr(user, "student") and getattr(user.student, "school_class_id", None):
            return {user.student.school_class_id}
        if hasattr(user, "parent"):
            return {int(c) for c in user.parent.students.values_list("school_class_id", flat=True).distinct() if c}
        if hasattr(user, "teacher"):
            if hasattr(user.teacher, "classes"):
                qs = user.teacher.classes.all()
                if qs.exists():
                    return {int(c.pk) for c in qs}
            sts = self._get_teacher_students_qs(user.teacher)
            if sts is not None:
                return {int(c) for c in sts.values_list("school_class_id", flat=True).distinct() if c}
        return None

    @staticmethod
    def _published_pairs():
        """
        Couples (school_class_id, term) dont le TermStatus est PUBLISHED.
        Utilisé pour filtrer les bulletins visibles aux parents et élèves.
        """
        return set(
            TermStatus.objects.filter(
                status=TermStatus.Status.PUBLISHED
            ).values_list("school_class_id", "term")
        )

    def list(self, request):
        user       = request.user
        student_id = request.query_params.get("student_id")
        class_id   = request.query_params.get("class_id")
        term       = request.query_params.get("term")
        include_missing_subjects = _parse_bool(request.query_params.get("include_missing_subjects"))
        full_weighting           = _parse_bool(request.query_params.get("full_weighting"))

        class_ids_for_ranking = self._determine_class_ids_for_ranking(
            request, user, class_id, student_id
        )

        vt = _valid_terms()
        ranking_grades_qs = self.Grade.objects.select_related(
            "student", "student__school_class", "subject"
        ).filter(term__in=vt)
        if term:
            ranking_grades_qs = ranking_grades_qs.filter(term__iexact=term)
        if class_ids_for_ranking is not None:
            ranking_grades_qs = ranking_grades_qs.filter(
                student__school_class__id__in=class_ids_for_ranking
            )
        ranking_grades_qs = ranking_grades_qs.order_by("student_id", "term")

        # Parents et élèves : uniquement les trimestres PUBLISHED
        is_restricted = (
            not user.is_staff
            and not user.is_superuser
            and not hasattr(user, "teacher")
        )

        if is_restricted:
            published = self._published_pairs()
            if not published:
                return Response([])
            visibility_filter = Q()
            for cls_id, pub_term in published:
                visibility_filter |= Q(student__school_class_id=cls_id, term=pub_term)
            ranking_grades_qs = ranking_grades_qs.filter(visibility_filter)

        t0 = std_time.time()
        ranking_report_cards = compute_report_cards_from_grades(
            ranking_grades_qs,
            include_missing_subjects=include_missing_subjects,
            full_weighting=full_weighting,
        )
        logger.info(f"compute_report_cards took {std_time.time() - t0:.2f}s")

        filtered = ranking_report_cards

        if user.is_staff or user.is_superuser:
            pass
        elif hasattr(user, "student"):
            s_pk     = str(user.student.pk)
            filtered = [r for r in ranking_report_cards if str(r["student"].pk) == s_pk]
        elif hasattr(user, "parent"):
            child_ids = {str(pk) for pk in user.parent.students.values_list("pk", flat=True)}
            filtered  = [r for r in ranking_report_cards if str(r["student"].pk) in child_ids]
        elif hasattr(user, "teacher"):
            sts = self._get_teacher_students_qs(user.teacher)
            if sts:
                t_ids    = {str(pk) for pk in sts.values_list("pk", flat=True)}
                filtered = [r for r in ranking_report_cards if str(r["student"].pk) in t_ids]
            else:
                filtered = []

        if student_id:
            filtered = [r for r in filtered if str(r["student"].pk) == str(student_id)]

        filtered.sort(key=lambda x: (str(x["student"]).lower(), x.get("term", "")))

        return Response(
            ReportCardSerializer(filtered, many=True, context={"request": request}).data
        )


# ─────────────────────────────────────────────────────────────────────────────
#  ATTENDANCE SESSIONS
# ─────────────────────────────────────────────────────────────────────────────

class AttendanceSessionViewSet(viewsets.ModelViewSet):
    serializer_class   = AttendanceSessionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields   = {
        "date":                         ["exact", "gte", "lte"],
        "status":                       ["exact"],
        "schedule_entry__school_class": ["exact"],
        "schedule_entry":               ["exact"],
    }
    ordering_fields = ["date", "status", "opened_at"]
    ordering        = ["-date"]

    def get_queryset(self):
        user = self.request.user
        qs   = AttendanceSession.objects.select_related(
            "schedule_entry__subject", "schedule_entry__school_class",
            "schedule_entry__teacher__user", "opened_by", "submitted_by",
        )
        if user.is_staff or user.is_superuser:
            return qs
        if hasattr(user, "teacher"):
            return qs.filter(schedule_entry__teacher=user.teacher)
        return qs.none()

    def perform_create(self, serializer):
        serializer.save(opened_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les administrateurs peuvent supprimer une session."},
                            status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        session = self.get_object()
        if not session.is_editable:
            return Response({"detail": "La session est déjà soumise ou annulée."}, status=400)
        with transaction.atomic():
            session.submit(request.user)
            session.attendances.update(updated_at=timezone.now())
        transaction.on_commit(lambda: _send_absence_notifications_for_session(session.id))
        return Response({"detail": "Session soumise."})

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        session = self.get_object()
        if not session.cancel(request.user):
            return Response({"detail": "Impossible d'annuler une session soumise."}, status=400)
        return Response({"detail": "Session annulée."})

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Réservé aux administrateurs."}, status=status.HTTP_403_FORBIDDEN)
        session = self.get_object()
        if not session.reopen(request.user):
            return Response({"detail": "La session n'est pas soumise."}, status=400)
        return Response({"detail": "Session réouverte."})

    @action(detail=True, methods=["get"])
    def sheet(self, request, pk=None):
        session  = self.get_object()
        students = Student.objects.filter(
            school_class=session.schedule_entry.school_class
        ).select_related("user").order_by("user__last_name", "user__first_name")

        absences = {a.student_id: a for a in session.attendances.select_related("student__user")}

        return Response({
            "session": AttendanceSessionSerializer(session, context={"request": request}).data,
            "students": [
                {
                    "id":         s.id,
                    "name":       f"{s.user.last_name} {s.user.first_name}",
                    "status":     absences[s.id].status if s.id in absences else "PRESENT",
                    "reason":     absences[s.id].reason if s.id in absences else None,
                    "absence_id": absences[s.id].id    if s.id in absences else None,
                }
                for s in students
            ],
        })


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT ATTENDANCE
# ─────────────────────────────────────────────────────────────────────────────

class StudentAttendanceViewSet(viewsets.ModelViewSet):
    serializer_class   = StudentAttendanceSerializer
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields   = {"session": ["exact"], "student": ["exact"], "date": ["exact", "gte", "lte"], "status": ["exact"]}
    ordering_fields    = ["date", "status"]
    ordering           = ["-date"]

    def get_queryset(self):
        user = self.request.user
        qs   = StudentAttendance.objects.select_related(
            "session__schedule_entry__subject",
            "session__schedule_entry__school_class",
            "student__user", "marked_by",
        )
        if user.is_staff or user.is_superuser:
            return qs
        if hasattr(user, "teacher"):
            return qs.filter(session__schedule_entry__teacher=user.teacher)
        if hasattr(user, "parent"):
            return qs.filter(student__parent=user.parent)
        if hasattr(user, "student"):
            return qs.filter(student=user.student)
        return qs.none()

    def _require_write(self):
        user = self.request.user
        if not (user.is_staff or user.is_superuser or hasattr(user, "teacher")):
            return Response({"detail": "Permission insuffisante."}, status=status.HTTP_403_FORBIDDEN)
        return None

    def create(self, request, *args, **kwargs):
        err = self._require_write()
        if err: return err
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        err = self._require_write()
        if err: return err
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        err = self._require_write()
        if err: return err
        instance = self.get_object()
        if not instance.session.is_editable:
            return Response({"detail": "Session soumise. Demandez une réouverture."}, status=400)
        return super().destroy(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
#  DAILY ATTENDANCE SHEET
# ─────────────────────────────────────────────────────────────────────────────

class DailyAttendanceSheetView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        class_id          = request.query_params.get("class_id")
        date_str          = request.query_params.get("date")
        schedule_entry_id = request.query_params.get("schedule_entry_id")

        if not class_id or not date_str:
            return Response({"detail": "class_id et date sont obligatoires."}, status=400)

        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Format attendu : YYYY-MM-DD."}, status=400)

        weekday    = target_date.weekday()
        entries_qs = ClassScheduleEntry.objects.filter(
            school_class_id=class_id, weekday=weekday
        ).select_related("subject", "teacher__user")

        if schedule_entry_id:
            entries_qs = entries_qs.filter(id=schedule_entry_id)

        user = request.user
        if not (user.is_staff or user.is_superuser) and hasattr(user, "teacher"):
            entries_qs = entries_qs.filter(teacher=user.teacher)

        entries = list(entries_qs.order_by("starts_at"))
        if not entries:
            return Response({"date": date_str, "weekday": weekday, "slots": [],
                             "message": "Aucun cours prévu ce jour."})

        students = list(
            Student.objects.filter(school_class_id=class_id)
            .select_related("user")
            .order_by("user__last_name", "user__first_name")
        )

        slots = []
        for entry in entries:
            session, _ = AttendanceSession.objects.get_or_create(
                schedule_entry=entry, date=target_date,
                defaults={"opened_by": request.user, "status": AttendanceSession.Status.OPEN},
            )
            absences = {a.student_id: a for a in session.attendances.all()}
            slots.append({
                "entry": {
                    "id": entry.id, "subject": entry.subject.name,
                    "starts_at": str(entry.starts_at), "ends_at": str(entry.ends_at),
                    "teacher": (
                        entry.teacher.user.get_full_name()
                        if entry.teacher and hasattr(entry.teacher, "user") else "N/A"
                    ),
                },
                "session":  AttendanceSessionSerializer(session, context={"request": request}).data,
                "students": [
                    {
                        "id":         s.id,
                        "name":       f"{s.user.last_name} {s.user.first_name}",
                        "status":     absences[s.id].status if s.id in absences else "PRESENT",
                        "reason":     absences[s.id].reason if s.id in absences else None,
                        "absence_id": absences[s.id].id    if s.id in absences else None,
                    }
                    for s in students
                ],
            })

        return Response({"date": date_str, "weekday": weekday, "slots": slots})


# ─────────────────────────────────────────────────────────────────────────────
#  ATTENDANCE HISTORY
# ─────────────────────────────────────────────────────────────────────────────

class StudentAttendanceHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user       = request.user
        student_id = request.query_params.get("student_id")
        date_from  = request.query_params.get("date_from")
        date_to    = request.query_params.get("date_to")

        if not student_id:
            return Response({"detail": "student_id est obligatoire."}, status=400)

        try:
            student = Student.objects.select_related("user", "school_class").get(pk=student_id)
        except Student.DoesNotExist:
            return Response({"detail": "Élève introuvable."}, status=404)

        if not (user.is_staff or user.is_superuser):
            if hasattr(user, "teacher"):
                if not ClassScheduleEntry.objects.filter(
                    school_class=student.school_class, teacher=user.teacher
                ).exists():
                    return Response({"detail": "Cet élève n'est pas dans vos classes."}, status=403)
            elif hasattr(user, "parent"):
                if student.parent != user.parent:
                    return Response({"detail": "Accès non autorisé."}, status=403)
            elif hasattr(user, "student"):
                if user.student != student:
                    return Response({"detail": "Accès non autorisé."}, status=403)
            else:
                return Response({"detail": "Accès non autorisé."}, status=403)

        def _parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d").date() if d else None
            except ValueError:
                return None

        stats = _compute_attendance_stats(student, _parse_date(date_from), _parse_date(date_to))
        return Response({
            "student_id":   student.id,
            "student_name": f"{student.user.last_name} {student.user.first_name}",
            "date_from": date_from, "date_to": date_to,
            **stats,
        })


def _compute_attendance_stats(student, date_from=None, date_to=None):
    sessions_qs = AttendanceSession.objects.filter(
        status=AttendanceSession.Status.SUBMITTED,
        schedule_entry__school_class=student.school_class,
    ).select_related("schedule_entry__subject")

    if date_from: sessions_qs = sessions_qs.filter(date__gte=date_from)
    if date_to:   sessions_qs = sessions_qs.filter(date__lte=date_to)

    sessions       = list(sessions_qs)
    total_sessions = len(sessions)

    if total_sessions == 0:
        return {
            "total_sessions": 0, "present_count": 0, "absent_count": 0,
            "late_count": 0, "excused_count": 0,
            "attendance_rate": None, "absence_rate": None,
            "by_subject": {}, "by_month": {}, "absences_detail": [],
        }

    session_ids = [s.id for s in sessions]
    absences_map = {
        a.session_id: a
        for a in StudentAttendance.objects.filter(
            session_id__in=session_ids, student=student
        ).select_related("session__schedule_entry__subject")
    }

    absent_count  = late_count = excused_count = 0
    by_subject    = defaultdict(lambda: {"total": 0, "present": 0, "absent": 0, "late": 0, "excused": 0})
    by_month      = defaultdict(lambda: {"total": 0, "present": 0, "absent": 0, "late": 0, "excused": 0})
    absences_detail = []

    for session in sessions:
        subject_name = getattr(session.schedule_entry.subject, "name", "Inconnu")
        month_key    = session.date.strftime("%Y-%m")
        by_subject[subject_name]["total"] += 1
        by_month[month_key]["total"]      += 1

        absence = absences_map.get(session.id)
        if absence:
            st = absence.status
            if st == "ABSENT":
                absent_count += 1
                by_subject[subject_name]["absent"] += 1
                by_month[month_key]["absent"]      += 1
            elif st == "LATE":
                late_count += 1
                by_subject[subject_name]["late"] += 1
                by_month[month_key]["late"]      += 1
            elif st == "EXCUSED":
                excused_count += 1
                by_subject[subject_name]["excused"] += 1
                by_month[month_key]["excused"]      += 1
            absences_detail.append({
                "date": str(session.date), "subject": subject_name,
                "status": absence.status, "reason": absence.reason,
            })
        else:
            by_subject[subject_name]["present"] += 1
            by_month[month_key]["present"]      += 1

    present_count       = total_sessions - absent_count - late_count - excused_count
    effectively_present = present_count + late_count + excused_count
    attendance_rate     = round(float(Decimal(effectively_present) / Decimal(total_sessions) * 100), 2)
    absence_rate        = round(float(Decimal(absent_count)        / Decimal(total_sessions) * 100), 2)

    return {
        "total_sessions": total_sessions, "present_count": present_count,
        "absent_count": absent_count, "late_count": late_count, "excused_count": excused_count,
        "attendance_rate": attendance_rate, "absence_rate": absence_rate,
        "by_subject": dict(by_subject), "by_month": dict(by_month),
        "absences_detail": sorted(absences_detail, key=lambda x: x["date"]),
    }


def _send_absence_notifications_for_session(session_id: int):
    try:
        Notification               = apps.get_model("notifications", "Notification")
        NotificationTemplate       = apps.get_model("notifications", "NotificationTemplate")
        UserNotificationPreference = apps.get_model("notifications", "UserNotificationPreference")
        from notifications.delivery import send_notification as _send
    except Exception as e:
        logger.debug("Notifications app indisponible : %s", e)
        return

    try:
        session = AttendanceSession.objects.select_related("schedule_entry__subject").get(pk=session_id)
    except AttendanceSession.DoesNotExist:
        return

    try:
        template, _ = NotificationTemplate.objects.get_or_create(
            key="absence_reported",
            defaults={
                "topic": "attendance",
                "title_template": "Absence signalée — {{ student_name }}",
                "body_template": (
                    "Bonjour {{ parent_name }}, {{ student_name }} a été signalé(e) "
                    "{{ status }} le {{ date }}{% if subject %} en {{ subject }}{% endif %}."
                    "{% if reason %} Motif : {{ reason }}.{% endif %}"
                ),
                "default_channels": ["inapp"],
            },
        )
    except Exception as e:
        template = None
        logger.exception("Erreur get_or_create template absence_reported: %s", e)

    absences = session.attendances.filter(notified_at__isnull=True).select_related(
        "student__user", "student__parent__user"
    )

    for absence in absences:
        student = absence.student
        try:
            student_name = student.user.get_full_name()
        except Exception:
            student_name = str(getattr(student, "id", ""))

        subject_name = getattr(session.schedule_entry.subject, "name", None)

        recipients = []
        if hasattr(student, "parent") and student.parent and getattr(student.parent, "user", None):
            recipients.append(student.parent.user)
        if not recipients and hasattr(student, "parents"):
            try:
                for p in student.parents.all():
                    if getattr(p, "user", None):
                        recipients.append(p.user)
            except Exception:
                pass
        if not recipients and getattr(student, "user", None):
            recipients.append(student.user)

        for recipient in recipients:
            if Notification.objects.filter(
                topic="attendance", recipient_user=recipient,
                payload__student_id=student.id, payload__session_id=session.id,
            ).exists():
                continue

            channels = template.default_channels if template and getattr(template, "default_channels", None) else ["inapp"]
            try:
                pref = UserNotificationPreference.objects.filter(user=recipient, topic="attendance").first()
                if pref and not pref.enabled:
                    continue
                if pref and pref.channels:
                    channels = pref.channels
            except Exception:
                pass

            payload = {
                "student_id": student.id, "student_name": student_name,
                "session_id": session.id, "date": str(session.date),
                "subject": subject_name, "status": absence.status, "reason": absence.reason,
                "parent_name": recipient.get_full_name() if hasattr(recipient, "get_full_name") else "",
            }

            try:
                notif = Notification.objects.create(
                    template=template, topic="attendance",
                    recipient_user=recipient, payload=payload, channels=channels,
                )
                try:
                    transaction.on_commit(lambda n=notif: _send(n))
                except Exception:
                    try:
                        _send(notif)
                    except Exception as e:
                        logger.exception("Fallback send_notification failed: %s", e)
                absence.notified_at = timezone.now()
                absence.save(update_fields=["notified_at"])
            except Exception:
                logger.exception("Failed to create Notification recipient=%s", getattr(recipient, "id", None))


# ─────────────────────────────────────────────────────────────────────────────
#  SUBJECT COMMENTS
# ─────────────────────────────────────────────────────────────────────────────

class SubjectCommentViewSet(viewsets.ModelViewSet):
    queryset           = SubjectComment.objects.all()
    serializer_class   = SubjectCommentSerializer
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ["student", "subject", "term"]

    def get_queryset(self):
        user = self.request.user
        vt   = _valid_terms()
        if user.is_staff or user.is_superuser:
            return SubjectComment.objects.filter(term__in=vt)
        if hasattr(user, "teacher"):
            teacher = user.teacher
            return SubjectComment.objects.filter(
                student__school_class__in=teacher.classes.all(),
                subject=teacher.subject,
                term__in=vt,
            )
        if hasattr(user, "parent"):
            return SubjectComment.objects.filter(student__parent=user.parent, term__in=vt)
        if hasattr(user, "student"):
            return SubjectComment.objects.filter(student=user.student, term__in=vt)
        return SubjectComment.objects.none()

    def perform_create(self, serializer):
        teacher = self.request.user.teacher
        student = serializer.validated_data["student"]
        subject = serializer.validated_data["subject"]
        term    = serializer.validated_data["term"]
        if student.school_class not in teacher.classes.all():
            raise PermissionDenied("Vous ne pouvez commenter que vos propres élèves.")
        if subject != teacher.subject:
            raise PermissionDenied("Vous ne pouvez commenter que votre matière.")
        if SubjectComment.objects.filter(student=student, subject=subject, term=term).exists():
            raise serializers.ValidationError("Un commentaire existe déjà pour cet élève / matière / trimestre.")
        serializer.save(teacher=teacher)

    def perform_update(self, serializer):
        teacher  = self.request.user.teacher
        instance = serializer.instance
        if instance.student.school_class not in teacher.classes.all():
            raise PermissionDenied("Vous ne pouvez modifier que les commentaires de vos propres élèves.")
        if instance.subject != teacher.subject:
            raise PermissionDenied("Vous ne pouvez modifier que votre matière.")
        serializer.save()


# ─────────────────────────────────────────────────────────────────────────────
#  TIMESLOTS
# ─────────────────────────────────────────────────────────────────────────────

class TimeSlotViewSet(viewsets.ModelViewSet):
    queryset           = TimeSlot.objects.all().order_by("start_time")
    serializer_class   = TimeSlotSerializer
    permission_classes = [IsAuthenticated]
    pagination_class   = None

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return self.queryset
        return TimeSlot.objects.none()


# ─────────────────────────────────────────────────────────────────────────────
#  ANNOUNCEMENTS
# ─────────────────────────────────────────────────────────────────────────────

class AnnouncementViewSet(viewsets.ModelViewSet):
    queryset           = Announcement.objects.all()
    serializer_class   = AnnouncementSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    parser_classes     = [MultiPartParser, FormParser]
    filter_backends    = [filters.SearchFilter, filters.OrderingFilter]
    search_fields      = ["title", "content"]
    ordering_fields    = ["created_at"]
    pagination_class   = None

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


# ─────────────────────────────────────────────────────────────────────────────
#  GENERATE TIMETABLE
# ─────────────────────────────────────────────────────────────────────────────

class GenerateTimetableView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        reset_timetable_table()
        try:
            result = run_timetable_pipeline(
                dry_run=request.data.get("dry_run", False),
                persist=request.data.get("persist", True),
            )
            return Response(result)
        except Exception as e:
            return Response({"detail": f"Erreur : {str(e)}"}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
#  TIMETABLE CONFLICTS
# ─────────────────────────────────────────────────────────────────────────────

class TimetableConflictsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return Response(detect_teacher_conflicts())

    def post(self, request, *args, **kwargs):
        dry_run = bool(request.data.get("dry_run", True))
        persist = bool(request.data.get("persist", False))
        if persist and not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les admins peuvent appliquer les résolutions."}, status=403)
        return Response(detect_and_resolve(dry_run=dry_run, persist=persist))


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEDULE CHECK
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        params   = request.query_params
        class_id = params.get("class_id")
        limit    = params.get("limit")
        verbose  = params.get("verbose")

        try:
            class_id_val = int(class_id) if class_id is not None else None
        except Exception:
            return Response({"detail": "class_id must be an integer"}, status=400)
        try:
            limit_val = int(limit) if limit is not None else 10
        except Exception:
            return Response({"detail": "limit must be an integer"}, status=400)

        verbose_val = str(verbose).lower() in ("1", "true", "yes", "on")

        try:
            return Response(run_check(class_id=class_id_val, limit=limit_val, verbose=verbose_val))
        except Exception as e:
            return Response({"detail": f"Erreur : {str(e)}"}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
#  TIMETABLE BATCH
# ─────────────────────────────────────────────────────────────────────────────

from academics.services.timetable_batch import validate_batch_operations, apply_batch_operations


class TimetableBatchValidateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Réservé aux administrateurs."}, status=403)
        ops = (request.data or {}).get("operations")
        if not isinstance(ops, list) or not ops:
            return Response({"detail": "'operations' doit être une liste non-vide."}, status=400)
        return Response(validate_batch_operations(ops))


class TimetableBatchApplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les administrateurs peuvent modifier l'emploi du temps."}, status=403)
        payload = request.data or {}
        ops     = payload.get("operations")
        force   = bool(payload.get("force", False))
        if not isinstance(ops, list) or not ops:
            return Response({"detail": "'operations' doit être une liste non-vide."}, status=400)
        result = apply_batch_operations(ops, force=force)
        if not result["valid"]:
            http_status = 500 if result.get("db_errors") else 400
            return Response(result, status=http_status)
        return Response(result)


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL YEAR CONFIG  (singleton)
# ─────────────────────────────────────────────────────────────────────────────

class SchoolYearConfigViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def list(self, request):
        return Response(SchoolYearConfigSerializer(SchoolYearConfig.get_solo()).data)

    def retrieve(self, request, pk=None):
        return Response(SchoolYearConfigSerializer(SchoolYearConfig.get_solo()).data)

    def _update(self, request, partial=False):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les administrateurs peuvent modifier cette configuration."},
                            status=status.HTTP_403_FORBIDDEN)
        config = SchoolYearConfig.get_solo()
        ser    = SchoolYearConfigSerializer(config, data=request.data, partial=partial)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    def update(self, request, pk=None):
        return self._update(request, partial=False)

    def partial_update(self, request, pk=None):
        return self._update(request, partial=True)


# ─────────────────────────────────────────────────────────────────────────────
#  TERM SUBJECT CONFIG
# ─────────────────────────────────────────────────────────────────────────────

class TermSubjectConfigViewSet(viewsets.ModelViewSet):
    serializer_class   = TermSubjectConfigSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    pagination_class   = None
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ["school_class", "subject", "term"]

    def get_queryset(self):
        return TermSubjectConfig.objects.select_related(
            "school_class", "subject"
        ).filter(term__in=_valid_terms())

    @action(detail=False, methods=["post"], url_path="bulk")
    def bulk_create(self, request):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Admin uniquement."}, status=403)

        school_class_id = request.data.get("school_class")
        term            = request.data.get("term")
        configs         = request.data.get("configs", [])

        missing = []
        if not school_class_id: missing.append("school_class")
        if not term:            missing.append("term")
        if not configs:         missing.append("configs (liste non vide)")
        if missing:
            return Response({"detail": f"Champs manquants : {', '.join(missing)}"}, status=400)

        vt = _valid_terms()
        if term not in vt:
            return Response(
                {"detail": f"Trimestre '{term}' invalide. Trimestres actifs : {', '.join(vt)}."},
                status=400,
            )

        try:
            school_class_id = int(school_class_id)
        except (ValueError, TypeError):
            return Response({"detail": "school_class doit être un entier."}, status=400)

        if not SchoolClass.objects.filter(id=school_class_id).exists():
            return Response({"detail": f"Classe id={school_class_id} introuvable."}, status=404)

        ts = TermStatus.objects.filter(school_class_id=school_class_id, term=term).first()
        if ts and not ts.is_editable:
            return Response(
                {"detail": f"Le trimestre {term} est verrouillé. Déverrouillez d'abord."},
                status=status.HTTP_423_LOCKED,
            )

        created = updated = errors = 0
        results = []

        for item in configs:
            subject_id  = item.get("subject")
            nb_interros = item.get("nb_interros", 3)
            nb_devoirs  = item.get("nb_devoirs",  2)

            if subject_id is None:
                errors += 1
                results.append({"status": "error", "error": "subject est requis."})
                continue

            try:
                subject_id  = int(subject_id)
                nb_interros = int(nb_interros)
                nb_devoirs  = int(nb_devoirs)
            except (ValueError, TypeError) as e:
                errors += 1
                results.append({"subject_id": subject_id, "status": "error", "error": str(e)})
                continue

            if not (1 <= nb_interros <= 3):
                errors += 1
                results.append({"subject_id": subject_id, "status": "error",
                                 "error": "nb_interros doit être entre 1 et 3."})
                continue
            if not (0 <= nb_devoirs <= 2):
                errors += 1
                results.append({"subject_id": subject_id, "status": "error",
                                 "error": "nb_devoirs doit être entre 0 et 2."})
                continue

            try:
                obj, was_created = TermSubjectConfig.objects.update_or_create(
                    school_class_id=school_class_id,
                    subject_id=subject_id,
                    term=term,
                    defaults={"nb_interros": nb_interros, "nb_devoirs": nb_devoirs},
                )
                if was_created:
                    created += 1
                else:
                    updated += 1
                results.append({
                    "subject_id":  subject_id,
                    "status":      "created" if was_created else "updated",
                    "nb_interros": obj.nb_interros,
                    "nb_devoirs":  obj.nb_devoirs,
                })
            except Exception as e:
                errors += 1
                results.append({"subject_id": subject_id, "status": "error", "error": str(e)})

        http_status = 200 if errors == 0 else (207 if (created + updated) > 0 else 400)
        return Response(
            {"created": created, "updated": updated, "errors": errors, "results": results},
            status=http_status,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  TERM STATUS
# ─────────────────────────────────────────────────────────────────────────────

class TermStatusViewSet(viewsets.ModelViewSet):
    serializer_class   = TermStatusSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    pagination_class   = None
    filter_backends    = [DjangoFilterBackend]
    filterset_fields   = ["school_class", "term", "status"]

    def get_queryset(self):
        return TermStatus.objects.select_related(
            "school_class", "locked_by"
        ).filter(term__in=_valid_terms())

    def destroy(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Seuls les administrateurs peuvent supprimer un statut."},
                            status=403)
        ts = self.get_object()
        if ts.status != TermStatus.Status.DRAFT:
            return Response({"detail": "Impossible de supprimer un trimestre verrouillé ou publié."},
                            status=400)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def lock(self, request, pk=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Admin uniquement."}, status=403)
        ts = self.get_object()
        try:
            ts.lock(request.user)
        except DjangoValidationError as e:
            return Response({"detail": str(e)}, status=400)
        except Exception as e:
            logger.exception("Erreur lors du lock %s: %s", ts, e)
            return Response({"detail": f"Erreur lors du calcul des moyennes : {str(e)}"}, status=500)
        ts.refresh_from_db()
        return Response(TermStatusSerializer(ts).data)

    @action(detail=True, methods=["post"])
    def unlock(self, request, pk=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Admin uniquement."}, status=403)
        ts = self.get_object()
        try:
            ts.unlock(request.user)
        except DjangoValidationError as e:
            return Response({"detail": str(e)}, status=400)
        ts.refresh_from_db()
        return Response(TermStatusSerializer(ts).data)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Admin uniquement."}, status=403)
        ts = self.get_object()
        try:
            ts.publish(request.user)
        except DjangoValidationError as e:
            return Response({"detail": str(e)}, status=400)
        ts.refresh_from_db()
        return Response(TermStatusSerializer(ts).data)

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Admin uniquement."}, status=403)
        ts = self.get_object()
        if ts.status != TermStatus.Status.PUBLISHED:
            return Response({"detail": "Ce trimestre n'est pas publié."}, status=400)
        ts.status       = TermStatus.Status.LOCKED
        ts.published_at = None
        ts.save(update_fields=["status", "published_at"])
        ts.refresh_from_db()
        return Response(TermStatusSerializer(ts).data)