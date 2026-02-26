from rest_framework import serializers
from django.contrib.auth.models import User

from core.models import Student, Parent, Teacher
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

# Assure-toi d'importer ton modèle Teacher et User si ce n'est pas déjà fait
# from .models import Teacher 

class SimpleTeacherSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    # Si tu as un champ 'subjects' ou 'specialty' dans le modèle Teacher, ajoute-le ici
    # subjects = ... 

    class Meta:
        # Remplace 'Teacher' par le nom exact de ton modèle Enseignant
        model = Teacher 
        fields = ['id', 'first_name', 'last_name', 'email'] # Ajoute 'subjects' ici si dispo
# ---- SCHOOL CLASS ----
from rest_framework import serializers
from .models import SchoolClass, Level
# NOTE: LevelSerializer, StudentSerializer, SimpleTeacherSerializer
# doivent exister dans ce module ou être importés plus haut dans le fichier.

class SchoolClassListSerializer(serializers.ModelSerializer):
    """
    Serializer léger pour la liste : conserve les mêmes noms de champs
    que ton frontend attend (id, name, level).
    Pas d'élèves, pas de profs.
    """
    level = LevelSerializer(read_only=True)

    class Meta:
        model = SchoolClass
        fields = ["id", "name", "level"]


class SchoolClassSerializer(serializers.ModelSerializer):
    # --- version détaillée existante (inchangée pour le comportement actuel) ---
    level = LevelSerializer(read_only=True)
    level_id = serializers.PrimaryKeyRelatedField(
        queryset=Level.objects.all(), write_only=True, source="level"
    )
    students = serializers.SerializerMethodField()
    teachers = serializers.SerializerMethodField()

    class Meta:
        model = SchoolClass
        fields = ["id", "name", "level", "level_id", "students", "teachers"]

    def get_students(self, obj):
        # logique inchangée fournie par toi
        request = self.context.get("request")
        if not request:
            return []
        user = request.user

        if user.is_staff or user.is_superuser:
            return StudentSerializer(obj.students.all(), many=True).data
        if hasattr(user, "parent"):
            qs = obj.students.filter(parent=user.parent)
            return StudentSerializer(qs, many=True).data
        if hasattr(user, "student"):
            return [
                {"first_name": s.user.first_name, "last_name": s.user.last_name}
                for s in obj.students.all()
            ]
        if hasattr(user, "teacher"):
            teacher = user.teacher
            if obj in teacher.classes.all():
                return StudentSerializer(obj.students.all(), many=True).data
            return []
        return []

    def get_teachers(self, obj):
        try:
            teachers_qs = obj.teacher_set.all()
        except AttributeError:
            teachers_qs = obj.teachers.all()

        return SimpleTeacherSerializer(teachers_qs, many=True).data
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


# serializers.py
from rest_framework import serializers
from academics.models import ClassScheduleEntry, Subject, SchoolClass

class ClassScheduleEntrySerializer(serializers.ModelSerializer):
    """
    Serializer pour ClassScheduleEntry : léger et sûr pour affichage et écriture.
    Expose également des champs lisibles (noms) et des heures formatées.
    """
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    teacher_name = serializers.SerializerMethodField()
    class_name = serializers.CharField(source='school_class.name', read_only=True)

    starts_at_formatted = serializers.SerializerMethodField()
    ends_at_formatted = serializers.SerializerMethodField()

    class Meta:
        model = ClassScheduleEntry
        fields = [
            'id',
            'school_class', 'class_name',
            'subject', 'subject_name',
            'teacher', 'teacher_name',
            'weekday',
            'starts_at', 'starts_at_formatted',
            'ends_at', 'ends_at_formatted',
        ]

    def get_teacher_name(self, obj):
        # Accès defensif : évite d'exploser si relation manquante
        try:
            user = obj.teacher.user
            last = getattr(user, "last_name", "") or ""
            first = getattr(user, "first_name", "") or ""
            return f"{last} {first}".strip() or "N/A"
        except Exception:
            return "N/A"

    def get_starts_at_formatted(self, obj):
        return obj.starts_at.strftime("%H:%M") if getattr(obj, "starts_at", None) else None

    def get_ends_at_formatted(self, obj):
        return obj.ends_at.strftime("%H:%M") if getattr(obj, "ends_at", None) else None

    def validate(self, data):
        """
        Validation défensive :
        - utilise les valeurs fournies dans `data` si présentes,
        - sinon, récupère les valeurs existantes sur l'instance (pour updates partiels).
        - nève pas d'erreur si une des valeurs est absente (la validation est alors ignorée).
        """
        instance = getattr(self, "instance", None)

        starts = data.get("starts_at", getattr(instance, "starts_at", None) if instance else None)
        ends = data.get("ends_at", getattr(instance, "ends_at", None) if instance else None)

        if starts is not None and ends is not None:
            if starts >= ends:
                raise serializers.ValidationError("L'heure de fin doit être après l'heure de début.")
        return data



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

# academics/serializers.py (patché)
from rest_framework import serializers
from academics.models import DraftGrade, Subject, ClassSubject
from core.models import Student, Teacher

class DraftGradeSerializer(serializers.ModelSerializer):
    # write helpers — alternatives : accept either *_ref or direct pk (one or the other)
    student_ref = serializers.SlugRelatedField(
        slug_field="id",
        queryset=Student.objects.all(),
        write_only=True,
        source="student",
        required=False
    )
    student = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(),
        write_only=True,
        required=False
    )

    subject_ref = serializers.SlugRelatedField(
        slug_field="id",
        queryset=Subject.objects.all(),
        write_only=True,
        source="subject",
        required=False
    )
    subject = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(),
        write_only=True,
        required=False
    )

    # read helpers (always read-only)
    student_id = serializers.CharField(source="student.id", read_only=True)
    student_name = serializers.CharField(source="student.user.get_full_name", read_only=True)
    subject_id = serializers.CharField(source="subject.id", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)

    # teacher read-only (teacher.id is a charfield in your model)
    teacher_id = serializers.CharField(source="teacher.id", read_only=True)
    teacher_name = serializers.CharField(source="teacher.user.get_full_name", read_only=True)

    class Meta:
        model = DraftGrade
        fields = [
            "id",
            # teacher is NOT writable by client — injected server-side
            "teacher_id", "teacher_name",
            # student (either pk OR student_ref)
            "student", "student_id", "student_name", "student_ref",
            # subject (either pk OR subject_ref)
            "subject", "subject_id", "subject_name", "subject_ref",
            "term",
            "interrogation1", "interrogation2", "interrogation3",
            "devoir1", "devoir2",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "teacher_id", "teacher_name",
            "student_id", "student_name",
            "subject_id", "subject_name",
            "created_at", "updated_at"
        ]

    def validate(self, data):
        """
        - On autorise either student (pk) OR student_ref (slug) OR both.
        - Même chose pour subject.
        - En création (instance is None) : require student + subject + term.
        - Vérification métier : la matière doit exister pour la classe de l'élève.
        """
        # resolved objects will be in data as 'student' and 'subject' thanks to source="student"
        student = data.get("student") or (self.instance.student if self.instance else None)
        subject = data.get("subject") or (self.instance.subject if self.instance else None)
        term = data.get("term") or (self.instance.term if self.instance else None)

        # creation: require student + subject + term
        if self.instance is None:
            missing = []
            if not student:
                missing.append("student / student_ref")
            if not subject:
                missing.append("subject / subject_ref")
            if not term:
                missing.append("term")
            if missing:
                raise serializers.ValidationError({"detail": f"Champs requis manquants: {', '.join(missing)}"})

        # if we have student & subject -> domain check
        if student and subject:
            if not ClassSubject.objects.filter(school_class=student.school_class, subject=subject).exists():
                raise serializers.ValidationError({
                    "subject": f"La matière '{subject.name}' n'est pas définie pour la classe '{student.school_class.name}'."
                })

        return data
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
# ... (tes imports existants)
from academics.models import Announcement # Ajoute Announcement ici

# =======================
# Serializer Annonces
# =======================
class AnnouncementSerializer(serializers.ModelSerializer):
    author_name = serializers.ReadOnlyField(source='created_by.username')
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Announcement
        fields = ["id", "title", "content", "image", "image_url", "created_by", "author_name", "created_at", "updated_at"]
        read_only_fields = ["created_by", "created_at", "updated_at"]

    def get_image_url(self, obj):
        request = self.context.get('request')
        if not obj.image:
            return None
        try:
            url = obj.image.url
        except Exception:
            return None
        if request is None:
            return url  # fallback (relative or storage-provided)
        return request.build_absolute_uri(url)
# serializers.py
from rest_framework import serializers
from .models import StudentAttendance

# ... tes imports existants
from rest_framework import serializers
from .models import StudentAttendance

class StudentAttendanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentAttendance
        fields = ['id', 'student', 'schedule_entry', 'date', 'status', 'reason']  # reason ajouté
