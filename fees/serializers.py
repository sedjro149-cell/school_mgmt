# finance/serializers.py
from rest_framework import serializers
from django.utils import timezone

from .models import FeeType, FeeTypeAmount, Fee, Payment
from academics.models import Level
from core.models import Student


class FeeTypeAmountSerializer(serializers.ModelSerializer):
    level_name = serializers.CharField(source="level.name", read_only=True)

    class Meta:
        model = FeeTypeAmount
        fields = ["id", "fee_type", "level", "level_name", "amount", "is_active"]


class FeeTypeSerializer(serializers.ModelSerializer):
    amounts = FeeTypeAmountSerializer(many=True, read_only=True)

    class Meta:
        model = FeeType
        fields = ["id", "name", "description", "is_active", "created_at", "amounts"]


from rest_framework import serializers
from .models import Payment, Fee
from django.contrib.auth import get_user_model

User = get_user_model()

class UserPublicSmallSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    first_name = serializers.CharField(allow_null=True)
    last_name = serializers.CharField(allow_null=True)


class StudentMiniSerializer(serializers.Serializer):
    id = serializers.CharField()
    first_name = serializers.SerializerMethodField()
    last_name = serializers.SerializerMethodField()
    full_name = serializers.SerializerMethodField()
    class_name = serializers.SerializerMethodField()
    level = serializers.SerializerMethodField()

    def get_first_name(self, student):
        return getattr(getattr(student, "user", None), "first_name", None) or getattr(student, "first_name", None)

    def get_last_name(self, student):
        return getattr(getattr(student, "user", None), "last_name", None) or getattr(student, "last_name", None)

    def get_full_name(self, student):
        fn = self.get_first_name(student) or ""
        ln = self.get_last_name(student) or ""
        return (fn + " " + ln).strip() or None

    def get_class_name(self, student):
        school_class = getattr(student, "school_class", None)
        if school_class:
            return getattr(school_class, "name", None) or getattr(school_class, "label", None)
        return getattr(student, "class_name", None)

    def get_level(self, student):
        school_class = getattr(student, "school_class", None)
        if school_class:
            lvl = getattr(school_class, "level", None)
            return getattr(lvl, "name", None) if lvl else None
        return None


class FeeSerializer(serializers.ModelSerializer):
    # IMPORTANT: remove source="student" because the field name is already 'student'
    student = StudentMiniSerializer(read_only=True)
    fee_type_name = serializers.CharField(source="fee_type.name", read_only=True)

    class Meta:
        model = Fee
        fields = ["id", "fee_type", "fee_type_name", "student", "amount", "paid", "payment_date", "created_at"]
        read_only_fields = ["id", "fee_type_name", "student", "paid", "payment_date", "created_at"]


class PaymentSerializer(serializers.ModelSerializer):
    fee_detail = FeeSerializer(source="fee", read_only=True)
    # student comes from fee.student (field name != source, so source is OK)
    student = StudentMiniSerializer(source="fee.student", read_only=True)
    validated_by = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "fee",
            "fee_detail",
            "student",
            "amount",
            "paid_at",
            "method",
            "reference",
            "note",
            "validated",
            "validated_by",
            "validated_at",
        ]
        read_only_fields = ["id", "fee_detail", "student", "validated_by", "validated_at"]

    def get_validated_by(self, obj):
        user = obj.validated_by
        if not user:
            return None
        return {
            "id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
        }

    def create(self, validated_data):
        payment = super().create(validated_data)
        try:
            payment.validate(user=None)
        except Exception:
            pass
        return payment
