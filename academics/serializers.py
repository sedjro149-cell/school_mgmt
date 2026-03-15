import re
from rest_framework import serializers
from django.contrib.auth.models import User

from core.models import Student, Parent, Teacher
from academics.models import (
    Announcement,
    AttendanceSession,
    ClassScheduleEntry,
    ClassSubject,
    DraftGrade,
    Grade,
    Level,
    SchoolClass,
    SchoolYearConfig,
    StudentAttendance,
    Subject,
    SubjectComment,
    TermStatus,
    TermSubjectConfig,
    TimeSlot,
    Weekday,
)



# ─────────────────────────────────────────────────────────────────────────────
#  USERS / CORE
# ─────────────────────────────────────────────────────────────────────────────

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model  = User
        fields = ["id", "username", "email"]


class ParentSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model  = Parent
        fields = ["id", "user", "phone"]


class StudentSerializer(serializers.ModelSerializer):
    user              = UserSerializer(read_only=True)
    parent            = ParentSerializer(read_only=True)
    school_class_id   = serializers.PrimaryKeyRelatedField(source="school_class", read_only=True)
    school_class_name = serializers.StringRelatedField(source="school_class", read_only=True)

    class Meta:
        model  = Student
        fields = ["id", "user", "date_of_birth", "school_class_id", "school_class_name", "parent"]


class SimpleTeacherSerializer(serializers.ModelSerializer):
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name  = serializers.CharField(source="user.last_name",  read_only=True)
    email      = serializers.CharField(source="user.email",      read_only=True)

    class Meta:
        model  = Teacher
        fields = ["id", "first_name", "last_name", "email"]


# ─────────────────────────────────────────────────────────────────────────────
#  NIVEAUX & CLASSES
# ─────────────────────────────────────────────────────────────────────────────

class LevelSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Level
        fields = ["id", "name"]


class SchoolClassListSerializer(serializers.ModelSerializer):
    level = LevelSerializer(read_only=True)

    class Meta:
        model  = SchoolClass
        fields = ["id", "name", "level"]


class SchoolClassSerializer(serializers.ModelSerializer):
    level    = LevelSerializer(read_only=True)
    level_id = serializers.PrimaryKeyRelatedField(
        queryset=Level.objects.all(), write_only=True, source="level"
    )
    students = serializers.SerializerMethodField()
    teachers = serializers.SerializerMethodField()

    class Meta:
        model  = SchoolClass
        fields = ["id", "name", "level", "level_id", "students", "teachers"]

    def get_students(self, obj):
        request = self.context.get("request")
        if not request:
            return []
        user = request.user
        if user.is_staff or user.is_superuser:
            return StudentSerializer(obj.students.all(), many=True).data
        if hasattr(user, "parent"):
            return StudentSerializer(obj.students.filter(parent=user.parent), many=True).data
        if hasattr(user, "student"):
            return [
                {"first_name": s.user.first_name, "last_name": s.user.last_name}
                for s in obj.students.all()
            ]
        if hasattr(user, "teacher") and obj in user.teacher.classes.all():
            return StudentSerializer(obj.students.all(), many=True).data
        return []

    def get_teachers(self, obj):
        try:
            qs = obj.teacher_set.all()
        except AttributeError:
            qs = obj.teachers.all()
        return SimpleTeacherSerializer(qs, many=True).data


# ─────────────────────────────────────────────────────────────────────────────
#  MATIÈRES & CLASS-SUBJECT
# ─────────────────────────────────────────────────────────────────────────────

class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model  = Subject
        fields = ["id", "name"]


class ClassSubjectInfoSerializer(serializers.Serializer):
    subject_id     = serializers.IntegerField()
    subject_name   = serializers.CharField()
    coefficient    = serializers.IntegerField()
    hours_per_week = serializers.IntegerField()
    is_optional    = serializers.BooleanField()


class GroupedClassSubjectSerializer(serializers.Serializer):
    class_id   = serializers.IntegerField()
    class_name = serializers.CharField()
    subjects   = ClassSubjectInfoSerializer(many=True)


class ClassSubjectSerializer(serializers.ModelSerializer):
    school_class = serializers.SerializerMethodField()
    subject      = serializers.SerializerMethodField()
    school_class_id = serializers.PrimaryKeyRelatedField(
        queryset=SchoolClass.objects.all(), write_only=True, source="school_class"
    )
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), write_only=True, source="subject"
    )

    class Meta:
        model  = ClassSubject
        fields = ["id", "school_class", "subject", "coefficient", "is_optional",
                  "hours_per_week", "school_class_id", "subject_id"]

    def get_school_class(self, obj):
        sc = obj.school_class
        if not sc:
            return None
        return {
            "id":    sc.id,
            "name":  sc.name,
            "level": {"id": sc.level.id, "name": sc.level.name} if sc.level else None,
        }

    def get_subject(self, obj):
        s = obj.subject
        if not s:
            return None
        return {
            "id":            s.id,
            "name":          s.name,
            "coefficient":   obj.coefficient,
            "hours_per_week": obj.hours_per_week,
            "is_optional":   obj.is_optional,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  NOTES — GradeSerializer
# ─────────────────────────────────────────────────────────────────────────────

class GradeSerializer(serializers.ModelSerializer):

    student_ref = serializers.SlugRelatedField(
        slug_field="id", queryset=Student.objects.all(),
        write_only=True, source="student",
    )
    subject_ref = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), write_only=True, source="subject",
    )

    student_id        = serializers.CharField(source="student.id",                read_only=True)
    subject_id        = serializers.IntegerField(source="subject.id",             read_only=True)
    student_firstname = serializers.CharField(source="student.user.first_name",   read_only=True)
    student_lastname  = serializers.CharField(source="student.user.last_name",    read_only=True)
    student_class     = serializers.CharField(source="student.school_class.name", read_only=True)
    student_class_id  = serializers.CharField(source="student.school_class.id",   read_only=True)
    student_code      = serializers.CharField(source="student.id",                read_only=True)
    subject_name      = serializers.CharField(source="subject.name",              read_only=True)
    subject_code      = serializers.IntegerField(source="subject.id",             read_only=True)

    class Meta:
        model  = Grade
        fields = [
            "id",
            "student_firstname", "student_lastname", "student_class",
            "student_class_id",  "student_code",
            "student_id", "student_ref",
            "subject_name", "subject_code", "subject_id", "subject_ref",
            "interrogation1", "interrogation2", "interrogation3",
            "devoir1", "devoir2",
            "average_interro", "average_subject", "average_coeff",
            "term", "created_at",
        ]
        read_only_fields = [
            "student_firstname", "student_lastname", "student_class",
            "student_class_id",  "student_code",
            "subject_name", "subject_code",
            "average_interro", "average_subject", "average_coeff",
            "created_at",
        ]

    def validate(self, data):
        student = data.get("student")
        subject = data.get("subject")
        term    = data.get("term")

        if not student or not subject:
            return data

        # Vérifier que la matière est enseignée dans la classe
        if not ClassSubject.objects.filter(
            school_class=student.school_class, subject=subject
        ).exists():
            raise serializers.ValidationError({
                "subject": (
                    f"La matière '{subject.name}' n'est pas enseignée "
                    f"dans la classe '{student.school_class.name}'."
                )
            })


        return data

    def to_representation(self, instance):
        """
        Masque les moyennes (→ null) pour les parents et élèves
        tant que le TermStatus du couple (school_class, term) n'est pas PUBLISHED.
        Le contexte is_restricted_role et published_pairs est injecté par
        GradeViewSet.get_serializer_context().
        """
        data = super().to_representation(instance)

        if not self.context.get("is_restricted_role"):
            return data

        published_pairs = self.context.get("published_pairs", set())

        try:
            class_id = instance.student.school_class_id
        except AttributeError:
            class_id = None

        if (class_id, instance.term) not in published_pairs:
            data["average_interro"] = None
            data["average_subject"] = None
            data["average_coeff"]   = None

        return data


# ─────────────────────────────────────────────────────────────────────────────
#  NOTES — GradeBulkLineSerializer
# ─────────────────────────────────────────────────────────────────────────────

class GradeBulkLineSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)

    student_id = serializers.SlugRelatedField(
        slug_field="id", queryset=Student.objects.all(),
        source="student", write_only=True,
    )
    subject_id = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), source="subject", write_only=True,
    )
    term = serializers.ChoiceField(choices=Grade.TERM_CHOICES)

    interrogation1 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    interrogation2 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    interrogation3 = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    devoir1        = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)
    devoir2        = serializers.DecimalField(max_digits=5, decimal_places=2, required=False, allow_null=True)

    def validate(self, data):
        student = data.get("student")
        subject = data.get("subject")
        term    = data.get("term")

        if not student or not subject:
            return data

        # Vérifier que la matière est enseignée dans la classe
        if not ClassSubject.objects.filter(
            school_class=student.school_class, subject=subject
        ).exists():
            raise serializers.ValidationError({
                "subject_id": (
                    f"La matière '{subject.name}' n'est pas enseignée "
                    f"dans la classe '{student.school_class.name}'."
                )
            })


        return data


# ─────────────────────────────────────────────────────────────────────────────
#  BULLETINS
# ─────────────────────────────────────────────────────────────────────────────

def _to_float_2(val):
    try:
        return round(float(val), 2) if val is not None else None
    except Exception:
        return None


class ReportCardSubjectSerializer(serializers.Serializer):
    subject         = serializers.SerializerMethodField()
    coefficient     = serializers.SerializerMethodField()
    interrogation1  = serializers.SerializerMethodField()
    interrogation2  = serializers.SerializerMethodField()
    interrogation3  = serializers.SerializerMethodField()
    devoir1         = serializers.SerializerMethodField()
    devoir2         = serializers.SerializerMethodField()
    average_interro = serializers.SerializerMethodField()
    average_subject = serializers.SerializerMethodField()
    average_coeff   = serializers.SerializerMethodField()

    def get_subject(self, obj):
        subj = getattr(obj, "subject", None)
        return getattr(subj, "name", str(subj)) if subj else None

    def get_coefficient(self, obj):
        return getattr(obj, "coefficient", None)

    def _num(self, obj, attr):
        return _to_float_2(getattr(obj, attr, None))

    def get_interrogation1(self, obj):  return self._num(obj, "interrogation1")
    def get_interrogation2(self, obj):  return self._num(obj, "interrogation2")
    def get_interrogation3(self, obj):  return self._num(obj, "interrogation3")
    def get_devoir1(self, obj):         return self._num(obj, "devoir1")
    def get_devoir2(self, obj):         return self._num(obj, "devoir2")
    def get_average_interro(self, obj): return self._num(obj, "average_interro")
    def get_average_subject(self, obj): return self._num(obj, "average_subject")
    def get_average_coeff(self, obj):   return self._num(obj, "average_coeff")


class ReportCardSerializer(serializers.Serializer):
    student_firstname = serializers.CharField(source="student.user.first_name")
    student_lastname  = serializers.CharField(source="student.user.last_name")
    student_class     = serializers.CharField(source="student.school_class.name")
    term              = serializers.CharField()
    subjects          = serializers.SerializerMethodField()
    term_average      = serializers.SerializerMethodField()
    rank              = serializers.SerializerMethodField()
    best_average      = serializers.SerializerMethodField()
    worst_average     = serializers.SerializerMethodField()

    def _get(self, obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    def get_subjects(self, obj):
        grades  = self._get(obj, "grades")
        term    = self._get(obj, "term")
        student = self._get(obj, "student")
        if not grades:
            return []

        comments_dict = {
            c.subject_id: c.comment
            for c in SubjectComment.objects.filter(student=student, term=term)
        }

        results = []
        for grade in grades:
            grade_data = ReportCardSubjectSerializer(grade).data
            grade_data["comment"] = comments_dict.get(getattr(grade, "subject_id", None), "")
            results.append(grade_data)
        return results

    def get_term_average(self, obj):
        val = self._get(obj, "term_average")
        if val is not None:
            return val
        grades = self._get(obj, "grades")
        if not grades:
            return None
        total, count = 0.0, 0
        for g in grades:
            v = getattr(g, "average_coeff", None)
            if v is not None:
                try:
                    total += float(v)
                    count += 1
                except Exception:
                    pass
        return round(total / count, 2) if count else None

    def get_rank(self, obj):
        return self._get(obj, "rank")

    def get_best_average(self, obj):
        val = self._get(obj, "best_average")
        return float(val) if val is not None else None

    def get_worst_average(self, obj):
        val = self._get(obj, "worst_average")
        return float(val) if val is not None else None


# ─────────────────────────────────────────────────────────────────────────────
#  BROUILLONS — DraftGradeSerializer
# ─────────────────────────────────────────────────────────────────────────────

class DraftGradeSerializer(serializers.ModelSerializer):
    student_ref = serializers.SlugRelatedField(
        slug_field="id", queryset=Student.objects.all(),
        write_only=True, source="student", required=False,
    )
    student = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(), write_only=True, required=False,
    )
    subject_ref = serializers.SlugRelatedField(
        slug_field="id", queryset=Subject.objects.all(),
        write_only=True, source="subject", required=False,
    )
    subject = serializers.PrimaryKeyRelatedField(
        queryset=Subject.objects.all(), write_only=True, required=False,
    )

    student_id   = serializers.CharField(source="student.id",                   read_only=True)
    student_name = serializers.CharField(source="student.user.get_full_name",   read_only=True)
    subject_id   = serializers.CharField(source="subject.id",                   read_only=True)
    subject_name = serializers.CharField(source="subject.name",                 read_only=True)
    teacher_id   = serializers.CharField(source="teacher.id",                   read_only=True)
    teacher_name = serializers.CharField(source="teacher.user.get_full_name",   read_only=True)

    class Meta:
        model  = DraftGrade
        fields = [
            "id",
            "teacher_id", "teacher_name",
            "student", "student_id", "student_name", "student_ref",
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
            "created_at", "updated_at",
        ]

    def validate(self, data):
        student = data.get("student") or (self.instance.student if self.instance else None)
        subject = data.get("subject") or (self.instance.subject if self.instance else None)
        term    = data.get("term")    or (self.instance.term    if self.instance else None)

        if self.instance is None:
            missing = []
            if not student: missing.append("student / student_ref")
            if not subject: missing.append("subject / subject_ref")
            if not term:    missing.append("term")
            if missing:
                raise serializers.ValidationError({"detail": f"Champs requis : {', '.join(missing)}"})

        if student and subject:
            if not ClassSubject.objects.filter(
                school_class=student.school_class, subject=subject
            ).exists():
                raise serializers.ValidationError({
                    "subject": (
                        f"La matière '{subject.name}' n'est pas définie "
                        f"pour la classe '{student.school_class.name}'."
                    )
                })


        return data


# ─────────────────────────────────────────────────────────────────────────────
#  COMMENTAIRES
# ─────────────────────────────────────────────────────────────────────────────

class SubjectCommentSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.user.get_full_name", read_only=True)
    subject_name = serializers.CharField(source="subject.name",               read_only=True)
    teacher_name = serializers.CharField(source="teacher.user.get_full_name", read_only=True)
    term_display = serializers.CharField(source="get_term_display",           read_only=True)

    class Meta:
        model  = SubjectComment
        fields = [
            "id",
            "student", "student_name",
            "subject", "subject_name",
            "teacher", "teacher_name",
            "term", "term_display",
            "comment", "created_at",
        ]


# ─────────────────────────────────────────────────────────────────────────────
#  EMPLOI DU TEMPS
# ─────────────────────────────────────────────────────────────────────────────

class TimeSlotSerializer(serializers.ModelSerializer):
    day_display = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model  = TimeSlot
        fields = ["id", "day", "day_display", "start_time", "end_time"]

    def get_day_display(self, obj):
        return Weekday(obj.day).label


class ClassScheduleEntrySerializer(serializers.ModelSerializer):
    subject_name        = serializers.CharField(source="subject.name",      read_only=True)
    class_name          = serializers.CharField(source="school_class.name", read_only=True)
    teacher_name        = serializers.SerializerMethodField()
    starts_at_formatted = serializers.SerializerMethodField()
    ends_at_formatted   = serializers.SerializerMethodField()

    class Meta:
        model  = ClassScheduleEntry
        fields = [
            "id", "school_class", "class_name",
            "subject", "subject_name",
            "teacher", "teacher_name",
            "weekday",
            "starts_at", "starts_at_formatted",
            "ends_at",   "ends_at_formatted",
        ]

    def get_teacher_name(self, obj):
        try:
            u = obj.teacher.user
            return f"{getattr(u, 'last_name', '')} {getattr(u, 'first_name', '')}".strip() or "N/A"
        except Exception:
            return "N/A"

    def get_starts_at_formatted(self, obj):
        return obj.starts_at.strftime("%H:%M") if getattr(obj, "starts_at", None) else None

    def get_ends_at_formatted(self, obj):
        return obj.ends_at.strftime("%H:%M") if getattr(obj, "ends_at", None) else None

    def validate(self, data):
        instance = getattr(self, "instance", None)
        starts   = data.get("starts_at", getattr(instance, "starts_at", None) if instance else None)
        ends     = data.get("ends_at",   getattr(instance, "ends_at",   None) if instance else None)
        if starts is not None and ends is not None and starts >= ends:
            raise serializers.ValidationError("L'heure de fin doit être après l'heure de début.")
        return data


# ─────────────────────────────────────────────────────────────────────────────
#  PRÉSENCES
# ─────────────────────────────────────────────────────────────────────────────

class AttendanceSessionSerializer(serializers.ModelSerializer):
    opened_by_name    = serializers.SerializerMethodField()
    submitted_by_name = serializers.SerializerMethodField()
    is_editable       = serializers.BooleanField(read_only=True)
    subject_name      = serializers.CharField(source="schedule_entry.subject.name",       read_only=True)
    starts_at         = serializers.TimeField(source="schedule_entry.starts_at",          read_only=True)
    ends_at           = serializers.TimeField(source="schedule_entry.ends_at",            read_only=True)
    class_name        = serializers.CharField(source="schedule_entry.school_class.name",  read_only=True)

    class Meta:
        model  = AttendanceSession
        fields = [
            "id", "schedule_entry", "date", "status",
            "opened_by", "opened_by_name", "opened_at",
            "submitted_by", "submitted_by_name", "submitted_at",
            "cancelled_at", "note",
            "is_editable", "subject_name", "starts_at", "ends_at", "class_name",
        ]
        read_only_fields = ["opened_by", "opened_at", "submitted_by", "submitted_at", "cancelled_at"]

    def get_opened_by_name(self, obj):
        return obj.opened_by.get_full_name() if obj.opened_by else None

    def get_submitted_by_name(self, obj):
        return obj.submitted_by.get_full_name() if obj.submitted_by else None

    def validate(self, data):
        entry = data.get("schedule_entry")
        date  = data.get("date")
        if entry and date and not self.instance:
            if AttendanceSession.objects.filter(schedule_entry=entry, date=date).exists():
                raise serializers.ValidationError(
                    "Une session existe déjà pour ce créneau à cette date."
                )
        return data


class StudentAttendanceSerializer(serializers.ModelSerializer):
    student_name   = serializers.SerializerMethodField(read_only=True)
    marked_by_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model  = StudentAttendance
        fields = [
            "id", "session", "student", "student_name",
            "date", "status", "reason",
            "marked_by", "marked_by_name",
            "notified_at", "created_at", "updated_at",
        ]
        read_only_fields = ["date", "marked_by", "marked_by_name", "notified_at", "created_at", "updated_at"]

    def get_student_name(self, obj):
        u = getattr(obj.student, "user", None)
        return f"{u.last_name} {u.first_name}" if u else str(obj.student)

    def get_marked_by_name(self, obj):
        return obj.marked_by.get_full_name() if obj.marked_by else None

    def validate(self, data):
        session = data.get("session") or (self.instance.session if self.instance else None)
        if session and not session.is_editable:
            raise serializers.ValidationError(
                "Cette session est déjà soumise ou annulée."
            )
        student = data.get("student") or (self.instance.student if self.instance else None)
        if session and student:
            if getattr(student, "school_class_id", None) != session.schedule_entry.school_class_id:
                raise serializers.ValidationError("Cet élève n'appartient pas à la classe de cette session.")
        return data

    def create(self, validated_data):
        validated_data["date"]      = validated_data["session"].date
        validated_data["marked_by"] = self.context["request"].user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data["marked_by"] = self.context["request"].user
        return super().update(instance, validated_data)


# ─────────────────────────────────────────────────────────────────────────────
#  ANNONCES
# ─────────────────────────────────────────────────────────────────────────────

class AnnouncementSerializer(serializers.ModelSerializer):
    author_name = serializers.ReadOnlyField(source="created_by.username")
    image_url   = serializers.SerializerMethodField()

    class Meta:
        model  = Announcement
        fields = ["id", "title", "content", "image", "image_url", "created_by",
                  "author_name", "created_at", "updated_at"]
        read_only_fields = ["created_by", "created_at", "updated_at"]

    def get_image_url(self, obj):
        request = self.context.get("request")
        if not obj.image:
            return None
        try:
            url = obj.image.url
        except Exception:
            return None
        return request.build_absolute_uri(url) if request else url


# ─────────────────────────────────────────────────────────────────────────────
#  CYCLE DE VIE DES TRIMESTRES
# ─────────────────────────────────────────────────────────────────────────────

class SchoolYearConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model  = SchoolYearConfig
        fields = ["id", "nb_terms", "current_year"]

    def validate_nb_terms(self, value):
        if value not in (2, 3):
            raise serializers.ValidationError("nb_terms doit être 2 ou 3.")
        return value

    def validate_current_year(self, value):
        if not re.match(r"^\d{4}-\d{4}$", value):
            raise serializers.ValidationError("Format attendu : YYYY-YYYY (ex: 2024-2025).")
        a, b = value.split("-")
        if int(b) != int(a) + 1:
            raise serializers.ValidationError("L'année de fin doit être l'année de début + 1.")
        return value


class TermSubjectConfigSerializer(serializers.ModelSerializer):
    school_class_name = serializers.CharField(source="school_class.name", read_only=True)
    subject_name      = serializers.CharField(source="subject.name",      read_only=True)

    class Meta:
        model  = TermSubjectConfig
        fields = [
            "id",
            "school_class", "school_class_name",
            "subject",      "subject_name",
            "term", "nb_interros", "nb_devoirs",
        ]

    def validate_nb_interros(self, value):
        if not (1 <= value <= 3):
            raise serializers.ValidationError("nb_interros doit être entre 1 et 3.")
        return value

    def validate_nb_devoirs(self, value):
        if not (0 <= value <= 2):
            raise serializers.ValidationError("nb_devoirs doit être entre 0 et 2.")
        return value

    def validate(self, data):
        school_class = data.get("school_class") or (self.instance.school_class if self.instance else None)
        subject      = data.get("subject")      or (self.instance.subject      if self.instance else None)
        term         = data.get("term")          or (self.instance.term          if self.instance else None)

        if school_class and subject:
            if not ClassSubject.objects.filter(school_class=school_class, subject=subject).exists():
                raise serializers.ValidationError({
                    "subject": (
                        f"La matière '{subject.name}' n'est pas enseignée "
                        f"dans la classe '{school_class.name}'."
                    )
                })

        if school_class and term:
            ts = TermStatus.objects.filter(school_class=school_class, term=term).first()
            if ts and not ts.is_editable:
                raise serializers.ValidationError(
                    f"Le trimestre {term} est verrouillé. Impossible de modifier la configuration."
                )

        if term:
            config      = SchoolYearConfig.get_solo()
            valid_terms = [f"T{n}" for n in range(1, config.nb_terms + 1)]
            if term not in valid_terms:
                raise serializers.ValidationError({
                    "term": (
                        f"Trimestre '{term}' invalide. "
                        f"L'école a {config.nb_terms} trimestres ({', '.join(valid_terms)})."
                    )
                })

        return data


class TermStatusSerializer(serializers.ModelSerializer):
    school_class_name = serializers.CharField(source="school_class.name", read_only=True)
    locked_by_name    = serializers.SerializerMethodField()
    is_editable       = serializers.BooleanField(read_only=True)
    subject_configs   = serializers.SerializerMethodField()

    class Meta:
        model  = TermStatus
        fields = [
            "id",
            "school_class", "school_class_name",
            "term", "status", "is_editable",
            "locked_by", "locked_by_name",
            "locked_at", "unlocked_at", "published_at",
            "subject_configs",
        ]
        read_only_fields = ["status", "locked_by", "locked_at", "unlocked_at", "published_at"]

    def get_locked_by_name(self, obj):
        return obj.locked_by.get_full_name() if obj.locked_by else None

    def get_subject_configs(self, obj):
        return [
            {
                "subject_id":   c.subject_id,
                "subject_name": c.subject.name,
                "nb_interros":  c.nb_interros,
                "nb_devoirs":   c.nb_devoirs,
            }
            for c in TermSubjectConfig.objects.filter(
                school_class=obj.school_class, term=obj.term
            ).select_related("subject")
        ]

    def validate(self, data):
        term = data.get("term") or (self.instance.term if self.instance else None)
        if term:
            config      = SchoolYearConfig.get_solo()
            valid_terms = [f"T{n}" for n in range(1, config.nb_terms + 1)]
            if term not in valid_terms:
                raise serializers.ValidationError({
                    "term": (
                        f"Trimestre '{term}' invalide. "
                        f"L'école a {config.nb_terms} trimestres ({', '.join(valid_terms)})."
                    )
                })
        return data