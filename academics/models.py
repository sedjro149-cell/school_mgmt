from django.db import models
from django.core.exceptions import ValidationError
from decimal import Decimal
from core.models import Student
from core.models import Teacher, Student, User



# =======================
# Niveaux et classes
# =======================
class Level(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SchoolClass(models.Model):
    name = models.CharField(max_length=50)
    level = models.ForeignKey(Level, on_delete=models.CASCADE, related_name="classes")

    class Meta:
        unique_together = ("name", "level")
        ordering = ["level__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.level})"





# =======================
# Matières
# =======================
class Subject(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ClassSubject(models.Model):
    school_class = models.ForeignKey(
        SchoolClass, on_delete=models.CASCADE, related_name="class_subjects"
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.CASCADE, related_name="class_subjects"
    )
    coefficient = models.PositiveIntegerField(default=1)
    is_optional = models.BooleanField(default=False)
    
    # Nouveau champ : nombre d'heures par semaine pour cette matière dans cette classe
    hours_per_week = models.PositiveIntegerField(default=0)  

    class Meta:
        unique_together = ("school_class", "subject")
        ordering = ["school_class__level__name", "school_class__name", "subject__name"]

    def __str__(self):
        opt = " (facultatif)" if self.is_optional else ""
        return f"{self.school_class} - {self.subject} (coef {self.coefficient}, {self.hours_per_week}h/semaine){opt}"

# =======================
# Emploi du temps
# =======================
class Weekday(models.IntegerChoices):
    MONDAY = 1, "Monday"
    TUESDAY = 2, "Tuesday"
    WEDNESDAY = 3, "Wednesday"
    THURSDAY = 4, "Thursday"
    FRIDAY = 5, "Friday"
    SATURDAY = 6, "Saturday"
    SUNDAY = 7, "Sunday"


class ClassScheduleEntry(models.Model):
    school_class = models.ForeignKey(
        SchoolClass, on_delete=models.CASCADE, related_name="timetable"
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="timetable_entries"
    )
    teacher = models.ForeignKey(
        "core.Teacher",
        on_delete=models.PROTECT,
        related_name="timetable_entries",
        null=True,
        blank=True
    )
    weekday = models.PositiveSmallIntegerField()
    starts_at = models.TimeField()
    ends_at = models.TimeField()


# =======================
# Notes et bulletins
# =======================
class Grade(models.Model):
    TERM_CHOICES = [("T1", "1er trimestre"), ("T2", "2e trimestre"), ("T3", "3e trimestre")]

    student = models.ForeignKey("core.Student", on_delete=models.CASCADE, related_name="grades")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="grades")
    term = models.CharField(max_length=10, choices=TERM_CHOICES)

    interrogation1 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    interrogation2 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    interrogation3 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    devoir1 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    devoir2 = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    average_interro = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    average_subject = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    average_coeff = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("student", "subject", "term")
        ordering = ["student__user__username", "subject__name"]

    def __str__(self):
        return f"{self.student} - {self.subject} ({self.term})"

    def clean(self):
        school_class = getattr(self.student, "school_class", None)
        if school_class is None:
            raise ValidationError("L'élève doit être rattaché à une classe avant d'enregistrer une note.")
        if not ClassSubject.objects.filter(school_class=school_class, subject=self.subject).exists():
            raise ValidationError(f"La matière « {self.subject} » n'est pas définie pour la classe « {school_class} ».")

    @property
    def coefficient(self):
        cs = ClassSubject.objects.filter(
            school_class=self.student.school_class, subject=self.subject
        ).first()
        return cs.coefficient if cs else 1

    def calculate_averages(self):
        interros = [n for n in [self.interrogation1, self.interrogation2, self.interrogation3] if n is not None]
        self.average_interro = round(sum(interros) / len(interros), 2) if interros else None
        devoirs = [n for n in [self.devoir1, self.devoir2] if n is not None]
        all_grades = devoirs + ([self.average_interro] if self.average_interro else [])
        self.average_subject = round(sum(all_grades) / len(all_grades), 2) if all_grades else None
        self.average_coeff = round(self.average_subject * self.coefficient, 2) if self.average_subject else None

    def save(self, *args, **kwargs):
        self.calculate_averages()
        super().save(*args, **kwargs)


# =======================
# Commentaires des professeurs sur les matières
# =======================
class SubjectComment(models.Model):
    TERM_CHOICES = [("T1", "1er trimestre"), ("T2", "2e trimestre"), ("T3", "3e trimestre")]

    student = models.ForeignKey("core.Student", on_delete=models.CASCADE, related_name="subject_comments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="subject_comments")
    teacher = models.ForeignKey("core.Teacher", on_delete=models.CASCADE, related_name="subject_comments")
    term = models.CharField(max_length=10, choices=TERM_CHOICES)

    comment = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("student", "subject", "term")
        ordering = ["student__user__username", "subject__name"]

    def __str__(self):
        return f"{self.student} - {self.subject} ({self.term})"

from django.db import models

class TimeSlot(models.Model):
    # Lier le créneau à un jour via Weekday
    day = models.IntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["day", "start_time"]

    def __str__(self):
        day_display = Weekday(self.day).label  # pour afficher "Monday", "Tuesday", etc.
        return f"{day_display} {self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}"
# models.py
from django.db import models

class StudentAttendance(models.Model):
    STATUS_CHOICES = [
        ('ABSENT', 'Absent'),
        ('LATE', 'Retard'),
        ('EXCUSED', 'Excusé'),
    ]

    student = models.ForeignKey(
        "core.Student", 
        on_delete=models.CASCADE, 
        related_name="attendances"
    )
    # On lie l'absence à un CRÉNEAU d'emploi du temps précis
    schedule_entry = models.ForeignKey(
        ClassScheduleEntry, 
        on_delete=models.CASCADE, 
        related_name="student_attendances"
    )
    date = models.DateField() # La date réelle (ex: 2025-12-01)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='ABSENT')
    reason = models.CharField(max_length=255, blank=True, null=True) # "Maladie", "Non justifié"...

    class Meta:
        # Un élève ne peut pas être marqué absent deux fois pour le même cours le même jour
        unique_together = ('student', 'schedule_entry', 'date')
        indexes = [
            models.Index(fields=['date', 'schedule_entry']),
        ]

    def __str__(self):
        return f"{self.student} - {self.status} - {self.date}"
class Announcement(models.Model):

    title = models.CharField(max_length=255, verbose_name="Titre")

    content = models.TextField(verbose_name="Contenu")

    image = models.ImageField(upload_to="announcements/", null=True, blank=True, verbose_name="Image")

    

    # Pour savoir qui a posté (généralement un admin)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="announcements")

    

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)



    class Meta:

        ordering = ["-created_at"] # Les plus récentes en premier

        verbose_name = "Annonce"

        verbose_name_plural = "Annonces"



    def __str__(self):

        return f"{self.title} ({self.created_at.strftime('%d/%m/%Y')})"
