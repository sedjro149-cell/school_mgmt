# finance/views.py
from rest_framework import viewsets, status, filters as drf_filters
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from django.shortcuts import get_object_or_404
from django.utils import timezone

from django_filters.rest_framework import DjangoFilterBackend

from .models import FeeType, FeeTypeAmount, Fee, Payment
from .serializers import FeeTypeSerializer, FeeTypeAmountSerializer, FeeSerializer, PaymentSerializer
from .permissions import IsStudentOrParentOrAdmin

from .filters import FeeFilter, PaymentFilter, FeeTypeFilter, FeeTypeAmountFilter

class FeeTypeViewSet(viewsets.ModelViewSet):
    queryset = FeeType.objects.all().order_by("name")
    serializer_class = FeeTypeSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [DjangoFilterBackend, drf_filters.SearchFilter, drf_filters.OrderingFilter]
    filterset_class = FeeTypeFilter
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]


class FeeTypeAmountViewSet(viewsets.ModelViewSet):
    queryset = FeeTypeAmount.objects.select_related("fee_type", "level").all()
    serializer_class = FeeTypeAmountSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [DjangoFilterBackend, drf_filters.OrderingFilter]
    filterset_class = FeeTypeAmountFilter
    ordering_fields = ["level__id", "amount", "fee_type__name"]


class FeeViewSet(viewsets.ModelViewSet):
    queryset = Fee.objects.select_related("fee_type", "student", "student__user").all()
    serializer_class = FeeSerializer
    permission_classes = [IsStudentOrParentOrAdmin]

    filter_backends = [DjangoFilterBackend, drf_filters.SearchFilter, drf_filters.OrderingFilter]
    filterset_class = FeeFilter
    search_fields = [
        "student__user__first_name",
        "student__user__last_name",
        "student__id",
        "fee_type__name",
    ]
    ordering_fields = ["amount", "created_at", "payment_date", "fee_type__name"]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_staff or user.is_superuser:
            return qs
        if hasattr(user, "student"):
            return qs.filter(student=user.student)
        if hasattr(user, "parent"):
            return qs.filter(student__parent=user.parent)
        return qs.none()

    def create(self, request, *args, **kwargs):
        data = request.data.copy()
        fee_type = data.get("fee_type_id") or data.get("fee_type")
        student_id = data.get("student")
        # If amount not provided, try infer from FeeTypeAmount using student's level
        if fee_type and student_id and not data.get("amount"):
            try:
                from core.models import Student
                student = Student.objects.get(pk=student_id)
                level = getattr(getattr(student, "school_class", None), "level", None)
                if level:
                    fta = FeeTypeAmount.objects.filter(fee_type_id=fee_type, level=level, is_active=True).first()
                    if fta:
                        data["amount"] = str(fta.amount)
            except Exception:
                pass
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch"], permission_classes=[IsAdminUser])
    def validate_fee(self, request, pk=None):
        fee = get_object_or_404(Fee, pk=pk)
        serializer = self.get_serializer(fee, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.select_related("fee", "fee__fee_type", "fee__student", "fee__student__user", "validated_by").all()
    serializer_class = PaymentSerializer
    permission_classes = [IsStudentOrParentOrAdmin]

    filter_backends = [DjangoFilterBackend, drf_filters.SearchFilter, drf_filters.OrderingFilter]
    filterset_class = PaymentFilter
    search_fields = [
        "fee__student__user__first_name",
        "fee__student__user__last_name",
        "reference",
        "note",
    ]
    ordering_fields = ["paid_at", "amount", "validated"]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_staff or user.is_superuser:
            return qs
        if hasattr(user, "student"):
            return qs.filter(fee__student=user.student)
        if hasattr(user, "parent"):
            return qs.filter(fee__student__parent=user.parent)
        return qs.none()

    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def validate_payment(self, request, pk=None):
        payment = get_object_or_404(Payment, pk=pk)
        if payment.validated:
            return Response({"detail": "Déjà validé."}, status=400)
        payment.validate(user=request.user)
        return Response({"detail": "Paiement validé.", "payment_id": payment.id})


# Statistic endpoints (inchangés)
from .utils.statistics import (
    get_global_stats,
    get_stats_by_class,
    get_stats_by_feetype,
    get_top_students,
    get_monthly_payments,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def fees_statistics(request):
    validated = request.query_params.get("validated", "1") != "0"
    stats = {
        "global": get_global_stats(validated_only=validated),
        "by_class": get_stats_by_class(validated_only=validated),
        "by_fee_type": get_stats_by_feetype(validated_only=validated),
        "top_students": get_top_students(n=10, validated_only=validated),
    }
    return Response(stats)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def fees_monthly(request):
    year = request.query_params.get("year")
    try:
        year = int(year) if year else None
    except ValueError:
        year = None
    validated = request.query_params.get("validated", "1") != "0"
    data = get_monthly_payments(year=year, validated_only=validated)
    return Response({"year": year or timezone.now().year, "monthly": data})
