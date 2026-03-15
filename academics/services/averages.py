# academics/services/averages.py
import logging
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

DEFAULT_NB_INTERROS = 3
DEFAULT_NB_DEVOIRS  = 2

_INTERRO_FIELDS = ["interrogation1", "interrogation2", "interrogation3"]
_DEVOIR_FIELDS  = ["devoir1", "devoir2"]


def _quant(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_averages_for_term(term_status) -> None:
    """
    Calcule et persiste les moyennes pour tous les Grade d'une classe × trimestre.
    Appelée UNIQUEMENT lors du TermStatus.lock().

    ─── Formule interros ───────────────────────────────────────────────────────

      nb_interros = 3  →  (i1_ou_0 + i2_ou_0 + i3_ou_0) / 3
                          diviseur fixe, null vaut 0 (comportement originel)

      nb_interros < 3  →  on prend les nb_interros MEILLEURES valeurs non-null
                          parmi les trois champs d'interrogation disponibles,
                          on les somme et on divise par nb_interros.
                          Si aucune valeur n'est renseignée → avg_interro = 0.

      Exemple : nb_interros=2, i1=8, i2=12, i3=15 (tous renseignés)
                → best 2 = [15, 12]  → avg_interro = (15+12)/2 = 13.5  ✓

    ─── Formule devoirs ────────────────────────────────────────────────────────

      avg_subject = (avg_interro + d1_ou_0 + d2_ou_0) / 3
      Si nb_devoirs < 2, les slots manquants valent 0.

    ─── Formule finale ─────────────────────────────────────────────────────────

      average_coeff = avg_subject × ClassSubject.coefficient
    """
    from academics.models import Grade, TermSubjectConfig, ClassSubject

    grades = list(
        Grade.objects.filter(
            student__school_class=term_status.school_class,
            term=term_status.term,
        ).select_related("student__school_class", "subject")
    )

    if not grades:
        logger.info(
            "compute_averages_for_term: aucun grade pour %s / %s",
            term_status.school_class, term_status.term,
        )
        return

    configs = {
        c.subject_id: c
        for c in TermSubjectConfig.objects.filter(
            school_class=term_status.school_class,
            term=term_status.term,
        )
    }

    coefficients = {
        cs.subject_id: Decimal(str(cs.coefficient))
        for cs in ClassSubject.objects.filter(school_class=term_status.school_class)
    }

    updates = []

    for grade in grades:
        config = configs.get(grade.subject_id)
        nb_i   = config.nb_interros if config else DEFAULT_NB_INTERROS
        nb_d   = config.nb_devoirs  if config else DEFAULT_NB_DEVOIRS
        coeff  = coefficients.get(grade.subject_id, Decimal("1"))

        # ── Calcul avg_interro ───────────────────────────────────────────────
        if nb_i == 3:
            # Comportement originel : somme des 3 champs (null = 0), diviseur fixe 3
            interro_sum = Decimal("0")
            for field in _INTERRO_FIELDS:
                val = getattr(grade, field, None)
                interro_sum += Decimal(str(val)) if val is not None else Decimal("0")
            avg_interro = _quant(interro_sum / Decimal("3"))

        else:
            # nb_i < 3 : prendre les nb_i MEILLEURES valeurs non-null disponibles
            available = sorted(
                [
                    Decimal(str(getattr(grade, f)))
                    for f in _INTERRO_FIELDS
                    if getattr(grade, f, None) is not None
                ],
                reverse=True,
            )
            best = available[:nb_i]   # les nb_i meilleures

            if best:
                avg_interro = _quant(sum(best) / Decimal(str(len(best))))
            else:
                avg_interro = Decimal("0")

        # ── Calcul devoirs ───────────────────────────────────────────────────
        d_vals = []
        for field in _DEVOIR_FIELDS[:nb_d]:
            val = getattr(grade, field, None)
            d_vals.append(Decimal(str(val)) if val is not None else Decimal("0"))
        while len(d_vals) < 2:
            d_vals.append(Decimal("0"))

        # ── Moyenne générale (diviseur 3 fixe) ───────────────────────────────
        avg_subject   = _quant((avg_interro + d_vals[0] + d_vals[1]) / Decimal("3"))
        average_coeff = _quant(avg_subject * coeff)

        updates.append((grade.pk, avg_interro, avg_subject, average_coeff))

    for pk, avg_i, avg_s, avg_c in updates:
        Grade.objects.filter(pk=pk).update(
            average_interro=avg_i,
            average_subject=avg_s,
            average_coeff=avg_c,
        )

    logger.info(
        "compute_averages_for_term: %d grades calculés — %s / %s",
        len(updates), term_status.school_class, term_status.term,
    )


def reset_averages_for_term(term_status) -> None:
    """
    Annule les moyennes calculées lors d'un unlock (retour en DRAFT).
    Les notes brutes ne sont pas touchées.
    """
    from academics.models import Grade

    count = Grade.objects.filter(
        student__school_class=term_status.school_class,
        term=term_status.term,
    ).update(
        average_interro=None,
        average_subject=None,
        average_coeff=None,
    )

    logger.info(
        "reset_averages_for_term: %d grades remis à null — %s / %s",
        count, term_status.school_class, term_status.term,
    )