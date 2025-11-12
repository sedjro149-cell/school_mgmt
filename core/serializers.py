from rest_framework import serializers
from django.contrib.auth.models import User
from django.db import transaction, IntegrityError
from rest_framework.exceptions import ValidationError

from .models import Parent, Student, Teacher
from academics.models import SchoolClass, ClassScheduleEntry, Subject
from academics.serializers import StudentSerializer as AcademicStudentSerializer


# ----- petits serializers utilitaires pour la sortie (read) -----
class SchoolClassSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = SchoolClass
        fields = ("id", "name", "level")


class SubjectSimpleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ("id", "name")


# =======================
# ===== USER SERIALIZER
# =======================
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
        # mise à jour partielle autorisée
        password = validated_data.pop("password", None)
        for attr in ("username", "first_name", "last_name", "email"):
            if attr in validated_data:
                setattr(instance, attr, validated_data[attr])
        if password:
            instance.set_password(password)
        instance.save()
        return instance


from rest_framework import serializers
from django.contrib.auth.models import User
from rest_framework.exceptions import ValidationError

from .models import Parent, Student, Teacher
from academics.models import SchoolClass, ClassScheduleEntry, Subject
# n'utilise pas AcademicStudentSerializer ici pour éviter ambiguïtés
# on fournit une représentation compacte et fiable des enfants :

class ParentStudentNestedSerializer(serializers.Serializer):
    id = serializers.CharField()
    username = serializers.SerializerMethodField()
    first_name = serializers.SerializerMethodField()
    last_name = serializers.SerializerMethodField()
    school_class = serializers.SerializerMethodField()

    def get_username(self, obj):
        u = getattr(obj, "user", None)
        return getattr(u, "username", None) or getattr(obj, "username", None) or ""

    def get_first_name(self, obj):
        u = getattr(obj, "user", None)
        return getattr(u, "first_name", None) or getattr(obj, "firstname", None) or getattr(obj, "first_name", None) or ""

    def get_last_name(self, obj):
        u = getattr(obj, "user", None)
        return getattr(u, "last_name", None) or getattr(obj, "lastname", None) or getattr(obj, "last_name", None) or ""

    def get_school_class(self, obj):
        sc = getattr(obj, "school_class", None)
        if sc:
            return {"id": getattr(sc, "id", None), "name": getattr(sc, "name", None)}
        return None


class ParentSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    firstname = serializers.CharField(source="user.first_name", read_only=True)
    lastname = serializers.CharField(source="user.last_name", read_only=True)

    # <-- NOTE: on retire `source="students"` (redondant) et on laisse DRF mapper le champ
    students = ParentStudentNestedSerializer(many=True, read_only=True)

    students_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Parent
        fields = ("id", "user", "firstname", "lastname", "phone", "students", "students_count")

    def get_students_count(self, obj):
        st = getattr(obj, "students", None)
        if st is None:
            return obj.students.count() if hasattr(obj, "students") else 0
        try:
            return len(st)
        except Exception:
            return obj.students.count() if hasattr(obj, "students") else 0

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        user = UserSerializer.create(UserSerializer(), validated_data=user_data)
        parent = Parent.objects.create(user=user, **validated_data)
        return parent

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        if user_data:
            UserSerializer.update(UserSerializer(), instance.user, user_data)
        instance.phone = validated_data.get("phone", instance.phone)
        instance.save()
        return instance


class ParentProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    students = AcademicStudentSerializer(many=True, read_only=True)

    class Meta:
        model = Parent
        fields = ("id", "user", "phone", "students")



# =======================
# ===== STUDENT SERIALIZER
# =======================
class StudentSerializer(serializers.ModelSerializer):
    # nested user (read + write)
    user = UserSerializer()

    # convenience read-only name fields (synchronisées côté modèle via save())
    firstname = serializers.CharField(source="user.first_name", read_only=True)
    lastname = serializers.CharField(source="user.last_name", read_only=True)

    # READ representations
    school_class = serializers.SerializerMethodField(read_only=True)
    parent = serializers.SerializerMethodField(read_only=True)

    # WRITE helpers
    school_class_id = serializers.SlugRelatedField(
        slug_field="id",
        queryset=SchoolClass.objects.all(),
        write_only=True,
        source="school_class",
        required=False,
    )
    parent_id = serializers.SlugRelatedField(
        slug_field="id",
        queryset=Parent.objects.all(),
        write_only=True,
        source="parent",
        required=False,
    )

    class Meta:
        model = Student
        fields = (
            "id",
            "user",
            "firstname",
            "lastname",
            "sex",
            "date_of_birth",
            "school_class",
            "school_class_id",
            "parent",
            "parent_id",
        )

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        user = UserSerializer.create(UserSerializer(), validated_data=user_data)
        student = Student.objects.create(user=user, **validated_data)
        return student

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        if user_data:
            UserSerializer.update(UserSerializer(), instance.user, user_data)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

    def get_school_class(self, obj):
        if obj.school_class:
            return {"id": obj.school_class.id, "name": obj.school_class.name}
        return None

    def get_parent(self, obj):
        # retourne un objet minimal pour le parent (id + username) et facilite l'affichage côté front
        if obj.parent:
            return {"id": obj.parent.id, "user": {"username": obj.parent.user.username}}
        return None


class StudentProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    parent = serializers.SerializerMethodField()
    school_class = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = ("id", "user", "sex", "date_of_birth", "school_class", "parent")

    def get_parent(self, obj):
        if obj.parent:
            return {
                "id": obj.parent.id,
                "user": UserSerializer(obj.parent.user).data,
                "phone": obj.parent.phone,
            }
        return None

    def get_school_class(self, obj):
        if obj.school_class:
            return {"id": obj.school_class.id, "name": obj.school_class.name}
        return None


# =======================
# ===== CLASS SCHEDULE / SCHOOL CLASS SERIALIZERS
# =======================
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


# =======================
# ===== TEACHER SERIALIZER (clean, readable)
# =======================
class TeacherSerializer(serializers.ModelSerializer):
    # nested user (read + write)
    user = UserSerializer()

    # convenience read-only name fields (synchronisées côté modèle via save())
    firstname = serializers.CharField(source="user.first_name", read_only=True)
    lastname = serializers.CharField(source="user.last_name", read_only=True)

    # READ representations
    subject = SubjectSimpleSerializer(read_only=True)
    classes = SchoolClassSimpleSerializer(many=True, read_only=True)

    # WRITE-only fields (API accepts subject_id and class_ids for creation/update)
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), write_only=True, source="subject", required=False, allow_null=True
    )
    class_ids = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(), many=True, write_only=True, source="classes", required=False
    )

    class Meta:
        model = Teacher
        fields = (
            "id",
            "user",
            "firstname",
            "lastname",
            "subject",
            "subject_id",
            "classes",
            "class_ids",
        )

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        class_list = validated_data.pop("classes", [])
        try:
            with transaction.atomic():
                user = UserSerializer.create(UserSerializer(), validated_data=user_data)
                teacher = Teacher.objects.create(user=user, **validated_data)
                if class_list:
                    teacher.classes.set(class_list)
                return teacher
        except IntegrityError as e:
            raise ValidationError({"detail": "Erreur base de données lors de la création.", "db_error": str(e)})
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError({"detail": "Erreur lors de la création de l'enseignant.", "error": str(e)})

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        class_list = validated_data.pop("classes", None)

        if user_data:
            UserSerializer.update(UserSerializer(), instance.user, user_data)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if class_list is not None:
            instance.classes.set(class_list)

        return instance
