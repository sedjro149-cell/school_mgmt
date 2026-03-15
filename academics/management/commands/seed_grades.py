"""
Management command : seed_grades
Usage :
    python manage.py seed_grades                  # peuple T1 (défaut)
    python manage.py seed_grades --term T2        # peuple T2
    python manage.py seed_grades --term T3 --overwrite   # écrase les notes existantes

Placer ce fichier dans :
    <app>/management/commands/seed_grades.py
"""

import random
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# ── Adapte ces imports à la structure réelle de ton projet ──────────────────
from academics.models import Grade, ClassSubject          # modèles notes + lien classe/matière
from core.models import Student                           # modèle élève
# ────────────────────────────────────────────────────────────────────────────

# ┌─────────────────────────────────────────────────────────────────────────┐
# │  CONFIGURATION – ajuste ces valeurs avant de lancer                     │
# └─────────────────────────────────────────────────────────────────────────┘

TERM = "T1"          # trimestre par défaut (T1 | T2 | T3)

# Plages aléatoires des notes (sur 20)
NOTE_MIN = Decimal("2.00")
NOTE_MAX = Decimal("20.00")

# Probabilité qu'une note soit None (élève absent / non noté)
PROB_NULL = 0.10     # 10 % de chances qu'une note soit None


def _random_note():
    """Renvoie une note aléatoire ou None."""
    if random.random() < PROB_NULL:
        return None
    raw = random.uniform(float(NOTE_MIN), float(NOTE_MAX))
    return Decimal(f"{raw:.2f}")


class Command(BaseCommand):
    help = "Peuple la base de données avec des notes fictives pour un trimestre donné."

    def add_arguments(self, parser):
        parser.add_argument(
            "--term",
            type=str,
            default=TERM,
            choices=["T1", "T2", "T3"],
            help="Trimestre à peupler (T1 | T2 | T3). Défaut : T1.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            default=False,
            help="Écrase les notes déjà existantes. Sans ce flag, les lignes existantes sont ignorées.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Simule l'opération sans rien enregistrer en base.",
        )

    # ─────────────────────────────────────────────────────────────────────
    def handle(self, *args, **options):
        term      = options["term"]
        overwrite = options["overwrite"]
        dry_run   = options["dry_run"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n{'[DRY-RUN] ' if dry_run else ''}Peuplement des notes — Trimestre : {term}"
        ))

        # 1. Récupère tous les élèves avec leur classe
        students = (
            Student.objects
            .select_related("user", "school_class")
            .filter(school_class__isnull=False)
        )

        if not students.exists():
            raise CommandError("Aucun élève avec une classe trouvé en base.")

        self.stdout.write(f"  → {students.count()} élève(s) trouvé(s)")

        # 2. Récupère les liens ClassSubject (classe → matières)
        class_subjects = (
            ClassSubject.objects
            .select_related("school_class", "subject")
            .all()
        )

        # Construit un dict {school_class_id: [subject, ...]}
        class_to_subjects: dict[int, list] = {}
        for cs in class_subjects:
            class_to_subjects.setdefault(cs.school_class_id, []).append(cs.subject)

        # 3. Pré-charge les Grade existants pour ce trimestre (évite N+1)
        existing_grades: dict[tuple, Grade] = {
            (str(g.student_id), g.subject_id): g
            for g in Grade.objects.filter(term=term).select_for_update()
        } if not dry_run else {
            (str(g.student_id), g.subject_id): g
            for g in Grade.objects.filter(term=term)
        }

        # ── Compteurs ────────────────────────────────────────────────────
        created = updated = skipped = errors = 0
        to_create: list[Grade] = []
        to_update: list[Grade] = []

        # 4. Génère les notes
        for student in students:
            subjects = class_to_subjects.get(student.school_class_id, [])

            if not subjects:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠  Aucune matière pour la classe '{student.school_class}' "
                    f"— élève {student.id} ignoré."
                ))
                continue

            for subject in subjects:
                key = (str(student.id), subject.id)

                note_data = {
                    "interrogation1": _random_note(),
                    "interrogation2": _random_note(),
                    "interrogation3": _random_note(),
                    "devoir1":        _random_note(),
                    "devoir2":        _random_note(),
                }

                if key in existing_grades:
                    if not overwrite:
                        skipped += 1
                        continue
                    # Mise à jour
                    g = existing_grades[key]
                    for field, value in note_data.items():
                        setattr(g, field, value)
                    # Désactive les signaux lourds (notifications, recalcul) comme bulk_upsert
                    g._suppress_notifications = True
                    g._skip_averages = True
                    to_update.append(g)
                    updated += 1
                else:
                    # Création
                    g = Grade(
                        student=student,
                        subject=subject,
                        term=term,
                        **note_data,
                    )
                    g._suppress_notifications = True
                    g._skip_averages = True
                    to_create.append(g)
                    created += 1

        # 5. Persistance en une transaction atomique
        if not dry_run:
            try:
                with transaction.atomic():
                    if to_create:
                        Grade.objects.bulk_create(to_create, batch_size=500)
                    if to_update:
                        Grade.objects.bulk_update(
                            to_update,
                            fields=[
                                "interrogation1", "interrogation2", "interrogation3",
                                "devoir1", "devoir2",
                            ],
                            batch_size=500,
                        )
            except Exception as exc:
                raise CommandError(f"Erreur lors de l'écriture en base : {exc}") from exc
        else:
            self.stdout.write(self.style.WARNING("  [DRY-RUN] Aucune donnée écrite."))

        # 6. Rapport final
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(  f"  ✔  Créées  : {created}"))
        if overwrite:
            self.stdout.write(self.style.SUCCESS(f"  ✔  Mises à jour : {updated}"))
        self.stdout.write(self.style.WARNING(  f"  –  Ignorées (déjà existantes) : {skipped}"))
        if errors:
            self.stdout.write(self.style.ERROR(f"  ✖  Erreurs : {errors}"))
        self.stdout.write("")