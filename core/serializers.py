from django.contrib.auth import get_user_model
from django.db import transaction, IntegrityError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
import logging

# Project imports
from .models import Parent, Student, Teacher
from academics.models import SchoolClass, ClassScheduleEntry, Subject, ClassSubject

User = get_user_model()
logger = logging.getLogger(__name__)


# ========================================================================
# 1. SIMPLE SERIALIZERS
# ========================================================================
class UserSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")


class SchoolClassSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchoolClass
        fields = ("id", "name", "level")


class SubjectSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ("id", "name")


# ========================================================================
# 2. USER SERIALIZER
# ========================================================================
class UserSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(read_only=True)
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = ("id", "username", "first_name", "last_name", "email", "password")

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        if not password:
            raise ValidationError({"password": "Le mot de passe est requis."})
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


# ========================================================================
# 3. PARENT SERIALIZERS
# ========================================================================
class StudentInParentSerializer(serializers.ModelSerializer):
    user = UserSimpleSerializer(read_only=True)
    school_class = SchoolClassSimpleSerializer(read_only=True)

    class Meta:
        model = Student
        fields = ("id", "user", "sex", "date_of_birth", "school_class")


class ParentSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    students = StudentInParentSerializer(many=True, read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    students_count = serializers.SerializerMethodField()

    class Meta:
        model = Parent
        fields = ("id", "user", "first_name", "last_name", "phone", "students", "students_count")

    def get_students_count(self, obj):
        prefetch_cache = getattr(obj, "_prefetched_objects_cache", None)
        if prefetch_cache and "students" in prefetch_cache:
            return len(prefetch_cache["students"])
        try:
            return obj.students.count()
        except Exception as e:
            logger.exception("get_students_count error for Parent %s: %s", getattr(obj, "id", "?"), str(e))
            return 0

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        with transaction.atomic():
            user = UserSerializer().create(validated_data=user_data)
            parent = Parent.objects.create(user=user, **validated_data)
        return parent

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        if user_data:
            UserSerializer().update(instance.user, user_data)
        return super().update(instance, validated_data)


class ParentProfileSerializer(ParentSerializer):
    pass


class ParentOptimizedReadSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    students = StudentInParentSerializer(many=True, read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    students_count = serializers.SerializerMethodField()

    class Meta:
        model = Parent
        fields = ("id", "user", "first_name", "last_name", "phone", "students", "students_count")

    def get_students_count(self, obj):
        prefetch_cache = getattr(obj, "_prefetched_objects_cache", None)
        if prefetch_cache and "students" in prefetch_cache:
            return len(prefetch_cache["students"])
        try:
            return obj.students.count()
        except Exception as e:
            logger.exception("get_students_count error for Parent %s: %s", getattr(obj, "id", "?"), str(e))
            return 0


class ParentOptimizedWriteSerializer(serializers.ModelSerializer):
    user = UserSerializer()

    class Meta:
        model = Parent
        fields = ("id", "user", "phone")

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        try:
            with transaction.atomic():
                user = UserSerializer().create(validated_data=user_data)
                parent = Parent.objects.create(user=user, **validated_data)
                return parent
        except IntegrityError as e:
            logger.exception("Parent create integrity error: %s", str(e))
            raise serializers.ValidationError({"detail": "Erreur d'intégrité DB.", "error": str(e)})

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        if user_data:
            UserSerializer().update(instance.user, user_data)
        return super().update(instance, validated_data)


class ParentOptimizedProfileSerializer(ParentOptimizedReadSerializer):
    pass


# ========================================================================
# 4. STUDENT SERIALIZERS
# ========================================================================
class ParentSimpleSerializer(serializers.ModelSerializer):
    user = UserSimpleSerializer(read_only=True)

    class Meta:
        model = Parent
        fields = ("id", "user", "phone")


class StudentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    school_class = SchoolClassSimpleSerializer(read_only=True)
    parent = ParentSimpleSerializer(read_only=True)

    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source="user",
        write_only=True, required=False, allow_null=True
    )
    school_class_id = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(), source="school_class",
        write_only=True, required=False, allow_null=True
    )
    parent_id = serializers.PrimaryKeyRelatedField(
        queryset=Parent.objects.all(), source="parent",
        write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = Student
        fields = (
            "id", "user", "user_id", "sex", "date_of_birth",
            "school_class", "school_class_id",
            "parent", "parent_id"
        )

    def create(self, validated_data):
        user_data = validated_data.pop("user", None)
        try:
            if user_data and isinstance(user_data, User):
                user = user_data
            elif user_data and isinstance(user_data, dict):
                user = UserSerializer().create(validated_data=user_data)
            else:
                user = validated_data.get("user", None)
            if user is None:
                raise serializers.ValidationError("User data missing or invalid for Student creation.")
            with transaction.atomic():
                student = Student.objects.create(
                    user=user,
                    **{k: v for k, v in validated_data.items() if k != "user"}
                )
            return student
        except Exception as e:
            logger.exception("Student create failed: %s", str(e))
            raise

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        if user_data:
            try:
                UserSerializer().update(instance.user, user_data)
            except Exception:
                logger.exception("Failed updating nested user for Student %s", instance.pk)
        return super().update(instance, validated_data)


class StudentListSerializer(serializers.ModelSerializer):
    user = UserSimpleSerializer(read_only=True)
    school_class = SchoolClassSimpleSerializer(read_only=True)
    parent = ParentSimpleSerializer(read_only=True)

    class Meta:
        model = Student
        fields = ("id", "user", "sex", "date_of_birth", "school_class", "parent")


class StudentProfileSerializer(StudentSerializer):
    pass


# ========================================================================
# 5. TEACHER SERIALIZERS
# ========================================================================
class TeacherSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    subject = SubjectSimpleSerializer(read_only=True)
    classes = SchoolClassSimpleSerializer(many=True, read_only=True)
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), source="subject",
        write_only=True, required=False, allow_null=True
    )
    class_ids = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(), source="classes",
        write_only=True, many=True, required=False
    )

    class Meta:
        model = Teacher
        fields = ("id", "user", "subject", "subject_id", "classes", "class_ids")

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        classes = validated_data.pop("classes", [])
        try:
            with transaction.atomic():
                user = UserSerializer().create(validated_data=user_data)
                teacher = Teacher.objects.create(user=user, **validated_data)
                if classes:
                    teacher.classes.set(classes)
                return teacher
        except IntegrityError as e:
            logger.exception("Teacher create integrity error: %s", str(e))
            raise ValidationError({"detail": "Erreur d'intégrité DB.", "error": str(e)})

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        classes = validated_data.pop("classes", None)
        if user_data:
            UserSerializer().update(instance.user, user_data)
        super().update(instance, validated_data)
        if classes is not None:
            instance.classes.set(classes)
        return instance


class TeacherFullSerializer(serializers.ModelSerializer):
    """
    Serializer pour list/retrieve — renvoie tout en nested.
    """
    user = UserSerializer(read_only=True)
    subject = SubjectSimpleSerializer(read_only=True)
    classes = SchoolClassSimpleSerializer(many=True, read_only=True)

    class Meta:
        model = Teacher
        fields = ("id", "user", "subject", "classes")


class TeacherWriteSerializer(serializers.ModelSerializer):
    """
    Serializer pour create/update.

    validate() applique deux règles métier :
      R1 — Un seul prof par (classe, matière).
      R2 — La matière doit être attribuée à la classe avant le prof.

    Ces vérifications sont aussi assurées au niveau DB par le signal
    m2m_changed dans core/models.py — les deux couches se complètent.
    """
    user = UserSerializer()
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), source="subject",
        write_only=True, required=False, allow_null=True
    )
    class_ids = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(), source="classes",
        write_only=True, many=True, required=False
    )

    class Meta:
        model = Teacher
        fields = ("id", "user", "subject_id", "class_ids")

    def validate(self, data):
        """
        Collecte toutes les erreurs R1 et R2 en un seul passage
        et les renvoie ensemble — l'admin voit tout d'un coup.
        """
        instance = self.instance  # None si create, Teacher si update

        subject = data.get("subject", getattr(instance, "subject", None))
        classes = data.get("classes", list(instance.classes.all()) if instance else [])

        # Rien à valider si pas de matière ou pas de classes
        if not subject or not classes:
            return data

        errors = []

        for cls in classes:
            # ── R2 : la matière doit être attribuée à la classe ──────────────
            if not ClassSubject.objects.filter(school_class=cls, subject=subject).exists():
                errors.append(
                    f"La matière « {subject.name} » n'est pas attribuée à la classe "
                    f"« {cls.name} ». Configurez d'abord la matière dans cette classe "
                    f"avant d'y affecter un professeur."
                )
                # R1 inutile à vérifier si R2 échoue déjà pour cette classe
                continue

            # ── R1 : un seul prof par (classe, matière) ──────────────────────
            qs = Teacher.objects.filter(subject=subject, classes=cls)
            if instance is not None:
                qs = qs.exclude(pk=instance.pk)
            conflict = qs.select_related("user").first()
            if conflict:
                errors.append(
                    f"La classe « {cls.name} » a déjà un professeur de "
                    f"« {subject.name} » : "
                    f"{conflict.first_name} {conflict.last_name} (id={conflict.pk}). "
                    f"Retirez-le d'abord avant d'en affecter un autre."
                )

        if errors:
            raise serializers.ValidationError({"classes": errors})

        return data

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        classes = validated_data.pop("classes", [])
        try:
            with transaction.atomic():
                user = UserSerializer().create(validated_data=user_data)
                teacher = Teacher.objects.create(user=user, **validated_data)
                if classes:
                    teacher.classes.set(classes)
                return teacher
        except IntegrityError as e:
            logger.exception("Teacher create integrity error: %s", str(e))
            raise serializers.ValidationError({"detail": "Erreur d'intégrité DB.", "error": str(e)})

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        classes = validated_data.pop("classes", None)
        if user_data:
            UserSerializer().update(instance.user, user_data)
        super().update(instance, validated_data)
        if classes is not None:
            instance.classes.set(classes)
        return instance


# ========================================================================
# 6. SCHEDULE UTILS
# ========================================================================
class ClassScheduleEntrySerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)

    class Meta:
        model = ClassScheduleEntry
        fields = ("id", "subject_name", "weekday", "starts_at", "ends_at", "room", "notes")


class SchoolClassSerializer(serializers.ModelSerializer):
    timetable = ClassScheduleEntrySerializer(many=True, read_only=True)

    class Meta:
        model = SchoolClass
        fields = ("id", "name", "level", "timetable")