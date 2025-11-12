from rest_framework import serializers
from django.contrib.auth.models import User

from core.models import Student, Parent
from academics.models import (
    Level,
    SchoolClass,
    Subject,
    ClassSubject,
    Grade,
    ClassScheduleEntry,
)


# ---- USER ----
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]





# ---- PARENT ----
class ParentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = Parent
        fields = ["id", "user", "phone"]


# ---- STUDENT ----
class StudentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    parent = ParentSerializer(read_only=True)
    school_class = serializers.StringRelatedField()

    class Meta:
        model = Student
        fields = ["id", "user", "date_of_birth", "school_class", "parent"]


# ---- LEVEL ----
class LevelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Level
        fields = ["id", "name"]


# ---- SCHOOL CLASS ----
class SchoolClassSerializer(serializers.ModelSerializer):
    level = LevelSerializer(read_only=True)
    level_id = serializers.PrimaryKeyRelatedField(
        queryset=Level.objects.all(), write_only=True, source="level"
    )
    students = serializers.SerializerMethodField()

    class Meta:
        model = SchoolClass
        fields = ["id", "name", "level", "level_id", "students"]

    def get_students(self, obj):
        """
        Affiche les étudiants selon le type d'utilisateur :
        - staff / admin : tous les étudiants, toutes infos
        - parent : seulement ses enfants
        - élève : tous les élèves de sa classe, uniquement noms/prénoms
        - enseignant : tous les élèves des classes qu'il enseigne
        """
        request = self.context.get("request")
        if not request:
            return []

        user = request.user

        # --- Admin/staff : accès complet
        if user.is_staff or user.is_superuser:
            return StudentSerializer(obj.students.all(), many=True).data

        # --- Parent : uniquement ses enfants
        if hasattr(user, "parent"):
            qs = obj.students.filter(parent=user.parent)
            return StudentSerializer(qs, many=True).data

        # --- Élève : uniquement les noms/prénoms des élèves de sa classe
        if hasattr(user, "student"):
            return [
                {"first_name": s.user.first_name, "last_name": s.user.last_name}
                for s in obj.students.all()
            ]

        # --- Enseignant : élèves de ses classes
        if hasattr(user, "teacher"):
            teacher = user.teacher
            if obj in teacher.classes.all():  # il doit être lié à cette classe
                return StudentSerializer(obj.students.all(), many=True).data
            return []  # si ce n'est pas sa classe → pas d'élèves

        # --- Autres rôles : rien
        return []

# ---- SUBJECT ----
class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ["id", "name"]


from rest_framework import serializers
from .models import ClassSubject, SchoolClass, Subject

# Serializer léger pour chaque matière dans la vue groupée
class ClassSubjectInfoSerializer(serializers.Serializer):
    subject_id = serializers.IntegerField()
    subject_name = serializers.CharField()
    coefficient = serializers.IntegerField()
    hours_per_week = serializers.IntegerField()
    is_optional = serializers.BooleanField()

# Serializer groupé (par classe)
class GroupedClassSubjectSerializer(serializers.Serializer):
    class_id = serializers.IntegerField()
    class_name = serializers.CharField()
    subjects = ClassSubjectInfoSerializer(many=True)

# Serializer principal pour CRUD sur ClassSubject
class ClassSubjectSerializer(serializers.ModelSerializer):
    # On renvoie une représentation contrôlée et minimale (pas les élèves)
    school_class = serializers.SerializerMethodField()
    subject = serializers.SerializerMethodField()

    # champs pour écrire (POST/PATCH/PUT)
    school_class_id = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(),
        write_only=True,
        source="school_class"
    )
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(),
        write_only=True,
        source="subject"
    )

    class Meta:
        model = ClassSubject
        fields = [
            "id",
            "school_class",
            "subject",
            "coefficient",
            "is_optional",
            "hours_per_week",
            "school_class_id",
            "subject_id",
        ]

    def get_school_class(self, obj):
        """Retourne uniquement les infos utiles de la classe (pas les élèves)."""
        sc = obj.school_class
        if not sc:
            return None
        return {
            "id": sc.id,
            "name": sc.name,
            "level": {"id": sc.level.id, "name": sc.level.name} if hasattr(sc, "level") and sc.level else None
        }

    def get_subject(self, obj):
        """Retourne uniquement les infos utiles de la matière pour cette classe."""
        subj = obj.subject
        if not subj:
            return None
        return {
            "id": subj.id,
            "name": subj.name,
            # coefficient & hours_per_week appartiennent à ClassSubject, on les renvoie ici
            "coefficient": obj.coefficient,
            "hours_per_week": obj.hours_per_week,
            "is_optional": obj.is_optional,
        }


# serializers.py (extrait)
class GradeSerializer(serializers.ModelSerializer):
    # champs pour écriture (éventuellement renommés pour éviter conflit avec read-only)
    student_ref = serializers.SlugRelatedField(
        slug_field="id", queryset=Student.objects.all(), write_only=True, source="student"
    )
    subject_ref = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), write_only=True, source="subject"
    )

    # champs pour lecture (exposent les PKs)
    student_id = serializers.CharField(source="student.id", read_only=True)
    subject_id = serializers.IntegerField(source="subject.id", read_only=True)

    # champs existants en lecture
    student_firstname = serializers.CharField(source="student.user.first_name", read_only=True)
    student_lastname = serializers.CharField(source="student.user.last_name", read_only=True)
    student_class = serializers.CharField(source="student.school_class.name", read_only=True)
    student_class_id = serializers.CharField(source="student.school_class.id", read_only=True)
    student_code = serializers.CharField(source="student.id", read_only=True)

    subject_name = serializers.CharField(source="subject.name", read_only=True)
    subject_code = serializers.IntegerField(source="subject.id", read_only=True)

    class Meta:
        model = Grade
        fields = [
            "id",
            "student_firstname", "student_lastname", "student_class", "student_class_id", "student_code",
            # lecture & écriture
            "student_id", "student_ref",
            "subject_name", "subject_code", "subject_id", "subject_ref",
            # notes
            "interrogation1", "interrogation2", "interrogation3",
            "devoir1", "devoir2",
            # calculs
            "average_interro", "average_subject", "average_coeff",
            "term", "created_at",
        ]
        read_only_fields = [
            "student_firstname", "student_lastname", "student_class", "student_class_id", "student_code",
            "subject_name", "subject_code",
            "average_interro", "average_subject", "average_coeff", "created_at",
            # les champs student_id/subject_id sont explicitement read_only via leur declaration
        ]

    def validate(self, data):
        student = data.get("student")
        subject = data.get("subject")

        if not student or not subject:
            return data  # rien à valider si info incomplète

        school_class = student.school_class

        # Vérifier si ce couple (classe, matière) existe dans ClassSubject
        exists = ClassSubject.objects.filter(
            school_class=school_class,
            subject=subject
        ).exists()

        if not exists:
            raise serializers.ValidationError({
                "subject": f"La matière '{subject.name}' n'est pas enseignée dans la classe '{school_class.name}'."
            })

        return data


## academics/serializers.py
from rest_framework import serializers
from academics.models import ClassScheduleEntry

class ClassScheduleEntrySerializer(serializers.ModelSerializer):
    school_class_name = serializers.StringRelatedField(source="school_class", read_only=True)
    subject_name = serializers.StringRelatedField(source="subject", read_only=True)
    teacher = serializers.PrimaryKeyRelatedField(read_only=True)  # renvoie l'ID du prof
    teacher_name = serializers.SerializerMethodField()  # pour le nom complet

    class Meta:
        model = ClassScheduleEntry
        fields = [
            "id",
            "school_class", "school_class_name",
            "subject", "subject_name",
            "teacher", "teacher_name",
            "weekday", "starts_at", "ends_at",
        ]

    def get_teacher_name(self, obj):
        if obj.teacher:
            return f"{obj.teacher.user.first_name} {obj.teacher.user.last_name}"
        return None



# ---- REPORT CARD ----
def _to_float_2(val):
    try:
        return round(float(val), 2) if val is not None else None
    except Exception:
        return None

# academics/serializers.py (ajouter)
from rest_framework import serializers
from .models import Grade
from core.models import Student
from academics.models import Subject

from rest_framework import serializers

# --- note : on suppose que Student.id est bien le code S000000 (CharField primary key) ---
class GradeBulkLineSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)

    # student_id: on accepte le slug alphanumérique (ex: 'S000123') et DRF
    # le résout en instance Student (source='student')
    student_id = serializers.SlugRelatedField(
        slug_field="id",
        queryset=Student.objects.all(),
        source="student",   # validated_data contiendra 'student': <Student instance>
        write_only=True
    )

    # subject_id : on attend l'id numérique de la matière (PrimaryKey)
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(),
        source="subject",
        write_only=True
    )

    term = serializers.ChoiceField(choices=Grade.TERM_CHOICES)

    interrogation1 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    interrogation2 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    interrogation3 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    devoir1 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    devoir2 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)

    def validate(self, data):
        """
        Ici data contiendra 'student' (Student instance) et 'subject' (Subject instance)
        car nous avons utilisé source="student" / source="subject".
        """
        student = data.get("student")   # instance Student
        subject = data.get("subject")   # instance Subject

        if not student or not subject:
            # si l'une des deux est manquante, DRF a géré ça avant; on renvoie simplement
            return data

        # Vérifier que la matière est bien enseignée dans la classe de l'élève
        exists = ClassSubject.objects.filter(school_class=student.school_class, subject=subject).exists()
        if not exists:
            raise serializers.ValidationError({
                "subject_id": f"La matière '{subject.name}' n'est pas enseignée dans la classe '{student.school_class.name}'."
            })

        return data


class ReportCardSubjectSerializer(serializers.Serializer):
    subject = serializers.SerializerMethodField()
    coefficient = serializers.SerializerMethodField()

    interrogation1 = serializers.SerializerMethodField()
    interrogation2 = serializers.SerializerMethodField()
    interrogation3 = serializers.SerializerMethodField()
    devoir1 = serializers.SerializerMethodField()
    devoir2 = serializers.SerializerMethodField()

    average_interro = serializers.SerializerMethodField()
    average_subject = serializers.SerializerMethodField()
    average_coeff = serializers.SerializerMethodField()

    def get_subject(self, obj):
        subj = getattr(obj, "subject", None)
        return getattr(subj, "name", str(subj)) if subj else None

    def get_coefficient(self, obj):
        # Utilise la propriété Grade.coefficient
        return getattr(obj, "coefficient", None)

    def _get_num(self, obj, attr):
        return _to_float_2(getattr(obj, attr, None))

    def get_interrogation1(self, obj): return self._get_num(obj, "interrogation1")
    def get_interrogation2(self, obj): return self._get_num(obj, "interrogation2")
    def get_interrogation3(self, obj): return self._get_num(obj, "interrogation3")
    def get_devoir1(self, obj): return self._get_num(obj, "devoir1")
    def get_devoir2(self, obj): return self._get_num(obj, "devoir2")
    def get_average_interro(self, obj): return self._get_num(obj, "average_interro")
    def get_average_subject(self, obj): return self._get_num(obj, "average_subject")
    def get_average_coeff(self, obj): return self._get_num(obj, "average_coeff")



# academics/serializers.py
from rest_framework import serializers
from academics.models import SubjectComment
from academics.serializers import ReportCardSubjectSerializer

class ReportCardSerializer(serializers.Serializer):
    student_firstname = serializers.CharField(source="student.user.first_name")
    student_lastname = serializers.CharField(source="student.user.last_name")
    student_class = serializers.CharField(source="student.school_class.name")
    term = serializers.CharField()
    subjects = serializers.SerializerMethodField()
    term_average = serializers.SerializerMethodField()
    rank = serializers.SerializerMethodField()
    best_average = serializers.SerializerMethodField()
    worst_average = serializers.SerializerMethodField()

    def get_subjects(self, obj):
        """
        Récupère les matières avec notes et commentaire du trimestre correspondant.
        """
        grades = obj.get("grades") if isinstance(obj, dict) else getattr(obj, "grades", None)
        if not grades:
            return []

        term = obj.get("term") if isinstance(obj, dict) else getattr(obj, "term", None)
        student = obj.get("student") if isinstance(obj, dict) else getattr(obj, "student", None)

        # Récupérer tous les commentaires de l'élève pour ce trimestre
        comments_qs = SubjectComment.objects.filter(student=student, term=term)
        comments_dict = {(c.subject_id): c.comment for c in comments_qs}

        # Sérialiser les matières avec ajout du commentaire
        subjects_data = []
        for grade in grades:
            grade_data = ReportCardSubjectSerializer(grade).data
            subject_id = getattr(grade, "subject_id", None)
            grade_data["comment"] = comments_dict.get(subject_id, "")
            subjects_data.append(grade_data)

        return subjects_data

    def get_term_average(self, obj):
        if isinstance(obj, dict) and obj.get("term_average") is not None:
            return obj.get("term_average")

        grades = obj.get("grades") if isinstance(obj, dict) else getattr(obj, "grades", None)
        if not grades:
            return None

        total, count = 0.0, 0
        for g in grades:
            val = getattr(g, "average_coeff", None)
            if val is not None:
                try:
                    total += float(val)
                    count += 1
                except Exception:
                    continue

        return round(total / count, 2) if count else None

    def _get_from_obj(self, obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    def get_rank(self, obj): return self._get_from_obj(obj, "rank")
    def get_best_average(self, obj):
        val = self._get_from_obj(obj, "best_average")
        return float(val) if val is not None else None

    def get_worst_average(self, obj):
        val = self._get_from_obj(obj, "worst_average")
        return float(val) if val is not None else None

# academics/serializers.py
from rest_framework import serializers
from academics.models import SubjectComment

class SubjectCommentSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.user.get_full_name", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    teacher_name = serializers.CharField(source="teacher.user.get_full_name", read_only=True)
    term_display = serializers.CharField(source="get_term_display", read_only=True)

    class Meta:
        model = SubjectComment
        fields = [
            "id",
            "student", "student_name",
            "subject", "subject_name",
            "teacher", "teacher_name",
            "term", "term_display",
            "comment", "created_at",  # supprimé updated_at
        ]


from rest_framework import serializers
from .models import TimeSlot, Weekday

class TimeSlotSerializer(serializers.ModelSerializer):
    # Affichage lisible du jour pour l'API
    day_display = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = TimeSlot
        fields = ["id", "day", "day_display", "start_time", "end_time"]

    def get_day_display(self, obj):
        return Weekday(obj.day).label
