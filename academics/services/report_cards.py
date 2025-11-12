# academics/services/report_cards.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Tuple, Optional, Iterable
from collections import defaultdict

from academics.models import Grade, ClassSubject  # ajuste si nécessaire


@dataclass
class SubjectLine:
    subject_id: int
    subject: str
    coefficient: Decimal
    average_subject: Optional[Decimal]
    average_coeff: Decimal


@dataclass
class ReportCardItem:
    student_id: int
    student_str: str
    term: str
    subjects: List[SubjectLine]
    average: Optional[Decimal]
    class_id: Optional[int] = None
    class_name: Optional[str] = None
    rank: Optional[int] = None
    best_average: Optional[Decimal] = None
    worst_average: Optional[Decimal] = None


def _quant(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_report_cards_from_grades(
    grades_iterable: Iterable[Grade],
    include_missing_subjects: bool = False,
    full_weighting: bool = True,
) -> List[Dict]:
    """
    Prend un iterable / queryset de Grade (déjà filtré par la view)
    et renvoie une liste d'objets dict avec :
    {
      "student": Student instance,
      "term": str,
      "grades": [Grade instances],
      "term_average": float | None,
      "class_id": int | None,
      "class_name": str | None,
      "rank": int | None,
      "best_average": float | None,
      "worst_average": float | None,
    }
    - full_weighting : active le calcul de la moyenne pondérée selon les coefficients.
    """
    grades = list(grades_iterable)
    if not grades:
        return []

    # Charger les ClassSubject pour les classes impliquées
    class_ids = {g.student.school_class_id for g in grades if getattr(g.student, "school_class_id", None)}
    class_subj_map: Dict[int, Dict[int, Tuple[str, Decimal]]] = {}
    class_total_coeffs: Dict[int, Decimal] = {}

    if class_ids:
        cs_qs = ClassSubject.objects.filter(school_class_id__in=class_ids).select_related("subject")
        for cs in cs_qs:
            cid = cs.school_class_id
            if cid not in class_subj_map:
                class_subj_map[cid] = {}
                class_total_coeffs[cid] = Decimal("0")
            coeff_dec = Decimal(str(cs.coefficient))
            class_subj_map[cid][cs.subject_id] = (cs.subject.name, coeff_dec)
            class_total_coeffs[cid] += coeff_dec

    # Grouper par (student_id, term)
    grouped: Dict[Tuple[int, str], List[Grade]] = {}
    for g in grades:
        key = (g.student_id, g.term)
        grouped.setdefault(key, []).append(g)

    items: List[Dict] = []
    for (student_id, term), g_list in grouped.items():
        student = g_list[0].student
        class_id = getattr(student, "school_class_id", None)
        class_name = getattr(getattr(student, "school_class", None), "name", None)

        # Construire les lignes matières
        subject_lines: Dict[int, SubjectLine] = {}
        for g in g_list:
            if class_id and class_id in class_subj_map and g.subject_id in class_subj_map[class_id]:
                subj_name, coeff = class_subj_map[class_id][g.subject_id]
            else:
                subj_name = getattr(g.subject, "name", str(getattr(g.subject, "id", "")))
                coeff = Decimal("1")

            avg_subject = Decimal(str(g.average_subject)) if g.average_subject is not None else None
            avg_coeff = Decimal("0")
            if avg_subject is not None:
                avg_coeff = _quant(avg_subject * coeff)

            subject_lines[g.subject_id] = SubjectLine(
                subject_id=g.subject_id,
                subject=subj_name,
                coefficient=Decimal(str(coeff)),
                average_subject=(None if avg_subject is None else _quant(avg_subject)),
                average_coeff=avg_coeff,
            )

        # Inclure matières manquantes si demandé
        if include_missing_subjects and class_id and class_id in class_subj_map:
            for subj_id, (subj_name, coeff) in class_subj_map[class_id].items():
                if subj_id not in subject_lines:
                    subject_lines[subj_id] = SubjectLine(
                        subject_id=subj_id,
                        subject=subj_name,
                        coefficient=Decimal(str(coeff)),
                        average_subject=None,
                        average_coeff=Decimal("0"),
                    )

        # --- CALCUL DE LA MOYENNE PONDÉRÉE ---
        weighted_total = Decimal("0")
        total_coeffs = Decimal("0")
        for sl in subject_lines.values():
            if sl.average_subject is not None:
                weighted_total += sl.average_subject * sl.coefficient
                total_coeffs += sl.coefficient

        term_avg = None
        if total_coeffs > 0:
            term_avg_dec = _quant(weighted_total / total_coeffs)
            term_avg = float(term_avg_dec)  # JSON-friendly

        items.append({
            "student": student,
            "term": term,
            "grades": g_list,
            "term_average": term_avg,
            "class_id": class_id,
            "class_name": class_name,
            "rank": None,
            "best_average": None,
            "worst_average": None,
        })

    # Calculer rangs + best/worst par (class_id, term)
    groups = defaultdict(list)
    for it in items:
        groups[(it["class_id"], it["term"])].append(it)

    for (_class_id, _term), group in groups.items():
        with_avg = [it for it in group if it["term_average"] is not None]
        if not with_avg:
            continue

        # Tri décroissant par moyenne, stabilisé par nom
        with_avg.sort(
            key=lambda x: (x["term_average"], f"{x['student'].user.last_name} {x['student'].user.first_name}".lower()),
            reverse=True
        )

        best = with_avg[0]["term_average"]
        worst = with_avg[-1]["term_average"]

        # Classement de compétition (1,1,3,4...)
        last_avg = None
        rank = 0
        seen = 0
        for it in with_avg:
            seen += 1
            if last_avg is None or it["term_average"] != last_avg:
                rank = seen
                last_avg = it["term_average"]
            it["rank"] = rank

        # Appliquer best/worst à tout le groupe
        for it in group:
            it["best_average"] = best
            it["worst_average"] = worst

    # Tri stable final (optionnel)
    items.sort(key=lambda it: (str(it["student"]).lower(), it["term"]))
    return items


# Helper wrapper utile si tu veux appeler par "user" (ancienne signature)
def compute_report_cards_for_user(user, term=None, include_missing_subjects=False, full_weighting=True):
    # 1) Déterminer les élèves à inclure pour le calcul
    if hasattr(user, "student") and user.student.school_class_id:
        ranking_students_qs = type(user.student).objects.filter(school_class_id=user.student.school_class_id)
    elif hasattr(user, "parent"):
        classes = user.parent.students.values_list('school_class_id', flat=True).distinct()
        ranking_students_qs = type(user.student).objects.filter(school_class_id__in=classes)
    else:
        ranking_students_qs = None  # admin

    grades_qs = Grade.objects.select_related("student", "student__school_class", "subject")
    if ranking_students_qs is not None:
        grades_qs = grades_qs.filter(student__in=ranking_students_qs)
    if term:
        grades_qs = grades_qs.filter(term=term)

    all_items = compute_report_cards_from_grades(grades_qs, include_missing_subjects, full_weighting)

    # 2) Filtrer pour ne garder que l'élève connecté ou les enfants du parent
    if hasattr(user, "student"):
        requested_ids = [user.student.pk]
        all_items = [it for it in all_items if it["student"].pk in requested_ids]
    elif hasattr(user, "parent"):
        requested_ids = list(user.parent.students.values_list("pk", flat=True))
        all_items = [it for it in all_items if it["student"].pk in requested_ids]

    return all_items
