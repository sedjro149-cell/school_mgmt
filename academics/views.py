import json
import logging
import time as std_time
from collections import defaultdict
from datetime import datetime, time
from typing import Any, Dict, List, Optional

from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_time
from django.utils import timezone

from rest_framework import filters, status, viewsets
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
    ClassScheduleEntry,
    ClassSubject,
    DraftGrade,
    Grade,
    Level,
    SchoolClass,
    StudentAttendance,
    Subject,
    SubjectComment,
    TimeSlot,
)
from academics.serializers import (
    AnnouncementSerializer,
    ClassScheduleEntrySerializer,
    DraftGradeSerializer,
    ReportCardSerializer,
    StudentAttendanceSerializer,
    SubjectCommentSerializer,
    TimeSlotSerializer,
)
from academics.services.report_cards import compute_report_cards_from_grades
from academics.timetable_by_level import run_timetable_pipeline
from academics.timetable_conflicts import detect_and_resolve, detect_teacher_conflicts

from .filters import GradeFilter
from .permissions import IsAdminOrParentReadOnly, IsAdminOrReadOnly
from .serializers import (
    ClassSubjectSerializer,
    GradeBulkLineSerializer,
    GradeSerializer,
    GroupedClassSubjectSerializer,
    LevelSerializer,
    ParentSerializer,
    SchoolClassListSerializer,
    SchoolClassSerializer,
    StudentSerializer,
    SubjectSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60


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
        cursor.execute(
            "ALTER SEQUENCE academics_classscheduleentry_id_seq RESTART WITH 1;"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  USERS
# ─────────────────────────────────────────────────────────────────────────────

class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
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


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENTS
# ─────────────────────────────────────────────────────────────────────────────

class StudentViewSet(viewsets.ModelViewSet):
    queryset = Student.objects.all()
    serializer_class = StudentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return Student.objects.all()
        if hasattr(user, "parent"):
            return Student.objects.filter(parent=user.parent).select_related("user", "school_class")
        if hasattr(user, "student"):
            return Student.objects.filter(user=user).select_related("user", "school_class")
        teacher = getattr(user, "teacher", None)
        if teacher:
            return (
                Student.objects
                .filter(school_class__teachers=teacher)
                .distinct()
                .select_related("user", "school_class")
            )
        return Student.objects.none()


# ─────────────────────────────────────────────────────────────────────────────
#  LEVELS
# ─────────────────────────────────────────────────────────────────────────────

class LevelViewSet(viewsets.ModelViewSet):
    queryset = Level.objects.all()
    serializer_class = LevelSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL CLASSES
# ─────────────────────────────────────────────────────────────────────────────

class SchoolClassViewSet(viewsets.ModelViewSet):
    queryset = SchoolClass.objects.all()
    serializer_class = SchoolClassSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        user = self.request.user
        if self.action == "list":
            return SchoolClass.objects.select_related("level").all()
        qs = SchoolClass.objects.select_related("level").prefetch_related(
            "students__user", "teachers__user"
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
        if self.action == "list":
            return SchoolClassListSerializer
        return SchoolClassSerializer


# ─────────────────────────────────────────────────────────────────────────────
#  SUBJECTS
# ─────────────────────────────────────────────────────────────────────────────

class SubjectViewSet(viewsets.ModelViewSet):
    queryset = Subject.objects.all()
    serializer_class = SubjectSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    pagination_class = None


# ─────────────────────────────────────────────────────────────────────────────
#  CLASS-SUBJECT
# ─────────────────────────────────────────────────────────────────────────────

class ClassSubjectViewSet(viewsets.ModelViewSet):
    queryset = ClassSubject.objects.all()
    serializer_class = ClassSubjectSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return ClassSubject.objects.all()
        if hasattr(user, "teacher"):
            return ClassSubject.objects.filter(
                school_class__in=user.teacher.classes.all()
            )
        if hasattr(user, "parent"):
            return ClassSubject.objects.filter(
                school_class__in=user.parent.students.values_list("school_class", flat=True)
            ).distinct()
        if hasattr(user, "student"):
            student = user.student
            if not student or not student.school_class:
                return ClassSubject.objects.none()
            return ClassSubject.objects.filter(school_class=student.school_class)
        return ClassSubject.objects.none()

    def perform_create(self, serializer):
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            raise PermissionDenied("Seuls les administrateurs peuvent créer des liaisons de matières.")
        serializer.save()

    def perform_update(self, serializer):
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            raise PermissionDenied("Seuls les administrateurs peuvent modifier des liaisons de matières.")
        serializer.save()

    def perform_destroy(self, instance):
        if not (self.request.user.is_staff or self.request.user.is_superuser):
            raise PermissionDenied("Seuls les administrateurs peuvent supprimer des liaisons de matières.")
        instance.delete()

    @action(detail=False, methods=["get"], url_path=r'by-class/(?P<class_id>\d+)')
    def by_class(self, request, class_id=None):
        try:
            school_class = SchoolClass.objects.get(id=class_id)
        except SchoolClass.DoesNotExist:
            raise NotFound("Classe introuvable.")
        serializer = self.get_serializer(
            ClassSubject.objects.filter(school_class=school_class), many=True
        )
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path=r'by-subject/(?P<subject_id>\d+)')
    def by_subject(self, request, subject_id=None):
        try:
            subject = Subject.objects.get(id=subject_id)
        except Subject.DoesNotExist:
            raise NotFound("Matière introuvable.")
        serializer = self.get_serializer(
            ClassSubject.objects.filter(subject=subject), many=True
        )
        return Response(serializer.data)

    @action(
        detail=False,
        methods=["get", "patch", "delete"],
        url_path=r'by-class-subject/(?P<class_id>\d+)/(?P<subject_id>\d+)',
    )
    def by_class_subject(self, request, class_id=None, subject_id=None):
        try:
            cs = ClassSubject.objects.get(school_class_id=class_id, subject_id=subject_id)
        except ClassSubject.DoesNotExist:
            raise NotFound("Association classe-matière introuvable.")

        if request.method == "GET":
            return Response(self.get_serializer(cs).data)

        if not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied("Seuls les administrateurs peuvent modifier cette matière.")

        if request.method == "PATCH":
            serializer = self.get_serializer(cs, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

        if request.method == "DELETE":
            cs.delete()
            return Response(
                {"detail": "Liaison supprimée avec succès."},
                status=status.HTTP_204_NO_CONTENT,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  DRAFT GRADES
# ─────────────────────────────────────────────────────────────────────────────

class DraftGradeViewSet(viewsets.ModelViewSet):
    queryset = DraftGrade.objects.all()
    serializer_class = DraftGradeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    pagination_class = None
    filterset_fields = ["student", "subject", "term"]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return DraftGrade.objects.all()
        if hasattr(user, "teacher"):
            teacher = user.teacher
            return DraftGrade.objects.filter(
                teacher=teacher,
                student__school_class__in=teacher.classes.all(),
                subject=teacher.subject,
            )
        if hasattr(user, "parent"):
            return DraftGrade.objects.filter(student__parent=user.parent)
        if hasattr(user, "student"):
            return DraftGrade.objects.filter(student=user.student)
        return DraftGrade.objects.none()

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, "teacher") and not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Vous devez être professeur pour créer des brouillons de notes.")

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
            raise serializers.ValidationError("Au moins une note doit être fournie dans le brouillon.")

        existing = (
            DraftGrade.objects
            .filter(teacher=teacher, student=student, subject=subject, term=term)
            .first()
            if teacher else None
        )
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
            (serializer.validated_data.get(f) is not None)
            or (getattr(instance, f) is not None)
            for f in note_fields
        ):
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
        user = request.user
        if not hasattr(user, "teacher") and not (user.is_staff or user.is_superuser):
            raise PermissionDenied("Seuls les profs peuvent soumettre des brouillons.")

        teacher         = user.teacher
        term            = request.data.get("term")
        school_class_id = request.data.get("school_class", None)

        if term not in dict(DraftGrade.TERM_CHOICES).keys():
            return Response(
                {"detail": "Champ 'term' manquant ou invalide."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        drafts_qs = DraftGrade.objects.filter(
            teacher=teacher, term=term, subject=teacher.subject
        )
        if school_class_id:
            drafts_qs = drafts_qs.filter(student__school_class_id=school_class_id)

        drafts = list(drafts_qs.select_related("student", "subject"))
        if not drafts:
            return Response(
                {"detail": "Aucun brouillon à soumettre pour les critères fournis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collisions = [
            {
                "student_id":   d.student.id,
                "student_name": d.student.user.get_full_name(),
                "subject_id":   d.subject.id,
                "term":         d.term,
            }
            for d in drafts
            if Grade.objects.filter(student=d.student, subject=d.subject, term=d.term).exists()
        ]
        if collisions:
            return Response(
                {
                    "detail": "Certains élèves ont déjà des notes finales pour ce (subject, term).",
                    "collisions": collisions,
                },
                status=status.HTTP_400_BAD_REQUEST,
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
                    g.save()
                    created.append({"grade_id": g.id, "student_id": d.student.id})

                if errors:
                    raise IntegrityError("Validation errors in drafts; abort transaction.")

                DraftGrade.objects.filter(id__in=[d.id for d in drafts]).delete()

        except IntegrityError as e:
            return Response(
                {"detail": "Soumission annulée.", "errors": errors or str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"created": created}, status=status.HTTP_201_CREATED)


# ─────────────────────────────────────────────────────────────────────────────
#  GRADES
# ─────────────────────────────────────────────────────────────────────────────

class GradeViewSet(viewsets.ModelViewSet):
    queryset = Grade.objects.all()
    serializer_class = GradeSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_class = GradeFilter
    pagination_class = None

    @action(detail=False, methods=["post"], url_path="bulk_upsert")
    def bulk_upsert(self, request):
        payload = request.data
        if not isinstance(payload, list):
            return Response(
                {"detail": "Payload must be a list of objects."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        MAX_LINES = 1000
        if len(payload) > MAX_LINES:
            return Response(
                {"detail": f"Too many items (max {MAX_LINES})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        results  = []
        created  = updated = errors = 0
        user     = request.user
        notify_list = []
        note_fields = ["interrogation1", "interrogation2", "interrogation3", "devoir1", "devoir2"]

        with transaction.atomic():
            for idx, item in enumerate(payload):
                serializer = GradeBulkLineSerializer(data=item)
                if not serializer.is_valid():
                    errors += 1
                    results.append({"index": idx, "input": item, "status": "error", "errors": serializer.errors})
                    continue

                valid         = serializer.validated_data
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
                    results.append({"index": idx, "input": item, "status": "error", "errors": "No updatable fields provided."})
                    continue

                if not (user.is_staff or user.is_superuser):
                    if hasattr(user, "teacher"):
                        if student.school_class not in user.teacher.classes.all():
                            errors += 1
                            results.append({"index": idx, "student_id": getattr(student, "id", None), "status": "error", "errors": "Permission denied for this student."})
                            continue
                    else:
                        errors += 1
                        results.append({"index": idx, "student_id": getattr(student, "id", None), "status": "error", "errors": "Permission denied."})
                        continue

                try:
                    if line_id:
                        try:
                            g = Grade.objects.select_for_update().get(id=line_id)
                        except Grade.DoesNotExist:
                            errors += 1
                            results.append({"index": idx, "student_id": student.id, "status": "error", "errors": "Grade id not found."})
                            continue

                        if str(g.student.id) != str(student.id):
                            errors += 1
                            results.append({"index": idx, "student_id": student.id, "status": "error", "errors": "Mismatched student for grade id."})
                            continue

                        for k, v in defaults.items():
                            setattr(g, k, v)
                        setattr(g, "_suppress_notifications", True)
                        g.save()
                        updated += 1
                        results.append({"index": idx, "student_id": student.id, "subject_id": subject.id, "status": "updated", "id": g.id, "average_interro": g.average_interro, "average_subject": g.average_subject, "average_coeff": g.average_coeff})
                        notify_list.append((g.id, "updated"))

                    else:
                        g, created_flag = Grade.objects.select_for_update().update_or_create(
                            student=student, subject=subject, term=term, defaults=defaults
                        )
                        setattr(g, "_suppress_notifications", True)
                        g.save()
                        op = "created" if created_flag else "updated"
                        if created_flag:
                            created += 1
                        else:
                            updated += 1
                        results.append({"index": idx, "student_id": student.id, "subject_id": subject.id, "status": op, "id": g.id, "average_interro": g.average_interro, "average_subject": g.average_subject, "average_coeff": g.average_coeff})
                        notify_list.append((g.id, op))

                except Exception as e:
                    errors += 1
                    results.append({"index": idx, "student_id": getattr(student, "id", None), "subject_id": getattr(subject, "id", None), "status": "error", "errors": str(e)})

        if notify_list:
            transaction.on_commit(lambda: notif_service.bulk_notify_grades(notify_list))

        return Response({"created": created, "updated": updated, "errors": errors, "results": results})


# ─────────────────────────────────────────────────────────────────────────────
#  CLASS SCHEDULE (CRUD)
# ─────────────────────────────────────────────────────────────────────────────

class ClassScheduleEntryViewSet(viewsets.ModelViewSet):
    queryset = ClassScheduleEntry.objects.select_related(
        "school_class", "subject", "teacher__user"
    ).all()
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return self.queryset
        if hasattr(user, "teacher"):
            return self.queryset.filter(teacher=user.teacher)
        return ClassScheduleEntry.objects.none()


# ─────────────────────────────────────────────────────────────────────────────
#  TIMETABLE (READ-ONLY)
# ─────────────────────────────────────────────────────────────────────────────

class TimetableViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ClassScheduleEntry.objects.select_related(
        "school_class", "subject", "teacher"
    ).all()
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["school_class", "teacher", "weekday", "school_class__level"]
    search_fields = [
        "school_class__name",
        "subject__name",
        "teacher__user__last_name",
        "teacher__user__first_name",
    ]
    ordering_fields = ["weekday", "starts_at"]

    def get_queryset(self):
        qs     = super().get_queryset()
        params = self.request.query_params
        user   = self.request.user

        def clean_val(v):
            if v is None or v in ("undefined", ""):
                return None
            return str(v).strip().rstrip("/")

        class_id   = clean_val(params.get("class_id") or params.get("school_class") or params.get("school_class_id"))
        teacher_id = clean_val(params.get("teacher_id") or params.get("teacher"))
        level_id   = clean_val(params.get("level_id") or params.get("school_class__level"))
        weekday    = clean_val(params.get("weekday"))

        allowed_class_ids = None
        if user.is_staff or user.is_superuser:
            allowed_class_ids = None
        elif hasattr(user, "student") and getattr(user.student, "school_class_id", None):
            allowed_class_ids = {user.student.school_class_id}
        elif hasattr(user, "parent"):
            allowed_class_ids = set(
                user.parent.students.values_list("school_class_id", flat=True).distinct()
            )
        elif hasattr(user, "teacher"):
            teacher_obj = user.teacher
            allowed_class_ids = set()
            try:
                allowed_class_ids.update(teacher_obj.classes.values_list("pk", flat=True))
            except Exception:
                pass
            allowed_class_ids.update(
                ClassScheduleEntry.objects.filter(teacher=teacher_obj)
                .values_list("school_class_id", flat=True)
                .distinct()
            )
        else:
            allowed_class_ids = set()

        if allowed_class_ids is not None:
            if not allowed_class_ids:
                return qs.none()
            qs = qs.filter(school_class_id__in=list(allowed_class_ids))

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
    pagination_class = None

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
            classes_qs = teacher.classes.all()
            if classes_qs.exists():
                return self.Student.objects.filter(school_class__in=classes_qs)
        try:
            student_ids = (
                self.Grade.objects
                .filter(teacher=teacher)
                .values_list("student_id", flat=True)
                .distinct()
            )
            return self.Student.objects.filter(pk__in=student_ids)
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
            classes = user.parent.students.values_list("school_class_id", flat=True).distinct()
            return {int(cid) for cid in classes if cid is not None}
        if hasattr(user, "teacher"):
            if hasattr(user.teacher, "classes"):
                classes_qs = user.teacher.classes.all()
                if classes_qs.exists():
                    return {int(c.pk) for c in classes_qs}
            teacher_students = self._get_teacher_students_qs(user.teacher)
            if teacher_students is not None:
                return {int(cid) for cid in teacher_students.values_list("school_class_id", flat=True).distinct() if cid is not None}
        return None

    def list(self, request):
        user       = request.user
        student_id = request.query_params.get("student_id")
        class_id   = request.query_params.get("class_id")
        term       = request.query_params.get("term")
        include_missing_subjects = _parse_bool(request.query_params.get("include_missing_subjects"))
        full_weighting           = _parse_bool(request.query_params.get("full_weighting"))

        class_ids_for_ranking = self._determine_class_ids_for_ranking(request, user, class_id, student_id)

        ranking_grades_qs = self.Grade.objects.select_related(
            "student", "student__school_class", "subject"
        )
        if term:
            ranking_grades_qs = ranking_grades_qs.filter(term__iexact=term)
        if class_ids_for_ranking is not None:
            ranking_grades_qs = ranking_grades_qs.filter(
                student__school_class__id__in=class_ids_for_ranking
            )
        ranking_grades_qs = ranking_grades_qs.order_by("student_id", "term")

        t0 = std_time.time()
        ranking_report_cards = compute_report_cards_from_grades(
            ranking_grades_qs,
            include_missing_subjects=include_missing_subjects,
            full_weighting=full_weighting,
        )
        logger.info(f"compute_report_cards (ranking) took {std_time.time() - t0:.2f}s")

        filtered_report_cards = ranking_report_cards

        if user.is_staff or user.is_superuser:
            pass
        elif hasattr(user, "student"):
            s_pk = str(user.student.pk)
            filtered_report_cards = [r for r in ranking_report_cards if str(r["student"].pk) == s_pk]
        elif hasattr(user, "parent"):
            child_ids = {str(pk) for pk in user.parent.students.values_list("pk", flat=True)}
            filtered_report_cards = [r for r in ranking_report_cards if str(r["student"].pk) in child_ids]
        elif hasattr(user, "teacher"):
            teacher_students = self._get_teacher_students_qs(user.teacher)
            if teacher_students:
                t_ids = {str(pk) for pk in teacher_students.values_list("pk", flat=True)}
                filtered_report_cards = [r for r in ranking_report_cards if str(r["student"].pk) in t_ids]
            else:
                filtered_report_cards = []

        if student_id:
            filtered_report_cards = [r for r in filtered_report_cards if str(r["student"].pk) == str(student_id)]

        filtered_report_cards.sort(key=lambda x: (str(x["student"]).lower(), x.get("term", "")))

        serializer = ReportCardSerializer(filtered_report_cards, many=True, context={"request": request})
        return Response(serializer.data)


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT ATTENDANCE
# ─────────────────────────────────────────────────────────────────────────────

# =============================================================================
#  À INTÉGRER dans academics/views.py
#
#  1. Remplacer StudentAttendanceViewSet existant par la version ci-dessous
#  2. Remplacer DailyAttendanceSheetView existant par la version ci-dessous
#  3. Ajouter AttendanceSessionViewSet et StudentAttendanceHistoryView
#  4. Dans academics/urls.py, brancher les nouvelles routes (voir bas de fichier)
# =============================================================================

# Ces imports s'ajoutent aux imports existants de views.py
# (StudentAttendance et AttendanceSession sont déjà dans academics.models)
from academics.models import AttendanceSession
from academics.serializers import AttendanceSessionSerializer
from collections import defaultdict
from decimal import Decimal


# ─────────────────────────────────────────────────────────────────────────────
#  ATTENDANCE SESSION VIEWSET
#  Remplace/complète l'ancienne gestion monolithique.
# ─────────────────────────────────────────────────────────────────────────────

class AttendanceSessionViewSet(viewsets.ModelViewSet):
    """
    Gestion des sessions d'appel.

    GET    /attendance/sessions/                   → liste (filtrée par rôle)
    POST   /attendance/sessions/                   → ouvrir une session
    PATCH  /attendance/sessions/{id}/              → modifier la note (si OPEN)
    DELETE /attendance/sessions/{id}/              → admin uniquement
    POST   /attendance/sessions/{id}/submit/       → valider + envoyer notifs
    POST   /attendance/sessions/{id}/cancel/       → annuler le cours
    POST   /attendance/sessions/{id}/reopen/       → rouvrir après soumission (admin)
    GET    /attendance/sessions/{id}/sheet/        → feuille complète
    """

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
            "schedule_entry__subject",
            "schedule_entry__school_class",
            "schedule_entry__teacher__user",
            "opened_by",
            "submitted_by",
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
            return Response(
                {"detail": "Seuls les administrateurs peuvent supprimer une session."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)

    # ------------------------------------------------------------------
    # submit → fige les absences + déclenche les notifications
    # ------------------------------------------------------------------
    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        session = self.get_object()
        if not session.is_editable:
            return Response(
                {"detail": "La session est déjà soumise ou annulée."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            session.submit(request.user)
            session.attendances.update(updated_at=timezone.now())

        # Hors transaction pour ne pas bloquer la réponse
        transaction.on_commit(lambda: _send_absence_notifications_for_session(session.id))

        return Response(
            {"detail": "Session soumise. Notifications en cours d'envoi."},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        session = self.get_object()
        if not session.cancel(request.user):
            return Response(
                {"detail": "Impossible d'annuler une session déjà soumise."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": "Session annulée."}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {"detail": "La réouverture est réservée aux administrateurs."},
                status=status.HTTP_403_FORBIDDEN,
            )
        session = self.get_object()
        if not session.reopen(request.user):
            return Response(
                {"detail": "La session n'est pas soumise."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"detail": "Session réouverte. Vous pouvez modifier les absences."},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"])
    def sheet(self, request, pk=None):
        """Feuille complète : session + statut de chaque élève."""
        session  = self.get_object()
        students = Student.objects.filter(
            school_class=session.schedule_entry.school_class
        ).select_related("user").order_by("user__last_name", "user__first_name")

        absences_by_student = {
            a.student_id: a
            for a in session.attendances.select_related("student__user")
        }

        students_data = [
            {
                "id":         s.id,
                "name":       f"{s.user.last_name} {s.user.first_name}",
                "status":     absences_by_student[s.id].status if s.id in absences_by_student else "PRESENT",
                "reason":     absences_by_student[s.id].reason if s.id in absences_by_student else None,
                "absence_id": absences_by_student[s.id].id    if s.id in absences_by_student else None,
            }
            for s in students
        ]

        return Response({
            "session":  AttendanceSessionSerializer(session, context={"request": request}).data,
            "students": students_data,
        })


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT ATTENDANCE VIEWSET — remplace l'existant
# ─────────────────────────────────────────────────────────────────────────────

class StudentAttendanceViewSet(viewsets.ModelViewSet):
    """
    CRUD des absences individuelles au sein d'une session OPEN.

    POST   → marquer un élève absent/retard/excusé
    PATCH  → modifier statut ou motif
    DELETE → supprimer l'absence (= marquer présent)

    Les notifications ne partent QU'à la soumission de la session,
    pas au marquage individuel.
    """

    serializer_class   = StudentAttendanceSerializer
    permission_classes = [IsAuthenticated]
    filter_backends    = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields   = {
        "session":  ["exact"],
        "student":  ["exact"],
        "date":     ["exact", "gte", "lte"],
        "status":   ["exact"],
    }
    ordering_fields = ["date", "status"]
    ordering        = ["-date"]

    def get_queryset(self):
        user = self.request.user
        qs   = StudentAttendance.objects.select_related(
            "session__schedule_entry__subject",
            "session__schedule_entry__school_class",
            "student__user",
            "marked_by",
        )
        if user.is_staff or user.is_superuser:
            return qs
        if hasattr(user, "teacher"):
            return qs.filter(session__schedule_entry__teacher=user.teacher)
        # Parents et élèves : lecture seule sur leurs propres données
        if hasattr(user, "parent"):
            return qs.filter(student__parent=user.parent)
        if hasattr(user, "student"):
            return qs.filter(student=user.student)
        return qs.none()

    def create(self, request, *args, **kwargs):
        # Seuls staff, admin et teachers peuvent créer des absences
        user = request.user
        if not (user.is_staff or user.is_superuser or hasattr(user, "teacher")):
            return Response(
                {"detail": "Vous n'êtes pas autorisé à enregistrer des absences."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        user = request.user
        if not (user.is_staff or user.is_superuser or hasattr(user, "teacher")):
            return Response(
                {"detail": "Vous n'êtes pas autorisé à modifier des absences."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        user     = request.user
        instance = self.get_object()
        if not (user.is_staff or user.is_superuser or hasattr(user, "teacher")):
            return Response(
                {"detail": "Vous n'êtes pas autorisé à supprimer des absences."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not instance.session.is_editable:
            return Response(
                {"detail": "Impossible de modifier une session soumise. Demandez une réouverture."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
#  DAILY ATTENDANCE SHEET — remplace l'existant
# ─────────────────────────────────────────────────────────────────────────────

class DailyAttendanceSheetView(APIView):
    """
    GET /academics/attendance/daily-sheet/?class_id=<id>&date=<YYYY-MM-DD>

    Retourne les créneaux du jour avec leur session (créée automatiquement si absente)
    et le statut de présence de chaque élève.

    Workflow frontend :
        1. Appel → sessions OPEN créées si inexistantes
        2. Admin coche les absents via POST /attendance/absences/
        3. Admin valide via POST /attendance/sessions/{id}/submit/
        → notifications envoyées aux parents uniquement à l'étape 3
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        class_id          = request.query_params.get("class_id")
        date_str          = request.query_params.get("date")
        schedule_entry_id = request.query_params.get("schedule_entry_id")

        if not class_id or not date_str:
            return Response(
                {"detail": "class_id et date sont obligatoires."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"detail": "Format de date invalide. Attendu : YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        weekday    = target_date.weekday()
        entries_qs = ClassScheduleEntry.objects.filter(
            school_class_id=class_id, weekday=weekday
        ).select_related("subject", "teacher__user")

        if schedule_entry_id:
            entries_qs = entries_qs.filter(id=schedule_entry_id)

        # Restriction enseignant : ne voit que ses propres cours
        user = request.user
        if not (user.is_staff or user.is_superuser) and hasattr(user, "teacher"):
            entries_qs = entries_qs.filter(teacher=user.teacher)

        entries = list(entries_qs.order_by("starts_at"))

        if not entries:
            return Response({
                "date":    date_str,
                "weekday": weekday,
                "slots":   [],
                "message": "Aucun cours prévu ce jour pour cette classe.",
            })

        # Charger les élèves une seule fois pour toutes les sessions
        students = list(
            Student.objects.filter(school_class_id=class_id)
            .select_related("user")
            .order_by("user__last_name", "user__first_name")
        )

        slots = []
        for entry in entries:
            session, _ = AttendanceSession.objects.get_or_create(
                schedule_entry=entry,
                date=target_date,
                defaults={
                    "opened_by": request.user,
                    "status":    AttendanceSession.Status.OPEN,
                },
            )

            absences_by_student = {
                a.student_id: a
                for a in session.attendances.all()
            }

            slots.append({
                "entry": {
                    "id":        entry.id,
                    "subject":   entry.subject.name,
                    "starts_at": str(entry.starts_at),
                    "ends_at":   str(entry.ends_at),
                    "teacher":   (
                        entry.teacher.user.get_full_name()
                        if entry.teacher and hasattr(entry.teacher, "user")
                        else "N/A"
                    ),
                },
                "session":  AttendanceSessionSerializer(session, context={"request": request}).data,
                "students": [
                    {
                        "id":         s.id,
                        "name":       f"{s.user.last_name} {s.user.first_name}",
                        "status":     absences_by_student[s.id].status if s.id in absences_by_student else "PRESENT",
                        "reason":     absences_by_student[s.id].reason if s.id in absences_by_student else None,
                        "absence_id": absences_by_student[s.id].id    if s.id in absences_by_student else None,
                    }
                    for s in students
                ],
            })

        return Response({"date": date_str, "weekday": weekday, "slots": slots})


# ─────────────────────────────────────────────────────────────────────────────
#  HISTORIQUE & ASSIDUITÉ — nouvelle vue
# ─────────────────────────────────────────────────────────────────────────────

class StudentAttendanceHistoryView(APIView):
    """
    GET /academics/attendance/history/?student_id=<id>[&date_from=YYYY-MM-DD][&date_to=YYYY-MM-DD]

    Retourne les statistiques d'assiduité d'un élève sur une période.

    Permissions :
        staff / admin → tout élève
        teacher       → élèves de ses classes uniquement
        parent        → ses propres enfants
        student       → lui-même
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user       = request.user
        student_id = request.query_params.get("student_id")
        date_from  = request.query_params.get("date_from")
        date_to    = request.query_params.get("date_to")

        if not student_id:
            return Response({"detail": "student_id est obligatoire."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            student = Student.objects.select_related("user", "school_class").get(pk=student_id)
        except Student.DoesNotExist:
            return Response({"detail": "Élève introuvable."}, status=status.HTTP_404_NOT_FOUND)

        # --- Vérification des permissions ---
        if not (user.is_staff or user.is_superuser):
            if hasattr(user, "teacher"):
                if not ClassScheduleEntry.objects.filter(
                    school_class=student.school_class,
                    teacher=user.teacher
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
            if not d:
                return None
            try:
                return datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                return None

        stats = _compute_attendance_stats(
            student,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
        )

        return Response({
            "student_id":   student.id,
            "student_name": f"{student.user.last_name} {student.user.first_name}",
            "date_from":    date_from,
            "date_to":      date_to,
            **stats,
        })


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER — calcul assiduité (fonction interne à views.py)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_attendance_stats(student, date_from=None, date_to=None):
    """
    Calcule les stats d'assiduité pour un élève sur une période.
    Retourne un dict avec total_sessions, taux, détail par matière et par mois.
    """
    sessions_qs = AttendanceSession.objects.filter(
        status=AttendanceSession.Status.SUBMITTED,
        schedule_entry__school_class=student.school_class,
    ).select_related("schedule_entry__subject")

    if date_from:
        sessions_qs = sessions_qs.filter(date__gte=date_from)
    if date_to:
        sessions_qs = sessions_qs.filter(date__lte=date_to)

    sessions       = list(sessions_qs)
    total_sessions = len(sessions)

    if total_sessions == 0:
        return {
            "total_sessions":  0,
            "present_count":   0,
            "absent_count":    0,
            "late_count":      0,
            "excused_count":   0,
            "attendance_rate": None,
            "absence_rate":    None,
            "by_subject":      {},
            "by_month":        {},
            "absences_detail": [],
        }

    session_ids = [s.id for s in sessions]
    absences_by_session = {
        a.session_id: a
        for a in StudentAttendance.objects.filter(
            session_id__in=session_ids, student=student
        ).select_related("session__schedule_entry__subject")
    }

    absent_count  = 0
    late_count    = 0
    excused_count = 0
    by_subject    = defaultdict(lambda: {"total": 0, "present": 0, "absent": 0, "late": 0, "excused": 0})
    by_month      = defaultdict(lambda: {"total": 0, "present": 0, "absent": 0, "late": 0, "excused": 0})
    absences_detail = []

    for session in sessions:
        subject_name = getattr(session.schedule_entry.subject, "name", "Inconnu")
        month_key    = session.date.strftime("%Y-%m")

        by_subject[subject_name]["total"] += 1
        by_month[month_key]["total"]      += 1

        absence = absences_by_session.get(session.id)
        if absence:
            if absence.status == "ABSENT":
                absent_count += 1
                by_subject[subject_name]["absent"] += 1
                by_month[month_key]["absent"]      += 1
            elif absence.status == "LATE":
                late_count += 1
                by_subject[subject_name]["late"] += 1
                by_month[month_key]["late"]      += 1
            elif absence.status == "EXCUSED":
                excused_count += 1
                by_subject[subject_name]["excused"] += 1
                by_month[month_key]["excused"]      += 1
            absences_detail.append({
                "date":    str(session.date),
                "subject": subject_name,
                "status":  absence.status,
                "reason":  absence.reason,
            })
        else:
            by_subject[subject_name]["present"] += 1
            by_month[month_key]["present"]      += 1

    present_count       = total_sessions - absent_count - late_count - excused_count
    effectively_present = present_count + late_count + excused_count  # retard + excusé = présence justifiée

    attendance_rate = round(float(Decimal(effectively_present) / Decimal(total_sessions) * 100), 2)
    absence_rate    = round(float(Decimal(absent_count)        / Decimal(total_sessions) * 100), 2)

    return {
        "total_sessions":  total_sessions,
        "present_count":   present_count,
        "absent_count":    absent_count,
        "late_count":      late_count,
        "excused_count":   excused_count,
        "attendance_rate": attendance_rate,
        "absence_rate":    absence_rate,
        "by_subject":      dict(by_subject),
        "by_month":        dict(by_month),
        "absences_detail": sorted(absences_detail, key=lambda x: x["date"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER — notifications envoyées à la soumission (même pattern que le reste)
# ─────────────────────────────────────────────────────────────────────────────

def _send_absence_notifications_for_session(session_id: int):
    """
    Appelé via transaction.on_commit — suit exactement le pattern existant dans views.py.
    """
    try:
        Notification               = apps.get_model("notifications", "Notification")
        NotificationTemplate       = apps.get_model("notifications", "NotificationTemplate")
        UserNotificationPreference = apps.get_model("notifications", "UserNotificationPreference")
        from notifications.delivery import send_notification as _send
    except Exception as e:
        logger.debug("Notifications app indisponible : %s", e)
        return

    try:
        session = AttendanceSession.objects.select_related(
            "schedule_entry__subject"
        ).get(pk=session_id)
    except AttendanceSession.DoesNotExist:
        return

    try:
        template, created = NotificationTemplate.objects.get_or_create(
            key="absence_reported",
            defaults={
                "topic":            "attendance",
                "title_template":   "Absence signalée — {{ student_name }}",
                "body_template": (
                    "Bonjour {{ parent_name }}, "
                    "{{ student_name }} a été signalé(e) {{ status }} le {{ date }}"
                    "{% if subject %} en {{ subject }}{% endif %}."
                    "{% if reason %} Motif : {{ reason }}.{% endif %}"
                ),
                "default_channels": ["inapp"],
            },
        )
        if created:
            logger.info("NotificationTemplate 'absence_reported' créé (fallback).")
    except Exception as e:
        template = None
        logger.exception("Erreur get_or_create template absence_reported: %s", e)

    absences = session.attendances.filter(
        notified_at__isnull=True
    ).select_related("student__user", "student__parent__user")

    for absence in absences:
        student = absence.student
        try:
            student_name = student.user.get_full_name() if getattr(student, "user", None) else str(student.id)
        except Exception:
            student_name = str(getattr(student, "id", ""))

        subject_name = getattr(session.schedule_entry.subject, "name", None)

        # Récupérer le/les parents (même logique que l'ancien _iter_student_parents)
        recipients = []
        if hasattr(student, "parent") and student.parent is not None:
            user_obj = getattr(student.parent, "user", None)
            if user_obj:
                recipients.append(user_obj)
        if not recipients and hasattr(student, "parents"):
            try:
                for p in student.parents.all():
                    u = getattr(p, "user", None)
                    if u:
                        recipients.append(u)
            except Exception:
                pass
        # Fallback : notifier l'élève lui-même
        if not recipients and getattr(student, "user", None):
            recipients.append(student.user)

        for recipient_user in recipients:
            # Déduplication
            if Notification.objects.filter(
                topic="attendance",
                recipient_user=recipient_user,
                payload__student_id=student.id,
                payload__session_id=session.id,
            ).exists():
                continue

            channels = (
                template.default_channels
                if template and getattr(template, "default_channels", None)
                else ["inapp"]
            )
            try:
                pref = UserNotificationPreference.objects.filter(
                    user=recipient_user, topic="attendance"
                ).first()
                if pref and not pref.enabled:
                    continue
                if pref and pref.channels:
                    channels = pref.channels
            except Exception:
                logger.exception(
                    "Erreur UserNotificationPreference user %s",
                    getattr(recipient_user, "id", None),
                )

            payload = {
                "student_id":   student.id,
                "student_name": student_name,
                "session_id":   session.id,
                "date":         str(session.date),
                "subject":      subject_name,
                "status":       absence.status,
                "reason":       absence.reason or None,
                "parent_name":  (
                    recipient_user.get_full_name()
                    if getattr(recipient_user, "get_full_name", None)
                    else getattr(recipient_user, "username", "")
                ),
            }

            try:
                notif = Notification.objects.create(
                    template=template,
                    topic="attendance",
                    recipient_user=recipient_user,
                    payload=payload,
                    channels=channels,
                )
                try:
                    transaction.on_commit(lambda n=notif: _send(n))
                except Exception:
                    try:
                        _send(notif)
                    except Exception as e:
                        logger.exception(
                            "Fallback send_notification failed notif %s: %s",
                            getattr(notif, "id", None), e,
                        )
                absence.notified_at = timezone.now()
                absence.save(update_fields=["notified_at"])
            except Exception:
                logger.exception(
                    "Failed to create Notification recipient=%s student=%s",
                    getattr(recipient_user, "id", None),
                    getattr(student, "id", None),
                )


# =============================================================================
#  URLS — à ajouter dans academics/urls.py
# =============================================================================
#
#  from academics.views import (
#      AttendanceSessionViewSet,
#      StudentAttendanceViewSet,
#      DailyAttendanceSheetView,
#      StudentAttendanceHistoryView,
#  )
#
#  router.register(r"attendance/sessions", AttendanceSessionViewSet, basename="attendance-session")
#  router.register(r"attendance/absences", StudentAttendanceViewSet, basename="student-attendance")
#
#  urlpatterns += [
#      path("attendance/daily-sheet/", DailyAttendanceSheetView.as_view(), name="attendance-daily-sheet"),
#      path("attendance/history/",     StudentAttendanceHistoryView.as_view(), name="attendance-history"),
#  ]
#
# =============================================================================
# ─────────────────────────────────────────────────────────────────────────────
#  SUBJECT COMMENTS
# ─────────────────────────────────────────────────────────────────────────────

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
                subject=teacher.subject,
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
        term    = serializer.validated_data["term"]

        if student.school_class not in teacher.classes.all():
            raise PermissionDenied("Vous ne pouvez commenter que vos propres élèves.")
        if subject != teacher.subject:
            raise PermissionDenied("Vous ne pouvez commenter que votre matière.")
        if SubjectComment.objects.filter(student=student, subject=subject, term=term).exists():
            raise serializers.ValidationError("Un commentaire pour cet élève, cette matière et ce trimestre existe déjà.")

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
    queryset = TimeSlot.objects.all().order_by("start_time")
    serializer_class = TimeSlotSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return self.queryset
        return TimeSlot.objects.none()


# ─────────────────────────────────────────────────────────────────────────────
#  GENERATE TIMETABLE
# ─────────────────────────────────────────────────────────────────────────────

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
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  TIMETABLE CONFLICTS
# ─────────────────────────────────────────────────────────────────────────────

class TimetableConflictsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        return Response(detect_teacher_conflicts(), status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        dry_run = bool(request.data.get("dry_run", True))
        persist = bool(request.data.get("persist", False))
        if persist and not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {"detail": "Seuls les admins peuvent appliquer les résolutions."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(detect_and_resolve(dry_run=dry_run, persist=persist), status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEDULE CHECK
# ─────────────────────────────────────────────────────────────────────────────

from academics.services.schedule_checker import run_check

class ScheduleCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        params = request.query_params
        class_id = params.get("class_id")
        limit    = params.get("limit")
        verbose  = params.get("verbose")

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
            return Response(
                run_check(class_id=class_id_val, limit=limit_val, verbose=verbose_val),
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            return Response(
                {"detail": f"Erreur lors de l'analyse: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  TIMETABLE BATCH VALIDATE / APPLY
# ─────────────────────────────────────────────────────────────────────────────

def _to_minutes_from_timeobj(t: time) -> int:
    return t.hour * 60 + t.minute


def _load_slots_ordered() -> List[Dict[str, Any]]:
    qs = list(TimeSlot.objects.all().order_by("day", "start_time", "end_time"))
    slots = []
    for idx, s in enumerate(qs):
        st = s.start_time
        et = s.end_time
        if st is None or et is None:
            continue
        start_min = _to_minutes_from_timeobj(st)
        end_min   = _to_minutes_from_timeobj(et)
        if end_min <= start_min:
            continue
        slots.append({
            "idx": idx, "db_obj": s, "weekday": s.day,
            "start": start_min, "end": end_min, "dur": end_min - start_min,
        })
    return slots


def _parse_time_str_or_obj(s: Optional[str]) -> Optional[time]:
    if s is None:
        return None
    if isinstance(s, time):
        return s
    return parse_time(s)


def _overlaps(a_weekday, a_start, a_end, b_weekday, b_start, b_end) -> bool:
    if a_weekday != b_weekday:
        return False
    return (a_start < b_end) and (b_start < a_end)


# =============================================================================
#  VUES — à intégrer dans academics/views.py
#  Remplacer TimetableBatchValidateView et TimetableBatchApplyView
# =============================================================================

from academics.services.timetable_batch import (
    validate_batch_operations,
    apply_batch_operations,
)

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status


class TimetableBatchValidateView(APIView):
    """
    POST /timetable/batch-validate/

    Simule des déplacements d'entrées et retourne un rapport complet
    SANS toucher à la DB.

    Body :
    {
        "operations": [
            {"entry_id": 42, "target_slot_idx": 7},
            {
                "entry_id": 55,
                "target_weekday": 2,
                "target_start": "10:00",
                "target_end": "12:00"
            }
        ]
    }

    Réponse :
    {
        "valid": true|false,
        "hard_errors": [...],
        "soft_warnings": [...],
        "preview": {
            "42": {"from": {...}, "to": {...}},
            ...
        }
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {"detail": "Réservé aux administrateurs."},
                status=status.HTTP_403_FORBIDDEN,
            )

        ops = (request.data or {}).get("operations")

        if not isinstance(ops, list) or not ops:
            return Response(
                {"detail": "Le champ 'operations' doit être une liste non-vide."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        report = validate_batch_operations(ops)
        return Response(report, status=status.HTTP_200_OK)


class TimetableBatchApplyView(APIView):
    """
    POST /timetable/batch-apply/

    Valide puis applique un batch de déplacements.
    Atomique : soit tout passe, soit rien n'est modifié.
    Re-valide dans la transaction après save — rollback automatique si conflit.

    Body :
    {
        "operations": [...],
        "force": false
    }

    Réponse :
    {
        "valid": true|false,
        "hard_errors": [...],
        "soft_warnings": [...],
        "preview": {...},
        "applied": [42, 55],
        "db_errors": [...],
        "message": "..."
    }

    Codes HTTP :
      200  → tout bon
      400  → erreurs de parsing ou conflits durs
      403  → non-admin
      500  → erreur DB inattendue
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {
                    "detail": "Seuls les administrateurs peuvent modifier l'emploi du temps."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        payload = request.data or {}
        ops = payload.get("operations")
        force = bool(payload.get("force", False))

        if not isinstance(ops, list) or not ops:
            return Response(
                {"detail": "Le champ 'operations' doit être une liste non-vide."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = apply_batch_operations(ops, force=force)

        if not result["valid"]:
            http_status = (
                status.HTTP_500_INTERNAL_SERVER_ERROR
                if result.get("db_errors")
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(result, status=http_status)

        return Response(result, status=status.HTTP_200_OK)





# ─────────────────────────────────────────────────────────────────────────────
#  academics/views.py — Ajouter cette vue
# ─────────────────────────────────────────────────────────────────────────────
#
#  POST /api/academics/class-subjects/copy-config/
#
#  Copie la configuration complète des matières (ClassSubject) d'une classe
#  source vers une ou plusieurs classes cibles.
#
#  Body :
#  {
#      "source_class_id": 3,
#      "target_class_ids": [7, 8, 9],
#      "overwrite": false   // si true, écrase les ClassSubject existants
#  }
#
#  Comportement par défaut (overwrite=false) :
#    - Si la classe cible a déjà un ClassSubject pour cette matière → on le saute
#    - On ne copie que ce qui manque
#
#  Comportement overwrite=true :
#    - Supprime tous les ClassSubject existants de la classe cible
#    - Recopie tout depuis la source
#    ATTENTION : supprime aussi les associations prof si elles sont liées
#
#  Réponse :
#  {
#      "results": [
#          {
#              "target_class_id": 7,
#              "target_class_name": "2ndB2",
#              "created": 12,
#              "skipped": 0,
#              "overwritten": 0,
#              "errors": []
#          },
#          ...
#      ],
#      "summary": {
#          "total_created": 24,
#          "total_skipped": 0,
#          "total_errors": 0
#      }
#  }
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.db import transaction

from academics.models import ClassSubject, SchoolClass


class CopyClassConfigView(APIView):
    """
    Copie la configuration des matières d'une classe vers d'autres classes.
    Réservé aux administrateurs.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {"detail": "Seuls les administrateurs peuvent copier une configuration."},
                status=status.HTTP_403_FORBIDDEN,
            )

        payload          = request.data or {}
        source_class_id  = payload.get("source_class_id")
        target_class_ids = payload.get("target_class_ids", [])
        overwrite        = bool(payload.get("overwrite", False))

        # ── Validation des entrées ────────────────────────────────────────────
        if not source_class_id:
            return Response(
                {"detail": "source_class_id est obligatoire."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(target_class_ids, list) or not target_class_ids:
            return Response(
                {"detail": "target_class_ids doit être une liste non-vide."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Récupérer la classe source
        try:
            source_class = SchoolClass.objects.get(id=source_class_id)
        except SchoolClass.DoesNotExist:
            return Response(
                {"detail": f"Classe source (id={source_class_id}) introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Récupérer les ClassSubjects de la source
        source_configs = list(
            ClassSubject.objects.filter(school_class=source_class)
            .select_related("subject")
        )
        if not source_configs:
            return Response(
                {
                    "detail": f"La classe source '{source_class}' n'a aucune matière configurée.",
                    "source_class_id": source_class_id,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        results = []
        total_created   = 0
        total_skipped   = 0
        total_errors    = 0

        for target_id in target_class_ids:
            result = {
                "target_class_id":   target_id,
                "target_class_name": None,
                "created":           0,
                "skipped":           0,
                "overwritten":       0,
                "errors":            [],
            }

            # Récupérer la classe cible
            try:
                target_class = SchoolClass.objects.get(id=target_id)
            except SchoolClass.DoesNotExist:
                result["errors"].append(f"Classe cible (id={target_id}) introuvable.")
                total_errors += 1
                results.append(result)
                continue

            if target_id == source_class_id:
                result["errors"].append("La classe cible est identique à la source.")
                results.append(result)
                continue

            result["target_class_name"] = str(target_class)

            try:
                with transaction.atomic():
                    if overwrite:
                        # Supprimer toute la config existante de la cible
                        deleted_count, _ = ClassSubject.objects.filter(
                            school_class=target_class
                        ).delete()
                        result["overwritten"] = deleted_count

                    # Récupérer les matières déjà présentes dans la cible
                    # (utile uniquement si overwrite=False)
                    existing_subject_ids = set(
                        ClassSubject.objects.filter(school_class=target_class)
                        .values_list("subject_id", flat=True)
                    )

                    to_create = []
                    for cs in source_configs:
                        if cs.subject_id in existing_subject_ids:
                            # Matière déjà configurée et overwrite=False → on saute
                            result["skipped"] += 1
                            total_skipped += 1
                            continue

                        to_create.append(ClassSubject(
                            school_class   = target_class,
                            subject        = cs.subject,
                            coefficient    = cs.coefficient,
                            hours_per_week = cs.hours_per_week,
                            is_optional    = cs.is_optional,
                        ))

                    # Bulk create pour la performance
                    if to_create:
                        ClassSubject.objects.bulk_create(to_create)
                        result["created"] = len(to_create)
                        total_created    += len(to_create)

            except Exception as exc:
                result["errors"].append(str(exc))
                total_errors += 1

            results.append(result)

        return Response(
            {
                "source_class_id":   source_class_id,
                "source_class_name": str(source_class),
                "source_subjects_count": len(source_configs),
                "overwrite": overwrite,
                "results": results,
                "summary": {
                    "total_created": total_created,
                    "total_skipped": total_skipped,
                    "total_errors":  total_errors,
                },
            },
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  academics/urls.py — Ajouter cette ligne dans urlpatterns
# ─────────────────────────────────────────────────────────────────────────────
#
#  from academics.views import CopyClassConfigView
#
#  path('class-subjects/copy-config/', CopyClassConfigView.as_view(),
#       name='copy-class-config'),
#
# ─────────────────────────────────────────────────────────────────────────────
#
#  EXEMPLES D'UTILISATION
# ─────────────────────────────────────────────────────────────────────────────
#
#  1. Copier la config de la classe 3 vers les classes 7, 8, 9
#     (sans écraser ce qui existe déjà) :
#
#     POST /api/academics/class-subjects/copy-config/
#     {
#         "source_class_id": 3,
#         "target_class_ids": [7, 8, 9]
#     }
#
#  2. Copier en écrasant toute la config existante des cibles :
#
#     POST /api/academics/class-subjects/copy-config/
#     {
#         "source_class_id": 3,
#         "target_class_ids": [7, 8, 9],
#         "overwrite": true
#     }
#
#  3. Dupliquer vers une seule classe :
#
#     POST /api/academics/class-subjects/copy-config/
#     {
#         "source_class_id": 3,
#         "target_class_ids": [10]
#     }
#
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#  ANNOUNCEMENTS
# ─────────────────────────────────────────────────────────────────────────────

class AnnouncementViewSet(viewsets.ModelViewSet):
    queryset = Announcement.objects.all()
    serializer_class = AnnouncementSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    parser_classes = [MultiPartParser, FormParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["title", "content"]
    ordering_fields = ["created_at"]
    pagination_class = None

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)
    