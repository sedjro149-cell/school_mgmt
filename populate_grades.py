import random
from core.models import Student
from academics.models import Grade, ClassSubject

terms = ["T1", "T2", "T3"]

students = Student.objects.all()

for student in students:
    if not student.school_class:
        continue  # ignorer ceux sans classe

    # Récupérer les matières définies pour la classe de l'élève
    class_subjects = ClassSubject.objects.filter(school_class=student.school_class)

    for cs in class_subjects:
        for term in terms:
            Grade.objects.update_or_create(
                student=student,
                subject=cs.subject,
                term=term,
                defaults={
                    "interrogation1": round(random.uniform(5, 20), 2),
                    "interrogation2": round(random.uniform(5, 20), 2),
                    "devoir1": round(random.uniform(5, 20), 2),
                    "devoir2": round(random.uniform(5, 20), 2),
                },
            )

print("Attribution des notes terminée ✅")
