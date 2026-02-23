from collections import defaultdict
from decimal import Decimal
import csv
import io
import logging

from django.db import transaction, connection
from django.db.models import Count, Prefetch
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page

from rest_framework import viewsets, generics, status, filters
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework_simplejwt.tokens import RefreshToken
from .serializers import TeacherFullSerializer, TeacherWriteSerializer


from django_filters.rest_framework import DjangoFilterBackend

# core models & serializers
from .models import Parent, Student, Teacher
from .serializers import (
    ParentSerializer,
    StudentSerializer,
    TeacherSerializer,
    ParentProfileSerializer,
    StudentProfileSerializer,
    StudentListSerializer,
)

# permissions
from .permissions import IsParentOrReadOnly, IsTeacherReadOnly

# academics (utilisés pour grades / classes / report card)
from academics.models import SchoolClass, Grade, ClassSubject
from academics.services.report_cards import compute_report_cards_from_grades

logger = logging.getLogger(__name__)


class StudentViewSet(viewsets.ModelViewSet):
    queryset = Student.objects.all()
    permission_classes = [IsAuthenticated, IsParentOrReadOnly]

    # Filtrage / recherche / ordering
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["user__first_name", "user__last_name", "user__username", "user__email"]
    filterset_fields = ["school_class", "sex"]
    ordering_fields = ["user__first_name", "user__last_name", "date_of_birth"]
    ordering = ["user__last_name", "user__first_name"]

    def get_queryset(self):
        user = self.request.user
        # charge légerement utile : select_related pour éviter N+1 sur user / parent / class
        queryset = Student.objects.select_related(
            "user",
            "school_class",
            "parent__user",
        ).all()

        # Admin / staff
        if user.is_staff or user.is_superuser:
            return queryset

        # Parent : uniquement ses enfants
        if hasattr(user, "parent"):
            return queryset.filter(parent=user.parent)

        # Élève : uniquement lui-même
        if hasattr(user, "student"):
            return queryset.filter(user=user)

        # Enseignant : élèves des classes où il enseigne
        if hasattr(user, "teacher"):
            teacher = user.teacher
            class_ids = teacher.classes.values_list("id", flat=True)
            return queryset.filter(school_class_id__in=class_ids).distinct()

        return queryset.none()

    def get_serializer_class(self):
        # Serializer léger pour la liste (performances)
        if self.action == "list":
            return StudentListSerializer
        # Détail / create / update utilisent les serializers complets
        if self.action == "retrieve":
            return StudentProfileSerializer
        return StudentSerializer

    @action(detail=False, methods=["post"], url_path="import-csv", parser_classes=[MultiPartParser, FormParser])
    def import_csv(self, request):
        """
        Endpoint: POST /api/core/admin/students/import-csv/
        Attends un fichier multipart form field 'file' (csv ou xlsx).
        Retour: { total_rows: int, results: [ { row: int, success: bool, student_id?, username?, error? } ] }
        """
        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response({"detail": "Aucun fichier envoyé."}, status=400)

        rows = []
        name = uploaded.name.lower()

        try:
            if name.endswith(".csv") or name.endswith(".txt"):
                raw = uploaded.read()
                try:
                    text = raw.decode("utf-8-sig")
                except Exception:
                    try:
                        text = raw.decode("cp1252")
                    except Exception:
                        text = raw.decode("utf-8", "ignore")
                reader = csv.DictReader(io.StringIO(text))
                for r in reader:
                    rows.append(r)

            elif name.endswith(".xlsx") or name.endswith(".xls"):
                try:
                    import openpyxl
                except ImportError:
                    return Response({"detail": "openpyxl requis pour lire les fichiers xlsx. Installer le package."}, status=500)

                wb = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)
                ws = wb.active
                it = ws.iter_rows(values_only=True)

                header_row = next(it, None)
                if not header_row:
                    return Response({"detail": "Fichier Excel vide."}, status=400)

                header = []
                for i, h in enumerate(header_row):
                    if h is None:
                        header.append(f"col{i}")
                    else:
                        header.append(str(h).strip())

                for ridx, row in enumerate(it, start=2):
                    obj = {}
                    for ci, cell in enumerate(row):
                        key = header[ci] if ci < len(header) else f"col{ci}"
                        obj[key] = cell
                    rows.append(obj)

            else:
                return Response({"detail": "Format de fichier non supporté (autorisé: .csv, .xlsx)."}, status=400)

        except Exception as exc:
            logger.exception("Error reading uploaded file: %s", str(exc))
            return Response({"detail": f"Erreur lecture fichier: {str(exc)}"}, status=400)

        # --- Helpers locaux -------------------------------------------------
        def norm_cell(val):
            """Retourne une string propre, gère None, float/int, date/datetime."""
            if val is None:
                return ""
            import datetime as _dt
            if isinstance(val, str):
                return val.strip()
            if isinstance(val, bool):
                return str(val)
            if isinstance(val, int):
                return str(val)
            if isinstance(val, float):
                # si entier flottant (12.0) -> '12'
                if val.is_integer():
                    return str(int(val))
                return str(val)
            if isinstance(val, (_dt.date, _dt.datetime)):
                return val.isoformat()
            return str(val).strip()

        def to_int_maybe(val):
            """Essaie de convertir en int proprement, retourne None si impossible/vides."""
            s = norm_cell(val)
            if s == "":
                return None
            try:
                f = float(s)
                return int(f)
            except Exception:
                try:
                    return int(s)
                except Exception:
                    return None

        # get_or_create_user logic (local, safe)
        def get_or_create_user_from_payload(user_payload):
            """
            user_payload: dict with keys username, email, first_name, last_name, password (may be None)
            Returns: (user_instance, created_bool)
            """
            from django.contrib.auth import get_user_model
            User = get_user_model()
            import time

            username = (user_payload.get("username") or "").strip() or None
            email = (user_payload.get("email") or "").strip() or None
            first_name = (user_payload.get("first_name") or "").strip() or ""
            last_name = (user_payload.get("last_name") or "").strip() or ""
            password = user_payload.get("password") or None

            # 1) try by username
            user = None
            if username:
                user = User.objects.filter(username=username).first()

            # 2) fallback by email
            if not user and email:
                user = User.objects.filter(email=email).first()

            if user:
                # non-destructive update des champs si manquants
                changed = False
                if first_name and user.first_name != first_name:
                    user.first_name = first_name
                    changed = True
                if last_name and user.last_name != last_name:
                    user.last_name = last_name
                    changed = True
                if email and user.email != email:
                    user.email = email
                    changed = True
                if changed:
                    user.save()
                return user, False

            # 3) create user
            # ensure username exists
            if not username:
                if email:
                    username = email.split("@")[0]
                else:
                    username = f"user_{int(time.time())}"

            base = username
            suffix = 0
            while User.objects.filter(username=username).exists():
                suffix += 1
                username = f"{base}{suffix}"

            if not password:
                password = User.objects.make_random_password()

            user = User.objects.create_user(
                username=username,
                email=email or "",
                password=password,
                first_name=first_name,
                last_name=last_name,
            )
            return user, True

        # -------------------------------------------------------------------
        results = []
        total = len(rows)

        for idx, r in enumerate(rows, start=1):
            # Normalisation sûre des champs
            first_name = norm_cell(r.get("first_name") or r.get("firstname") or r.get("prénom") or r.get("prenom"))
            last_name = norm_cell(r.get("last_name") or r.get("lastname") or r.get("nom"))
            email = norm_cell(r.get("email") or "")
            dob = norm_cell(r.get("date_of_birth") or r.get("dob") or r.get("date") or "")
            sex_raw = norm_cell(r.get("sex") or r.get("gender") or "")
            sex = sex_raw.upper()[:1] if sex_raw else ""
            school_class_id = to_int_maybe(r.get("school_class") or r.get("school_class_id") or r.get("class"))
            parent_id = to_int_maybe(r.get("parent_id") or r.get("parent"))
            password = norm_cell(r.get("password") or r.get("passwd") or "") or None

            # username fallback
            if email:
                username = email.split("@")[0]
            else:
                uname = f"{first_name}.{last_name}".strip().lower().replace(" ", ".")
                username = uname or f"user{idx}"

            user_payload = {
                "username": username,
                "email": email or "",
                "first_name": first_name,
                "last_name": last_name,
                "password": password,
            }

            try:
                with transaction.atomic():
                    # Ensure user exists (or create)
                    user_obj, created_user = get_or_create_user_from_payload(user_payload)

                    # Build student payload using user_id (serializer expects user via user_id write_only field)
                    student_payload = {
                        "user_id": user_obj.id,
                        "date_of_birth": dob or None,
                        "sex": sex or "M",
                        "school_class_id": school_class_id,
                        "parent_id": parent_id,
                    }

                    serializer = StudentSerializer(data=student_payload)
                    try:
                        serializer.is_valid(raise_exception=True)
                        student = serializer.save()
                        results.append({"row": idx, "success": True, "student_id": student.id, "username": student.user.username})
                    except Exception as ser_exc:
                        # Validation errors or other serializer exceptions
                        logger.exception("Validation error row %s: %s", idx, str(ser_exc))
                        # Try to extract serializer errors if present
                        err_msg = None
                        try:
                            # If it's a DRF ValidationError, it may have detail attribute
                            err_msg = getattr(ser_exc, "detail", None) or str(ser_exc)
                        except Exception:
                            err_msg = str(ser_exc)
                        results.append({"row": idx, "success": False, "error": err_msg, "username": user_obj.username})
                        # rollback transaction for this line
                        raise

            except Exception as e:
                # Already logged above; ensure result exists in case of other exceptions
                if not results or results[-1].get("row") != idx:
                    logger.exception("CSV import row %s failed: %s", idx, str(e))
                    results.append({"row": idx, "success": False, "error": str(e), "username": username})
                # continue to next row (transaction ensures this row rolled back)
                continue

        return Response({"total_rows": total, "results": results}, status=200)

    @action(detail=False, methods=["get"], url_path=r"by-class/(?P<class_id>[^/.]+)")
    def by_class(self, request, class_id=None):
        students = (
            self.get_queryset()
            .filter(school_class_id=class_id)
            .order_by("user__last_name", "user__first_name")
        )

        # Pagination désactivée volontairement pour cette action
        serializer = self.get_serializer(students, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="by-teacher")
    def by_teacher(self, request):
        if not hasattr(request.user, "teacher"):
            return Response({"detail": "Vous n’êtes pas un enseignant."}, status=403)

        students = self.get_queryset().order_by("user__last_name", "user__first_name")
        page = self.paginate_queryset(students)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(students, many=True)
        return Response(serializer.data)


# --- Ajoute / remplace le ParentViewSet dans core/views.py ---
from django.db.models import Prefetch
from rest_framework import viewsets, filters
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# modèles
from core.models import Parent, Student
from academics.models import SchoolClass   # classes & autres se trouvent dans academics

# serializers (les nouveaux optimisés que tu as ajoutés plus haut)
from core.serializers import (
    ParentOptimizedReadSerializer,
    ParentOptimizedWriteSerializer,
)

# permissions locales
from core.permissions import IsParentOrReadOnly

# pagination simple — garde la même que pour teachers
class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class ParentViewSet(viewsets.ModelViewSet):
    """
    ParentViewSet optimisé :
    - pagination active
    - recherche serveur-side (?search=)
    - filtres via django-filter
    - select_related / prefetch_related pour éviter N+1
    - serializers read/write séparés (optimisés)
    """
    queryset = Parent.objects.all()
    permission_classes = [IsAuthenticated, IsParentOrReadOnly]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        "phone": ["exact", "icontains"],
        "id": ["exact"],
    }
    # recherche sur user + phone + nom/prénom d'enfants et nom de la classe de l'enfant
    search_fields = [
        "user__first_name",
        "user__last_name",
        "user__username",
        "user__email",
        "phone",
        "students__user__first_name",
        "students__user__last_name",
        "students__school_class__name",
    ]
    ordering_fields = ["user__last_name", "id", "phone"]
    pagination_class = StandardResultsSetPagination

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return ParentOptimizedWriteSerializer
        return ParentOptimizedReadSerializer

    def get_queryset(self):
        """
        Construire queryset optimisé : select_related user et prefetch students
        avec student.user et student.school_class pour éviter N+1.
        Appliquer permissions (admin vs parent connecté).
        """
        user = self.request.user

        base_qs = Parent.objects.all().select_related("user").prefetch_related(
            Prefetch(
                "students",
                queryset=Student.objects.select_related("user", "school_class").all()
            )
        )

        if user.is_staff or user.is_superuser:
            qs = base_qs
        elif hasattr(user, "parent"):
            qs = base_qs.filter(user=user)
        else:
            qs = Parent.objects.none()

        return qs.distinct()

    # override list pour forcer paginated response claire (DRF fait ça déjà)
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

class TeacherViewSet(viewsets.ModelViewSet):
    """
    ViewSet optimisé :
    - recherche instantanée via ?search=xxx (SearchFilter)
    - filtrage via DjangoFilterBackend (ex: subject, classes)
    - override paginate_queryset pour permettre ?no_pagination=1
    - préfetch/select_related pour éviter N+1
    - différents serializers pour read vs write
    """
    queryset = Teacher.objects.all()
    permission_classes = [IsAuthenticated, IsTeacherReadOnly]

    # enable search & ordering & django-filter
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        "subject__id": ["exact"],
        "classes__id": ["exact"],
    }
    # fields accessibles par ?search=...
    search_fields = [
        "user__first_name",
        "user__last_name",
        "user__username",
        "subject__name",
        "classes__name",
    ]
    ordering_fields = ["user__last_name", "subject__name", "id"]

    # Default serializer (fallback)
    serializer_class = TeacherFullSerializer

    def get_serializer_class(self):
        # Use write serializer for create/update/partial_update/destroy
        if self.action in ("create", "update", "partial_update", "destroy"):
            return TeacherWriteSerializer
        # For list/retrieve and custom actions, return full nested serializer
        return TeacherFullSerializer

    def get_queryset(self):
        user = self.request.user
        base_qs = Teacher.objects.all()

        if user.is_staff or user.is_superuser:
            qs = base_qs
        elif hasattr(user, "teacher"):
            qs = base_qs.filter(user=user)
        else:
            qs = Teacher.objects.none()

        # Préfetch / select_related pour éviter N+1
        qs = qs.select_related("user", "subject").prefetch_related(
            Prefetch("classes", queryset=SchoolClass.objects.all())
        ).distinct()

        return qs

    def paginate_queryset(self, queryset):
        """
        Permet de désactiver la pagination côté backend en ajoutant ?no_pagination=1
        (utile quand ton front n'est pas prêt pour paginated response).
        Si tu veux forcer pagination côté front, retire cette logique.
        """
        if self.request.query_params.get("no_pagination") == "1":
            return None
        return super().paginate_queryset(queryset)

    # Keep your custom actions but reuse the optimized queryset & serializer
    @action(detail=False, methods=["get"], url_path=r"by-class/(?P<class_id>[^/.]+)")
    def by_class(self, request, class_id=None):
        user = request.user
        try:
            school_class = SchoolClass.objects.get(id=class_id)
        except SchoolClass.DoesNotExist:
            return Response({"detail": "Classe introuvable."}, status=404)

        if user.is_staff or user.is_superuser:
            teachers = school_class.teachers.all().distinct()
        elif hasattr(user, "teacher"):
            teacher = user.teacher
            if not teacher.classes.filter(id=class_id).exists():
                return Response({"detail": "Vous n’enseignez pas dans cette classe."}, status=403)
            teachers = school_class.teachers.all().distinct()
        else:
            return Response({"detail": "Accès non autorisé."}, status=403)

        # Serializer & Response
        serializer = self.get_serializer(teachers.prefetch_related("classes"), many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="by-level/(?P<level_id>[^/.]+)")
    def by_level(self, request, level_id=None):
        user = request.user
        if not (user.is_staff or user.is_superuser):
            return Response({"detail": "Accès refusé."}, status=403)

        teachers = Teacher.objects.filter(classes__level_id=level_id).distinct()
        serializer = self.get_serializer(teachers, many=True)
        return Response(serializer.data)

# ------------------------------------------------------------------
# REGISTER VIEWS (Parent / Student / Teacher)
# ------------------------------------------------------------------
class ParentRegisterView(generics.CreateAPIView):
    serializer_class = ParentSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        parent = serializer.save()
        refresh = RefreshToken.for_user(parent.user)
        return Response({
            "parent_id": parent.id,
            "refresh": str(refresh),
            "access": str(refresh.access_token)
        }, status=status.HTTP_201_CREATED)


class StudentRegisterView(generics.CreateAPIView):
    serializer_class = StudentSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        student = serializer.save()
        refresh = RefreshToken.for_user(student.user)
        return Response({
            "student_id": student.id,
            "refresh": str(refresh),
            "access": str(refresh.access_token)
        }, status=status.HTTP_201_CREATED)


class TeacherRegisterView(generics.CreateAPIView):
    serializer_class = TeacherSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        teacher = serializer.save()
        refresh = RefreshToken.for_user(teacher.user)
        return Response({
            "teacher_id": teacher.id,
            "refresh": str(refresh),
            "access": str(refresh.access_token)
        }, status=status.HTTP_201_CREATED)


# ------------------------------------------------------------------
# PROFILE VIEW (connected user)
# ------------------------------------------------------------------
class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        if hasattr(user, "parent"):
            parent = user.parent
            serializer = ParentProfileSerializer(parent)
            return Response(serializer.data)

        if hasattr(user, "student"):
            student = user.student
            serializer = StudentProfileSerializer(student)
            return Response(serializer.data)

        if hasattr(user, "teacher"):
            teacher = user.teacher
            serializer = TeacherSerializer(teacher)
            return Response(serializer.data)

        return Response({"detail": "User has no profile."}, status=404)


# ------------------------------------------------------------------
# DASHBOARD: simple stats
# ------------------------------------------------------------------
class DashboardStatsView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(cache_page(30))
    def get(self, request):
        students_count = Student.objects.count()
        teachers_count = Teacher.objects.count()
        parents_count = Parent.objects.count()

        students_by_sex_qs = Student.objects.values('sex').annotate(count=Count('id'))
        students_by_sex = {entry['sex']: entry['count'] for entry in students_by_sex_qs}

        top_classes_qs = (
            SchoolClass.objects
            .annotate(student_count=Count('students'))
            .order_by('-student_count')[:8]
            .values('id', 'name', 'student_count')
        )
        top_classes = list(top_classes_qs)

        return Response({
            "students_count": students_count,
            "teachers_count": teachers_count,
            "parents_count": parents_count,
            "students_by_sex": students_by_sex,
            "top_classes": top_classes,
        })


# ------------------------------------------------------------------
# DASHBOARD: best students (per level, per term, overall)
# ------------------------------------------------------------------
CACHE_SECONDS = 300


class DashboardTopStudentsView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        user = request.user
        term = request.query_params.get("term")
        level_id = request.query_params.get("level_id")
        try:
            top_n = int(request.query_params.get("top_n", 1))
            if top_n < 1:
                top_n = 1
        except Exception:
            top_n = 1

        # 1) périmètre élèves selon rôle
        if user.is_staff or user.is_superuser:
            students_qs = Student.objects.all()
        elif hasattr(user, "teacher"):
            teacher = user.teacher
            students_qs = Student.objects.filter(school_class__in=teacher.classes.all()).distinct()
        elif hasattr(user, "parent"):
            students_qs = Student.objects.filter(parent=user.parent).distinct()
        elif hasattr(user, "student"):
            students_qs = Student.objects.filter(pk=user.student.pk)
        else:
            return Response({"detail": "Accès non autorisé."}, status=status.HTTP_403_FORBIDDEN)

        # 2) restreindre par niveau si demandé
        if level_id:
            students_qs = students_qs.filter(school_class__level_id=level_id)

        # 3) grades pour le périmètre
        grades_qs = Grade.objects.select_related("student", "student__school_class", "student__school_class__level", "subject") \
                                 .filter(student__in=students_qs)
        if term:
            grades_qs = grades_qs.filter(term__iexact=term)

        # 4) calcul des bulletins (ton utilitaire)
        report_cards = compute_report_cards_from_grades(
            grades_qs,
            include_missing_subjects=False,
            full_weighting=True,
        )

        # 5) préparer index
        per_level = defaultdict(list)
        per_term = defaultdict(list)
        per_student_aggregate = defaultdict(list)

        for item in report_cards:
            student = item["student"]
            class_id = item.get("class_id")
            term_key = item.get("term")
            avg = item.get("term_average")
            if avg is None:
                continue
            try:
                level = getattr(getattr(student, "school_class", None), "level_id", None)
            except Exception:
                level = None
            per_term[term_key].append(item)
            per_level[level].append(item)
            per_student_aggregate[student.pk].append({
                "student": student, "avg": avg, "class_id": class_id, "class_name": item.get("class_name")
            })

        # helper sort
        def sort_and_take(items_list, n):
            items_with_avg = [it for it in items_list if it.get("term_average") is not None]
            items_with_avg.sort(
                key=lambda x: (x["term_average"], f"{x['student'].user.last_name or ''} {x['student'].user.first_name or ''}".lower()),
                reverse=True
            )
            return items_with_avg[:n]

        # per_level_best
        per_level_best = []
        for level_key, items_list in per_level.items():
            top_items = sort_and_take(items_list, top_n)
            serialized_top = []
            for it in top_items:
                s = it["student"]
                serialized_top.append({
                    "student_id": s.id,
                    "first_name": s.user.first_name,
                    "last_name": s.user.last_name,
                    "class_id": it.get("class_id"),
                    "class_name": it.get("class_name"),
                    "term": it.get("term"),
                    "term_average": it.get("term_average"),
                    "rank_in_class": it.get("rank"),
                })
            per_level_best.append({"level_id": level_key, "top": serialized_top})

        # per_level_by_term
        per_level_by_term = {}
        for level_key, items_list in per_level.items():
            by_term = defaultdict(list)
            for it in items_list:
                by_term[it["term"]].append(it)
            per_term_summary = {}
            for t, li in by_term.items():
                take = sort_and_take(li, top_n)
                per_term_summary[t] = [
                    {
                        "student_id": s["student"].id,
                        "first_name": s["student"].user.first_name,
                        "last_name": s["student"].user.last_name,
                        "class_id": s.get("class_id"),
                        "class_name": s.get("class_name"),
                        "term_average": s.get("term_average"),
                        "rank_in_class": s.get("rank")
                    } for s in take
                ]
            per_level_by_term[str(level_key)] = per_term_summary

        # per_term_best
        per_term_best = []
        for t, items_list in per_term.items():
            top_items = sort_and_take(items_list, top_n)
            serialized_top = []
            for it in top_items:
                s = it["student"]
                serialized_top.append({
                    "student_id": s.id,
                    "first_name": s.user.first_name,
                    "last_name": s.user.last_name,
                    "class_id": it.get("class_id"),
                    "class_name": it.get("class_name"),
                    "term": it.get("term"),
                    "term_average": it.get("term_average"),
                    "rank_in_class": it.get("rank"),
                })
            per_term_best.append({"term": t, "top": serialized_top})

        # best overall (mean of term_averages)
        overall_list = []
        for student_pk, records in per_student_aggregate.items():
            avgs = [r["avg"] for r in records if r["avg"] is not None]
            if not avgs:
                continue
            overall_avg = float(Decimal(sum(avgs)) / Decimal(len(avgs)))
            student = records[0]["student"]
            overall_list.append({
                "student_id": student.id,
                "first_name": student.user.first_name,
                "last_name": student.user.last_name,
                "class_id": records[0].get("class_id"),
                "class_name": records[0].get("class_name"),
                "overall_average": round(overall_avg, 2)
            })

        overall_list.sort(key=lambda x: (x["overall_average"], f"{x['last_name']} {x['first_name']}".lower()), reverse=True)
        top_overall = overall_list[:top_n]

        return Response({
            "requested_term": term,
            "requested_level_id": level_id,
            "top_overall": top_overall,
            "top_per_term": per_term_best,
            "top_per_level": per_level_best,
            "top_per_level_by_term": per_level_by_term,
        }, status=status.HTTP_200_OK)