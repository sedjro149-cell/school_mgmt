# academics/management/commands/populate_grades.py
"""
Commande de peuplement synthétique des notes pour un trimestre donné.

Usage :
    py manage.py populate_grades --term T1
    py manage.py populate_grades --term T2 --class-ids 3,7
    py manage.py populate_grades --term T1 --dry-run --seed 42
    py manage.py populate_grades --term T3 --dump report_t3.json

Profils d'élèves (distribués aléatoirement à chaque lancement) :
    faible  (25 %) — notes centrées entre  2 et  9  — beaucoup ratent la moyenne
    moyen   (50 %) — notes centrées entre  8 et 14  — peuvent passer ou pas
    fort    (25 %) — notes centrées entre 13 et 20  — frôlent 15-18

Les profils sont assignés PAR ÉLÈVE, pas par note : un élève fort est fort dans
toutes les matières, mais avec de la variance (±2 pts) pour rester réaliste.
"""

import json
import random
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from academics.models import (
    ClassSubject,
    Grade,
    SchoolClass,
    TermStatus,
)
from core.models import Student

# ─── Profils d'élèves ────────────────────────────────────────────────────────
# Chaque profil : (poids, centre_bas, centre_haut, écart_max)
# La note générée = clamp(centre + uniform(-écart, +écart), 0, 20)
PROFILES = [
    ("faible", 0.25,  2.0,  9.0, 3.0),   # entre 2 et 9, bruits de ±3
    ("moyen",  0.50,  8.0, 14.0, 2.5),   # entre 8 et 14, bruits de ±2.5
    ("fort",   0.25, 13.0, 20.0, 2.0),   # entre 13 et 20, bruits de ±2
]

PROFILE_NAMES  = [p[0] for p in PROFILES]
PROFILE_WEIGHTS = [p[1] for p in PROFILES]


def _assign_profile() -> tuple:
    """Tire un profil au sort selon les poids définis."""
    chosen = random.choices(PROFILES, weights=PROFILE_WEIGHTS, k=1)[0]
    return chosen  # (nom, poids, bas, haut, écart)


def _gen_mark(profile: tuple) -> Decimal:
    """
    Génère une note réaliste pour un profil donné.
    - Centre aléatoire dans [bas, haut]
    - Bruit gaussien d'écart-type = écart/2
    - Arrondi au 0.25 le plus proche, clampé [0, 20]
    """
    _, _, bas, haut, ecart = profile
    centre = random.uniform(bas, haut)
    bruit  = random.gauss(0, ecart / 2)
    val    = centre + bruit
    val    = max(0.0, min(20.0, val))

    # Arrondi au 0.25 le plus proche
    quarters = round(val * 4) / 4
    quarters = max(0.0, min(20.0, quarters))
    return Decimal(str(quarters)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ─── Commande ────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Peuple la DB avec des notes synthétiques réalistes pour un trimestre donné."

    def add_arguments(self, parser):
        parser.add_argument(
            "--term", required=True, choices=["T1", "T2", "T3"],
            help="Trimestre à peupler (T1, T2 ou T3).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Simule sans rien écrire en base.",
        )
        parser.add_argument(
            "--seed", type=int, default=None,
            help="Graine aléatoire pour reproductibilité.",
        )
        parser.add_argument(
            "--class-ids",
            help="IDs de classes séparés par des virgules (ex: 3,7,12).",
        )
        parser.add_argument(
            "--student-ids",
            help="IDs d'élèves séparés par des virgules.",
        )
        parser.add_argument(
            "--dump",
            help="Chemin vers un fichier JSON pour le rapport complet.",
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Affichage détaillé pendant l'exécution.",
        )
        parser.add_argument(
            "--force-locked", action="store_true",
            help="Écrire même si le trimestre est LOCKED (dangereux — réservé aux tests).",
        )

    def handle(self, *args, **options):
        if options["seed"] is not None:
            random.seed(options["seed"])

        term        = options["term"]
        dry_run     = options["dry_run"]
        verbose     = options["verbose"]
        force       = options["force_locked"]

        # ── Filtres optionnels ────────────────────────────────────────────────
        class_ids = None
        if options.get("class_ids"):
            try:
                class_ids = [int(x.strip()) for x in options["class_ids"].split(",") if x.strip()]
            except ValueError:
                raise CommandError("--class-ids : liste d'entiers séparés par des virgules attendue.")

        student_ids = None
        if options.get("student_ids"):
            student_ids = [x.strip() for x in options["student_ids"].split(",") if x.strip()]

        # ── Validation du terme contre SchoolYearConfig ─────────────────────
        from academics.models import SchoolYearConfig
        year_config = SchoolYearConfig.get_solo()
        valid_terms = [f"T{n}" for n in range(1, year_config.nb_terms + 1)]
        if term not in valid_terms:
            raise CommandError(
                f"Le trimestre '{term}' n'existe pas dans la configuration actuelle : "
                f"l'école a {year_config.nb_terms} trimestre(s) "
                f"({', '.join(valid_terms)}). "
                f"Modifiez SchoolYearConfig via l'admin ou TermManager si vous souhaitez activer T3."
            )

        # ── Récupération des classes ──────────────────────────────────────────
        classes_qs = SchoolClass.objects.all()
        if class_ids:
            classes_qs = classes_qs.filter(id__in=class_ids)

        classes = list(classes_qs)
        if not classes:
            raise CommandError("Aucune classe trouvée (vérifiez --class-ids).")

        # ── Vérification TermStatus globale ──────────────────────────────────
        # On avertit (ou bloque) si le trimestre est déjà verrouillé.
        locked_classes = []
        for cls in classes:
            ts = TermStatus.objects.filter(school_class=cls, term=term).first()
            if ts and not ts.is_editable:
                locked_classes.append(f"{cls} [statut: {ts.get_status_display()}]")

        if locked_classes:
            msg = (
                f"Les classes suivantes ont le trimestre {term} verrouillé :\n"
                + "\n".join(f"  • {c}" for c in locked_classes)
            )
            if force:
                self.stdout.write(self.style.WARNING(f"[ATTENTION] {msg}\n--force-locked activé, on continue."))
            else:
                raise CommandError(
                    f"{msg}\n\n"
                    "Déverrouillez le trimestre via l'admin (TermManager) avant de repeupler, "
                    "ou utilisez --force-locked pour ignorer cette vérification (tests uniquement)."
                )

        # ── Rapport ──────────────────────────────────────────────────────────
        report = {
            "timestamp":                  datetime.now().isoformat(),
            "term":                       term,
            "dry_run":                    dry_run,
            "classes_processed":          0,
            "students_targeted":          0,
            "grades_created":             0,
            "grades_updated":             0,
            "skipped_no_subjects":        0,
            "skipped_no_students":        0,
            "errors":                     0,
            "profile_distribution":       {"faible": 0, "moyen": 0, "fort": 0},
            "details_sample":             [],
        }

        # ── Boucle principale ─────────────────────────────────────────────────
        for cls in classes:
            if verbose:
                self.stdout.write(f"\n▶ Classe : {cls} (id={cls.id})")

            class_subjects = list(
                ClassSubject.objects.filter(school_class=cls).select_related("subject")
            )
            if not class_subjects:
                if verbose:
                    self.stdout.write(f"  ⚠ Aucun ClassSubject pour {cls} — ignorée.")
                report["skipped_no_subjects"] += 1
                continue

            students_qs = Student.objects.filter(school_class=cls)
            if student_ids:
                students_qs = students_qs.filter(id__in=student_ids)
            students = list(students_qs)

            if not students:
                if verbose:
                    self.stdout.write(f"  ⚠ Aucun élève dans {cls} — ignorée.")
                report["skipped_no_students"] += 1
                continue

            report["classes_processed"]  += 1
            report["students_targeted"]  += len(students)

            # Assigner un profil stable par élève pour ce peuplement
            # (même profil pour toutes les matières d'un même élève)
            student_profiles = {s.id: _assign_profile() for s in students}

            for student in students:
                profile = student_profiles[student.id]
                profile_name = profile[0]
                report["profile_distribution"][profile_name] += 1

                if verbose:
                    self.stdout.write(
                        f"  Élève {student.id} — profil : {profile_name}"
                    )

                for cs in class_subjects:
                    subject = cs.subject

                    # Générer les 5 champs de notes
                    # (les champs excédentaires par rapport à TermSubjectConfig
                    # sont simplement ignorés au calcul lors du lock — cf. services/averages.py)
                    i1 = _gen_mark(profile)
                    i2 = _gen_mark(profile)
                    i3 = _gen_mark(profile)
                    d1 = _gen_mark(profile)
                    d2 = _gen_mark(profile)

                    defaults = {
                        "interrogation1": i1,
                        "interrogation2": i2,
                        "interrogation3": i3,
                        "devoir1":        d1,
                        "devoir2":        d2,
                    }

                    try:
                        if dry_run:
                            exists = Grade.objects.filter(
                                student=student, subject=subject, term=term
                            ).exists()
                            if exists:
                                report["grades_updated"] += 1
                                action = "would_update"
                            else:
                                report["grades_created"] += 1
                                action = "would_create"
                        else:
                            with transaction.atomic():
                                g, created_flag = Grade.objects.update_or_create(
                                    student=student,
                                    subject=subject,
                                    term=term,
                                    defaults=defaults,
                                )
                                # Pas de g.save() supplémentaire :
                                # update_or_create a déjà sauvegardé.
                                # Les moyennes sont calculées uniquement au lock.
                                if created_flag:
                                    report["grades_created"] += 1
                                    action = "created"
                                else:
                                    report["grades_updated"] += 1
                                    action = "updated"

                        if len(report["details_sample"]) < 50:
                            report["details_sample"].append({
                                "student_id":   str(student.id),
                                "class":        str(cls),
                                "subject":      subject.name,
                                "profil":       profile_name,
                                "action":       action,
                                "i1": str(i1), "i2": str(i2), "i3": str(i3),
                                "d1": str(d1), "d2": str(d2),
                            })

                    except IntegrityError as e:
                        report["errors"] += 1
                        if verbose:
                            self.stdout.write(
                                self.style.ERROR(
                                    f"    [ERREUR IntegrityError] "
                                    f"student={student.id} subject={subject.id}: {e}"
                                )
                            )
                    except Exception as e:
                        report["errors"] += 1
                        if verbose:
                            self.stdout.write(
                                self.style.ERROR(
                                    f"    [ERREUR] student={student.id} subject={subject.id}: {e}"
                                )
                            )

        # ── Résumé ────────────────────────────────────────────────────────────
        summary = {
            "term":              report["term"],
            "dry_run":           report["dry_run"],
            "classes_processed": report["classes_processed"],
            "students_targeted": report["students_targeted"],
            "grades_created":    report["grades_created"],
            "grades_updated":    report["grades_updated"],
            "errors":            report["errors"],
            "profile_distribution": report["profile_distribution"],
        }

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Peuplement {term} terminé (dry_run={'OUI' if dry_run else 'NON'})."
            )
        )
        self.stdout.write(json.dumps(summary, indent=2, ensure_ascii=False))

        if options.get("dump"):
            try:
                with open(options["dump"], "w", encoding="utf-8") as f:
                    json.dump(report, f, default=str, indent=2, ensure_ascii=False)
                self.stdout.write(self.style.SUCCESS(f"Rapport complet écrit dans {options['dump']}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Impossible d'écrire le fichier dump : {e}"))