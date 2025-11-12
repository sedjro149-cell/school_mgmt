import random
from django.db import models
from django.contrib.auth.models import User
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
        # Auto-sync avec l’utilisateur lié
        if self.user:
            self.first_name = self.user.first_name
            self.last_name = self.user.last_name
        super().save(*args, **kwargs)
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
    
    # Duplication des noms pour la DB
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
        # Auto-sync avec l’utilisateur lié
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
    sex = models.CharField(max_length=1, choices=SEX_CHOICES, default='M')  # Nouveau champ

    parent = models.ForeignKey(Parent, on_delete=models.SET_NULL, null=True, blank=True, related_name="students")
    school_class = models.ForeignKey(
        "academics.SchoolClass",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="students"
    )

    # ✅ Nouveau : pour ne pas regénérer les fees en boucle
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
