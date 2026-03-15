from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from decimal import Decimal

from core.models import Student, Teacher, User


# ─────────────────────────────────────────────────────────────────────────────
#  NIVEAUX ET CLASSES
# ─────────────────────────────────────────────────────────────────────────────

class Level(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SchoolClass(models.Model):
    name  = models.CharField(max_length=50)
    level = models.ForeignKey(Level, on_delete=models.CASCADE, related_name="classes")

    class Meta:
        unique_together = ("name", "level")
        ordering = ["level__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.level})"


# ─────────────────────────────────────────────────────────────────────────────
#  MATIÈRES
# ─────────────────────────────────────────────────────────────────────────────

class Subject(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ClassSubject(models.Model):
    school_class   = models.ForeignKey(SchoolClass, on_delete=models.CASCADE, related_name="class_subjects")
    subject        = models.ForeignKey(Subject,     on_delete=models.CASCADE, related_name="class_subjects")
    coefficient    = models.PositiveIntegerField(default=1)
    is_optional    = models.BooleanField(default=False)
    hours_per_week = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("school_class", "subject")
        ordering = ["school_class__level__name", "school_class__name", "subject__name"]

    def __str__(self):
        opt = " (facultatif)" if self.is_optional else ""
        return f"{self.school_class} - {self.subject} (coef {self.coefficient}, {self.hours_per_week}h/semaine){opt}"


# ─────────────────────────────────────────────────────────────────────────────
#  EMPLOI DU TEMPS
# ─────────────────────────────────────────────────────────────────────────────

class Weekday(models.IntegerChoices):
    MONDAY    = 1, "Monday"
    TUESDAY   = 2, "Tuesday"
    WEDNESDAY = 3, "Wednesday"
    THURSDAY  = 4, "Thursday"
    FRIDAY    = 5, "Friday"
    SATURDAY  = 6, "Saturday"
    SUNDAY    = 7, "Sunday"


class ClassScheduleEntry(models.Model):
    school_class = models.ForeignKey(SchoolClass, on_delete=models.CASCADE, related_name="timetable")
    subject      = models.ForeignKey(Subject,     on_delete=models.PROTECT,  related_name="timetable_entries")
    teacher      = models.ForeignKey(
        "core.Teacher", on_delete=models.PROTECT,
        related_name="timetable_entries", null=True, blank=True,
    )
    weekday   = models.PositiveSmallIntegerField()
    starts_at = models.TimeField()
    ends_at   = models.TimeField()


class TimeSlot(models.Model):
    day        = models.IntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time   = models.TimeField()

    class Meta:
        ordering = ["day", "start_time"]

    def __str__(self):
        return f"{Weekday(self.day).label} {self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}"


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION ANNÉE SCOLAIRE  (singleton)
# ─────────────────────────────────────────────────────────────────────────────

class SchoolYearConfig(models.Model):
    """
    Configuration globale de l'année scolaire. Un seul enregistrement (pk=1).
    nb_terms : 2 ou 3 — contrôle quels trimestres sont autorisés.
    current_year : affichage uniquement, ex. "2024-2025".
    """
    NB_TERMS_CHOICES = [(2, "2 trimestres"), (3, "3 trimestres")]

    nb_terms     = models.PositiveSmallIntegerField(choices=NB_TERMS_CHOICES, default=3)
    current_year = models.CharField(max_length=9, default="2024-2025")

    class Meta:
        verbose_name = "Configuration de l'année scolaire"

    def __str__(self):
        return f"Config {self.current_year} — {self.nb_terms} trimestres"

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION PÉDAGOGIQUE PAR MATIÈRE × CLASSE × TRIMESTRE
# ─────────────────────────────────────────────────────────────────────────────

class TermSubjectConfig(models.Model):
    """
    Définit combien d'interrogations et de devoirs sont prévus pour une matière
    dans une classe et un trimestre donné.

    Ces valeurs servent de DIVISEUR FIXE lors du calcul des moyennes au lock,
    ET contraignent la saisie : les champs au-delà de la config sont annulés.

    Valeurs par défaut si absent au lock : nb_interros=3, nb_devoirs=2.
    """
    school_class = models.ForeignKey(SchoolClass, on_delete=models.CASCADE, related_name="term_subject_configs")
    subject      = models.ForeignKey(Subject,     on_delete=models.CASCADE, related_name="term_subject_configs")
    term         = models.CharField(max_length=10)
    nb_interros  = models.PositiveSmallIntegerField(default=3)
    nb_devoirs   = models.PositiveSmallIntegerField(default=2)

    class Meta:
        unique_together = ("school_class", "subject", "term")
        ordering = ["school_class__name", "subject__name", "term"]

    def __str__(self):
        return f"{self.school_class} — {self.subject} — {self.term} ({self.nb_interros}I / {self.nb_devoirs}D)"


# ─────────────────────────────────────────────────────────────────────────────
#  STATUT DE TRIMESTRE PAR CLASSE
# ─────────────────────────────────────────────────────────────────────────────

class TermStatus(models.Model):
    """
    Cycle de vie d'un trimestre pour une classe.

    DRAFT     → saisie ouverte, moyennes non calculées
    LOCKED    → saisie bloquée, moyennes calculées et stockées dans Grade
    PUBLISHED → moyennes visibles par élèves et parents

    Les transitions se font via lock() / unlock() / publish().
    """

    class Status(models.TextChoices):
        DRAFT     = "draft",     "Brouillon (saisie ouverte)"
        LOCKED    = "locked",    "Verrouillé (moyennes calculées)"
        PUBLISHED = "published", "Publié (visible aux élèves)"

    school_class = models.ForeignKey(SchoolClass, on_delete=models.CASCADE, related_name="term_statuses")
    term         = models.CharField(max_length=10)
    status       = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    locked_by    = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="locked_terms")
    locked_at    = models.DateTimeField(null=True, blank=True)
    unlocked_at  = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("school_class", "term")
        ordering = ["school_class__name", "term"]

    def __str__(self):
        return f"{self.school_class} — {self.term} [{self.get_status_display()}]"

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    def lock(self, user):
        from django.db import transaction
        from academics.services.averages import compute_averages_for_term
        if self.status != self.Status.DRAFT:
            raise ValidationError(f"Ce trimestre n'est pas en brouillon (statut : {self.get_status_display()}).")
        with transaction.atomic():
            self.status    = self.Status.LOCKED
            self.locked_at = timezone.now()
            self.locked_by = user
            self.save(update_fields=["status", "locked_at", "locked_by"])
            compute_averages_for_term(self)

    def unlock(self, user):
        from django.db import transaction
        from academics.services.averages import reset_averages_for_term
        if self.status == self.Status.DRAFT:
            raise ValidationError("Ce trimestre est déjà en brouillon.")
        with transaction.atomic():
            self.status      = self.Status.DRAFT
            self.unlocked_at = timezone.now()
            self.locked_at   = None
            self.locked_by   = None
            self.save(update_fields=["status", "unlocked_at", "locked_at", "locked_by"])
            reset_averages_for_term(self)

    def publish(self, user):
        if self.status != self.Status.LOCKED:
            raise ValidationError("Verrouillez d'abord le trimestre avant de le publier.")
        self.status       = self.Status.PUBLISHED
        self.published_at = timezone.now()
        self.save(update_fields=["status", "published_at"])


# ─────────────────────────────────────────────────────────────────────────────
#  NOTES
# ─────────────────────────────────────────────────────────────────────────────

class Grade(models.Model):
    TERM_CHOICES = [("T1", "1er trimestre"), ("T2", "2e trimestre"), ("T3", "3e trimestre")]

    student = models.ForeignKey("core.Student", on_delete=models.CASCADE, related_name="grades")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="grades")
    term    = models.CharField(max_length=10, choices=TERM_CHOICES)

    interrogation1 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    interrogation2 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    interrogation3 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    devoir1        = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    devoir2        = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    average_interro = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    average_subject = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    average_coeff   = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("student", "subject", "term")
        ordering = ["student__user__username", "subject__name"]

    def __str__(self):
        return f"{self.student} - {self.subject} ({self.term})"

    def clean(self):
        school_class = getattr(self.student, "school_class", None)
        if school_class is None:
            raise ValidationError("L'élève doit être rattaché à une classe.")
        if not ClassSubject.objects.filter(school_class=school_class, subject=self.subject).exists():
            raise ValidationError(
                f"La matière « {self.subject} » n'est pas définie pour la classe « {school_class} »."
            )

    @property
    def coefficient(self):
        cs = ClassSubject.objects.filter(
            school_class=self.student.school_class, subject=self.subject
        ).first()
        return cs.coefficient if cs else 1

    def save(self, *args, **kwargs):
        # Les moyennes sont calculées UNIQUEMENT au verrouillage via compute_averages_for_term().
        # Les champs interrogation/devoir excédentaires par rapport à TermSubjectConfig
        # sont ignorés au calcul (pas nullifiés à la saisie).
        super().save(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
#  BROUILLONS DE NOTES (enseignants)
# ─────────────────────────────────────────────────────────────────────────────

class DraftGrade(models.Model):
    TERM_CHOICES = [("T1", "1er trimestre"), ("T2", "2e trimestre"), ("T3", "3e trimestre")]

    teacher = models.ForeignKey("core.Teacher", on_delete=models.CASCADE, related_name="draft_grades")
    student = models.ForeignKey("core.Student", on_delete=models.CASCADE, related_name="draft_grades")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="draft_grades")
    term    = models.CharField(max_length=10, choices=TERM_CHOICES)

    interrogation1 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    interrogation2 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    interrogation3 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    devoir1        = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    devoir2        = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("teacher", "student", "subject", "term")
        ordering = ["teacher__user__username", "student__user__username", "subject__name"]

    def __str__(self):
        return f"Draft {self.teacher} - {self.student} - {self.subject} ({self.term})"


# ─────────────────────────────────────────────────────────────────────────────
#  COMMENTAIRES DES PROFESSEURS
# ─────────────────────────────────────────────────────────────────────────────

class SubjectComment(models.Model):
    TERM_CHOICES = [("T1", "1er trimestre"), ("T2", "2e trimestre"), ("T3", "3e trimestre")]

    student = models.ForeignKey("core.Student", on_delete=models.CASCADE, related_name="subject_comments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="subject_comments")
    teacher = models.ForeignKey("core.Teacher", on_delete=models.CASCADE, related_name="subject_comments")
    term    = models.CharField(max_length=10, choices=TERM_CHOICES)
    comment = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("student", "subject", "term")
        ordering = ["student__user__username", "subject__name"]

    def __str__(self):
        return f"{self.student} - {self.subject} ({self.term})"


# ─────────────────────────────────────────────────────────────────────────────
#  ANNONCES
# ─────────────────────────────────────────────────────────────────────────────

class Announcement(models.Model):
    title      = models.CharField(max_length=255)
    content    = models.TextField()
    image      = models.ImageField(upload_to="announcements/", null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="announcements")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Annonce"
        verbose_name_plural = "Annonces"

    def __str__(self):
        return f"{self.title} ({self.created_at.strftime('%d/%m/%Y')})"


# ─────────────────────────────────────────────────────────────────────────────
#  PRÉSENCES
# ─────────────────────────────────────────────────────────────────────────────

class AttendanceSession(models.Model):
    """
    Feuille d'appel pour un créneau à une date.
    OPEN → SUBMITTED → notifications envoyées.
    CANCELLED → cours annulé.
    """

    class Status(models.TextChoices):
        OPEN      = "OPEN",      "En cours"
        SUBMITTED = "SUBMITTED", "Validé"
        CANCELLED = "CANCELLED", "Annulé"

    schedule_entry = models.ForeignKey(
        "ClassScheduleEntry", on_delete=models.CASCADE, related_name="attendance_sessions"
    )
    date     = models.DateField()
    status   = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="attendance_sessions_opened",
    )
    opened_at    = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="attendance_sessions_submitted",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    note         = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ("schedule_entry", "date")
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["status"]),
        ]
        ordering = ["-date", "schedule_entry__starts_at"]

    def __str__(self):
        return f"Session {self.schedule_entry} — {self.date} [{self.status}]"

    @property
    def is_editable(self):
        return self.status == self.Status.OPEN

    def submit(self, user):
        if self.status != self.Status.OPEN:
            return False
        self.status       = self.Status.SUBMITTED
        self.submitted_at = timezone.now()
        self.submitted_by = user
        self.save(update_fields=["status", "submitted_at", "submitted_by"])
        return True

    def cancel(self, user):
        if self.status == self.Status.SUBMITTED:
            return False
        self.status       = self.Status.CANCELLED
        self.cancelled_at = timezone.now()
        self.save(update_fields=["status", "cancelled_at"])
        return True

    def reopen(self, user):
        if self.status != self.Status.SUBMITTED:
            return False
        self.status       = self.Status.OPEN
        self.submitted_at = None
        self.submitted_by = None
        self.save(update_fields=["status", "submitted_at", "submitted_by"])
        return True


class StudentAttendance(models.Model):
    """
    Non-présence dans une session. L'absence de ligne = présent (session SUBMITTED).
    """
    STATUS_CHOICES = [
        ("ABSENT",  "Absent"),
        ("LATE",    "En retard"),
        ("EXCUSED", "Excusé"),
    ]

    session    = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE,
                                   related_name="attendances", null=True, blank=True)
    student    = models.ForeignKey("core.Student", on_delete=models.CASCADE, related_name="attendances")
    date       = models.DateField(db_index=True)
    status     = models.CharField(max_length=10, choices=STATUS_CHOICES, default="ABSENT")
    reason     = models.CharField(max_length=255, blank=True, null=True)
    marked_by  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, related_name="attendances_marked")
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("student", "session")
        indexes = [
            models.Index(fields=["date", "student"]),
            models.Index(fields=["session", "status"]),
        ]
        ordering = ["date", "student__user__last_name"]

    def __str__(self):
        return f"{self.student} — {self.status} — {self.date}"