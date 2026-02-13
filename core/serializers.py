# core/serializers.py

from django.contrib.auth import get_user_model
from django.db import transaction, IntegrityError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

# Project imports
from .models import Parent, Student, Teacher
from academics.models import SchoolClass, ClassScheduleEntry, Subject

User = get_user_model()

# ==============================================================================
# 1. SIMPLE SERIALIZERS (Helpers pour éviter la récursion et charger vite)
# ==============================================================================

class UserSimpleSerializer(serializers.ModelSerializer):
    """Pour l'affichage léger des infos utilisateur."""
    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")
        read_only = True


class SchoolClassSimpleSerializer(serializers.ModelSerializer):
    """Représentation minimale d'une classe."""
    class Meta:
        model = SchoolClass
        fields = ("id", "name", "level")
        read_only = True


class SubjectSimpleSerializer(serializers.ModelSerializer):
    """Représentation minimale d'une matière."""
    class Meta:
        model = Subject
        fields = ("id", "name")
        read_only = True


# ==============================================================================
# 2. USER SERIALIZER (Base)
# ==============================================================================

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


# ==============================================================================
# 3. PARENT SERIALIZERS
# ==============================================================================

class StudentInParentSerializer(serializers.ModelSerializer):
    """
    Optimisé pour la liste des enfants dans l'objet Parent.
    Evite la récursion Parent -> Student -> Parent.
    """
    user = UserSimpleSerializer(read_only=True)
    school_class = SchoolClassSimpleSerializer(read_only=True)

    class Meta:
        model = Student
        fields = ("id", "user", "sex", "date_of_birth", "school_class")


class ParentSerializer(serializers.ModelSerializer):
    # Nested Write : On crée le User en même temps que le Parent
    user = UserSerializer()
    
    # Nested Read : On affiche les enfants directement
    students = StudentInParentSerializer(many=True, read_only=True)
    
    # Champs pratiques
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    students_count = serializers.SerializerMethodField()

    class Meta:
        model = Parent
        fields = ("id", "user", "first_name", "last_name", "phone", "students", "students_count")

    def get_students_count(self, obj):
        # Utilise le prefetch s'il existe, sinon compte DB
        if hasattr(obj, "students"):
            try:
                return len(obj.students.all())
            except Exception:
                pass
        return obj.students.count()

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
    """Peut être étendu si le profil a besoin de moins ou plus d'infos."""
    pass


# ==============================================================================
# 4. STUDENT SERIALIZERS
# ==============================================================================

class ParentSimpleSerializer(serializers.ModelSerializer):
    """Pour afficher le parent dans l'objet Student sans boucle infinie."""
    user = UserSimpleSerializer(read_only=True)
    
    class Meta:
        model = Parent
        fields = ("id", "user", "phone")


class StudentSerializer(serializers.ModelSerializer):
    # --- READ ONLY (Nested Representations) ---
    # Ces champs seront remplis automatiquement grâce au select_related du ViewSet
    user = UserSerializer()
    school_class = SchoolClassSimpleSerializer(read_only=True)
    parent = ParentSimpleSerializer(read_only=True)

    # --- WRITE ONLY (IDs pour la création/modif) ---
    school_class_id = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(), 
        source="school_class", 
        write_only=True, 
        required=False, 
        allow_null=True
    )
    parent_id = serializers.PrimaryKeyRelatedField(
        queryset=Parent.objects.all(), 
        source="parent", 
        write_only=True, 
        required=False, 
        allow_null=True
    )

    class Meta:
        model = Student
        fields = (
            "id", "user", "sex", "date_of_birth", 
            "school_class", "school_class_id", 
            "parent", "parent_id"
        )

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        with transaction.atomic():
            user = UserSerializer().create(validated_data=user_data)
            student = Student.objects.create(user=user, **validated_data)
        return student

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        if user_data:
            UserSerializer().update(instance.user, user_data)
        return super().update(instance, validated_data)


class StudentListSerializer(serializers.ModelSerializer):
    """
    Version ultra-légère pour les listes (Dashboard, ListViews).
    Optimisée pour le rendu rapide de milliers de lignes.
    """
    user = UserSimpleSerializer(read_only=True)
    school_class_name = serializers.CharField(source="school_class.name", read_only=True, default=None)
    parent_name = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = ("id", "user", "sex", "date_of_birth", "school_class_name", "parent_name")

    def get_parent_name(self, obj):
        # Accès sécurisé : si parent est None, on ne crash pas.
        # Grâce au select_related('parent__user'), aucun appel DB ici.
        if obj.parent and obj.parent.user:
            return f"{obj.parent.user.last_name} {obj.parent.user.first_name}"
        return None


class StudentProfileSerializer(StudentSerializer):
    """Identique au serializer complet pour l'instant."""
    pass


# ==============================================================================
# 5. TEACHER SERIALIZERS
# ==============================================================================

class TeacherSerializer(serializers.ModelSerializer):
    user = UserSerializer()
    
    # --- READ fields ---
    subject = SubjectSimpleSerializer(read_only=True)
    classes = SchoolClassSimpleSerializer(many=True, read_only=True)
    
    # --- WRITE fields ---
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), 
        source="subject", 
        write_only=True, 
        required=False, 
        allow_null=True
    )
    class_ids = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(), 
        source="classes", 
        write_only=True, 
        many=True, 
        required=False
    )

    class Meta:
        model = Teacher
        fields = (
            "id", "user", "subject", "subject_id", 
            "classes", "class_ids"
        )

    def create(self, validated_data):
        user_data = validated_data.pop("user")
        classes = validated_data.pop("classes", []) # M2M handling
        
        try:
            with transaction.atomic():
                user = UserSerializer().create(validated_data=user_data)
                teacher = Teacher.objects.create(user=user, **validated_data)
                
                if classes:
                    teacher.classes.set(classes)
                return teacher

        except IntegrityError as e:
            raise ValidationError({"detail": "Erreur d'intégrité DB.", "error": str(e)})

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        classes = validated_data.pop("classes", None) # None signifie "ne pas toucher"

        if user_data:
            UserSerializer().update(instance.user, user_data)
        
        # Update standard fields
        super().update(instance, validated_data)

        # Update M2M si fourni
        if classes is not None:
            instance.classes.set(classes)
        
        return instance


# ==============================================================================
# 6. SCHEDULE UTILS
# ==============================================================================

class ClassScheduleEntrySerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source="subject.name", read_only=True)

    class Meta:
        model = ClassScheduleEntry
        fields = ("id", "subject_name", "weekday", "starts_at", "ends_at", "room", "notes")


class SchoolClassSerializer(serializers.ModelSerializer):
    """Vue détaillée d'une classe avec son emploi du temps."""
    timetable = ClassScheduleEntrySerializer(many=True, read_only=True)

    class Meta:
        model = SchoolClass
        fields = ("id", "name", "level", "timetable")