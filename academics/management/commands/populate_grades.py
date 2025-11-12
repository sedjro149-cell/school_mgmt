"""
Django management command: populate_grades

Usage (place this file in an app's management/commands/ directory):
    python manage.py populate_grades [--term T1] [--decimal-rate 0.2] [--seed 42] [--dry-run]

Ce que fait le script :
- Parcourt tous les élèves (core.Student)
- Pour chaque élève, récupère les ClassSubject rattachés à sa classe
- Pour chaque matière, crée ou met à jour une instance Grade pour le trimestre demandé (par défaut T1)
- Remplit interrogation1, interrogation2, interrogation3, devoir1, devoir2
- Les notes vont de 5 à 20, avec pas plus d'une note contenant une partie décimale sur ~1/decimal_rate notes (par défaut 0.2 -> 1 note sur 5)
- Les décimales possibles sont des quarts: .25, .50, .75 (les entiers restent possibles)
- Respecte la relation Classe <-> Matière (ClassSubject). Si l'élève n'a pas de classe ou si la matière n'est pas définie pour la classe, l'entrée est ignorée.

Notes :
- Le script utilise django.apps.apps.get_model pour être résilient aux noms d'apps (on suppose que Student est dans l'app 'core' et que Grade, ClassSubject et Subject sont dans l'app 'academics').
- Les valeurs sont stockées comme Decimal et arrondies à 2 décimales (champ DecimalField du modèle).

"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.apps import apps
from decimal import Decimal
import random
import sys


def make_mark(min_val=5, max_val=20, decimal_rate=0.2):
    """Retourne un Decimal en pas de 0.25 entre min_val et max_val.
    Par défaut on met une partie décimale dans ~decimal_rate des cas.
    """
    # Choix de la partie entière
    int_part = random.randint(min_val, max_val)
    frac = Decimal('0')
    if int_part < max_val and random.random() < decimal_rate:
        frac_choice = random.choice([Decimal('0.25'), Decimal('0.5'), Decimal('0.75')])
        value = Decimal(int_part) + frac_choice
    else:
        value = Decimal(int_part)
    # Juste au cas où : clamp entre min_val et max_val
    if value < Decimal(min_val):
        value = Decimal(min_val)
    if value > Decimal(max_val):
        value = Decimal(max_val)
    # conformité aux DecimalField (2 décimales)
    return value.quantize(Decimal('0.01'))


class Command(BaseCommand):
    help = 'Peuple les notes (Grade) pour tous les élèves pour un trimestre donné (par défaut T1).'

    def add_arguments(self, parser):
        parser.add_argument('--term', type=str, default='T1', help='Trimestre à peupler (T1/T2/T3).')
        parser.add_argument('--decimal-rate', type=float, default=0.2, help='Probabilité qu\'une note ait une partie décimale (ex: 0.2 => 1/5 notes).')
        parser.add_argument('--seed', type=int, default=None, help='Seed pour le RNG (reproductible).')
        parser.add_argument('--dry-run', action='store_true', help="Ne pas écrire en base, afficher seulement le résumé.")
        parser.add_argument('--limit-students', type=int, default=0, help='Limiter le nombre d\'élèves traités (0 => tous).')

    def handle(self, *args, **options):
        # configuration
        term = options['term']
        decimal_rate = float(options['decimal_rate'])
        seed = options['seed']
        dry_run = options['dry_run']
        limit = options['limit_students']

        if seed is not None:
            random.seed(seed)

        # Résolution des modèles (on suppose app_label 'core' et 'academics')
        try:
            Student = apps.get_model('core', 'Student')
            Grade = apps.get_model('academics', 'Grade')
            ClassSubject = apps.get_model('academics', 'ClassSubject')
            Subject = apps.get_model('academics', 'Subject')
        except LookupError as e:
            self.stderr.write('Erreur: impossible de trouver les modèles attendus. Vérifie les app_label/model names.\n' + str(e))
            sys.exit(2)

        students_qs = Student.objects.select_related('school_class', 'user').all()
        total_students = students_qs.count()
        if limit and limit > 0:
            students_qs = students_qs[:limit]

        created = 0
        updated = 0
        skipped = 0
        errors = 0
        processed_pairs = 0

        with transaction.atomic():
            for student in students_qs:
                if not getattr(student, 'school_class', None):
                    skipped += 1
                    self.stdout.write(f"SKIP: Élève {getattr(student, 'id', '??')} sans classe.")
                    continue

                class_subjects = ClassSubject.objects.filter(school_class=student.school_class).select_related('subject')
                if not class_subjects.exists():
                    skipped += 1
                    self.stdout.write(f"SKIP: Aucune matière pour la classe {student.school_class} (élève {student.id}).")
                    continue

                for cs in class_subjects:
                    subject = cs.subject
                    # génère 5 notes pour ce couple
                    try:
                        defaults = {
                            'interrogation1': make_mark(decimal_rate=decimal_rate),
                            'interrogation2': make_mark(decimal_rate=decimal_rate),
                            'interrogation3': make_mark(decimal_rate=decimal_rate),
                            'devoir1': make_mark(decimal_rate=decimal_rate),
                            'devoir2': make_mark(decimal_rate=decimal_rate),
                        }

                        if dry_run:
                            processed_pairs += 1
                            continue

                        g, created_flag = Grade.objects.update_or_create(
                            student=student,
                            subject=subject,
                            term=term,
                            defaults=defaults,
                        )
                        # save() appellera calculate_averages() si présent sur le modèle
                        g.save()

                        if created_flag:
                            created += 1
                        else:
                            updated += 1
                        processed_pairs += 1

                    except Exception as e:
                        errors += 1
                        self.stderr.write(f"ERROR for student {student.id} subject {getattr(subject, 'id', '??')}: {e}")

        # résumé
        self.stdout.write('\n=== Résumé ===')
        self.stdout.write(f'Trimester: {term}')
        self.stdout.write(f'Students scanned: {total_students}')
        if limit and limit > 0:
            self.stdout.write(f' (limit: {limit})')
        self.stdout.write(f'Pairs processed (student x subject): {processed_pairs}')
        self.stdout.write(f'Created: {created}  Updated: {updated}  Skipped students/classes: {skipped}  Errors: {errors}')
        if dry_run:
            self.stdout.write('\n(Mode dry-run — aucune écriture en base)')

        self.stdout.write('\nTerminé.')
