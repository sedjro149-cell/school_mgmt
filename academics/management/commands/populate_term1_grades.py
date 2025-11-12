# academics/management/commands/populate_term1_grades.py
from decimal import Decimal, ROUND_HALF_UP
import random
import json
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from django.db.models import Prefetch

from academics.models import Grade, ClassSubject, SchoolClass
from core.models import Student

TERM = "T1"


def _quantize_quarter(value: Decimal) -> Decimal:
    """Round to 2 decimals (DecimalField) and ensure .00/.25/.50/.75 possible values."""
    # We expect value to already be Decimal
    # Round to nearest 0.25 then quantize to 2 dec places
    quarters = (value * 4).quantize(Decimal('1'), rounding=ROUND_HALF_UP) / Decimal(4)
    return Decimal(quarters).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Command(BaseCommand):
    help = "Populate DB with synthetic Grade entries for TERM='T1' (first trimester)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Do not persist; only simulate and report.")
        parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility.")
        parser.add_argument("--min-score", type=float, default=5.0, help="Minimum generated score (inclusive).")
        parser.add_argument("--max-score", type=float, default=20.0, help="Maximum generated score (inclusive).")
        parser.add_argument("--quarters-prob", type=float, default=0.2,
                            help="Probability a generated note will include a quarter fraction (0.25/0.50/0.75). Default 0.2 (1/5).")
        parser.add_argument("--class-ids", help="Comma-separated list of SchoolClass ids to limit population (optional).")
        parser.add_argument("--student-ids", help="Comma-separated list of Student ids to limit population (optional).")
        parser.add_argument("--dump", help="Optional path to dump full JSON report.")
        parser.add_argument("--verbose", action="store_true", help="Verbose output while running.")

    def handle(self, *args, **options):
        if options["seed"] is not None:
            random.seed(options["seed"])

        dry_run = options["dry_run"]
        min_score = Decimal(str(options["min_score"]))
        max_score = Decimal(str(options["max_score"]))
        quarters_prob = float(options["quarters_prob"])
        verbose = options["verbose"]

        # Build class filter
        class_ids = None
        if options.get("class_ids"):
            try:
                class_ids = [int(x.strip()) for x in options["class_ids"].split(",") if x.strip()]
            except Exception:
                raise CommandError("Invalid --class-ids format; expected comma-separated ints.")

        student_ids = None
        if options.get("student_ids"):
            try:
                student_ids = [x.strip() for x in options["student_ids"].split(",") if x.strip()]
            except Exception:
                raise CommandError("Invalid --student-ids format; expected comma-separated ids.")

        # Collect target classes
        classes_qs = SchoolClass.objects.all()
        if class_ids:
            classes_qs = classes_qs.filter(id__in=class_ids)

        classes = list(classes_qs)
        if not classes:
            raise CommandError("No classes found to populate (check --class-ids).")

        # Prepare report counters and details
        report = {
            "timestamp": datetime.now().isoformat(),
            "term": TERM,
            "classes_processed": 0,
            "students_targeted": 0,
            "grades_created": 0,
            "grades_updated": 0,
            "skipped_no_class_for_student": 0,
            "errors": 0,
            "details_sample": []
        }

        # Pre-fetch students per class efficiently
        for cls in classes:
            if verbose:
                self.stdout.write(f"> Classe: {cls} (id={cls.id})")

            # ClassSubjects for this class (subjects taught in the class)
            cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
            class_subjects = list(cs_qs)
            if not class_subjects:
                if verbose:
                    self.stdout.write(f"  - Aucun ClassSubject défini pour la classe {cls}. On saute.")
                continue

            # Students in this class (apply student_ids filter if provided)
            students_qs = Student.objects.filter(school_class=cls)
            if student_ids:
                # student_ids may be numeric or slug; try to match by id field (string) or pk
                # We accept raw values and filter by id__in
                students_qs = students_qs.filter(id__in=student_ids)
            students = list(students_qs)
            if not students:
                if verbose:
                    self.stdout.write(f"  - Aucun élève trouvé pour la classe {cls}.")
                continue

            report["classes_processed"] += 1
            report["students_targeted"] += len(students)

            # For each student and each class_subject, create/update Grade for TERM
            # Use transaction per class for safety
            if dry_run:
                # Do not create transaction in dry-run, we'll simulate and count
                tx_context = dummy_context = lambda: (yield)  # not used: handle separately
            else:
                tx_context = transaction.atomic

            # iterate students
            for student in students:
                # defensive: skip students without school_class (shouldn't happen here)
                if getattr(student, "school_class", None) is None:
                    report["skipped_no_class_for_student"] += 1
                    if verbose:
                        self.stdout.write(f"    - Élève {student.id} n'a pas de school_class; skip.")
                    continue

                for cs in class_subjects:
                    subject = cs.subject
                    # generate five marks (3 interros, 2 devoirs)
                    def gen_mark():
                        # choose integer base
                        base = Decimal(random.randint(int(min_score), int(max_score)))
                        # sometimes we want to include fraction
                        if random.random() < quarters_prob:
                            frac = random.choice([Decimal('0.25'), Decimal('0.50'), Decimal('0.75')])
                            val = base + frac
                        else:
                            val = base
                        # ensure within min/max
                        if val < min_score:
                            val = min_score
                        if val > max_score:
                            val = max_score
                        # quantize to 2 decimals
                        return _quantize_quarter(Decimal(val))

                    i1 = gen_mark()
                    i2 = gen_mark()
                    i3 = gen_mark()
                    d1 = gen_mark()
                    d2 = gen_mark()

                    # Build defaults payload
                    defaults = {
                        "interrogation1": i1,
                        "interrogation2": i2,
                        "interrogation3": i3,
                        "devoir1": d1,
                        "devoir2": d2,
                    }

                    try:
                        if dry_run:
                            # simulate update_or_create: check if exists
                            exists = Grade.objects.filter(student=student, subject=subject, term=TERM).exists()
                            if exists:
                                report["grades_updated"] += 1
                                sample_action = "would_update"
                            else:
                                report["grades_created"] += 1
                                sample_action = "would_create"
                            if len(report["details_sample"]) < 50:
                                report["details_sample"].append({
                                    "student_id": str(student.id),
                                    "student_repr": str(student),
                                    "class_id": cls.id,
                                    "subject_id": subject.id,
                                    "subject_name": subject.name,
                                    "action": sample_action,
                                    "interrogation1": str(i1),
                                    "devoir1": str(d1),
                                })
                        else:
                            with transaction.atomic():
                                g, created_flag = Grade.objects.update_or_create(
                                    student=student,
                                    subject=subject,
                                    term=TERM,
                                    defaults=defaults
                                )
                                # save to trigger calculate_averages() in save()
                                g.save()
                                if created_flag:
                                    report["grades_created"] += 1
                                    action = "created"
                                else:
                                    report["grades_updated"] += 1
                                    action = "updated"
                                if len(report["details_sample"]) < 50:
                                    report["details_sample"].append({
                                        "student_id": str(student.id),
                                        "student_repr": str(student),
                                        "class_id": cls.id,
                                        "subject_id": subject.id,
                                        "subject_name": subject.name,
                                        "action": action,
                                        "interrogation1": str(i1),
                                        "devoir1": str(d1),
                                    })
                    except IntegrityError as e:
                        report["errors"] += 1
                        if verbose:
                            self.stdout.write(f"    [ERREUR] IntegrityError pour student={student.id} subject={subject.id}: {e}")
                    except Exception as e:
                        report["errors"] += 1
                        if verbose:
                            self.stdout.write(f"    [ERREUR] Exception pour student={student.id} subject={subject.id}: {e}")

        # end classes loop

        # final report printing
        self.stdout.write(self.style.SUCCESS("Population TERM='T1' terminé (simulation=%s)." % ("YES" if dry_run else "NO")))
        self.stdout.write(json.dumps({
            "timestamp": report["timestamp"],
            "term": report["term"],
            "classes_processed": report["classes_processed"],
            "students_targeted": report["students_targeted"],
            "grades_created": report["grades_created"],
            "grades_updated": report["grades_updated"],
            "skipped_no_class_for_student": report["skipped_no_class_for_student"],
            "errors": report["errors"],
            "details_sample_count": len(report["details_sample"]),
        }, indent=2, ensure_ascii=False))

        if options.get("dump"):
            try:
                with open(options["dump"], "w", encoding="utf-8") as f:
                    json.dump(report, f, default=str, indent=2, ensure_ascii=False)
                self.stdout.write(self.style.SUCCESS(f"Dumped report to {options['dump']}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Could not write dump file: {e}"))

        return
