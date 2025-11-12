# academics/views.py
import json
import logging
import random
import math
import time
from collections import defaultdict
from datetime import datetime

from django.db import transaction
from django.contrib.auth.models import User

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, NotFound

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


# ----------------------------
# SCHOOL CLASSES
# ----------------------------
class SchoolClassViewSet(viewsets.ModelViewSet):
    queryset = SchoolClass.objects.all()
    serializer_class = SchoolClassSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user

        # --- Cas admin : accès total ---
        if user.is_staff or user.is_superuser:
            return SchoolClass.objects.all()

        # --- Cas enseignant ---
        if hasattr(user, "teacher"):
            teacher = user.teacher
            return teacher.classes.all()

        # --- Cas parent ---
        if hasattr(user, "parent"):
            parent = user.parent
            if not parent:
                return SchoolClass.objects.none()
            return SchoolClass.objects.filter(students__parent=parent).distinct()

        # --- Cas élève ---
        if hasattr(user, "student"):
            student = user.student
            if not student:
                return SchoolClass.objects.none()
            return SchoolClass.objects.filter(students=student)

        return SchoolClass.objects.none()


# ----------------------------
# SUBJECTS
# ----------------------------
class SubjectViewSet(viewsets.ModelViewSet):
    queryset = Subject.objects.all()
    serializer_class = SubjectSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]


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


# ----------------------------
# GRADE
# ----------------------------
# imports utiles (si pas déjà présents en haut du fichier)
from django.db import transaction
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action

class GradeViewSet(viewsets.ModelViewSet):
    queryset = Grade.objects.all()
    serializer_class = GradeSerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend]
    filterset_class = GradeFilter

    def get_queryset(self):
        user = self.request.user
        qs = Grade.objects.select_related("student", "student__school_class", "subject")

        # Admin / staff
        if user.is_staff or user.is_superuser:
            return qs

        # Enseignant
        if hasattr(user, "teacher"):
            teacher = user.teacher
            allowed_classes = teacher.classes.all()
            qs = qs.filter(student__school_class__in=allowed_classes).distinct()
            student_id = self.request.query_params.get("student")
            if student_id:
                qs = qs.filter(student__id=student_id)
            return qs

        # Parent
        if hasattr(user, "parent"):
            parent = user.parent
            if parent:
                qs = qs.filter(student__parent=parent)
                student_id = self.request.query_params.get("student")
                if student_id:
                    qs = qs.filter(student__id=student_id)
                return qs
            return Grade.objects.none()

        # Élève
        if hasattr(user, "student"):
            student = user.student
            if student:
                return qs.filter(student=student)
            return Grade.objects.none()

        return Grade.objects.none()

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

                # --- IMPORTANT: detecter quels champs ont été fournis dans le JSON ---
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

                    else:
                        g, created_flag = Grade.objects.select_for_update().update_or_create(
                            student=student,
                            subject=subject,
                            term=term,
                            defaults=defaults
                        )
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
                except Exception as e:
                    errors += 1
                    results.append({
                        "index": idx,
                        "student_id": getattr(student, "id", None),
                        "subject_id": getattr(subject, "id", None),
                        "status": "error",
                        "errors": str(e)
                    })

        return Response({"created": created, "updated": updated, "errors": errors, "results": results})


# ----------------------------
# CLASS SCHEDULE (CRUD)
# ----------------------------
class ClassScheduleEntryViewSet(viewsets.ModelViewSet):
    queryset = ClassScheduleEntry.objects.all()
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated, IsAdminOrReadOnly]

    def get_queryset(self):
        user = self.request.user

        if user.is_staff or user.is_superuser:
            return ClassScheduleEntry.objects.all()

        # --- Enseignant : emploi du temps de ses classes ---
        if hasattr(user, "teacher"):
            teacher = user.teacher
            return ClassScheduleEntry.objects.filter(school_class__in=teacher.classes.all())

        # --- Élève : emploi du temps de sa classe ---
        if hasattr(user, "student"):
            student = user.student
            if not student or not student.school_class:
                return ClassScheduleEntry.objects.none()
            return ClassScheduleEntry.objects.filter(school_class=student.school_class)

        # --- Parent : emploi du temps des classes de ses enfants ---
        if hasattr(user, "parent"):
            parent = user.parent
            if not parent:
                return ClassScheduleEntry.objects.none()

            qs = ClassScheduleEntry.objects.filter(
                school_class__in=parent.students.values_list("school_class", flat=True)
            )

            # Optionnel : filtrer par enfant précis
            student_id = self.request.query_params.get("student")
            if student_id:
                if parent.students.filter(id=student_id).exists():
                    qs = qs.filter(
                        school_class__in=parent.students.filter(id=student_id).values_list("school_class", flat=True)
                    )
                else:
                    return ClassScheduleEntry.objects.none()

            return qs

        return ClassScheduleEntry.objects.none()


# ----------------------------
# REPORT CARDS
# ----------------------------
def _parse_bool(val: str) -> bool:
    if val is None:
        return False
    return str(val).lower() in ("1", "true", "yes", "y", "on")


from django.db.models import Q

class ReportCardViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _get_teacher_students_qs(self, teacher):
        """
        Essaye plusieurs façons de récupérer les élèves liés à un teacher :
         - teacher.students (si tu as M2M/related_name)
         - via teacher.classes (si teacher.classes -> SchoolClass)
         - via Grade.objects.filter(teacher=teacher) -> student ids
        Retourne un QuerySet de Student ou None si impossible.
        """
        Student = Grade._meta.get_field("student").related_model  # safe way to get Student model

        # 1) relation directe teacher.students
        try:
            qs = teacher.students.all()
            # si la relation existe et n'est pas vide (ou bien on accepte vide)
            return qs
        except Exception:
            pass

        # 2) teacher.classes -> récupérer élèves dans ces classes
        try:
            classes_qs = teacher.classes.all()
            if classes_qs.exists():
                return Student.objects.filter(school_class__in=classes_qs)
        except Exception:
            pass

        # 3) fallback via grades (les élèves pour qui il y a des notes signées par ce teacher)
        try:
            student_ids = Grade.objects.filter(teacher=teacher).values_list("student_id", flat=True).distinct()
            return Student.objects.filter(pk__in=student_ids)
        except Exception:
            pass

        # si rien, retourne None pour laisser le comportement upstream (ou empty)
        return None

    def list(self, request):
        user = request.user

        # params...
        student_id = request.query_params.get("student_id")
        class_id = request.query_params.get("class_id")
        term = request.query_params.get("term")
        include_missing_subjects = _parse_bool(request.query_params.get("include_missing_subjects"))
        full_weighting = _parse_bool(request.query_params.get("full_weighting"))

        # ---------- Périmètre pour calcul des rangs ----------
        ranking_students_qs = None

        # Admins voient tout
        if user.is_staff or user.is_superuser:
            ranking_students_qs = None  # None signale "tout" dans ton code existant

        # Étudiant connecté -> sa classe
        elif hasattr(user, "student") and user.student.school_class_id:
            Student = Grade._meta.get_field("student").related_model
            ranking_students_qs = Student.objects.filter(school_class_id=user.student.school_class_id)

        # Parent connecté -> classes des enfants
        elif hasattr(user, "parent"):
            children_classes = user.parent.students.values_list('school_class_id', flat=True).distinct()
            Student = Grade._meta.get_field("student").related_model
            ranking_students_qs = Student.objects.filter(school_class_id__in=children_classes)

        # Enseignant connecté -> récupérer ses élèves (plusieurs cas essayés)
        elif hasattr(user, "teacher"):
            teacher = user.teacher
            teacher_students = self._get_teacher_students_qs(teacher)
            # si on récupère un queryset vide, on le prend ; si None -> on laisse None (mais on peut préférer .none())
            if teacher_students is not None:
                ranking_students_qs = teacher_students
            else:
                # sécurité : si on n'a aucune info sur le teacher, on n'autorise rien
                from django.apps import apps
                Student = Grade._meta.get_field("student").related_model
                ranking_students_qs = Student.objects.none()

        # ---------- Construction du queryset grades ----------
        grades_qs = Grade.objects.select_related("student", "student__school_class", "subject")
        if ranking_students_qs is not None:
            grades_qs = grades_qs.filter(student__in=ranking_students_qs)

        if class_id:
            grades_qs = grades_qs.filter(student__school_class__id=class_id)
        if term:
            grades_qs = grades_qs.filter(term__iexact=term)

        grades_qs = grades_qs.order_by("student_id", "term")

        # Calcul des bulletins
        all_report_cards = compute_report_cards_from_grades(
            grades_qs,
            include_missing_subjects=include_missing_subjects,
            full_weighting=full_weighting,
        )

        # ---------- Filtrage final selon rôle / params ----------
        if user.is_staff or user.is_superuser:
            filtered_report_cards = all_report_cards

        elif hasattr(user, "student"):
            student_id_str = str(user.student.pk)
            filtered_report_cards = [it for it in all_report_cards if str(it["student"].pk) == student_id_str]

        elif hasattr(user, "parent"):
            requested_ids = set(str(pk) for pk in user.parent.students.values_list("pk", flat=True))
            filtered_report_cards = [it for it in all_report_cards if str(it["student"].pk) in requested_ids]

        elif hasattr(user, "teacher"):
            # si teacher_students était calculé ci-dessus, on peut réutiliser sa liste d'ids
            teacher = user.teacher
            teacher_students_qs = self._get_teacher_students_qs(teacher) or Grade._meta.get_field("student").related_model.objects.none()
            teacher_ids = set(str(pk) for pk in teacher_students_qs.values_list("pk", flat=True))
            filtered_report_cards = [it for it in all_report_cards if str(it["student"].pk) in teacher_ids]

        elif student_id:
            filtered_report_cards = [it for it in all_report_cards if str(it["student"].pk) == str(student_id)]

        else:
            # aucune restriction détectée (ex : une API interne) -> tant que la permission le permet on renvoie tout
            filtered_report_cards = all_report_cards

        # tri + sérialisation comme avant...
        filtered_report_cards.sort(key=lambda it: (str(it["student"]).lower(), it["term"]))
        serializer = ReportCardSerializer(filtered_report_cards, many=True, context={"request": request})
        return Response(serializer.data)

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
class TimeSlotViewSet(viewsets.ModelViewSet):
    queryset = TimeSlot.objects.all()
    serializer_class = TimeSlotSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return TimeSlot.objects.all()
        # Ici, tu peux filtrer par classe si nécessaire
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

    Règles d'accès :
      - staff / superuser : voient tout
      - étudiant : uniquement sa classe
      - parent : classes de tous ses enfants
      - enseignant : uniquement les classes où il intervient
    """
    queryset = ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher")
    serializer_class = ClassScheduleEntrySerializer
    permission_classes = [IsAuthenticated]

    # backends
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["school_class", "teacher", "weekday", "school_class__level"]
    search_fields = ["school_class__name", "subject__name", "teacher__user__last_name", "teacher__user__first_name"]
    ordering_fields = ["weekday", "starts_at"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        user = self.request.user

        # ---------- Debug visible (utile en dev) ----------
        print("== TIMETABLE DEBUG raw QUERY_STRING ==", self.request.META.get("QUERY_STRING"))
        print("== TIMETABLE DEBUG get_full_path ==", self.request.get_full_path())
        print("== TIMETABLE DEBUG query_params dict ==", dict(params))

        def clean_val(v):
            if v is None:
                return None
            s = str(v).strip()
            while s.endswith('/'):
                s = s[:-1]
            return s

        # accept multiple possible param names (robuste)
        class_id_raw = params.get("class_id") or params.get("school_class") or params.get("school_class_id")
        teacher_id_raw = params.get("teacher_id") or params.get("teacher")
        level_id_raw = params.get("level_id") or params.get("school_class__level")
        weekday_raw = params.get("weekday")

        class_id = clean_val(class_id_raw)
        teacher_id = clean_val(teacher_id_raw)
        level_id = clean_val(level_id_raw)
        weekday = clean_val(weekday_raw)

        print("== TIMETABLE DEBUG cleaned params ==", {
            "class_id": class_id,
            "teacher_id": teacher_id,
            "level_id": level_id,
            "weekday": weekday
        })

        # ---------- Calculer périmètre autorisé (school_class ids) ----------
        allowed_class_ids = None  # None = "tout" (admin)

        # Admins voient tout
        if user.is_staff or user.is_superuser:
            allowed_class_ids = None

        # Étudiant -> uniquement sa classe
        elif hasattr(user, "student") and getattr(user.student, "school_class_id", None):
            allowed_class_ids = {user.student.school_class_id}

        # Parent -> classes de tous ses enfants
        elif hasattr(user, "parent"):
            allowed_class_ids = set(user.parent.students.values_list('school_class_id', flat=True).distinct())

        # Teacher -> essayer plusieurs stratégies pour récupérer les classes qu'il enseigne
        elif hasattr(user, "teacher"):
            teacher = user.teacher
            allowed_class_ids = set()
            # 1) relation teacher.classes (M2M) s'il existe
            try:
                classes_qs = teacher.classes.all()
                if classes_qs.exists():
                    allowed_class_ids.update(classes_qs.values_list("pk", flat=True))
            except Exception:
                # ignore si la relation n'existe pas
                pass

            # 2) fallback : regarder les entrées d'emploi du temps existantes signées par ce teacher
            if not allowed_class_ids:
                try:
                    allowed_class_ids.update(
                        ClassScheduleEntry.objects.filter(teacher=teacher)
                        .values_list("school_class_id", flat=True)
                        .distinct()
                    )
                except Exception:
                    pass

            # si on n'a vraiment rien, allowed_class_ids restera set() -> aucune autorisation

        else:
            # Utilisateur anonyme / autre rôle : aucune classe autorisée
            allowed_class_ids = set()

        print("== TIMETABLE DEBUG allowed_class_ids =", allowed_class_ids if allowed_class_ids is not None else "ALL")

        # ---------- Appliquer les filtres fournis, mais en intersectant avec allowed_class_ids ----------
        # Handle class_id param (int or name)
        if class_id:
            try:
                class_pk = int(class_id)
                if allowed_class_ids is not None and class_pk not in allowed_class_ids:
                    # l'utilisateur demande une classe qu'il n'est pas autorisé à voir
                    return qs.none()
                qs = qs.filter(school_class_id=class_pk)
                print(f"== TIMETABLE DEBUG applied filter school_class_id={class_pk}")
            except Exception:
                # treat as name
                if allowed_class_ids is not None:
                    qs = qs.filter(school_class__id__in=list(allowed_class_ids), school_class__name=class_id)
                else:
                    qs = qs.filter(school_class__name=class_id)
                print(f"== TIMETABLE DEBUG applied filter school_class__name='{class_id}'")
            print("== TIMETABLE DEBUG count after class filter =", qs.count())
        else:
            # si pas de class_id fourni, restreindre globalement si nécessaire
            if allowed_class_ids is not None:
                if len(allowed_class_ids) == 0:
                    return qs.none()
                qs = qs.filter(school_class_id__in=list(allowed_class_ids))
                print("== TIMETABLE DEBUG applied allowed_class_ids restriction")

        # teacher_id filter
        if teacher_id:
            try:
                tpk = int(teacher_id)
                # si user est teacher, n'autorise que sa propre pk
                if hasattr(user, "teacher") and (user.teacher.pk != tpk):
                    return qs.none()
                qs = qs.filter(teacher_id=tpk)
                print(f"== TIMETABLE DEBUG applied filter teacher_id={tpk}")
            except Exception:
                # string/username case
                if hasattr(user, "teacher"):
                    # teacher cannot impersonate another teacher -> restrict to self
                    qs = qs.filter(teacher_id=user.teacher.pk)
                    print("== TIMETABLE DEBUG teacher requested a teacher string but user is teacher -> restricted to self")
                else:
                    # try lookup by username
                    qs = qs.filter(teacher__user__username=teacher_id)
                    print(f"== TIMETABLE DEBUG applied filter teacher__user__username='{teacher_id}'")
            print("== TIMETABLE DEBUG count after teacher filter =", qs.count())

        # level filter (honored but still confined by allowed classes)
        if level_id:
            try:
                v = int(level_id)
                qs = qs.filter(school_class__level_id=v)
                print(f"== TIMETABLE DEBUG applied filter level_id={v}")
            except Exception:
                qs = qs.filter(school_class__level__name=level_id)
                print(f"== TIMETABLE DEBUG applied filter level__name='{level_id}'")
            print("== TIMETABLE DEBUG count after level filter =", qs.count())

        # weekday filter
        if weekday:
            try:
                v = int(weekday)
                qs = qs.filter(weekday=v)
                print(f"== TIMETABLE DEBUG applied filter weekday={v}")
            except Exception:
                print("== TIMETABLE DEBUG invalid weekday filter (ignored) =", weekday)
            print("== TIMETABLE DEBUG count after weekday filter =", qs.count())

        # Final SQL for inspection
        try:
            print("== TIMETABLE DEBUG final SQL ==", str(qs.query))
        except Exception as e:
            print("== TIMETABLE DEBUG error building SQL ==", e)

        print("== TIMETABLE DEBUG final count =", qs.count())
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
