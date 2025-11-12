# fees/utils/statistics.py
from collections import OrderedDict
from decimal import Decimal

from django.db.models import Sum, Value, DecimalField
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from fees.models import Fee, Payment, FeeType, FeeTypeAmount
from academics.models import SchoolClass
from core.models import Student


def _decimal(n):
    if n is None:
        return Decimal("0.00")
    return Decimal(n)


def get_global_stats(validated_only=True):
    """
    total_due: somme des montants demandés (sum Fee.amount)
    total_paid: somme des paiements éventuellement validés (sum Payment.amount)
    remaining: total_due - total_paid
    rate: percentage
    """
    fees_qs = Fee.objects.all()
    payments_qs = Payment.objects.all()
    if validated_only:
        payments_qs = payments_qs.filter(validated=True)

    total_due = fees_qs.aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"]
    total_paid = payments_qs.aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"]

    total_due = _decimal(total_due)
    total_paid = _decimal(total_paid)
    remaining = total_due - total_paid
    rate = (total_paid / total_due * 100) if total_due > 0 else Decimal("0.00")

    return {
        "total_due": float(total_due),
        "total_paid": float(total_paid),
        "remaining": float(remaining),
        "rate": round(float(rate), 2),
    }


def get_stats_by_class(validated_only=True):
    """
    Retourne la liste des stats par SchoolClass.
    """
    payments_qs = Payment.objects.all()
    if validated_only:
        payments_qs = payments_qs.filter(validated=True)

    class_stats = []
    classes = SchoolClass.objects.select_related("level").all()
    for cls in classes:
        fees = Fee.objects.filter(student__school_class=cls)
        class_due = fees.aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"]
        class_paid = payments_qs.filter(fee__in=fees).aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"]

        class_due = _decimal(class_due)
        class_paid = _decimal(class_paid)
        remaining = class_due - class_paid
        rate = (class_paid / class_due * 100) if class_due > 0 else Decimal("0.00")

        # statut simple basé sur taux
        if rate >= 80:
            status = "very_good"
        elif rate >= 50:
            status = "average"
        else:
            status = "low"

        class_stats.append({
            "class_id": cls.id,
            "class_name": cls.name,
            "level": getattr(cls.level, "name", None),
            "students_count": Student.objects.filter(school_class=cls).count(),
            "total_due": float(class_due),
            "total_paid": float(class_paid),
            "remaining": float(remaining),
            "rate": round(float(rate), 2),
            "status": status,
        })
    return class_stats


def get_stats_by_feetype(validated_only=True):
    """
    Agrégation du total dû / payé par FeeType.
    Renvoie aussi les montants configurés par niveau (via FeeTypeAmount).
    """
    payments_qs = Payment.objects.all()
    if validated_only:
        payments_qs = payments_qs.filter(validated=True)

    data = []
    for ft in FeeType.objects.all():
        # Somme des fees réellement créés (assignés aux élèves)
        fees_qs = Fee.objects.filter(fee_type=ft)
        due = fees_qs.aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"]
        paid = payments_qs.filter(fee__in=fees_qs).aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"]

        due = _decimal(due)
        paid = _decimal(paid)
        remaining = due - paid
        rate = (paid / due * 100) if due > 0 else Decimal("0.00")

        # récupérer montants par niveau configurés
        amounts = []
        for fta in ft.amounts.select_related("level").all():
            amounts.append({
                "level_id": fta.level.id,
                "level_name": getattr(fta.level, "name", None),
                "amount": float(_decimal(fta.amount)),
                "is_active": bool(fta.is_active),
            })

        data.append({
            "fee_type_id": ft.id,
            "fee_type_name": ft.name,
            "amounts_by_level": amounts,
            "total_due": float(due),
            "total_paid": float(paid),
            "remaining": float(remaining),
            "rate": round(float(rate), 2),
            "count_fees": fees_qs.count(),
        })
    return data


def get_top_students(n=10, validated_only=True):
    """
    Retourne les top n élèves par montant restant dû (ordre décroissant).
    """
    payments_qs = Payment.objects.all()
    if validated_only:
        payments_qs = payments_qs.filter(validated=True)

    result = []
    students = Student.objects.all()
    for s in students:
        fees = Fee.objects.filter(student=s)
        due = _decimal(fees.aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"])
        paid = _decimal(payments_qs.filter(fee__in=fees).aggregate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField())))["total"])
        remaining = due - paid
        name = ""
        if getattr(s, "first_name", None) or getattr(s, "last_name", None):
            name = f"{getattr(s, 'first_name', '')} {getattr(s, 'last_name', '')}".strip()
        else:
            # fallback if Student proxies user
            user = getattr(s, "user", None)
            name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip() if user else str(s)

        result.append({
            "student_id": s.id,
            "student_name": name,
            "total_due": float(due),
            "total_paid": float(paid),
            "remaining": float(remaining),
        })

    # sort by remaining desc
    result = sorted(result, key=lambda x: x["remaining"], reverse=True)
    return result[:n]


def get_monthly_payments(year=None, validated_only=True):
    """
    Série temporelle : total payé par mois (grouped by month).
    Returns list of {month: 'YYYY-MM', total_paid: float}
    """
    payments_qs = Payment.objects.all()
    if validated_only:
        payments_qs = payments_qs.filter(validated=True)

    if year is None:
        year = timezone.now().year

    qs = payments_qs.filter(paid_at__year=year)
    qs = qs.annotate(month=TruncMonth("paid_at")).values("month").annotate(total=Coalesce(Sum("amount"), Value(0, output_field=DecimalField()))).order_by("month")

    data = []
    for row in qs:
        month = row["month"]
        total = _decimal(row["total"])
        data.append({"month": month.strftime("%Y-%m"), "total_paid": float(total)})

    return data
