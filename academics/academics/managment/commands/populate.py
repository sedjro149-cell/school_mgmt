# FILE: academics/management/commands/populate_academics.py
from django.core.management.base import BaseCommand
from django.db import transaction

from academics.models import Level, SchoolClass, Subject, ClassSubject


class Command(BaseCommand):
    help = "Peuple la base : niveaux, classes, matières et liaisons classe↔matière (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        # 1) Niveaux demandés
        levels_def = [
            "6e",
            "5e",
            "4e MC",
            "4e ML",
            "3e MC",
            "3e ML",
            "Seconde",
        ]

        created_levels = []
        for name in levels_def:
            lvl, created = Level.objects.get_or_create(name=name)
            created_levels.append((lvl, created))

        # 2) Classes : pour les niveaux de collège on crée une classe unique (nom = niveau)
        #    Pour la Seconde on crée des sections A/B/C/D
        created_classes = []
        for lvl_name in ["6e", "5e", "4e MC", "4e ML", "3e MC", "3e ML"]:
            lvl = Level.objects.get(name=lvl_name)
            cls, created = SchoolClass.objects.get_or_create(name=lvl_name, level=lvl)
            created_classes.append((cls, created))

        # Seconde A/B/C/D
        seconde_lvl = Level.objects.get(name="Seconde")
        for suffix in ("A", "B", "C", "D"):
            cls_name = f"Seconde {suffix}"
            cls, created = SchoolClass.objects.get_or_create(name=cls_name, level=seconde_lvl)
            created_classes.append((cls, created))

        # 3) Matières (subjects)
        subjects = [
            "Maths",
            "Français",
            "Hist-Géo",
            "SVT",
            "Physique-Chimie",
            "EPS",
            "Philo",
            "Eco Fam",
            "Economie",
            "Allemand",
            "Espagnol",
            "Anglais",
            "Musique",
            "Autres",
        ]

        created_subjects = []
        for s in subjects:
            subj, created = Subject.objects.get_or_create(name=s)
            created_subjects.append((subj, created))

        # 4) Liaisons classe ↔ matière (ClassSubject)
        #    Règles simples appliquées :
        #      - Matières de base (Maths, Français, Hist-Géo, SVT, Physique-Chimie, EPS, Anglais) -> toutes les classes
        #      - Allemand / Espagnol : présentes partout, mais marquées facultatives pour 6e et 5e
        #      - Philo, Eco Fam, Economie : réservées à la Seconde
        #      - Musique et Autres : ajoutées partout en facultatif

        base_names = ["Maths", "Français", "Hist-Géo", "SVT", "Physique-Chimie", "EPS", "Anglais"]
        lang_names = ["Allemand", "Espagnol"]
        seconde_only = ["Philo", "Eco Fam", "Economie"]
        optional_everywhere = ["Musique", "Autres"]

        report = {
            "class_subjects_created": 0,
            "class_subjects_skipped": 0,
        }

        all_classes = SchoolClass.objects.select_related('level').all()
        for cls in all_classes:
            lvl_name = cls.level.name

            # base subjects
            for name in base_names:
                subj = Subject.objects.get(name=name)
                cs, created = ClassSubject.objects.get_or_create(
                    school_class=cls,
                    subject=subj,
                    defaults={
                        "coefficient": 1,
                        "is_optional": False,
                        "hours_per_week": 3,
                    },
                )
                if created:
                    report["class_subjects_created"] += 1
                else:
                    report["class_subjects_skipped"] += 1

            # languages
            for name in lang_names:
                subj = Subject.objects.get(name=name)
                is_optional = lvl_name in ("6e", "5e")
                cs, created = ClassSubject.objects.get_or_create(
                    school_class=cls,
                    subject=subj,
                    defaults={
                        "coefficient": 1,
                        "is_optional": is_optional,
                        "hours_per_week": 2,
                    },
                )
                if created:
                    report["class_subjects_created"] += 1
                else:
                    report["class_subjects_skipped"] += 1

            # seconde-only
            if lvl_name == "Seconde" or lvl_name.startswith("Seconde "):
                for name in seconde_only:
                    subj = Subject.objects.get(name=name)
                    cs, created = ClassSubject.objects.get_or_create(
                        school_class=cls,
                        subject=subj,
                        defaults={
                            "coefficient": 1,
                            "is_optional": False,
                            "hours_per_week": 2,
                        },
                    )
                    if created:
                        report["class_subjects_created"] += 1
                    else:
                        report["class_subjects_skipped"] += 1

            # optional everywhere
            for name in optional_everywhere:
                subj = Subject.objects.get(name=name)
                cs, created = ClassSubject.objects.get_or_create(
                    school_class=cls,
                    subject=subj,
                    defaults={
                        "coefficient": 0,
                        "is_optional": True,
                        "hours_per_week": 1,
                    },
                )
                if created:
                    report["class_subjects_created"] += 1
                else:
                    report["class_subjects_skipped"] += 1

        # Résumé
        self.stdout.write(self.style.SUCCESS("--- Populate academics finished ---"))
        self.stdout.write(f"Levels created or existing: {len(created_levels)}")
        self.stdout.write(f"Classes created or existing: {len(created_classes)}")
        self.stdout.write(f"Subjects created or existing: {len(created_subjects)}")
        self.stdout.write(f"ClassSubjects created: {report['class_subjects_created']}")
        self.stdout.write(f"ClassSubjects skipped (already existed): {report['class_subjects_skipped']}")
        self.stdout.write(self.style.SUCCESS("Run completed."))

# End of file
