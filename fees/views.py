from rest_framework import viewsets, status, filters as drf_filters
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.apps import apps

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

    def update(self, request, *args, **kwargs):
        """
        Override update to optionally propagate due_date to existing Fees.
        Query params:
            ?propagate=true -> propagate the due_date to related Fee records
            ?override=true -> when propagating, overwrite existing fee.due_date (otherwise only null ones)
        """
        propagate = request.query_params.get("propagate", "false").lower() in ("1", "true", "yes")
        override = request.query_params.get("override", "false").lower() in ("1", "true", "yes")
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        # propagation si demandé et due_date fournie
        if propagate:
            new_due = serializer.validated_data.get("due_date", None)
            if new_due is not None:
                qs = instance.student_fees.all()
                if not override:
                    qs = qs.filter(due_date__isnull=True)
                qs.update(due_date=new_due)

        return Response(serializer.data)


class FeeTypeAmountViewSet(viewsets.ModelViewSet):
    queryset = FeeTypeAmount.objects.select_related("fee_type", "level").all()
    serializer_class = FeeTypeAmountSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [DjangoFilterBackend, drf_filters.OrderingFilter]
    filterset_class = FeeTypeAmountFilter
    ordering_fields = ["level__id", "amount", "fee_type__name"]


from django.db import models
from django.db.models import Sum, F, Value, Q, DecimalField, ExpressionWrapper
from django.db.models.functions import Coalesce
from rest_framework import viewsets, status, filters as drf_filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.apps import apps

from .models import Fee, FeeTypeAmount
from .serializers import FeeSerializer
from .permissions import IsStudentOrParentOrAdmin
from .filters import FeeFilter


class FeeViewSet(viewsets.ModelViewSet):
    queryset = (
        Fee.objects.select_related("fee_type", "student", "student__user")
        .annotate(
            annotated_total_paid=Coalesce(
                Sum('payments__amount', filter=Q(payments__validated=True)),
                Value(0, output_field=DecimalField(max_digits=12, decimal_places=2)),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .annotate(
            annotated_total_remaining=ExpressionWrapper(
                F('amount') - F('annotated_total_paid'),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .all()
    )

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

        if fee_type and student_id and not data.get("amount"):
            try:
                from core.models import Student
                student = Student.objects.get(pk=student_id)
                level = getattr(getattr(student, "school_class", None), "level", None)

                if level:
                    fta = FeeTypeAmount.objects.filter(
                        fee_type_id=fee_type,
                        level=level,
                        is_active=True
                    ).first()
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

    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def set_due_date(self, request, pk=None):
        fee = self.get_object()
        due_date = request.data.get("due_date")

        if not due_date:
            return Response({"detail": "due_date required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from django.utils.dateparse import parse_date
            parsed = parse_date(due_date)
            if parsed is None:
                raise ValueError
        except Exception:
            return Response(
                {"detail": "invalid date format. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST
            )

        fee.due_date = parsed
        fee.save(update_fields=["due_date"])

        return Response({"detail": "due_date updated", "fee_id": fee.id, "due_date": fee.due_date})

    @action(detail=True, methods=["post"], permission_classes=[IsAdminUser])
    def trigger_reminder(self, request, pk=None):
        fee = self.get_object()

        Notification = None
        NotificationTemplate = None
        UserNotificationPreference = None
        send_notification = None

        try:
            Notification = apps.get_model('notifications', 'Notification')
            NotificationTemplate = apps.get_model('notifications', 'NotificationTemplate')
            UserNotificationPreference = apps.get_model('notifications', 'UserNotificationPreference')

            from notifications.delivery import send_notification as _send
            send_notification = _send

        except Exception:
            Notification = None

        if not Notification:
            return Response({"detail": "notifications app not available"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        try:
            template = NotificationTemplate.objects.get(key="fees_due_0")
        except NotificationTemplate.DoesNotExist:
            return Response({"detail": "template fees_due_0 missing"},
                            status=status.HTTP_400_BAD_REQUEST)

        created_count = 0

        # Parents
        for parent in fee.student.parents.all():
            user_obj = getattr(parent, "user", None)
            if not user_obj:
                continue

            existed = Notification.objects.filter(
                topic='fees',
                recipient_user=user_obj,
                payload__fee_id=fee.id,
                payload__reminder_offset=0
            ).exists()

            if existed:
                continue

            channels = template.default_channels or ['inapp']

            try:
                pref = UserNotificationPreference.objects.get(user=user_obj, topic='fees')
                if not pref.enabled:
                    continue
                channels = pref.channels or channels
            except Exception:
                pass

            payload = {
                "fee_id": fee.id,
                "fee_type": fee.fee_type.name,
                "student_name": fee.student.user.get_full_name()
                if getattr(fee.student, "user", None)
                else f"{fee.student.first_name} {fee.student.last_name}",
                "amount_due": float(fee.amount),
                "due_date": fee.due_date.isoformat() if fee.due_date else None,
                "reminder_offset": 0
            }

            notif = Notification.objects.create(
                template=template,
                topic='fees',
                recipient_user=user_obj,
                payload=payload,
                channels=channels
            )

            if send_notification:
                try:
                    send_notification(notif)
                except Exception:
                    pass

            created_count += 1

        # Student
        student_user = getattr(fee.student, "user", None)

        if student_user:
            existed = Notification.objects.filter(
                topic='fees',
                recipient_user=student_user,
                payload__fee_id=fee.id,
                payload__reminder_offset=0
            ).exists()

            if not existed:
                channels = template.default_channels or ['inapp']

                try:
                    pref = UserNotificationPreference.objects.get(user=student_user, topic='fees')
                    if not pref.enabled:
                        channels = []
                    else:
                        channels = pref.channels or channels
                except Exception:
                    pass

                payload = {
                    "fee_id": fee.id,
                    "fee_type": fee.fee_type.name,
                    "student_name": fee.student.user.get_full_name()
                    if getattr(fee.student, "user", None)
                    else f"{fee.student.first_name} {fee.student.last_name}",
                    "amount_due": float(fee.amount),
                    "due_date": fee.due_date.isoformat() if fee.due_date else None,
                    "reminder_offset": 0
                }

                notif = Notification.objects.create(
                    template=template,
                    topic='fees',
                    recipient_user=student_user,
                    payload=payload,
                    channels=channels
                )

                if send_notification:
                    try:
                        send_notification(notif)
                    except Exception:
                        pass

                created_count += 1

        return Response({"detail": "reminders triggered", "created": created_count})


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
                return Response({"detail": "Déjà validé."}, status=status.HTTP_400_BAD_REQUEST)

            try:
                payment.validate(user=request.user)
                return Response({"detail": "Paiement validé.", "payment_id": payment.id}, status=status.HTTP_200_OK)
            except PermissionDenied as e:
                return Response({"detail": str(e)}, status=status.HTTP_403_FORBIDDEN)
            except ValidationError as e:
                # ValidationError peut contenir un dict / message
                msg = e.message if hasattr(e, 'message') else str(e)
                return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                 # log complet pour debuggage
                 import logging
                 logger = logging.getLogger(__name__)
                 logger.exception("Erreur lors de validate_payment for payment_id=%s user=%r", pk, request.user)
                 return Response({"detail": "Erreur interne lors de la validation."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# Statistic endpoints (unchanged)
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
