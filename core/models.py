import random
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.core.exceptions import ValidationError


def generate_teacher_id():
    """Génère un ID unique sous la forme T000000."""
    while True:
        new_id = f"T{random.randint(0, 999999):06d}"
        if not Teacher.objects.filter(id=new_id).exists():
            return new_id


class Teacher(models.Model):
    id = models.CharField(max_length=7, primary_key=True, default=generate_teacher_id, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    first_name = models.CharField(max_length=30, default="", blank=True)
    last_name = models.CharField(max_length=30, default="", blank=True)

    # Un enseignant enseigne une seule matière
    subject = models.ForeignKey(
        "academics.Subject",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teachers"
    )

    # Un enseignant peut avoir plusieurs classes
    classes = models.ManyToManyField(
        "academics.SchoolClass",
        blank=True,
        related_name="teachers"
    )

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self):
        cls = f" ({', '.join([c.name for c in self.classes.all()])})" if self.classes.exists() else ""
        subj = f" - {self.subject.name}" if self.subject else ""
        return f"{self.first_name} {self.last_name}{cls}{subj}" or self.user.username

    @property
    def role(self):
        return "teacher"

    def save(self, *args, **kwargs):
        # Auto-sync avec l'utilisateur lié
        if self.user:
            self.first_name = self.user.first_name
            self.last_name = self.user.last_name
        super().save(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
#  Signal m2m_changed — dernier rempart au niveau DB
#
#  Déclenché sur teacher.classes.add() / .set() depuis N'IMPORTE OÙ :
#  shell Django, script, service interne, migration manuelle, etc.
#  Le serializer validate() couvre les appels HTTP — ce signal couvre le reste.
#
#  Placé ici dans models.py : s'enregistre automatiquement à l'import
#  du module, sans configuration supplémentaire dans apps.py.
# ─────────────────────────────────────────────────────────────────────────────

@receiver(m2m_changed, sender=Teacher.classes.through)
def enforce_teacher_class_constraints(sender, instance, action, pk_set, **kwargs):
    """
    R1 — Un seul prof par (classe, matière) :
         Aucun autre Teacher avec le même subject ne peut être lié
         à la même classe.

    R2 — La matière doit être attribuée à la classe avant le prof :
         ClassSubject(school_class=cls, subject=subject) doit exister.

    On intervient sur pre_add et pre_set uniquement (avant modification).
    pre_remove et post_* sont ignorés.
    """
    if action not in ("pre_add", "pre_set"):
        return

    subject = getattr(instance, "subject", None)

    # Pas de matière assignée au prof → rien à vérifier
    if not subject:
        return

    # pk_set peut être None pour pre_set avec liste vide
    if not pk_set:
        return

    # Import local pour éviter les imports circulaires
    # (Teacher est dans core, ClassSubject/SchoolClass sont dans academics)
    from academics.models import ClassSubject, SchoolClass

    classes_to_add = SchoolClass.objects.filter(pk__in=pk_set).select_related("level")

    errors = []

    for cls in classes_to_add:
        # ── R2 : la matière doit être attribuée à la classe ──────────────────
        if not ClassSubject.objects.filter(school_class=cls, subject=subject).exists():
            errors.append(
                f"La matière « {subject.name} » n'est pas attribuée à la classe "
                f"« {cls.name} ». Configurez d'abord la matière dans cette classe "
                f"avant d'y affecter un professeur."
            )
            # Inutile de vérifier R1 si R2 échoue déjà
            continue

        # ── R1 : un seul prof par (classe, matière) ──────────────────────────
        conflict = (
            Teacher.objects
            .filter(subject=subject, classes=cls)
            .exclude(pk=instance.pk)
            .select_related("user")
            .first()
        )
        if conflict:
            errors.append(
                f"La classe « {cls.name} » a déjà un professeur de "
                f"« {subject.name} » : "
                f"{conflict.first_name} {conflict.last_name} (id={conflict.pk}). "
                f"Retirez-le d'abord avant d'en affecter un autre."
            )

    if errors:
        raise ValidationError(errors)


# =======================
# Parent
# =======================
def generate_parent_id():
    """Génère un ID unique sous la forme P000000."""
    while True:
        new_id = f"P{random.randint(0, 999999):06d}"
        if not Parent.objects.filter(id=new_id).exists():
            return new_id


class Parent(models.Model):
    id = models.CharField(max_length=7, primary_key=True, default=generate_parent_id, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    first_name = models.CharField(max_length=30, default="", blank=True)
    last_name = models.CharField(max_length=30, default="", blank=True)

    phone = models.CharField(max_length=20, blank=True, null=True)

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}".strip() or self.user.username

    @property
    def role(self):
        return "parent"

    def save(self, *args, **kwargs):
        if self.user:
            self.first_name = self.user.first_name
            self.last_name = self.user.last_name
        super().save(*args, **kwargs)


# =======================
# Student
# =======================
def generate_student_id():
    """Génère un ID unique sous la forme S000000."""
    while True:
        new_id = f"S{random.randint(0, 999999):06d}"
        if not Student.objects.filter(id=new_id).exists():
            return new_id


class Student(models.Model):
    SEX_CHOICES = [
        ('M', 'Masculin'),
        ('F', 'Féminin'),
    ]

    id = models.CharField(max_length=7, primary_key=True, default=generate_student_id, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    first_name = models.CharField(max_length=30, default="", blank=True)
    last_name = models.CharField(max_length=30, default="", blank=True)

    date_of_birth = models.DateField()
    sex = models.CharField(max_length=1, choices=SEX_CHOICES, default='M')

    parent = models.ForeignKey(Parent, on_delete=models.SET_NULL, null=True, blank=True, related_name="students")
    school_class = models.ForeignKey(
        "academics.SchoolClass",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="students"
    )

    fees_initialized = models.BooleanField(default=False)

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self):
        cls = f" ({self.school_class})" if self.school_class else ""
        return f"{self.first_name} {self.last_name}{cls}" or self.user.username

    @property
    def role(self):
        return "student"

    @property
    def timetable(self):
        if self.school_class:
            return self.school_class.timetable.all()
        return []

    def save(self, *args, **kwargs):
        if self.user:
            self.first_name = self.user.first_name
            self.last_name = self.user.last_name
        super().save(*args, **kwargs)