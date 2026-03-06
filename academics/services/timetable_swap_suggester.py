# academics/services/timetable_swap_suggester.py
"""
Suggéreur de permutations pour résoudre les violations C3 (jours consécutifs).

PROBLÈMES DE LA VERSION PRÉCÉDENTE :
  1. Ne regardait que les entrées de la MÊME classe comme bloqueurs
  2. Ne faisait pas de swap cross-classe (même prof, deux classes différentes)
  3. La recherche de slots libres ignorait le reste de l'école

NOUVELLE STRATÉGIE (school-wide) :

  NIVEAU 1 — Déplacement direct :
    Slot libre pour E (prof + classe libres, pas de nouveau C3).

  NIVEAU 2A — Swap cross-classe même prof :
    E est prof Serge à 2ndB2 vendredi.
    Serge enseigne aussi à 1èreB2 lundi.
    Si 2ndB2 libre lundi ET 1èreB2 libre vendredi → échange les deux slots.
    Aucun conflit prof car c'est le même enseignant.

  NIVEAU 2B — Libération de slot via prof déplacé school-wide :
    Un slot S de la classe cible est occupé par (prof Y, matière B).
    On cherche où Y peut aller dans TOUTE l'école.
    Si Y est libre à S_new (prof + classe Y libres) → déplacer Y,
    puis mettre E dans le slot libéré.

  NIVEAU 3 — Chaîne cross-classe :
    Combine 2A et 2B sur plusieurs maillons.
"""
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set

from academics.models import ClassScheduleEntry, TimeSlot

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_min(t) -> int:
    return t.hour * 60 + t.minute


def _overlaps(a_s: int, a_e: int, b_s: int, b_e: int) -> bool:
    return a_s < b_e and b_s < a_e


def _load_all_entries() -> Dict[int, dict]:
    result = {}
    for e in ClassScheduleEntry.objects.select_related(
        "school_class", "subject", "teacher"
    ).all():
        if not e.starts_at or not e.ends_at:
            continue
        s_min = _to_min(e.starts_at)
        e_min = _to_min(e.ends_at)
        result[e.id] = {
            "id":           e.id,
            "class_id":     e.school_class_id,
            "class_name":   str(e.school_class),
            "subject_id":   e.subject_id,
            "subject_name": str(e.subject),
            "teacher_id":   e.teacher_id,
            "teacher_name": str(e.teacher) if e.teacher else None,
            "weekday":      e.weekday,
            "starts_at":    e.starts_at,
            "ends_at":      e.ends_at,
            "start_min":    s_min,
            "end_min":      e_min,
            "dur":          e_min - s_min,
        }
    return result


def _load_timeslots() -> List[dict]:
    slots = []
    for s in TimeSlot.objects.all().order_by("day", "start_time"):
        s_min = _to_min(s.start_time)
        e_min = _to_min(s.end_time)
        if e_min <= s_min:
            continue
        slots.append({
            "weekday":    s.day,
            "start_time": s.start_time,
            "end_time":   s.end_time,
            "start_min":  s_min,
            "end_min":    e_min,
            "dur":        e_min - s_min,
        })
    return slots


def _entry_repr(e: dict) -> dict:
    return {
        "id":           e["id"],
        "class_id":     e["class_id"],
        "class_name":   e["class_name"],
        "subject_id":   e["subject_id"],
        "subject_name": e["subject_name"],
        "teacher_id":   e["teacher_id"],
        "teacher_name": e["teacher_name"],
        "weekday":      e["weekday"],
        "starts_at":    str(e["starts_at"]),
        "ends_at":      str(e["ends_at"]),
    }


def _slot_from_entry(e: dict) -> dict:
    """Convertit une entrée en pseudo-slot pour réutiliser _op()."""
    return {
        "weekday":    e["weekday"],
        "start_time": e["starts_at"],
        "end_time":   e["ends_at"],
        "start_min":  e["start_min"],
        "end_min":    e["end_min"],
        "dur":        e["dur"],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ScheduleState — simulation en mémoire, school-wide
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleState:
    def __init__(self, entries: Dict[int, dict]):
        self._e: Dict[int, dict] = {k: dict(v) for k, v in entries.items()}

    def all(self) -> List[dict]:
        return list(self._e.values())

    def get(self, eid: int) -> Optional[dict]:
        return self._e.get(eid)

    def apply_move(self, eid: int, weekday: int, starts_at, ends_at,
                   start_min: int, end_min: int) -> "ScheduleState":
        """Retourne un NOUVEL état immuable avec le déplacement appliqué."""
        new = {k: dict(v) for k, v in self._e.items()}
        new[eid] = {
            **new[eid],
            "weekday":   weekday,
            "starts_at": starts_at,
            "ends_at":   ends_at,
            "start_min": start_min,
            "end_min":   end_min,
        }
        return ScheduleState(new)

    def teacher_free(self, tid, weekday: int, s: int, e: int,
                     exclude: Set[int] = None) -> bool:
        """
        Vérifie school-wide : le prof est-il libre à ce créneau dans TOUTES ses classes ?
        C'est ici que la version précédente échouait — elle ne vérifiait que la classe cible.
        """
        exclude = exclude or set()
        for entry in self._e.values():
            if entry["id"] in exclude:
                continue
            if entry["teacher_id"] != tid:
                continue
            if entry["weekday"] != weekday:
                continue
            if _overlaps(s, e, entry["start_min"], entry["end_min"]):
                return False
        return True

    def class_free(self, cid: int, weekday: int, s: int, e: int,
                   exclude: Set[int] = None) -> bool:
        """Vérifie si la classe est libre à ce créneau."""
        exclude = exclude or set()
        for entry in self._e.values():
            if entry["id"] in exclude:
                continue
            if entry["class_id"] != cid:
                continue
            if entry["weekday"] != weekday:
                continue
            if _overlaps(s, e, entry["start_min"], entry["end_min"]):
                return False
        return True

    def creates_c3(self, cid: int, sid: int, new_weekday: int,
                   exclude: Set[int] = None) -> bool:
        """Vrai si ajouter (cid, sid) au jour new_weekday crée un C3."""
        exclude = exclude or set()
        existing = set()
        for entry in self._e.values():
            if entry["id"] in exclude:
                continue
            if entry["class_id"] == cid and entry["subject_id"] == sid:
                existing.add(entry["weekday"])
        for d in existing:
            if abs(d - new_weekday) == 1:
                return True
        return False

    def count_c3(self) -> int:
        by_cs = defaultdict(set)
        for entry in self._e.values():
            by_cs[(entry["class_id"], entry["subject_id"])].add(entry["weekday"])
        total = 0
        for days in by_cs.values():
            sd = sorted(days)
            for i in range(len(sd) - 1):
                if sd[i + 1] - sd[i] == 1:
                    total += 1
        return total

    def entries_by_teacher(self, tid) -> List[dict]:
        """Toutes les entrées d'un prof dans TOUTE l'école."""
        return [e for e in self._e.values() if e["teacher_id"] == tid]

    def entries_by_class(self, cid: int) -> List[dict]:
        return [e for e in self._e.values() if e["class_id"] == cid]

    def free_slots_for_teacher_and_class(
        self, tid, cid: int, dur: int,
        timeslots: List[dict],
        exclude: Set[int] = None,
    ) -> List[dict]:
        """
        Slots où prof tid (school-wide) ET classe cid sont simultanément libres.
        C'est la recherche élargie que la version précédente ne faisait pas.
        """
        exclude = exclude or set()
        result = []
        for slot in timeslots:
            if slot["dur"] != dur:
                continue
            if not self.teacher_free(tid, slot["weekday"],
                                     slot["start_min"], slot["end_min"], exclude):
                continue
            if not self.class_free(cid, slot["weekday"],
                                   slot["start_min"], slot["end_min"], exclude):
                continue
            result.append(slot)
        return result


# ─────────────────────────────────────────────────────────────────────────────
#  Score — moins = meilleur
# ─────────────────────────────────────────────────────────────────────────────

def _score(depth: int, c3_before: int, c3_after: int) -> float:
    delta_resolved = c3_before - c3_after
    return (depth * 10) + (c3_after * 50) - (delta_resolved * 5)


# ─────────────────────────────────────────────────────────────────────────────
#  Moteur principal
# ─────────────────────────────────────────────────────────────────────────────

def suggest_swaps_for_entry(
    target_entry_id: int,
    max_chain_depth: int = 2,
    max_suggestions: int = 8,
) -> dict:
    """
    Cherche des permutations school-wide pour résoudre le C3 de l'entrée cible.

    Retourne des suggestions triées par score (moins perturbateur en premier).
    Les opérations de chaque suggestion sont directement utilisables
    avec POST /timetable-batch-apply/.
    """
    all_entries = _load_all_entries()
    timeslots   = _load_timeslots()
    state0      = ScheduleState(all_entries)

    target = all_entries.get(target_entry_id)
    if not target:
        return {
            "target_entry": None, "suggestions": [],
            "message": f"Entrée {target_entry_id} introuvable.",
        }

    c3_initial   = state0.count_c3()
    suggestions  = []
    seen: Set[frozenset] = set()

    # ── Helpers locaux ────────────────────────────────────────────────────────

    def _op(entry_dict: dict, to_slot: dict, reason: str) -> dict:
        return {
            "entry_id":   entry_dict["id"],
            "entry_info": _entry_repr(entry_dict),
            "from": {
                "weekday":   entry_dict["weekday"],
                "starts_at": str(entry_dict["starts_at"]),
                "ends_at":   str(entry_dict["ends_at"]),
            },
            "to": {
                "weekday":   to_slot["weekday"],
                "starts_at": str(to_slot.get("start_time", to_slot.get("starts_at"))),
                "ends_at":   str(to_slot.get("end_time",   to_slot.get("ends_at"))),
            },
            "reason": reason,
        }

    def _register(ops: list, state_final: ScheduleState, explanation: str):
        """Enregistre une suggestion si elle améliore le C3 et n'est pas un doublon."""
        key = frozenset(
            (op["entry_id"], op["to"]["weekday"], op["to"]["starts_at"])
            for op in ops
        )
        if key in seen:
            return
        seen.add(key)
        c3_final = state_final.count_c3()
        if c3_final >= c3_initial:
            return
        suggestions.append({
            "depth":        len(ops),
            "operations":   ops,
            "c3_before":    c3_initial,
            "c3_after":     c3_final,
            "c3_resolved":  c3_initial - c3_final,
            "c3_remaining": c3_final,
            "explanation":  explanation,
            "score":        _score(len(ops), c3_initial, c3_final),
        })

    # ──────────────────────────────────────────────────────────────────────────
    #  NIVEAU 1 — Déplacement direct (school-wide pour le prof)
    # ──────────────────────────────────────────────────────────────────────────
    for slot in timeslots:
        if slot["dur"] != target["dur"]:
            continue
        if slot["weekday"] == target["weekday"]:
            continue
        if not state0.teacher_free(
            target["teacher_id"], slot["weekday"],
            slot["start_min"], slot["end_min"],
            exclude={target_entry_id},
        ):
            continue
        if not state0.class_free(
            target["class_id"], slot["weekday"],
            slot["start_min"], slot["end_min"],
            exclude={target_entry_id},
        ):
            continue
        if state0.creates_c3(
            target["class_id"], target["subject_id"],
            slot["weekday"], exclude={target_entry_id},
        ):
            continue
        state1 = state0.apply_move(
            target_entry_id, slot["weekday"],
            slot["start_time"], slot["end_time"],
            slot["start_min"], slot["end_min"],
        )
        _register(
            [_op(target, slot, "Déplacement direct vers slot libre.")],
            state1,
            f"Déplacer directement '{target['subject_name']}' "
            f"(classe {target['class_name']}) "
            f"du jour {target['weekday']} → jour {slot['weekday']} "
            f"({slot['start_time']}–{slot['end_time']}).",
        )

    if max_chain_depth < 2:
        return _build_result(target, c3_initial, suggestions, max_suggestions)

    # ──────────────────────────────────────────────────────────────────────────
    #  NIVEAU 2A — Swap cross-classe même prof
    #  Ex : Serge a Philo en 2ndB2 vendredi ET en 1èreB2 lundi.
    #  2ndB2 libre lundi + 1èreB2 libre vendredi → on échange les deux slots.
    #  Aucun conflit prof car c'est le même enseignant qui enseigne les deux.
    # ──────────────────────────────────────────────────────────────────────────
    other_entries_same_prof = [
        e for e in state0.entries_by_teacher(target["teacher_id"])
        if e["id"] != target_entry_id
        and e["class_id"] != target["class_id"]
        and e["dur"] == target["dur"]
    ]

    for other in other_entries_same_prof:
        # target peut-il prendre le slot de other ?
        if not state0.class_free(
            target["class_id"], other["weekday"],
            other["start_min"], other["end_min"],
            exclude={target_entry_id},
        ):
            continue
        # other peut-il prendre le slot de target ?
        if not state0.class_free(
            other["class_id"], target["weekday"],
            target["start_min"], target["end_min"],
            exclude={other["id"]},
        ):
            continue
        # Pas de nouveau C3 pour target à sa nouvelle position ?
        if state0.creates_c3(
            target["class_id"], target["subject_id"],
            other["weekday"], exclude={target_entry_id},
        ):
            continue

        state1 = state0.apply_move(
            target_entry_id, other["weekday"],
            other["starts_at"], other["ends_at"],
            other["start_min"], other["end_min"],
        ).apply_move(
            other["id"], target["weekday"],
            target["starts_at"], target["ends_at"],
            target["start_min"], target["end_min"],
        )
        _register(
            [
                _op(target, _slot_from_entry(other),
                    f"Échange cross-classe : '{target['subject_name']}' "
                    f"passe du jour {target['weekday']} au jour {other['weekday']} "
                    f"(slot de la classe {other['class_name']})."),
                _op(other, _slot_from_entry(target),
                    f"En retour : '{other['subject_name']}' de {other['class_name']} "
                    f"passe du jour {other['weekday']} au jour {target['weekday']}. "
                    f"Même prof — aucun conflit enseignant."),
            ],
            state1,
            f"Swap cross-classe même prof ({target['teacher_name']}) : "
            f"'{target['subject_name']}' ({target['class_name']}, jour {target['weekday']}) "
            f"↔ '{other['subject_name']}' ({other['class_name']}, jour {other['weekday']}). "
            f"Aucun conflit prof car c'est le même enseignant.",
        )

    # ──────────────────────────────────────────────────────────────────────────
    #  NIVEAU 2B — Libération de slot via prof déplacé school-wide
    #
    #  Scénario : la classe cible est pleine à un slot S occupé par (prof Y, matière B).
    #  On prend prof Y et on cherche dans TOUTE l'école où il peut aller.
    #  Si Y est libre à S_new (et sa classe aussi) → Y part, slot libéré pour target.
    # ──────────────────────────────────────────────────────────────────────────
    for blocker in state0.entries_by_class(target["class_id"]):
        if blocker["id"] == target_entry_id:
            continue
        if blocker["dur"] != target["dur"]:
            continue

        # Chercher un slot de remplacement pour blocker (school-wide pour son prof)
        free_for_blocker = state0.free_slots_for_teacher_and_class(
            blocker["teacher_id"],
            blocker["class_id"],
            blocker["dur"],
            timeslots,
            exclude={blocker["id"]},
        )

        for s_new in free_for_blocker:
            if s_new["weekday"] == blocker["weekday"]:
                continue

            # Simuler : déplacer le bloqueur
            state1 = state0.apply_move(
                blocker["id"], s_new["weekday"],
                s_new["start_time"], s_new["end_time"],
                s_new["start_min"], s_new["end_min"],
            )

            # Slot libéré = ancien slot du bloqueur
            freed_wd = blocker["weekday"]
            freed_s  = blocker["start_min"]
            freed_e  = blocker["end_min"]

            # Prof de target libre sur ce slot libéré ?
            if not state1.teacher_free(
                target["teacher_id"], freed_wd,
                freed_s, freed_e, exclude={target_entry_id},
            ):
                continue
            # Classe cible libre sur ce slot libéré ?
            if not state1.class_free(
                target["class_id"], freed_wd,
                freed_s, freed_e, exclude={target_entry_id},
            ):
                continue
            # Pas de nouveau C3 pour target ?
            if state1.creates_c3(
                target["class_id"], target["subject_id"],
                freed_wd, exclude={target_entry_id},
            ):
                continue

            state2 = state1.apply_move(
                target_entry_id, freed_wd,
                blocker["starts_at"], blocker["ends_at"],
                freed_s, freed_e,
            )
            _register(
                [
                    _op(blocker, s_new,
                        f"Déplacer '{blocker['subject_name']}' "
                        f"(prof {blocker['teacher_name']}) "
                        f"du jour {blocker['weekday']} → jour {s_new['weekday']} "
                        f"({s_new['start_time']}–{s_new['end_time']}). "
                        f"Recherche school-wide des disponibilités."),
                    _op(target, _slot_from_entry(blocker),
                        f"'{target['subject_name']}' prend le slot libéré "
                        f"(jour {freed_wd}, "
                        f"{blocker['starts_at']}–{blocker['ends_at']})."),
                ],
                state2,
                f"1) Déplacer '{blocker['subject_name']}' "
                f"(prof {blocker['teacher_name']}, entrée #{blocker['id']}) "
                f"vers jour {s_new['weekday']} "
                f"({s_new['start_time']}–{s_new['end_time']}). "
                f"2) '{target['subject_name']}' occupe le slot libéré "
                f"(jour {freed_wd}).",
            )

    if max_chain_depth < 3:
        return _build_result(target, c3_initial, suggestions, max_suggestions)

    # ──────────────────────────────────────────────────────────────────────────
    #  NIVEAU 3 — Chaîne cross-classe
    #  Le bloqueur Y ne peut pas être déplacé directement →
    #  on swap Y avec une autre de ses classes, ce qui libère son slot,
    #  puis target prend ce slot.
    # ──────────────────────────────────────────────────────────────────────────
    for blocker in state0.entries_by_class(target["class_id"]):
        if blocker["id"] == target_entry_id:
            continue
        if blocker["dur"] != target["dur"]:
            continue

        # Autres entrées du prof du bloqueur dans d'AUTRES classes
        blocker_cross = [
            e for e in state0.entries_by_teacher(blocker["teacher_id"])
            if e["id"] != blocker["id"]
            and e["class_id"] != blocker["class_id"]
            and e["dur"] == blocker["dur"]
        ]

        for alt in blocker_cross:
            # Swap blocker ↔ alt possible ?
            if not state0.class_free(
                blocker["class_id"], alt["weekday"],
                alt["start_min"], alt["end_min"], exclude={blocker["id"]},
            ):
                continue
            if not state0.class_free(
                alt["class_id"], blocker["weekday"],
                blocker["start_min"], blocker["end_min"], exclude={alt["id"]},
            ):
                continue

            # Simuler le swap blocker ↔ alt
            state1 = state0.apply_move(
                blocker["id"], alt["weekday"],
                alt["starts_at"], alt["ends_at"],
                alt["start_min"], alt["end_min"],
            ).apply_move(
                alt["id"], blocker["weekday"],
                blocker["starts_at"], blocker["ends_at"],
                blocker["start_min"], blocker["end_min"],
            )

            # Après le swap, est-ce que le slot original du bloqueur
            # est maintenant libre pour target ?
            freed_wd = blocker["weekday"]
            freed_s  = blocker["start_min"]
            freed_e  = blocker["end_min"]

            if not state1.teacher_free(
                target["teacher_id"], freed_wd,
                freed_s, freed_e, exclude={target_entry_id},
            ):
                continue
            if not state1.class_free(
                target["class_id"], freed_wd,
                freed_s, freed_e, exclude={target_entry_id},
            ):
                continue
            if state1.creates_c3(
                target["class_id"], target["subject_id"],
                freed_wd, exclude={target_entry_id},
            ):
                continue

            state2 = state1.apply_move(
                target_entry_id, freed_wd,
                blocker["starts_at"], blocker["ends_at"],
                freed_s, freed_e,
            )
            _register(
                [
                    _op(blocker, _slot_from_entry(alt),
                        f"Maillon 1 : '{blocker['subject_name']}' "
                        f"(prof {blocker['teacher_name']}) échange son créneau "
                        f"du jour {blocker['weekday']} avec la classe {alt['class_name']} "
                        f"(jour {alt['weekday']})."),
                    _op(alt, _slot_from_entry(blocker),
                        f"Maillon 2 : '{alt['subject_name']}' de {alt['class_name']} "
                        f"vient au jour {blocker['weekday']} à la place."),
                    _op(target, _slot_from_entry(blocker),
                        f"Maillon 3 : '{target['subject_name']}' prend le slot "
                        f"maintenant libre (jour {freed_wd})."),
                ],
                state2,
                f"Chaîne 3 maillons : "
                f"1) '{blocker['subject_name']}' swap avec {alt['class_name']} ; "
                f"2) '{alt['subject_name']}' de {alt['class_name']} vient à sa place ; "
                f"3) '{target['subject_name']}' occupe le slot libéré (jour {freed_wd}).",
            )

    return _build_result(target, c3_initial, suggestions, max_suggestions)


def _build_result(target: dict, c3_initial: int,
                  suggestions: list, max_suggestions: int) -> dict:
    suggestions.sort(key=lambda s: (s["score"], s["depth"]))
    suggestions = suggestions[:max_suggestions]
    for i, s in enumerate(suggestions):
        s["rank"] = i + 1

    if suggestions:
        msg = (
            f"{len(suggestions)} suggestion(s) trouvée(s). "
            "La #1 est la moins perturbatrice. "
            "Copiez ses 'operations' dans /timetable-batch-apply/ pour appliquer."
        )
    else:
        msg = (
            "Aucune permutation trouvée. "
            "Essayez depth=3 ou vérifiez manuellement. "
            f"Prof : {target['teacher_name']}."
        )

    return {
        "target_entry":     _entry_repr(target),
        "initial_c3_count": c3_initial,
        "suggestions_count": len(suggestions),
        "suggestions":      suggestions,
        "message":          msg,
    }


# =============================================================================
#  VUE — à coller dans views.py
# =============================================================================

from academics.services.timetable_swap_suggester import suggest_swaps_for_entry

class TimetableSwapSuggestView(APIView):
    
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({"detail": "Réservé aux administrateurs."},
                            status=status.HTTP_403_FORBIDDEN)
        entry_id = request.query_params.get("entry_id")
        if not entry_id:
            return Response({"detail": "entry_id obligatoire."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            entry_id = int(entry_id)
        except ValueError:
            return Response({"detail": "entry_id doit être un entier."},
                            status=status.HTTP_400_BAD_REQUEST)
        depth = min(3, max(1, int(request.query_params.get("depth", 2))))
        max_s = min(20, max(1, int(request.query_params.get("max", 8))))
        try:
            result = suggest_swaps_for_entry(entry_id,
                                             max_chain_depth=depth,
                                             max_suggestions=max_s)
        except Exception as e:
            logger.exception("TimetableSwapSuggestView: %s", e)
            return Response({"detail": str(e)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(result, status=status.HTTP_200_OK)


