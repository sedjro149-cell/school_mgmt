# academics/filters.py
import django_filters
from django.db.models import Q
from .models import Grade

class GradeFilter(django_filters.FilterSet):
    student_name = django_filters.CharFilter(method="filter_student_name")
    student_id = django_filters.CharFilter(field_name="student__id", label="Student ID")
    school_class = django_filters.NumberFilter(field_name="student__school_class__id", label="Class")
    subject = django_filters.NumberFilter(field_name="subject__id", label="Subject")
    term = django_filters.CharFilter(field_name="term", lookup_expr="exact")

    class Meta:
        model = Grade
        fields = ["student_id", "school_class", "subject", "term"]

    def filter_student_name(self, queryset, name, value):
        return queryset.filter(
            Q(student__user__first_name__icontains=value)
            | Q(student__user__last_name__icontains=value)
            | Q(student__user__username__icontains=value)
        )
