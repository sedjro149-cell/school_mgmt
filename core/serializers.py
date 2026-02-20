from django.contrib.auth import get_user_model
from django.db import transaction, IntegrityError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
import logging

# Project imports
from .models import Parent, Student, Teacher
from academics.models import SchoolClass, ClassScheduleEntry, Subject

User = get_user_model()
logger = logging.getLogger(__name__)


# ========================================================================
# 1. SIMPLE SERIALIZERS (Helpers pour éviter la récursion et charger vite)
# ========================================================================
class UserSimpleSerializer(serializers.ModelSerializer):
    """Pour l'affichage léger des infos utilisateur."""
    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")


class SchoolClassSimpleSerializer(serializers.ModelSerializer):
    """Représentation minimale d'une classe."""
    class Meta:
        model = SchoolClass
        fields = ("id", "name", "level")


class SubjectSimpleSerializer(serializers.ModelSerializer):
    """Représentation minimale d'une matière."""
    class Meta:
        model = Subject
        fields = ("id", "name")


# ========================================================================
# 2. USER SERIALIZER (Base)
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
        """
        IMPORTANT: on sauvegarde toujours l'instance après mise à jour des champs.
        Avant : on sauvait seulement quand password était présenté -> changements non persistés.
        """
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        # save ALWAYS to persist first_name / last_name / email changes
        instance.save()
        return instance


# ========================================================================
# 3. PARENT SERIALIZERS
# ========================================================================
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
    # Nested Write : On crée le User en même temps que le Parent (via payload)
    user = UserSerializer()

    # Nested Read : On affiche les enfants directement (léger)
    students = StudentInParentSerializer(many=True, read_only=True)

    # Champs pratiques
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    students_count = serializers.SerializerMethodField()

    class Meta:
        model = Parent
        fields = ("id", "user", "first_name", "last_name", "phone", "students", "students_count")

    def get_students_count(self, obj):
        """
        Utiliser le cache de prefetch (si présent) pour éviter une requête supplémentaire.
        """
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
    """Peut être étendu si le profil a besoin de moins ou plus d'infos."""
    pass


# ========================================================================
# 4. STUDENT SERIALIZERS
# ========================================================================
class ParentSimpleSerializer(serializers.ModelSerializer):
    """Pour afficher le parent dans l'objet Student sans boucle infinie."""
    user = UserSimpleSerializer(read_only=True)

    class Meta:
        model = Parent
        fields = ("id", "user", "phone")

# --- Ajoute ceci DANS core/serializers.py (sous les serializers existants). ---
from django.db import transaction
from django.db.utils import IntegrityError
from rest_framework import serializers
import logging

logger = logging.getLogger(__name__)

# NOTE:
# On suppose que dans ce même fichier existent déjà :
# - UserSerializer (complet, utilisé pour write)
# - UserSimpleSerializer (léger)
# - StudentInParentSerializer (léger, read-only pour lister les enfants)
# Si tu n'as pas ces noms EXACTS, adapte le nom local, mais normalement tu as déjà des variantes.

class ParentOptimizedReadSerializer(serializers.ModelSerializer):
    """
    Serializer optimisé pour list/retrieve — lecture seule, nested léger pour user + students.
    Utiliser dans les endpoints paginés / list pour renvoyer tout ce dont le front a besoin.
    """
    user = UserSerializer(read_only=True)  # user complet si besoin ; on peut remplacer par UserSimpleSerializer si on veut alléger
    students = StudentInParentSerializer(many=True, read_only=True)

    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    students_count = serializers.SerializerMethodField()

    class Meta:
        model = Parent
        fields = ("id", "user", "first_name", "last_name", "phone", "students", "students_count")

    def get_students_count(self, obj):
        # Utilise le cache de prefetch pour ne pas lancer de requête supplémentaire
        prefetch_cache = getattr(obj, "_prefetched_objects_cache", None)
        if prefetch_cache and "students" in prefetch_cache:
            return len(prefetch_cache["students"])
        try:
            return obj.students.count()
        except Exception as e:
            logger.exception("get_students_count error for Parent %s: %s", getattr(obj, "id", "?"), str(e))
            return 0


class ParentOptimizedWriteSerializer(serializers.ModelSerializer):
    """
    Serializer pour create/update — accepte nested user pour créer/update l'objet User.
    Ne touche pas à la relation students ici (gestion séparée).
    """
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


# Petite alias si tu veux l'exposer explicitement
class ParentOptimizedProfileSerializer(ParentOptimizedReadSerializer):
    pass
class StudentSerializer(serializers.ModelSerializer):
    # --- READ ONLY (Nested Representations) ---
    # Rendre `user` read_only pour éviter la création implicite massive lors de list() sur gros volumes.
    user = UserSerializer(read_only=True)
    school_class = SchoolClassSimpleSerializer(read_only=True)
    parent = ParentSimpleSerializer(read_only=True)

    # --- WRITE ONLY (IDs pour la création/modif) ---
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        source="user",
        write_only=True,
        required=False,
        allow_null=True
    )
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
            "id", "user", "user_id", "sex", "date_of_birth",
            "school_class", "school_class_id",
            "parent", "parent_id"
        )

    def create(self, validated_data):
        """
        Deux modes supportés :
         - payload contient un dict 'user' (nested) -> on crée le User
         - payload contient 'user' résolu (instance User, via user_id PK) -> on l'utilise
         - sinon on tente de récupérer 'user' dans validated_data
        """
        user_data = validated_data.pop("user", None)

        try:
            # Cas 1: user_data est déjà une instance User (PrimaryKeyRelatedField)
            if user_data and isinstance(user_data, User):
                user = user_data

            # Cas 2: user_data est un dict (nested write)
            elif user_data and isinstance(user_data, dict):
                user = UserSerializer().create(validated_data=user_data)

            # Cas 3: pas de 'user' poppé — DRF peut avoir mis l'instance dans validated_data['user']
            else:
                user = validated_data.get("user", None)

            if user is None:
                # Si ton modèle exige un user, lever une erreur claire
                raise serializers.ValidationError("User data missing or invalid for Student creation.")

            with transaction.atomic():
                student = Student.objects.create(user=user, **{k: v for k, v in validated_data.items() if k != "user"})
            return student

        except Exception as e:
            logger.exception("Student create failed: %s", str(e))
            # Re-raise pour que DRF traite l'exception proprement (ou raise serializers.ValidationError si tu préfères)
            raise

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", None)
        if user_data:
            try:
                UserSerializer().update(instance.user, user_data)
            except Exception:
                logger.exception("Failed updating nested user for Student %s", instance.pk)
        return super().update(instance, validated_data)


### Dans ton fichier serializers.py ###

class StudentListSerializer(serializers.ModelSerializer):
    """
    Version optimisée pour les listes, mais avec la structure attendue par le frontend.
    """
    user = UserSimpleSerializer(read_only=True)
    # On utilise les serializers imbriqués au lieu de simples chaînes
    school_class = SchoolClassSimpleSerializer(read_only=True)
    parent = ParentSimpleSerializer(read_only=True)

    class Meta:
        model = Student
        fields = ("id", "user", "sex", "date_of_birth", "school_class", "parent")

class StudentProfileSerializer(StudentSerializer):
    """Identique au serializer complet pour l'instant."""
    pass


# ========================================================================
# 5. TEACHER SERIALIZERS
# ========================================================================
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
        classes = validated_data.pop("classes", [])  # M2M handling

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
        classes = validated_data.pop("classes", None)  # None signifie "ne pas toucher"

        if user_data:
            UserSerializer().update(instance.user, user_data)

        # Update standard fields
        super().update(instance, validated_data)

        # Update M2M si fourni
        if classes is not None:
            instance.classes.set(classes)

        return instance

from rest_framework import serializers
from django.db import transaction
from django.db.utils import IntegrityError
import logging

logger = logging.getLogger(__name__)

class TeacherFullSerializer(serializers.ModelSerializer):
    """
    Serializer utilisé pour list/retrieve : renvoie tout (user, subject, classes) en nested
    => front n'a plus à re-fetcher des ids.
    """
    user = UserSerializer(read_only=True)
    subject = SubjectSimpleSerializer(read_only=True)
    classes = SchoolClassSimpleSerializer(many=True, read_only=True)

    class Meta:
        model = Teacher
        fields = ("id", "user", "subject", "classes")


class TeacherWriteSerializer(serializers.ModelSerializer):
    """
    Serializer pour create/update (accepts related ids).
    Garde la logique create/update que tu as déjà mais séparée pour éviter confusion
    entre read-only nested fields et write PK fields.
    """
    user = UserSerializer()
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
        fields = ("id", "user", "subject_id", "class_ids")

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
        classes = validated_data.pop("classes", None)  # None => ne pas toucher
        if user_data:
            UserSerializer().update(instance.user, user_data)
        # update autres champs standards
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
    """Vue détaillée d'une classe avec son emploi du temps."""
    timetable = ClassScheduleEntrySerializer(many=True, read_only=True)

    class Meta:
        model = SchoolClass
        fields = ("id", "name", "level", "timetable")