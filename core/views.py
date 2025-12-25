# core/views.py
from collections import defaultdict
from decimal import Decimal

from django.db import transaction, connection
from django.db.models import Count, Prefetch
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page

from rest_framework import viewsets, generics, status, filters
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

# core models & serializers
from .models import Parent, Student, Teacher
from .serializers import (
    ParentSerializer,
    StudentSerializer,
    TeacherSerializer,
    ParentProfileSerializer,
    StudentProfileSerializer,
)

# permissions (tu as déjà ces classes dans ton projet)
from .permissions import IsParentOrReadOnly, IsTeacherReadOnly

# academics (utilisés pour grades / classes / report card)
from academics.models import SchoolClass, Grade, ClassSubject
from academics.services.report_cards import compute_report_cards_from_grades


# ------------------------------------------------------------------
# STUDENT viewset
# ------------------------------------------------------------------
class StudentViewSet(viewsets.ModelViewSet):
    """
    ViewSet optimisé avec Eager Loading (select_related).
    """
    # LIGNE AJOUTÉE ICI POUR CORRIGER L'ERREUR "basename"
    queryset = Student.objects.all()
    
    serializer_class = StudentSerializer
    permission_classes = [IsAuthenticated, IsParentOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        
        # OPTIMISATION CRITIQUE :
        # On écrase le queryset par défaut pour appliquer le select_related
        queryset = Student.objects.select_related(
            'user', 
            'school_class', 
            'parent__user'
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

    @action(detail=False, methods=["get"], url_path=r"by-class/(?P<class_id>[^/.]+)")
    def by_class(self, request, class_id=None):
        # On filtre sur le queryset optimisé
        students = self.get_queryset().filter(school_class_id=class_id).order_by("user__last_name", "user__first_name")
        serializer = self.get_serializer(students, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="by-teacher")
    def by_teacher(self, request):
        if not hasattr(request.user, "teacher"):
             return Response({"detail": "Vous n’êtes pas un enseignant."}, status=403)
        
        students = self.get_queryset().order_by("user__last_name", "user__first_name")
        serializer = self.get_serializer(students, many=True)
        return Response(serializer.data)


# ------------------------------------------------------------------
# PARENT CRUD
# ------------------------------------------------------------------
# core/views.py (extrait)
from django.db.models import Prefetch
# ... autres imports ...
from .models import Parent, Student  # assure-toi que Student est importé depuis le bon module

class ParentViewSet(viewsets.ModelViewSet):
    queryset = Parent.objects.all()  # -> indispensable pour router DRF
    serializer_class = ParentSerializer
    permission_classes = [IsAuthenticated, IsParentOrReadOnly]

    def get_queryset(self):
        user = self.request.user

        # base queryset: select_related user (one-to-one) et prefetch students avec user + school_class
        base_qs = Parent.objects.all().select_related("user").prefetch_related(
            Prefetch(
                "students",
                queryset=Student.objects.select_related("user", "school_class").all()
            )
        )

        if user.is_staff or user.is_superuser:
            return base_qs

        if hasattr(user, "parent"):
            # pour le parent connecté, on renvoie uniquement son parent
            return base_qs.filter(user=user)

        return Parent.objects.none()


# ------------------------------------------------------------------
# TEACHER CRUD
# ------------------------------------------------------------------
class TeacherViewSet(viewsets.ModelViewSet):
    """
    ViewSet pour gérer les enseignants :
    - Admin/staff : accès complet
    - Enseignant : accès à son propre profil
    """
    queryset = Teacher.objects.all()
    serializer_class = TeacherSerializer
    permission_classes = [IsAuthenticated, IsTeacherReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            return Teacher.objects.all()
        if hasattr(user, "teacher"):
            return Teacher.objects.filter(user=user)
        return Teacher.objects.none()

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

        serializer = self.get_serializer(teachers, many=True)
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
    permission_classes = [AllowAny]  # si tu veux restreindre à admins, remplace par IsAdminUser

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
    """
    GET /core/dashboard/stats/
    Retourne des métriques simples :
      - students_count, teachers_count, parents_count
      - students_by_sex (ex: {"M": 10, "F": 7})
      - top_classes (id, name, student_count)
    """
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
CACHE_SECONDS = 30

class DashboardTopStudentsView(APIView):
    """
    GET /core/dashboard/top-students/
    Query params:
      - term=<T1|T2|T3>         (optionnel)
      - level_id=<id>           (optionnel)
      - top_n=<int>             (optionnel, default 1)
    """
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
