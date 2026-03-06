# ─────────────────────────────────────────────────────────────────────────────
#  academics/services/timetable_batch.py
#
#  Service de validation et d'application des modifications manuelles
#  d'emplois du temps (batch validate / batch apply).
#
#  ARCHITECTURE :
#    La logique métier est ici (pure functions).
#    Les vues (views.py) ne font qu'appeler ces fonctions.
#
#  NIVEAUX DE RÉSULTAT :
#    ERREUR  (hard) → conflit horaire prof ou classe. Bloque le save.
#    ALERTE  (soft) → C2 (même matière 2x/jour) ou C3 (jours consécutifs).
#                     Ne bloque pas si l'admin force avec force=True.
# ─────────────────────────────────────────────────────────────────────────────
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils.dateparse import parse_time

from academics.models import ClassScheduleEntry, TimeSlot

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers bas niveau
# ─────────────────────────────────────────────────────────────────────────────

def _to_min(t) -> int:
    """time object → minutes depuis minuit."""
    return t.hour * 60 + t.minute


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _parse_time(val) -> Optional[Any]:
    if val is None:
        return None
    from datetime import time
    if isinstance(val, time):
        return val
    parsed = parse_time(str(val))
    return parsed


def _load_timeslots_indexed() -> Dict[int, Dict]:
    """
    Charge tous les TimeSlots. Retourne un dict idx → slot_dict.
    L'index est la position dans le queryset trié (même logique que le générateur).
    """
    slots = {}
    for idx, s in enumerate(TimeSlot.objects.all().order_by("day", "start_time")):
        start_min = _to_min(s.start_time)
        end_min = _to_min(s.end_time)
        if end_min <= start_min:
            continue
        slots[idx] = {
            "idx": idx,
            "db_obj": s,
            "weekday": s.day,
            "start_min": start_min,
            "end_min": end_min,
            "dur": end_min - start_min,
            "start_time": s.start_time,
            "end_time": s.end_time,
        }
    return slots


# ─────────────────────────────────────────────────────────────────────────────
#  Représentation mémoire d'une entrée simulée
# ─────────────────────────────────────────────────────────────────────────────

def _entry_to_sim(e: ClassScheduleEntry) -> Dict:
    """Convertit un objet Django en dict simulable."""
    start_min = _to_min(e.starts_at) if e.starts_at else None
    end_min = _to_min(e.ends_at) if e.ends_at else None
    return {
        "id": e.id,
        "school_class_id": e.school_class_id,
        "subject_id": e.subject_id,
        "teacher_id": e.teacher_id,
        "weekday": e.weekday,
        "starts_at": e.starts_at,
        "ends_at": e.ends_at,
        "start_min": start_min,
        "end_min": end_min,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Moteur de validation — cœur du système
# ─────────────────────────────────────────────────────────────────────────────

def validate_schedule_state(sim_entries: Dict[int, Dict]) -> Tuple[List, List]:
    """
    Valide un état complet d'emploi du temps (simulation en mémoire).

    Paramètres :
      sim_entries : dict id → sim_dict (produit par _entry_to_sim ou inline)

    Retourne :
      (hard_errors, soft_warnings)

      hard_errors : conflits qui DOIVENT être corrigés avant tout save.
        - teacher_conflict : prof dans deux cours simultanément
        - class_conflict   : classe dans deux cours simultanément

      soft_warnings : violations pédagogiques signalées à l'admin.
        - c2_same_subject_same_day : même matière deux fois le même jour
        - c3_consecutive_days      : même matière sur jours consécutifs
    """
    entries = list(sim_entries.values())

    # Index par (teacher, weekday) et (class, weekday) pour les hard checks
    by_teacher_day: Dict = defaultdict(list)
    by_class_day: Dict = defaultdict(list)
    # Index par (class, subject, weekday) pour C2
    by_class_subj_day: Dict = defaultdict(list)
    # Index par (class, subject) → set of weekdays pour C3
    by_class_subj_days: Dict = defaultdict(set)

    for e in entries:
        if e.get("start_min") is None or e.get("end_min") is None:
            continue
        if e.get("teacher_id"):
            by_teacher_day[(e["teacher_id"], e["weekday"])].append(e)
        by_class_day[(e["school_class_id"], e["weekday"])].append(e)
        if e.get("subject_id"):
            by_class_subj_day[(e["school_class_id"], e["subject_id"], e["weekday"])].append(e)
            by_class_subj_days[(e["school_class_id"], e["subject_id"])].add(e["weekday"])

    def _all_overlap_pairs(lst: List) -> List[Tuple]:
        """
        Trouve TOUTES les paires en conflit — O(n²) mais n ≤ ~15 entrées/groupe.
        Contrairement à une boucle i,i+1, cette version ne rate pas les conflits
        non-adjacents (ex: A chevauche C mais pas B entre eux).
        """
        pairs = []
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i], lst[j]
                if _overlaps(a["start_min"], a["end_min"], b["start_min"], b["end_min"]):
                    pairs.append((a, b))
        return pairs

    hard_errors: List[Dict] = []
    soft_warnings: List[Dict] = []

    # ── Hard : conflits prof ──────────────────────────────────────────────────
    for (tid, day), ents in by_teacher_day.items():
        for a, b in _all_overlap_pairs(ents):
            hard_errors.append({
                "type": "teacher_conflict",
                "teacher_id": tid,
                "weekday": day,
                "entry_ids": [a["id"], b["id"]],
                "class_ids": [a["school_class_id"], b["school_class_id"]],
                "times": [
                    f"{a['starts_at']} → {a['ends_at']}",
                    f"{b['starts_at']} → {b['ends_at']}",
                ],
                "message": (
                    f"Le professeur (id={tid}) a deux cours simultanés le jour {day}."
                ),
            })

    # ── Hard : conflits classe ────────────────────────────────────────────────
    for (cid, day), ents in by_class_day.items():
        for a, b in _all_overlap_pairs(ents):
            hard_errors.append({
                "type": "class_conflict",
                "class_id": cid,
                "weekday": day,
                "entry_ids": [a["id"], b["id"]],
                "subject_ids": [a.get("subject_id"), b.get("subject_id")],
                "times": [
                    f"{a['starts_at']} → {a['ends_at']}",
                    f"{b['starts_at']} → {b['ends_at']}",
                ],
                "message": (
                    f"La classe (id={cid}) a deux cours simultanés le jour {day}."
                ),
            })

    # ── Soft : C2 — même matière deux fois le même jour ──────────────────────
    for (cid, sid, day), ents in by_class_subj_day.items():
        if len(ents) > 1:
            soft_warnings.append({
                "type": "c2_same_subject_same_day",
                "class_id": cid,
                "subject_id": sid,
                "weekday": day,
                "count": len(ents),
                "entry_ids": [e["id"] for e in ents],
                "message": (
                    f"La matière (id={sid}) apparaît {len(ents)}× le jour {day} "
                    f"pour la classe (id={cid}). Recommandation : répartir sur des jours différents."
                ),
            })

    # ── Soft : C3 — même matière sur jours consécutifs ───────────────────────
    seen_c3 = set()
    for (cid, sid), days in by_class_subj_days.items():
        sorted_days = sorted(days)
        for i in range(len(sorted_days) - 1):
            d1, d2 = sorted_days[i], sorted_days[i + 1]
            if d2 - d1 == 1:
                key = (cid, sid, d1, d2)
                if key in seen_c3:
                    continue
                seen_c3.add(key)
                entries_d1 = [e["id"] for e in by_class_subj_day.get((cid, sid, d1), [])]
                entries_d2 = [e["id"] for e in by_class_subj_day.get((cid, sid, d2), [])]
                soft_warnings.append({
                    "type": "c3_consecutive_days",
                    "class_id": cid,
                    "subject_id": sid,
                    "days": [d1, d2],
                    "entry_ids_day1": entries_d1,
                    "entry_ids_day2": entries_d2,
                    "message": (
                        f"La matière (id={sid}) est programmée les jours {d1} et {d2} "
                        f"(consécutifs) pour la classe (id={cid}). "
                        f"Recommandation : intercaler un jour de repos."
                    ),
                })

    return hard_errors, soft_warnings


# ─────────────────────────────────────────────────────────────────────────────
#  Application des opérations
# ─────────────────────────────────────────────────────────────────────────────

def apply_batch_operations(
    operations: List[Dict],
    force: bool = False,
) -> Dict:
    """
    Valide puis applique un batch d'opérations de déplacement d'entrées.

    Chaque opération est un dict avec :
      entry_id          (int, requis)
      target_slot_idx   (int, optionnel) → index dans le queryset TimeSlot ordonné
      OU
      target_weekday    (int) + target_start (HH:MM) + target_end (HH:MM)

    Paramètres :
      force=False → refuse si des soft_warnings existent
      force=True  → accepte les warnings (admin override conscient)

    Retourne un rapport complet :
      valid           : bool
      hard_errors     : liste des conflits bloquants
      soft_warnings   : liste des violations pédagogiques
      preview         : dict entry_id → {from, to}
      applied         : liste des entry_ids modifiés (vide si erreur)
      db_errors       : erreurs survenues pendant le save
    """
    # ── 1. Charger les données nécessaires UNE SEULE FOIS ────────────────────
    slots_by_idx = _load_timeslots_indexed()

    all_db_entries = list(
        ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher").all()
    )
    sim_entries = {e.id: _entry_to_sim(e) for e in all_db_entries}

    # ── 2. Parser et valider les opérations ──────────────────────────────────
    parse_errors = []
    parsed_ops = []   # list of (entry_id, new_weekday, new_start, new_end)
    preview = {}

    for op in operations:
        entry_id = op.get("entry_id")
        if entry_id is None:
            parse_errors.append({"op": op, "error": "entry_id requis."})
            continue

        entry_id = int(entry_id)
        if entry_id not in sim_entries:
            parse_errors.append({"entry_id": entry_id, "error": "Entrée introuvable en DB."})
            continue

        current = sim_entries[entry_id]

        # Résolution de la cible
        target_slot_idx = op.get("target_slot_idx")
        if target_slot_idx is not None:
            target_slot_idx = int(target_slot_idx)
            slot = slots_by_idx.get(target_slot_idx)
            if slot is None:
                parse_errors.append({
                    "entry_id": entry_id,
                    "error": f"target_slot_idx={target_slot_idx} introuvable.",
                })
                continue
            new_weekday = slot["weekday"]
            new_start = slot["start_time"]
            new_end = slot["end_time"]
            new_start_min = slot["start_min"]
            new_end_min = slot["end_min"]
        else:
            tw = op.get("target_weekday")
            ts = op.get("target_start")
            te = op.get("target_end")
            if tw is None or ts is None or te is None:
                parse_errors.append({
                    "entry_id": entry_id,
                    "error": "Fournir target_slot_idx OU (target_weekday + target_start + target_end).",
                })
                continue
            new_weekday = int(tw)
            new_start = _parse_time(ts)
            new_end = _parse_time(te)
            if new_start is None or new_end is None:
                parse_errors.append({
                    "entry_id": entry_id,
                    "error": "Format horaire invalide (attendu HH:MM).",
                })
                continue
            new_start_min = _to_min(new_start)
            new_end_min = _to_min(new_end)
            if new_end_min <= new_start_min:
                parse_errors.append({
                    "entry_id": entry_id,
                    "error": "target_end doit être strictement après target_start.",
                })
                continue

        # Enregistrer le preview
        preview[entry_id] = {
            "from": {
                "weekday": current["weekday"],
                "starts_at": str(current["starts_at"]),
                "ends_at": str(current["ends_at"]),
            },
            "to": {
                "weekday": new_weekday,
                "starts_at": str(new_start),
                "ends_at": str(new_end),
            },
        }

        # Appliquer la modification dans sim_entries (simulation)
        sim_entries[entry_id] = {
            **current,
            "weekday": new_weekday,
            "starts_at": new_start,
            "ends_at": new_end,
            "start_min": new_start_min,
            "end_min": new_end_min,
        }

        parsed_ops.append((entry_id, new_weekday, new_start, new_end))

    if parse_errors:
        return {
            "valid": False,
            "hard_errors": parse_errors,
            "soft_warnings": [],
            "preview": preview,
            "applied": [],
            "db_errors": [],
        }

    # ── 3. Validation complète de l'état simulé ──────────────────────────────
    hard_errors, soft_warnings = validate_schedule_state(sim_entries)

    # ── 4. Décision ──────────────────────────────────────────────────────────
    if hard_errors:
        return {
            "valid": False,
            "hard_errors": hard_errors,
            "soft_warnings": soft_warnings,
            "preview": preview,
            "applied": [],
            "db_errors": [],
            "message": (
                f"{len(hard_errors)} conflit(s) dur(s) détecté(s). "
                "Corrigez les conflits avant d'appliquer."
            ),
        }

    if soft_warnings and not force:
        return {
            "valid": False,
            "hard_errors": [],
            "soft_warnings": soft_warnings,
            "preview": preview,
            "applied": [],
            "db_errors": [],
            "message": (
                f"{len(soft_warnings)} alerte(s) pédagogique(s) détectée(s) "
                "(même matière le même jour ou jours consécutifs). "
                "Passez force=true pour appliquer quand même."
            ),
        }

    # ── 5. Application en DB ──────────────────────────────────────────────────
    applied = []
    db_errors = []

    try:
        with transaction.atomic():
            # select_for_update : verrouillage des lignes pendant la transaction
            locked_entries = {
                e.id: e
                for e in ClassScheduleEntry.objects.select_for_update().filter(
                    id__in=[op[0] for op in parsed_ops]
                )
            }

            for entry_id, new_weekday, new_start, new_end in parsed_ops:
                entry = locked_entries.get(entry_id)
                if entry is None:
                    db_errors.append({
                        "entry_id": entry_id,
                        "error": "Entrée introuvable au moment du save (supprimée entre-temps ?).",
                    })
                    continue
                entry.weekday = new_weekday
                entry.starts_at = new_start
                entry.ends_at = new_end
                entry.save(update_fields=["weekday", "starts_at", "ends_at"])
                applied.append(entry_id)

            # ── Re-validation post-save DANS la transaction ──────────────────
            # Si la re-validation échoue → rollback automatique
            post_entries = {
                e.id: _entry_to_sim(e)
                for e in ClassScheduleEntry.objects.select_for_update().all()
            }
            post_hard, _ = validate_schedule_state(post_entries)
            if post_hard:
                # Forcer le rollback en levant une exception
                raise _RollbackSignal(
                    f"Re-validation post-save a détecté {len(post_hard)} conflit(s). "
                    "Transaction annulée."
                )

    except _RollbackSignal as sig:
        return {
            "valid": False,
            "hard_errors": [{"type": "post_save_conflict", "message": str(sig)}],
            "soft_warnings": soft_warnings,
            "preview": preview,
            "applied": [],
            "db_errors": [str(sig)],
            "message": str(sig),
        }
    except Exception as exc:
        logger.exception("apply_batch_operations: transaction failed: %s", exc)
        return {
            "valid": False,
            "hard_errors": [],
            "soft_warnings": soft_warnings,
            "preview": preview,
            "applied": [],
            "db_errors": [str(exc)],
            "message": f"Erreur inattendue lors du save : {exc}",
        }

    return {
        "valid": True,
        "hard_errors": [],
        "soft_warnings": soft_warnings,
        "preview": preview,
        "applied": applied,
        "db_errors": db_errors,
        "message": (
            f"{len(applied)} entrée(s) modifiée(s) avec succès."
            + (
                f" {len(soft_warnings)} alerte(s) pédagogique(s) acceptée(s) par l'admin (force=true)."
                if soft_warnings and force
                else ""
            )
        ),
    }


class _RollbackSignal(Exception):
    """Signal interne pour déclencher un rollback propre depuis la transaction."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
#  Validation seule (dry-run)
# ─────────────────────────────────────────────────────────────────────────────

def validate_batch_operations(operations: List[Dict]) -> Dict:
    """
    Simule les opérations et retourne le rapport de validation SANS
    toucher à la DB. Utilisé par TimetableBatchValidateView.
    """
    slots_by_idx = _load_timeslots_indexed()
    all_db_entries = list(ClassScheduleEntry.objects.all())
    sim_entries = {e.id: _entry_to_sim(e) for e in all_db_entries}

    parse_errors = []
    preview = {}

    for op in operations:
        entry_id = op.get("entry_id")
        if entry_id is None:
            parse_errors.append({"op": op, "error": "entry_id requis."})
            continue
        entry_id = int(entry_id)
        if entry_id not in sim_entries:
            parse_errors.append({"entry_id": entry_id, "error": "Entrée introuvable."})
            continue

        current = sim_entries[entry_id]
        target_slot_idx = op.get("target_slot_idx")

        if target_slot_idx is not None:
            slot = slots_by_idx.get(int(target_slot_idx))
            if slot is None:
                parse_errors.append({"entry_id": entry_id, "error": "slot_idx introuvable."})
                continue
            new_weekday = slot["weekday"]
            new_start = slot["start_time"]
            new_end = slot["end_time"]
            new_start_min = slot["start_min"]
            new_end_min = slot["end_min"]
        else:
            tw = op.get("target_weekday")
            ts = _parse_time(op.get("target_start"))
            te = _parse_time(op.get("target_end"))
            if tw is None or ts is None or te is None:
                parse_errors.append({"entry_id": entry_id, "error": "Données cible incomplètes."})
                continue
            new_weekday = int(tw)
            new_start = ts
            new_end = te
            new_start_min = _to_min(ts)
            new_end_min = _to_min(te)
            if new_end_min <= new_start_min:
                parse_errors.append({"entry_id": entry_id, "error": "end <= start."})
                continue

        preview[entry_id] = {
            "from": {
                "weekday": current["weekday"],
                "starts_at": str(current["starts_at"]),
                "ends_at": str(current["ends_at"]),
            },
            "to": {
                "weekday": new_weekday,
                "starts_at": str(new_start),
                "ends_at": str(new_end),
            },
        }

        sim_entries[entry_id] = {
            **current,
            "weekday": new_weekday,
            "starts_at": new_start,
            "ends_at": new_end,
            "start_min": new_start_min,
            "end_min": new_end_min,
        }

    if parse_errors:
        return {
            "valid": False,
            "hard_errors": parse_errors,
            "soft_warnings": [],
            "preview": preview,
        }

    hard_errors, soft_warnings = validate_schedule_state(sim_entries)

    return {
        "valid": not hard_errors,
        "hard_errors": hard_errors,
        "soft_warnings": soft_warnings,
        "preview": preview,
        "message": (
            "Validation OK — aucun conflit dur."
            if not hard_errors
            else f"{len(hard_errors)} conflit(s) dur(s) détecté(s)."
        ),
    }


