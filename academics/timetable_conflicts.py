# academics/timetable_conflicts.py
"""
Détection et résolution des conflits d'emploi du temps.

CORRECTIONS APPLIQUÉES :
  - is_slot_free remplacé par une vérification temporelle réelle (tous les overlaps)
  - Relocation uniquement vers des slots de MÊME durée (préserve les quotas)
  - Re-vérification complète après chaque déplacement
  - Transaction atomique englobant TOUS les saves
  - Rapport détaillé distinguant erreurs, résolutions, et non-résolus
"""
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from django.db import transaction

from academics.models import ClassScheduleEntry, TimeSlot

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers temporels
# ─────────────────────────────────────────────────────────────────────────────

def _to_min(t) -> int:
    """Convertit un objet time en minutes depuis minuit."""
    return t.hour * 60 + t.minute


def _duration(entry) -> int:
    return _to_min(entry.ends_at) - _to_min(entry.starts_at)


def _time_overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Vrai si deux intervalles [a_start, a_end[ et [b_start, b_end[ se chevauchent."""
    return a_start < b_end and b_start < a_end


# ─────────────────────────────────────────────────────────────────────────────
#  Vérification de disponibilité (correction centrale)
# ─────────────────────────────────────────────────────────────────────────────

def _is_free(
    all_entries: List,
    exclude_id: int,
    teacher_id: Optional[int],
    class_id: int,
    weekday: int,
    start_min: int,
    end_min: int,
) -> bool:
    """
    Vérifie qu'aucune entrée existante (hors exclude_id) ne chevauche
    la fenêtre (weekday, start_min, end_min) pour ce prof ET cette classe.

    C'est le remplacement correct de l'ancienne is_slot_free qui ne
    vérifiait que le slot exact, pas les chevauchements temporels réels.
    """
    for e in all_entries:
        if e.id == exclude_id:
            continue
        if e.weekday != weekday:
            continue
        e_start = _to_min(e.starts_at)
        e_end = _to_min(e.ends_at)
        if not _time_overlaps(start_min, end_min, e_start, e_end):
            continue
        # Conflit prof
        if teacher_id and e.teacher_id == teacher_id:
            return False
        # Conflit classe
        if e.school_class_id == class_id:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  Chargement des slots disponibles
# ─────────────────────────────────────────────────────────────────────────────

def _load_timeslots() -> List[Dict]:
    """Retourne tous les TimeSlots sous forme de dicts enrichis."""
    slots = []
    for s in TimeSlot.objects.all().order_by("day", "start_time"):
        start_min = _to_min(s.start_time)
        end_min = _to_min(s.end_time)
        if end_min <= start_min:
            continue
        slots.append({
            "db_obj": s,
            "weekday": s.day,
            "start_min": start_min,
            "end_min": end_min,
            "dur": end_min - start_min,
        })
    return slots


# ─────────────────────────────────────────────────────────────────────────────
#  Détection
# ─────────────────────────────────────────────────────────────────────────────

def detect_teacher_conflicts() -> Dict:
    """
    Balaye tous les ClassScheduleEntry et retourne :
      - teacher_conflicts  : chevauchements horaires entre profs
      - class_conflicts    : chevauchements horaires entre classes
      - c2_violations      : même matière deux fois le même jour (même classe)
      - c3_violations      : même matière sur deux jours consécutifs (même classe)
      - meta               : compteurs
    """
    entries = list(
        ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher").all()
    )

    by_teacher_day = defaultdict(list)
    by_class_day = defaultdict(list)
    by_class_subject_day = defaultdict(list)
    by_class_subject = defaultdict(set)

    for e in entries:
        if e.teacher_id and e.starts_at and e.ends_at:
            by_teacher_day[(e.teacher_id, e.weekday)].append(e)
        if e.school_class_id and e.starts_at and e.ends_at:
            by_class_day[(e.school_class_id, e.weekday)].append(e)
        if e.school_class_id and e.subject_id:
            by_class_subject_day[(e.school_class_id, e.subject_id, e.weekday)].append(e)
            by_class_subject[(e.school_class_id, e.subject_id)].add(e.weekday)

    def _find_overlaps_in_group(ents):
        """Toutes les paires qui se chevauchent (O(n²) mais n petit)."""
        pairs = []
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                a, b = ents[i], ents[j]
                if _time_overlaps(_to_min(a.starts_at), _to_min(a.ends_at),
                                  _to_min(b.starts_at), _to_min(b.ends_at)):
                    pairs.append((a, b))
        return pairs

    def _entry_repr(e):
        return {
            "id": e.id,
            "class_id": e.school_class_id,
            "class_name": str(e.school_class),
            "subject_id": e.subject_id,
            "subject_name": str(e.subject),
            "teacher_id": e.teacher_id,
            "teacher_name": str(e.teacher) if e.teacher else None,
            "weekday": e.weekday,
            "starts_at": str(e.starts_at),
            "ends_at": str(e.ends_at),
        }

    teacher_conflicts = []
    for (tid, day), ents in by_teacher_day.items():
        pairs = _find_overlaps_in_group(ents)
        if pairs:
            teacher_conflicts.append({
                "teacher_id": tid,
                "teacher_name": str(ents[0].teacher) if ents[0].teacher else None,
                "weekday": day,
                "overlapping_pairs": [
                    {"entry_a": _entry_repr(a), "entry_b": _entry_repr(b)}
                    for a, b in pairs
                ],
            })

    class_conflicts = []
    for (cid, day), ents in by_class_day.items():
        pairs = _find_overlaps_in_group(ents)
        if pairs:
            class_conflicts.append({
                "class_id": cid,
                "class_name": str(ents[0].school_class),
                "weekday": day,
                "overlapping_pairs": [
                    {"entry_a": _entry_repr(a), "entry_b": _entry_repr(b)}
                    for a, b in pairs
                ],
            })

    # C2 : même matière deux fois le même jour
    c2_violations = []
    for (cid, sid, day), ents in by_class_subject_day.items():
        if len(ents) > 1:
            c2_violations.append({
                "class_id": cid,
                "subject_id": sid,
                "weekday": day,
                "count": len(ents),
                "entry_ids": [e.id for e in ents],
                "message": f"La matière apparaît {len(ents)} fois le même jour pour cette classe.",
            })

    # C3 : même matière sur jours consécutifs
    c3_violations = []
    for (cid, sid), days in by_class_subject.items():
        sorted_days = sorted(days)
        for i in range(len(sorted_days) - 1):
            if sorted_days[i + 1] - sorted_days[i] == 1:
                d1, d2 = sorted_days[i], sorted_days[i + 1]
                entries_d1 = by_class_subject_day.get((cid, sid, d1), [])
                entries_d2 = by_class_subject_day.get((cid, sid, d2), [])
                c3_violations.append({
                    "class_id": cid,
                    "subject_id": sid,
                    "days": [d1, d2],
                    "entry_ids_day1": [e.id for e in entries_d1],
                    "entry_ids_day2": [e.id for e in entries_d2],
                    "message": "La matière est programmée sur deux jours consécutifs.",
                })

    return {
        "teacher_conflicts": teacher_conflicts,
        "class_conflicts": class_conflicts,
        "c2_violations": c2_violations,
        "c3_violations": c3_violations,
        "meta": {
            "num_entries": len(entries),
            "num_teacher_conflicts": len(teacher_conflicts),
            "num_class_conflicts": len(class_conflicts),
            "num_c2_violations": len(c2_violations),
            "num_c3_violations": len(c3_violations),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Résolution automatique (corrigée)
# ─────────────────────────────────────────────────────────────────────────────

def attempt_resolve_conflicts(dry_run: bool = True) -> Dict:
    """
    Tente de résoudre les conflits DURS (chevauchements prof/classe).
    NE touche PAS aux violations C2/C3 — celles-ci sont signalées à l'admin
    pour intervention manuelle via le batch apply.

    CORRECTIONS par rapport à l'ancienne version :
      1. Vérification temporelle réelle (pas slot_key)
      2. Relocation uniquement vers slot de MÊME durée (préserve quotas)
      3. Re-vérification après chaque déplacement sur la liste en mémoire
      4. Un seul transaction.atomic() englobant tous les saves
      5. Rollback complet si un save échoue

    Paramètres :
      dry_run=True  → calcule et retourne les proposals sans toucher la DB
      dry_run=False → applique les changements dans une transaction atomique

    Retour :
      resolved        : conflits résolus avec détail du déplacement
      unresolved      : conflits que le greedy n'a pas pu résoudre
      proposals       : liste des changements (entry_id, from, to)
      applied_count   : nombre d'entrées modifiées en DB (0 si dry_run)
      errors          : erreurs survenues
    """
    timeslots = _load_timeslots()

    # Travailler sur une copie en mémoire des entrées (on ne touche pas la DB avant la fin)
    db_entries = list(
        ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher").all()
    )

    # Représentation mémoire mutable : on travaille sur des dicts
    # pour pouvoir simuler les déplacements sans toucher la DB
    working = {}  # id → dict avec les champs pertinents
    for e in db_entries:
        working[e.id] = {
            "id": e.id,
            "db_ref": e,                         # référence à l'objet Django
            "teacher_id": e.teacher_id,
            "class_id": e.school_class_id,
            "subject_id": e.subject_id,
            "weekday": e.weekday,
            "starts_at": e.starts_at,
            "ends_at": e.ends_at,
            "start_min": _to_min(e.starts_at),
            "end_min": _to_min(e.ends_at),
            "dur": _duration(e),
            "modified": False,
        }

    def working_as_entries():
        """Vue des entrées de travail comme liste de pseudo-entries pour _is_free."""
        class _E:
            def __init__(self, w):
                self.id = w["id"]
                self.teacher_id = w["teacher_id"]
                self.school_class_id = w["class_id"]
                self.weekday = w["weekday"]
                self.starts_at = w["starts_at"]
                self.ends_at = w["ends_at"]
        return [_E(w) for w in working.values()]

    def find_teacher_conflicts():
        """Retourne les paires (id_a, id_b) en conflit enseignant."""
        by_teacher_day = defaultdict(list)
        for w in working.values():
            if w["teacher_id"]:
                by_teacher_day[(w["teacher_id"], w["weekday"])].append(w)
        conflicts = []
        for ents in by_teacher_day.values():
            for i in range(len(ents)):
                for j in range(i + 1, len(ents)):
                    a, b = ents[i], ents[j]
                    if _time_overlaps(a["start_min"], a["end_min"], b["start_min"], b["end_min"]):
                        conflicts.append((a["id"], b["id"]))
        return conflicts

    # Candidats : slots de même durée que l'entrée à déplacer
    def candidate_slots_for(w_entry):
        target_dur = w_entry["dur"]
        return [s for s in timeslots if s["dur"] == target_dur]

    proposals = []
    resolved = []
    unresolved = []

    conflicts = find_teacher_conflicts()

    for (id_a, id_b) in conflicts:
        # Re-vérifier : le conflit existe-t-il encore dans l'état courant ?
        wa = working.get(id_a)
        wb = working.get(id_b)
        if not wa or not wb:
            continue
        if not _time_overlaps(wa["start_min"], wa["end_min"], wb["start_min"], wb["end_min"]):
            continue  # déjà résolu par un déplacement précédent

        moved = False
        # Essayer de déplacer wb en priorité (entrée "plus tard")
        for candidate_entry, candidates in [(wb, candidate_slots_for(wb)), (wa, candidate_slots_for(wa))]:
            if moved:
                break
            for slot in candidates:
                # Ne pas rester sur le même créneau
                if (slot["weekday"] == candidate_entry["weekday"]
                        and slot["start_min"] == candidate_entry["start_min"]):
                    continue

                all_pseudo_entries = working_as_entries()
                free = _is_free(
                    all_entries=all_pseudo_entries,
                    exclude_id=candidate_entry["id"],
                    teacher_id=candidate_entry["teacher_id"],
                    class_id=candidate_entry["class_id"],
                    weekday=slot["weekday"],
                    start_min=slot["start_min"],
                    end_min=slot["end_min"],
                )
                if not free:
                    continue

                # Appliquer le déplacement dans l'état mémoire
                proposal = {
                    "entry_id": candidate_entry["id"],
                    "from": {
                        "weekday": candidate_entry["weekday"],
                        "starts_at": str(candidate_entry["starts_at"]),
                        "ends_at": str(candidate_entry["ends_at"]),
                    },
                    "to": {
                        "weekday": slot["weekday"],
                        "starts_at": str(slot["db_obj"].start_time),
                        "ends_at": str(slot["db_obj"].end_time),
                    },
                }
                proposals.append(proposal)

                working[candidate_entry["id"]].update({
                    "weekday": slot["weekday"],
                    "starts_at": slot["db_obj"].start_time,
                    "ends_at": slot["db_obj"].end_time,
                    "start_min": slot["start_min"],
                    "end_min": slot["end_min"],
                    "modified": True,
                })

                resolved.append({
                    "conflict": {"entry_ids": [id_a, id_b]},
                    "moved_entry_id": candidate_entry["id"],
                    "to": proposal["to"],
                })
                moved = True
                break

        if not moved:
            unresolved.append({
                "entry_ids": [id_a, id_b],
                "reason": "Aucun slot de même durée disponible pour ce prof et cette classe.",
            })

    # Après résolution greedy, re-vérifier qu'on n'a pas créé de nouveaux conflits
    remaining_conflicts = find_teacher_conflicts()
    if remaining_conflicts:
        for (id_a, id_b) in remaining_conflicts:
            unresolved.append({
                "entry_ids": [id_a, id_b],
                "reason": "Conflit résiduel détecté après résolution greedy.",
            })

    applied_count = 0
    errors = []

    if not dry_run:
        modified_entries = [w for w in working.values() if w["modified"]]
        try:
            with transaction.atomic():
                for w in modified_entries:
                    entry = w["db_ref"]
                    entry.weekday = w["weekday"]
                    entry.starts_at = w["starts_at"]
                    entry.ends_at = w["ends_at"]
                    entry.save(update_fields=["weekday", "starts_at", "ends_at"])
                    applied_count += 1
        except Exception as exc:
            errors.append(str(exc))
            applied_count = 0
            logger.exception("attempt_resolve_conflicts: transaction failed: %s", exc)

    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "proposals": proposals,
        "applied_count": applied_count,
        "dry_run": dry_run,
        "errors": errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Wrapper principal
# ─────────────────────────────────────────────────────────────────────────────

def detect_and_resolve(dry_run: bool = True, persist: bool = False) -> Dict:
    """
    Pipeline complet : détecte puis tente de résoudre les conflits durs.
    Les violations C2/C3 sont détectées et signalées mais NON résolues
    automatiquement — elles sont laissées à l'appréciation de l'admin
    via le batch apply.

    Paramètres :
      dry_run=True, persist=False  → simulation pure, rien en DB
      dry_run=False, persist=True  → résolution + persistance
    """
    before = detect_teacher_conflicts()

    if (before["meta"]["num_teacher_conflicts"] == 0
            and before["meta"]["num_class_conflicts"] == 0):
        return {
            "detected_before": before,
            "resolve_report": {"skipped": True, "reason": "Aucun conflit dur détecté."},
            "detected_after": before,
        }

    resolve_report = attempt_resolve_conflicts(dry_run=dry_run or not persist)

    after = detect_teacher_conflicts()

    return {
        "detected_before": before,
        "resolve_report": resolve_report,
        "detected_after": after,
        "summary": {
            "hard_conflicts_before": (
                before["meta"]["num_teacher_conflicts"]
                + before["meta"]["num_class_conflicts"]
            ),
            "hard_conflicts_after": (
                after["meta"]["num_teacher_conflicts"]
                + after["meta"]["num_class_conflicts"]
            ),
            "c2_violations": after["meta"]["num_c2_violations"],
            "c3_violations": after["meta"]["num_c3_violations"],
            "admin_action_needed": (
                after["meta"]["num_c2_violations"] > 0
                or after["meta"]["num_c3_violations"] > 0
                or after["meta"]["num_teacher_conflicts"] > 0
            ),
        },
    }