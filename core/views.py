import csv
import datetime
import io
import logging
import time
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Prefetch
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page

from django_filters.rest_framework import DjangoFilterBackend

from rest_framework import viewsets, generics, status, filters
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from academics.models import SchoolClass, Grade, ClassSubject
from academics.services.report_cards import compute_report_cards_from_grades

from .models import Parent, Student, Teacher
from .permissions import IsParentOrReadOnly, IsTeacherReadOnly
from .serializers import (
    ParentOptimizedReadSerializer,
    ParentOptimizedWriteSerializer,
    ParentProfileSerializer,
    ParentSerializer,
    StudentListSerializer,
    StudentProfileSerializer,
    StudentSerializer,
    TeacherFullSerializer,
    TeacherSerializer,
    TeacherWriteSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()

MAX_IMPORT_ROWS = 1_000


# ===========================================================================
# Helpers pour l'import CSV/XLSX
# ===========================================================================

def _norm_cell(val) -> str:
    """Retourne une string propre quel que soit le type de cellule Excel/CSV."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return str(int(val)) if val.is_integer() else str(val)
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.isoformat()
    return str(val).strip()


def _to_int_maybe(val):
    """Convertit proprement en int, renvoie None si vide ou impossible."""
    s = _norm_cell(val)
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _get_or_create_user(payload: dict, existing_usernames: set, existing_emails: set):
    """
    Récupère ou crée un User à partir du payload.

    Les sets `existing_usernames` et `existing_emails` sont pré-chargés avant
    la boucle d'import pour éviter les requêtes N+1 de vérification d'unicité.
    Les nouveaux usernames créés sont ajoutés aux sets en mémoire afin que les
    lignes suivantes du même import en tiennent compte.

    Retourne (user, created: bool)
    """
    username  = (payload.get("username") or "").strip() or None
    email     = (payload.get("email") or "").strip() or None
    first     = (payload.get("first_name") or "").strip()
    last      = (payload.get("last_name") or "").strip()
    password  = payload.get("password") or None

    # 1) Recherche par username
    user = User.objects.filter(username=username).first() if username else None

    # 2) Fallback par email
    if not user and email:
        user = User.objects.filter(email=email).first()

    # 3) Mise à jour non-destructive si l'utilisateur existe déjà
    if user:
        changed = False
        for attr, new_val in [("first_name", first), ("last_name", last), ("email", email)]:
            if new_val and getattr(user, attr) != new_val:
                setattr(user, attr, new_val)
                changed = True
        if changed:
            user.save(update_fields=["first_name", "last_name", "email"])
        return user, False

    # 4) Création — résolution de collision de username en mémoire
    if not username:
        if email:
            username = email.split("@")[0]
        else:
            username = f"{first}.{last}".lower().replace(" ", ".").strip(".") or f"user_{int(time.time())}"

    base = username
    suffix = 0
    while username in existing_usernames:
        suffix += 1
        username = f"{base}{suffix}"

    # Mise à jour immédiate des sets pour les lignes suivantes du même import
    existing_usernames.add(username)
    if email:
        existing_emails.add(email)

    user = User.objects.create_user(
        username=username,
        email=email or "",
        password=password or User.objects.make_random_password(),
        first_name=first,
        last_name=last,
    )
    return user, True


# ===========================================================================
# Pagination partagée
# ===========================================================================

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


# ===========================================================================
# StudentViewSet
# ===========================================================================

class StudentViewSet(viewsets.ModelViewSet):
    queryset = Student.objects.all()
    permission_classes = [IsAuthenticated, IsParentOrReadOnly]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields   = ["user__first_name", "user__last_name", "user__username", "user__email"]
    filterset_fields = ["school_class", "sex"]
    ordering_fields  = ["user__first_name", "user__last_name", "date_of_birth"]
    ordering         = ["user__last_name", "user__first_name"]

    def get_queryset(self):
        user = self.request.user
        queryset = Student.objects.select_related(
            "user",
            "school_class",
            "parent__user",
        ).all()

        if user.is_staff or user.is_superuser:
            return queryset
        if hasattr(user, "parent"):
            return queryset.filter(parent=user.parent)
        if hasattr(user, "student"):
            return queryset.filter(user=user)
        if hasattr(user, "teacher"):
            class_ids = user.teacher.classes.values_list("id", flat=True)
            return queryset.filter(school_class_id__in=class_ids).distinct()

        return queryset.none()

    def get_serializer_class(self):
        if self.action == "list":
            return StudentListSerializer
        if self.action == "retrieve":
            return StudentProfileSerializer
        return StudentSerializer

    # -----------------------------------------------------------------------
    # Import CSV / XLSX
    # -----------------------------------------------------------------------

    @action(
        detail=False,
        methods=["post"],
        url_path="import-csv",
        parser_classes=[MultiPartParser, FormParser],
    )
    def import_csv(self, request):
        """
        POST /api/core/admin/students/import-csv/
        Champ multipart : 'file' (.csv / .txt / .xlsx / .xls)

        Réponse :
        {
            "total_rows":     int,
            "success_count":  int,
            "error_count":    int,
            "results": [
                {
                    "row":        int,
                    "success":    bool,
                    "student_id": int,       # si succès
                    "username":   str,       # si succès
                    "warnings":   [str],     # optionnel
                    "error":      str        # si échec
                }
            ]
        }
        """
        uploaded = request.FILES.get("file")
        if not uploaded:
            return Response({"detail": "Aucun fichier envoyé."}, status=400)

        rows = []
        name = uploaded.name.lower()

        # --- Lecture du fichier ---
        try:
            if name.endswith((".csv", ".txt")):
                raw = uploaded.read()
                text = None
                for encoding in ("utf-8-sig", "cp1252"):
                    try:
                        text = raw.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                if text is None:
                    text = raw.decode("utf-8", errors="ignore")
                rows = list(csv.DictReader(io.StringIO(text)))

            elif name.endswith((".xlsx", ".xls")):
                try:
                    import openpyxl
                except ImportError:
                    return Response(
                        {"detail": "openpyxl est requis pour lire les fichiers xlsx. Installez le package."},
                        status=500,
                    )
                wb = openpyxl.load_workbook(uploaded, read_only=True, data_only=True)
                ws = wb.active
                it = ws.iter_rows(values_only=True)
                header_row = next(it, None)
                if not header_row:
                    return Response({"detail": "Fichier Excel vide."}, status=400)
                header = [
                    str(h).strip() if h is not None else f"col{i}"
                    for i, h in enumerate(header_row)
                ]
                for row in it:
                    rows.append({
                        header[ci] if ci < len(header) else f"col{ci}": cell
                        for ci, cell in enumerate(row)
                    })
            else:
                return Response(
                    {"detail": "Format non supporté. Utilisez .csv ou .xlsx."},
                    status=400,
                )
        except Exception as exc:
            logger.exception("Erreur lecture fichier : %s", exc)
            return Response({"detail": f"Erreur lecture fichier : {exc}"}, status=400)

        # --- Limite de sécurité ---
        if len(rows) > MAX_IMPORT_ROWS:
            return Response(
                {"detail": f"Fichier trop volumineux. Maximum {MAX_IMPORT_ROWS} lignes autorisées."},
                status=400,
            )

        # --- Pré-chargement des usernames / emails pour éviter N+1 ---
        existing_usernames: set = set(User.objects.values_list("username", flat=True))
        existing_emails: set    = set(User.objects.filter(email__gt="").values_list("email", flat=True))

        # --- Traitement ligne par ligne ---
        results = []

        for idx, r in enumerate(rows, start=1):
            warnings = []

            first_name      = _norm_cell(r.get("first_name") or r.get("firstname") or r.get("prénom") or r.get("prenom"))
            last_name       = _norm_cell(r.get("last_name")  or r.get("lastname")  or r.get("nom"))
            email           = _norm_cell(r.get("email") or "")
            dob             = _norm_cell(r.get("date_of_birth") or r.get("dob") or r.get("date") or "")
            sex_raw         = _norm_cell(r.get("sex") or r.get("gender") or "")
            sex             = sex_raw.upper()[:1] if sex_raw else ""
            school_class_id = _to_int_maybe(r.get("school_class") or r.get("school_class_id") or r.get("class"))
            parent_id       = _to_int_maybe(r.get("parent_id") or r.get("parent"))
            password        = _norm_cell(r.get("password") or r.get("passwd") or "") or None

            # Validation minimale — rejet immédiat sans toucher la DB
            if not first_name and not last_name:
                results.append({
                    "row": idx,
                    "success": False,
                    "error": "first_name et last_name sont tous les deux vides.",
                })
                continue

            # Warnings non-bloquants
            if not sex:
                warnings.append("Champ 'sex' absent ou vide → valeur par défaut 'M' utilisée.")
                sex = "M"
            if not dob:
                warnings.append("Champ 'date_of_birth' absent.")

            # Construction du username
            if email:
                username = email.split("@")[0]
            else:
                username = f"{first_name}.{last_name}".lower().replace(" ", ".").strip(".")
                if not username:
                    username = f"user{idx}"

            user_payload = {
                "username":   username,
                "email":      email,
                "first_name": first_name,
                "last_name":  last_name,
                "password":   password,
            }

            # Transaction atomique par ligne
            try:
                with transaction.atomic():
                    user_obj, _created = _get_or_create_user(
                        user_payload, existing_usernames, existing_emails
                    )
                    student_payload = {
                        "user_id":        user_obj.id,
                        "date_of_birth":  dob or None,
                        "sex":            sex,
                        "school_class_id": school_class_id,
                        "parent_id":      parent_id,
                    }
                    serializer = StudentSerializer(data=student_payload)
                    serializer.is_valid(raise_exception=True)
                    student = serializer.save()

                results.append({
                    "row":        idx,
                    "success":    True,
                    "student_id": student.id,
                    "username":   student.user.username,
                    **({"warnings": warnings} if warnings else {}),
                })

            except Exception as exc:
                logger.exception("Import CSV — ligne %s : %s", idx, exc)
                results.append({
                    "row":      idx,
                    "success":  False,
                    "error":    getattr(exc, "detail", None) or str(exc),
                    "username": username,
                    **({"warnings": warnings} if warnings else {}),
                })

        success_count = sum(1 for r in results if r["success"])

        return Response(
            {
                "total_rows":    len(rows),
                "success_count": success_count,
                "error_count":   len(results) - success_count,
                "results":       results,
            },
            status=200,
        )

    # -----------------------------------------------------------------------
    # Actions supplémentaires
    # -----------------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path=r"by-class/(?P<class_id>[^/.]+)")
    def by_class(self, request, class_id=None):
        students = (
            self.get_queryset()
            .filter(school_class_id=class_id)
            .order_by("user__last_name", "user__first_name")
        )
        serializer = self.get_serializer(students, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="by-teacher")
    def by_teacher(self, request):
        if not hasattr(request.user, "teacher"):
            return Response({"detail": "Vous n'êtes pas un enseignant."}, status=403)

        students = self.get_queryset().order_by("user__last_name", "user__first_name")
        page = self.paginate_queryset(students)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(students, many=True)
        return Response(serializer.data)


# ===========================================================================
# ParentViewSet
# ===========================================================================

class ParentViewSet(viewsets.ModelViewSet):
    """
    ParentViewSet optimisé :
    - pagination active
    - recherche server-side via ?search=
    - filtres via django-filter
    - select_related / prefetch_related pour éviter N+1
    - serializers read / write séparés
    """
    queryset = Parent.objects.all()
    permission_classes = [IsAuthenticated, IsParentOrReadOnly]
    pagination_class = StandardResultsSetPagination

    filter_backends  = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {"phone": ["exact", "icontains"], "id": ["exact"]}
    search_fields    = [
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

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return ParentOptimizedWriteSerializer
        return ParentOptimizedReadSerializer

    def get_queryset(self):
        user = self.request.user
        base_qs = Parent.objects.select_related("user").prefetch_related(
            Prefetch(
                "students",
                queryset=Student.objects.select_related("user", "school_class").all(),
            )
        )

        if user.is_staff or user.is_superuser:
            qs = base_qs
        elif hasattr(user, "parent"):
            qs = base_qs.filter(user=user)
        else:
            qs = Parent.objects.none()

        return qs.distinct()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


# ===========================================================================
# TeacherViewSet
# ===========================================================================

class TeacherViewSet(viewsets.ModelViewSet):
    """
    ViewSet optimisé :
    - recherche via ?search=
    - filtrage via DjangoFilterBackend
    - pagination désactivable via ?no_pagination=1
    - prefetch / select_related pour éviter N+1
    - serializers read / write séparés
    """
    queryset = Teacher.objects.all()
    permission_classes = [IsAuthenticated, IsTeacherReadOnly]
    serializer_class = TeacherFullSerializer

    filter_backends  = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {"subject__id": ["exact"], "classes__id": ["exact"]}
    search_fields    = [
        "user__first_name",
        "user__last_name",
        "user__username",
        "subject__name",
        "classes__name",
    ]
    ordering_fields = ["user__last_name", "subject__name", "id"]

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return TeacherWriteSerializer
        return TeacherFullSerializer

    def get_queryset(self):
        user = self.request.user

        if user.is_staff or user.is_superuser:
            qs = Teacher.objects.all()
        elif hasattr(user, "teacher"):
            qs = Teacher.objects.filter(user=user)
        else:
            qs = Teacher.objects.none()

        return qs.select_related("user", "subject").prefetch_related(
            Prefetch("classes", queryset=SchoolClass.objects.all())
        ).distinct()

    def paginate_queryset(self, queryset):
        """Permet de désactiver la pagination via ?no_pagination=1."""
        if self.request.query_params.get("no_pagination") == "1":
            return None
        return super().paginate_queryset(queryset)

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
            if not user.teacher.classes.filter(id=class_id).exists():
                return Response({"detail": "Vous n'enseignez pas dans cette classe."}, status=403)
            teachers = school_class.teachers.all().distinct()
        else:
            return Response({"detail": "Accès non autorisé."}, status=403)

        serializer = self.get_serializer(teachers.prefetch_related("classes"), many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path=r"by-level/(?P<level_id>[^/.]+)")
    def by_level(self, request, level_id=None):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Accès refusé."}, status=403)
        teachers = Teacher.objects.filter(classes__level_id=level_id).distinct()
        serializer = self.get_serializer(teachers, many=True)
        return Response(serializer.data)


# ===========================================================================
# Vues d'enregistrement (Register)
# ===========================================================================

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
            "refresh":   str(refresh),
            "access":    str(refresh.access_token),
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
            "refresh":    str(refresh),
            "access":     str(refresh.access_token),
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
            "refresh":    str(refresh),
            "access":     str(refresh.access_token),
        }, status=status.HTTP_201_CREATED)


# ===========================================================================
# Vue Profil (utilisateur connecté)
# ===========================================================================

class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user

        if hasattr(user, "parent"):
            return Response(ParentProfileSerializer(user.parent).data)
        if hasattr(user, "student"):
            return Response(StudentProfileSerializer(user.student).data)
        if hasattr(user, "teacher"):
            return Response(TeacherSerializer(user.teacher).data)

        return Response({"detail": "Aucun profil associé à cet utilisateur."}, status=404)


# ===========================================================================
# Dashboard — statistiques générales
# ===========================================================================

# ===========================================================================
# Dashboard — statistiques générales
# ===========================================================================

class DashboardStatsView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(cache_page(60))  # 30 s → 60 s, stats globales peu volatiles
    def get(self, request):
        # ✅ Un seul aggregate() au lieu de 3 .count() séparés
        counts = User.objects.aggregate(
            students_count=Count("student", distinct=True),
            teachers_count=Count("teacher", distinct=True),
            parents_count=Count("parent", distinct=True),
        )

        # ✅ values/annotate déjà optimal, rien à changer
        students_by_sex = {
            entry["sex"]: entry["count"]
            for entry in Student.objects.values("sex").annotate(count=Count("id"))
        }

        # ✅ only() pour ne pas charger les champs inutiles des classes
        top_classes = list(
            SchoolClass.objects
            .only("id", "name")
            .annotate(student_count=Count("students"))
            .order_by("-student_count")[:8]
            .values("id", "name", "student_count")
        )

        return Response({
            **counts,
            "students_by_sex": students_by_sex,
            "top_classes":     top_classes,
        })


# ===========================================================================
# Dashboard — meilleurs élèves  ⏸️  MIS EN PAUSE
# ===========================================================================
CACHE_SECONDS = 300
class DashboardTopStudentsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # 🔴 Fonctionnalité temporairement désactivée le temps de la refonte.
        # Pour réactiver : supprimer ce bloc et décommenter le code ci-dessous.
        return Response(
            {
                "detail": (
                    "La fonctionnalité 'meilleurs élèves' est temporairement "
                    "indisponible. Elle sera réactivée après optimisation."
                )
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # -------------------------------------------------------------------------
    # ⬇️  Ancien code conservé pour la prochaine version — NE PAS SUPPRIMER
    # -------------------------------------------------------------------------
    # @method_decorator(cache_page(CACHE_SECONDS))
    # def get(self, request):
    #     ... (tout le code existant ici)
    permission_classes = [IsAuthenticated]

    @method_decorator(cache_page(CACHE_SECONDS))
    def get(self, request):
        user     = request.user
        term     = request.query_params.get("term")
        level_id = request.query_params.get("level_id")
        try:
            top_n = max(1, int(request.query_params.get("top_n", 1)))
        except (ValueError, TypeError):
            top_n = 1

        # Périmètre élèves selon le rôle
        if user.is_staff or user.is_superuser:
            students_qs = Student.objects.all()
        elif hasattr(user, "teacher"):
            students_qs = Student.objects.filter(school_class__in=user.teacher.classes.all()).distinct()
        elif hasattr(user, "parent"):
            students_qs = Student.objects.filter(parent=user.parent).distinct()
        elif hasattr(user, "student"):
            students_qs = Student.objects.filter(pk=user.student.pk)
        else:
            return Response({"detail": "Accès non autorisé."}, status=status.HTTP_403_FORBIDDEN)

        if level_id:
            students_qs = students_qs.filter(school_class__level_id=level_id)

        grades_qs = (
            Grade.objects
            .select_related(
                "student",
                "student__school_class",
                "student__school_class__level",
                "subject",
            )
            .filter(student__in=students_qs)
        )
        if term:
            grades_qs = grades_qs.filter(term__iexact=term)

        report_cards = compute_report_cards_from_grades(
            grades_qs,
            include_missing_subjects=False,
            full_weighting=True,
        )

        per_level            = defaultdict(list)
        per_term             = defaultdict(list)
        per_student_aggregate = defaultdict(list)

        for item in report_cards:
            student  = item["student"]
            avg      = item.get("term_average")
            term_key = item.get("term")
            if avg is None:
                continue
            level = getattr(getattr(student, "school_class", None), "level_id", None)
            per_term[term_key].append(item)
            per_level[level].append(item)
            per_student_aggregate[student.pk].append({
                "student":    student,
                "avg":        avg,
                "class_id":   item.get("class_id"),
                "class_name": item.get("class_name"),
            })

        def _sort_and_take(items_list, n):
            return sorted(
                [it for it in items_list if it.get("term_average") is not None],
                key=lambda x: (
                    x["term_average"],
                    f"{x['student'].user.last_name or ''} {x['student'].user.first_name or ''}".lower(),
                ),
                reverse=True,
            )[:n]

        def _serialize_item(it):
            s = it["student"]
            return {
                "student_id":   s.id,
                "first_name":   s.user.first_name,
                "last_name":    s.user.last_name,
                "class_id":     it.get("class_id"),
                "class_name":   it.get("class_name"),
                "term":         it.get("term"),
                "term_average": it.get("term_average"),
                "rank_in_class": it.get("rank"),
            }

        # Top par niveau
        per_level_best = [
            {"level_id": level_key, "top": [_serialize_item(it) for it in _sort_and_take(items, top_n)]}
            for level_key, items in per_level.items()
        ]

        # Top par niveau et par trimestre
        per_level_by_term = {}
        for level_key, items in per_level.items():
            by_term = defaultdict(list)
            for it in items:
                by_term[it["term"]].append(it)
            per_level_by_term[str(level_key)] = {
                t: [_serialize_item(it) for it in _sort_and_take(li, top_n)]
                for t, li in by_term.items()
            }

        # Top par trimestre
        per_term_best = [
            {"term": t, "top": [_serialize_item(it) for it in _sort_and_take(items, top_n)]}
            for t, items in per_term.items()
        ]

        # Top général (moyenne des moyennes trimestrielles)
        overall_list = []
        for student_pk, records in per_student_aggregate.items():
            avgs = [r["avg"] for r in records if r["avg"] is not None]
            if not avgs:
                continue
            overall_avg = float(Decimal(sum(avgs)) / Decimal(len(avgs)))
            student = records[0]["student"]
            overall_list.append({
                "student_id":      student.id,
                "first_name":      student.user.first_name,
                "last_name":       student.user.last_name,
                "class_id":        records[0].get("class_id"),
                "class_name":      records[0].get("class_name"),
                "overall_average": round(overall_avg, 2),
            })

        overall_list.sort(
            key=lambda x: (x["overall_average"], f"{x['last_name']} {x['first_name']}".lower()),
            reverse=True,
        )

        return Response({
            "requested_term":         term,
            "requested_level_id":     level_id,
            "top_overall":            overall_list[:top_n],
            "top_per_term":           per_term_best,
            "top_per_level":          per_level_best,
            "top_per_level_by_term":  per_level_by_term,
        }, status=status.HTTP_200_OK)