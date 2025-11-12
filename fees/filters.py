# finance/filters.py
from django.db.models import Q
import django_filters
from django_filters import rest_framework as filters

from .models import Fee, Payment, FeeType, FeeTypeAmount


class FeeFilter(filters.FilterSet):
    """
    Filtres pour les Fees :
      - student (id)
      - fee_type (id)
      - paid (bool)
      - level : match soit le niveau de la classe de l'élève, soit les FeeTypeAmount liés au FeeType.
      - amount_min / amount_max
      - created_after / created_before
    """
    student = filters.CharFilter(field_name="student__id", lookup_expr="iexact")
    fee_type = filters.NumberFilter(field_name="fee_type__id")
    paid = filters.BooleanFilter(field_name="paid")
    level = filters.NumberFilter(method="filter_by_level")
    amount_min = filters.NumberFilter(field_name="amount", lookup_expr="gte")
    amount_max = filters.NumberFilter(field_name="amount", lookup_expr="lte")
    created_after = filters.DateFilter(field_name="created_at", lookup_expr="gte")
    created_before = filters.DateFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = Fee
        fields = ["student", "fee_type", "paid", "level", "amount_min", "amount_max", "created_after", "created_before"]

    def filter_by_level(self, queryset, name, value):
        # Inclut fees où l'élève est dans une classe dont level==value
        # OU où le FeeType a un FeeTypeAmount pour ce level.
        return queryset.filter(
            Q(student__school_class__level__id=value) |
            Q(fee_type__amounts__level__id=value)
        ).distinct()


class PaymentFilter(filters.FilterSet):
    """
    Filtres pour Payments :
      - fee (id)
      - student (via fee__student)
      - validated (bool)
      - date range paid_at
      - amount min/max
      - reference contains
    """
    fee = filters.NumberFilter(field_name="fee__id")
    student = filters.CharFilter(field_name="fee__student__id", lookup_expr="iexact")
    validated = filters.BooleanFilter(field_name="validated")
    paid_after = filters.DateFilter(field_name="paid_at", lookup_expr="gte")
    paid_before = filters.DateFilter(field_name="paid_at", lookup_expr="lte")
    amount_min = filters.NumberFilter(field_name="amount", lookup_expr="gte")
    amount_max = filters.NumberFilter(field_name="amount", lookup_expr="lte")
    reference = filters.CharFilter(field_name="reference", lookup_expr="icontains")

    class Meta:
        model = Payment
        fields = ["fee", "student", "validated", "paid_after", "paid_before", "amount_min", "amount_max", "reference"]


class FeeTypeFilter(filters.FilterSet):
    """
    Filtres pour FeeType :
      - name icontains
      - is_active
      - level (renvoie fee types qui ont un FeeTypeAmount pour ce level)
    """
    name = filters.CharFilter(field_name="name", lookup_expr="icontains")
    is_active = filters.BooleanFilter(field_name="is_active")
    level = filters.NumberFilter(method="filter_by_level")

    class Meta:
        model = FeeType
        fields = ["name", "is_active", "level"]

    def filter_by_level(self, queryset, name, value):
        return queryset.filter(amounts__level__id=value).distinct()


class FeeTypeAmountFilter(filters.FilterSet):
    """
    Filtres pour FeeTypeAmount :
      - fee_type
      - level
      - amount range
      - is_active
    """
    fee_type = filters.NumberFilter(field_name="fee_type__id")
    level = filters.NumberFilter(field_name="level__id")
    amount_min = filters.NumberFilter(field_name="amount", lookup_expr="gte")
    amount_max = filters.NumberFilter(field_name="amount", lookup_expr="lte")
    is_active = filters.BooleanFilter(field_name="is_active")

    class Meta:
        model = FeeTypeAmount
        fields = ["fee_type", "level", "amount_min", "amount_max", "is_active"]
