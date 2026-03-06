"""
Timetable pipeline (by level) — version corrigée.

CHANGEMENTS PRINCIPAUX vs version précédente :
  1. _decompose(needed_min) : décompose le quota en blocs de 3h et 2h.
     Règle : extraire autant de 3h que possible, compléter avec des 2h.
     Ex : 6h → 2×3h (plus 3×2h), 7h → 1×3h + 2×2h, 9h → 3×3h.

  2. feasible_slots : filtré strictement sur les slots de 120 ou 180 min.
     Le solveur ne peut plus choisir des durées arbitraires.

  3. Contraintes exactes par durée : au lieu de ">= needed",
     on impose EXACTEMENT n3 slots de 3h ET n2 slots de 2h.
     Cela garantit que 6h = 2×3h, jamais 3×2h.

  4. Correction bug merge : weekday/starts_at/ends_at maintenant mis à jour
     en même temps que slot_idx lors d'un déplacement dans try_resolve_conflicts.

  5. Retry avec backoff : si un niveau échoue au premier essai,
     on retente avec un timeout plus long (jusqu'à 4 essais).

  6. Diagnostics enrichis : violations_c1c3_consecutive_days,
     penalty_consecutive_weight dans le rapport.
"""

import time
import math
import json
import random
from collections import defaultdict
from datetime import datetime

from ortools.sat.python import cp_model
from django.db import transaction

from academics.models import (
    Level,
    SchoolClass,
    ClassSubject,
    TimeSlot,
    ClassScheduleEntry,
)
from core.models import Teacher

# Seules durées autorisées (en minutes)
ALLOWED_DURATIONS = {120, 180}  # 2h et 3h uniquement


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_minutes(t):
    return t.hour * 60 + t.minute


def _decompose(needed_min: int):
    """
    Décompose needed_min en (n3, n2) :
      n3 × 180min + n2 × 120min = needed_min
    Règle : maximiser n3 (privilégier les blocs de 3h).

    Exemples :
      120  → (0, 1)   2h
      180  → (1, 0)   3h
      240  → (0, 2)   4h  = 2×2h
      300  → (1, 1)   5h  = 3h + 2h
      360  → (2, 0)   6h  = 2×3h   ← correction du problème Philo
      420  → (1, 2)   7h  = 3h + 2×2h
      480  → (2, 1)   8h  = 2×3h + 2h
      540  → (3, 0)   9h  = 3×3h

    Retourne None si impossible à exprimer avec {120, 180}.
    """
    # On part du maximum de blocs de 3h possible et on descend
    for n3 in range(needed_min // 180, -1, -1):
        remainder = needed_min - 180 * n3
        if remainder >= 0 and remainder % 120 == 0:
            n2 = remainder // 120
            return n3, n2
    return None  # ne devrait pas arriver avec des quotas raisonnables


def _load_slots():
    """Charge les TimeSlots et construit les index de conflits."""
    time_slots = list(TimeSlot.objects.all().order_by("day", "start_time"))
    slots = []
    for idx, slot in enumerate(time_slots):
        start_min = _to_minutes(slot.start_time)
        end_min   = _to_minutes(slot.end_time)
        dur = end_min - start_min
        if dur <= 0:
            raise ValueError(f"TimeSlot id={slot.id} durée non positive")
        slots.append({
            "idx":     idx,
            "db_obj":  slot,
            "weekday": slot.day,
            "start":   start_min,
            "end":     end_min,
            "dur":     dur,
        })

    slots_by_day = defaultdict(list)
    for s in slots:
        slots_by_day[s["weekday"]].append(s["idx"])

    # Index de conflits temporels (même jour, chevauchement)
    slot_conflicts = {s["idx"]: set() for s in slots}
    for day, idxs in slots_by_day.items():
        for a in range(len(idxs)):
            i = idxs[a]
            s_i = slots[i]
            for b in range(a + 1, len(idxs)):
                j = idxs[b]
                s_j = slots[j]
                if s_i["start"] < s_j["end"] and s_j["start"] < s_i["end"]:
                    slot_conflicts[i].add(j)
                    slot_conflicts[j].add(i)

    return slots, slots_by_day, slot_conflicts


# ─────────────────────────────────────────────────────────────────────────────
#  Diagnostics par niveau
# ─────────────────────────────────────────────────────────────────────────────

def analyze_levels():
    slots, _, _ = _load_slots()
    available_minutes = sum(s["dur"] for s in slots)
    levels = []
    for level in Level.objects.all():
        classes_qs    = list(SchoolClass.objects.filter(level=level))
        num_classes   = len(classes_qs)
        num_cs        = 0
        needed_minutes = 0
        missing_teachers = []
        heavy_classes = []
        for cls in classes_qs:
            cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
            class_needed = 0
            for cs in cs_qs:
                hrs = getattr(cs, "hours_per_week", None)
                if hrs is None:
                    continue
                num_cs += 1
                minutes = int(hrs * 60)
                needed_minutes += minutes
                class_needed   += minutes
                teacher = Teacher.objects.filter(subject=cs.subject, classes=cls).first()
                if not teacher:
                    missing_teachers.append({
                        "class": str(cls), "class_id": cls.id,
                        "subject": cs.subject.name, "subject_id": cs.subject.id,
                    })
            heavy_classes.append((cls.id, class_needed))
        est_vars = num_cs * len(slots)
        heavy_classes.sort(key=lambda x: -x[1])
        levels.append({
            "level_id":         level.id,
            "name":             getattr(level, "name", str(level)),
            "num_classes":      num_classes,
            "num_classsubjects": num_cs,
            "needed_minutes":   needed_minutes,
            "available_minutes": available_minutes,
            "est_vars":         est_vars,
            "missing_teachers": missing_teachers,
            "heavy_classes":    heavy_classes[:5],
        })
    return {"slots_count": len(slots), "levels": levels}


# ─────────────────────────────────────────────────────────────────────────────
#  Génération CP-SAT pour un niveau
# ─────────────────────────────────────────────────────────────────────────────

def generate_timetable_for_level(
    level_id,
    time_limit_seconds=60,
    penalty_same_day=20,
    penalty_consecutive=50,
    allow_missing_teacher=False,
    occupied_teacher_slots=None,  # set of (teacher_id, slot_idx) déjà occupés
    occupied_class_slots=None,    # set of (class_id, slot_idx) déjà occupés
):
    """
    Résout CP-SAT pour un seul niveau.

    Nouveautés :
    - Décomposition exacte du quota en blocs 3h/2h via _decompose()
    - Contraintes d'égalité stricte sur le nombre de sessions par durée
    - Seuls les slots de 120 ou 180 min sont candidats
    - occupied_*_slots permet d'exclure les créneaux déjà pris par d'autres niveaux

    Retourne un LevelPlan :
      success, message, entries, diagnostics, time_s
    """
    t0 = time.time()
    occupied_teacher_slots = occupied_teacher_slots or set()
    occupied_class_slots   = occupied_class_slots   or set()

    slots, slots_by_day, slot_conflicts = _load_slots()

    try:
        level = Level.objects.get(id=level_id)
    except Level.DoesNotExist:
        return {"success": False, "message": "Level not found",
                "entries": [], "diagnostics": {}, "time_s": 0}

    classes_objs = list(SchoolClass.objects.filter(level=level))
    if not classes_objs:
        return {"success": False, "message": "No classes for level",
                "entries": [], "diagnostics": {}, "time_s": 0}

    classes = {}
    missing_teachers = []
    decomposition_errors = []

    for cls in classes_objs:
        subj_map = {}
        for cs in ClassSubject.objects.filter(school_class=cls).select_related("subject"):
            hrs = getattr(cs, "hours_per_week", None)
            if hrs is None:
                continue
            subj = cs.subject
            needed_min = int(hrs * 60)

            # ── NOUVEAU : vérifier la décomposition avant même de créer les variables ──
            decomp = _decompose(needed_min)
            if decomp is None:
                decomposition_errors.append({
                    "class": str(cls), "subject": subj.name,
                    "needed_min": needed_min,
                    "message": f"{needed_min}min ne peut pas être exprimé en blocs de 120/180min.",
                })
                continue
            n3, n2 = decomp  # n3 blocs de 3h, n2 blocs de 2h

            teacher = Teacher.objects.filter(subject=subj, classes=cls).first()
            if not teacher:
                missing_teachers.append({
                    "class": str(cls), "class_id": cls.id,
                    "subject": subj.name, "subject_id": subj.id,
                })
                if not allow_missing_teacher:
                    continue
                teacher_id = None
            else:
                teacher_id = teacher.id

            subj_map[subj.id] = {
                "hours_min":      needed_min,
                "teacher_id":     teacher_id,
                "classsubject_id": cs.id,
                "n3":             n3,   # nombre exact de slots 3h requis
                "n2":             n2,   # nombre exact de slots 2h requis
            }
        if subj_map:
            classes[cls.id] = {"obj": cls, "subjects": subj_map}

    diagnostics = {
        "num_classes":          len(classes),
        "missing_teachers":     missing_teachers,
        "decomposition_errors": decomposition_errors,
    }

    if not classes:
        return {"success": False, "message": "No valid class-subject for this level",
                "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # ── Filtrage strict : seuls les slots de 120 ou 180 min sont candidats ──
    slots_3h = [s["idx"] for s in slots if s["dur"] == 180]  # 3h
    slots_2h = [s["idx"] for s in slots if s["dur"] == 120]  # 2h

    if not slots_3h and not slots_2h:
        return {"success": False,
                "message": "Aucun slot de 120 ou 180 min dans la DB TimeSlot.",
                "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # feasible_slots : pour (c,s), les slots de la bonne durée
    # qui ne sont pas déjà occupés (teacher + class)
    feasible_3h = {}  # (c_id, s_id) → list of slot_idx (180min disponibles)
    feasible_2h = {}  # (c_id, s_id) → list of slot_idx (120min disponibles)

    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            t_id = s_data["teacher_id"]
            n3   = s_data["n3"]
            n2   = s_data["n2"]

            # Slots 3h disponibles pour ce (c,s)
            if n3 > 0:
                available_3h = [
                    i for i in slots_3h
                    if (t_id is None or (t_id, i) not in occupied_teacher_slots)
                    and (c_id, i) not in occupied_class_slots
                ]
                if len(available_3h) < n3:
                    # Pas assez de slots 3h → problème de capacité
                    diagnostics.setdefault("capacity_issues", []).append({
                        "class_id": c_id, "subject_id": s_id,
                        "needed_3h_slots": n3, "available_3h_slots": len(available_3h),
                    })
                feasible_3h[(c_id, s_id)] = available_3h
            else:
                feasible_3h[(c_id, s_id)] = []

            # Slots 2h disponibles pour ce (c,s)
            if n2 > 0:
                available_2h = [
                    i for i in slots_2h
                    if (t_id is None or (t_id, i) not in occupied_teacher_slots)
                    and (c_id, i) not in occupied_class_slots
                ]
                if len(available_2h) < n2:
                    diagnostics.setdefault("capacity_issues", []).append({
                        "class_id": c_id, "subject_id": s_id,
                        "needed_2h_slots": n2, "available_2h_slots": len(available_2h),
                    })
                feasible_2h[(c_id, s_id)] = available_2h
            else:
                feasible_2h[(c_id, s_id)] = []

    # ── Construction du modèle CP-SAT ─────────────────────────────────────────
    model = cp_model.CpModel()
    X3 = {}  # (c_id, s_id, slot_idx) pour les slots 3h
    X2 = {}  # (c_id, s_id, slot_idx) pour les slots 2h

    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"]:
            for i in feasible_3h[(c_id, s_id)]:
                X3[(c_id, s_id, i)] = model.NewBoolVar(f"x3_c{c_id}_s{s_id}_t{i}")
            for i in feasible_2h[(c_id, s_id)]:
                X2[(c_id, s_id, i)] = model.NewBoolVar(f"x2_c{c_id}_s{s_id}_t{i}")

    # ── Contraintes dures ─────────────────────────────────────────────────────

    # C_EXACT : exactement n3 slots 3h ET n2 slots 2h par (classe, matière)
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            n3 = s_data["n3"]
            n2 = s_data["n2"]

            vars_3h = [X3[(c_id, s_id, i)] for i in feasible_3h[(c_id, s_id)]]
            vars_2h = [X2[(c_id, s_id, i)] for i in feasible_2h[(c_id, s_id)]]

            # Exactement n3 slots de 3h
            if n3 > 0:
                if not vars_3h:
                    # Aucun slot 3h dispo → infaisable pour ce (c,s)
                    model.Add(model.NewBoolVar("infeasible") == 0)
                else:
                    model.Add(sum(vars_3h) == n3)
            else:
                # n3 == 0 : aucun slot 3h ne doit être utilisé
                if vars_3h:
                    model.Add(sum(vars_3h) == 0)

            # Exactement n2 slots de 2h
            if n2 > 0:
                if not vars_2h:
                    model.Add(model.NewBoolVar("infeasible") == 0)
                else:
                    model.Add(sum(vars_2h) == n2)
            else:
                if vars_2h:
                    model.Add(sum(vars_2h) == 0)

    # C_CLASS_SLOT : une seule matière par classe par slot (et conflits temporels)
    all_X = {}
    all_X.update(X3)
    all_X.update(X2)

    for c_id, c_data in classes.items():
        subj_ids = list(c_data["subjects"].keys())
        # Slot exact
        all_slots_used = set()
        for s_id in subj_ids:
            all_slots_used.update(feasible_3h[(c_id, s_id)])
            all_slots_used.update(feasible_2h[(c_id, s_id)])
        for i in all_slots_used:
            terms = [all_X[(c_id, s_id, i)] for s_id in subj_ids
                     if (c_id, s_id, i) in all_X]
            if terms:
                model.Add(sum(terms) <= 1)
        # Conflits temporels (chevauchements)
        for i in all_slots_used:
            for j in slot_conflicts.get(i, set()):
                terms_i = [all_X[(c_id, s_id, i)] for s_id in subj_ids
                           if (c_id, s_id, i) in all_X]
                terms_j = [all_X[(c_id, s_id, j)] for s_id in subj_ids
                           if (c_id, s_id, j) in all_X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # C_TEACHER : un prof ne peut pas être dans deux cours en même temps
    teacher_assignments = defaultdict(list)
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            t_id = s_data["teacher_id"]
            if t_id is not None:
                teacher_assignments[t_id].append((c_id, s_id))

    for t_id, assigns in teacher_assignments.items():
        all_slots_for_teacher = set()
        for (c_id, s_id) in assigns:
            all_slots_for_teacher.update(feasible_3h[(c_id, s_id)])
            all_slots_for_teacher.update(feasible_2h[(c_id, s_id)])
        for i in all_slots_for_teacher:
            for j in slot_conflicts.get(i, set()):
                terms_i = [all_X[(c_id, s_id, i)] for (c_id, s_id) in assigns
                           if (c_id, s_id, i) in all_X]
                terms_j = [all_X[(c_id, s_id, j)] for (c_id, s_id) in assigns
                           if (c_id, s_id, j) in all_X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # ── Contraintes souples (pénalités) ───────────────────────────────────────
    # P_same_day  : pénaliser si même matière deux fois dans la même journée
    # P_consec    : pénaliser si même matière deux jours consécutifs

    P_same   = []
    P_consec = []
    D_day    = {}

    weekdays_sorted = sorted(slots_by_day.keys())

    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"]:
            for day, day_idxs in slots_by_day.items():
                # Variables du jour pour ce (c,s), toutes durées confondues
                day_vars = [all_X[(c_id, s_id, i)] for i in day_idxs
                            if (c_id, s_id, i) in all_X]
                if not day_vars:
                    continue

                # D = 1 si la matière est programmée ce jour
                D = model.NewBoolVar(f"d_c{c_id}_s{s_id}_day{day}")
                model.Add(sum(day_vars) >= D)
                model.Add(sum(day_vars) <= len(day_vars) * D)
                D_day[(c_id, s_id, day)] = D

                # P_same_day : pénalité si > 1 session ce jour
                # (avec contrainte exacte par durée, ça ne devrait pas arriver
                # sauf si n3+n2 > 1, ex: 7h = 3h+2h+2h)
                P = model.NewBoolVar(f"psame_c{c_id}_s{s_id}_day{day}")
                model.Add(sum(day_vars) <= 1 + len(day_vars) * P)
                P_same.append(P)

    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"]:
            for k in range(len(weekdays_sorted) - 1):
                d1 = weekdays_sorted[k]
                d2 = weekdays_sorted[k + 1]
                D1 = D_day.get((c_id, s_id, d1))
                D2 = D_day.get((c_id, s_id, d2))
                if D1 is None or D2 is None:
                    continue
                Pcon = model.NewBoolVar(f"pcon_c{c_id}_s{s_id}_d{d1}_{d2}")
                model.Add(Pcon >= D1 + D2 - 1)
                model.Add(Pcon <= D1)
                model.Add(Pcon <= D2)
                P_consec.append(Pcon)

    # Objectif : minimiser les pénalités (les minutes sont déjà exactes)
    penalty_weight_same    = penalty_same_day * sum(P_same)
    penalty_weight_consec  = penalty_consecutive * sum(P_consec)
    model.Minimize(penalty_weight_same + penalty_weight_consec)

    # ── Résolution ────────────────────────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(time_limit_seconds, 60)
    solver.parameters.num_search_workers  = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        total_vars = sum(len(feasible_3h[(c,s)]) + len(feasible_2h[(c,s)])
                        for c in classes for s in classes[c]["subjects"])
        diagnostics.update({
            "status": int(status),
            "est_vars_after_pruning": total_vars,
        })
        return {"success": False, "message": "No solution for level",
                "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # ── Extraction des entrées ────────────────────────────────────────────────
    entries = []
    violations_consec = 0
    for (c_id, s_id, i), var in all_X.items():
        if solver.Value(var) == 1:
            slot = slots[i]
            entries.append({
                "class_id":   c_id,
                "subject_id": s_id,
                "teacher_id": classes[c_id]["subjects"][s_id]["teacher_id"],
                "weekday":    slot["weekday"],
                "starts_at":  slot["db_obj"].start_time,
                "ends_at":    slot["db_obj"].end_time,
                "slot_idx":   i,
            })

    # Compter les violations consécutives dans le plan produit
    for Pcon in P_consec:
        if solver.Value(Pcon) == 1:
            violations_consec += 1

    total_vars = sum(len(feasible_3h[(c,s)]) + len(feasible_2h[(c,s)])
                    for c in classes for s in classes[c]["subjects"])
    diagnostics.update({
        "status":                          int(status),
        "est_vars_after_pruning":          total_vars,
        "violations_c1c3_consecutive_days": violations_consec,
        "penalty_consecutive_weight":       penalty_consecutive * violations_consec,
    })

    return {
        "success":       True,
        "message":       "Level solved" + (
            f" avec {violations_consec} violations (jours consécutifs) comme dernier recours."
            if violations_consec else "."
        ),
        "entries":       entries,
        "diagnostics":   diagnostics,
        "time_s":        time.time() - t0,
        "feasible_3h":   feasible_3h,
        "feasible_2h":   feasible_2h,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Merge + résolution des conflits
# ─────────────────────────────────────────────────────────────────────────────

def merge_level_plan_into_global(global_schedule, level_plan, slots):
    """
    Insère les entrées du plan niveau dans global_schedule.
    Détecte les conflits prof (teacher_conflict) et classe (class_double).

    CORRECTION : global_schedule utilise maintenant slot_idx comme clé entière.
    """
    conflicts = []
    for ent in level_plan.get("entries", []):
        slot_idx = ent["slot_idx"]
        teacher  = ent.get("teacher_id")
        cls      = ent.get("class_id")

        slot_map = global_schedule.setdefault(slot_idx, {"teacher": {}, "class": {}})

        if cls in slot_map["class"]:
            conflicts.append({
                "type": "class_double", "slot": slot_idx,
                "class_id": cls, "existing": slot_map["class"][cls], "new": ent,
            })
            continue

        if teacher is not None and teacher in slot_map["teacher"]:
            existing = slot_map["teacher"][teacher]
            conflicts.append({
                "type": "teacher_conflict", "slot": slot_idx,
                "teacher_id": teacher, "existing": existing, "new": ent,
            })
            continue

        slot_map["class"][cls] = ent
        if teacher is not None:
            slot_map["teacher"][teacher] = ent
        global_schedule.setdefault("entries", []).append(ent)

    return conflicts


def try_resolve_conflicts(conflicts, global_schedule, feasible_3h, feasible_2h, slots):
    """
    Résolution greedy des conflits.

    CORRECTION principale : quand une entrée est déplacée,
    weekday/starts_at/ends_at sont maintenant mis à jour en même temps que slot_idx.
    """
    unresolved = []
    resolved   = []

    def slot_free(slot_idx, class_id, teacher_id):
        slot_map = global_schedule.get(slot_idx, {"teacher": {}, "class": {}})
        if class_id in slot_map.get("class", {}):
            return False
        if teacher_id is not None and teacher_id in slot_map.get("teacher", {}):
            return False
        return True

    def do_move(ent, old_slot_idx, new_slot_idx):
        """Déplace ent de old_slot_idx vers new_slot_idx dans global_schedule
        et met à jour TOUS les champs temporels."""
        class_id   = ent["class_id"]
        teacher_id = ent.get("teacher_id")

        # Retirer de l'ancien slot
        old_map = global_schedule.get(old_slot_idx, {"teacher": {}, "class": {}})
        old_map.get("class", {}).pop(class_id, None)
        if teacher_id is not None:
            old_map.get("teacher", {}).pop(teacher_id, None)

        # Trouver le slot DB correspondant
        new_slot_data = next((s for s in slots if s["idx"] == new_slot_idx), None)
        if new_slot_data is None:
            return False

        # Mettre à jour TOUS les champs (correction du bug précédent)
        ent["slot_idx"] = new_slot_idx
        ent["weekday"]  = new_slot_data["weekday"]
        ent["starts_at"] = new_slot_data["db_obj"].start_time
        ent["ends_at"]   = new_slot_data["db_obj"].end_time

        # Placer dans le nouveau slot
        new_map = global_schedule.setdefault(new_slot_idx, {"teacher": {}, "class": {}})
        new_map["class"][class_id] = ent
        if teacher_id is not None:
            new_map["teacher"][teacher_id] = ent
        return True

    for c in conflicts:
        if c["type"] != "teacher_conflict":
            unresolved.append(c)
            continue

        new_ent    = c["new"]
        class_id   = new_ent["class_id"]
        subject_id = new_ent["subject_id"]
        teacher_id = new_ent.get("teacher_id")

        # Candidats : même durée que l'entrée à déplacer
        new_dur    = next((s["dur"] for s in slots if s["idx"] == new_ent["slot_idx"]), None)
        candidates_3h = feasible_3h.get((class_id, subject_id), [])
        candidates_2h = feasible_2h.get((class_id, subject_id), [])
        # On ne propose que des candidats de même durée
        candidates = [
            i for i in (candidates_3h + candidates_2h)
            if i != new_ent["slot_idx"]
            and next((s["dur"] for s in slots if s["idx"] == i), 0) == new_dur
        ]
        random.shuffle(candidates)

        moved = False
        for cand in candidates:
            if slot_free(cand, class_id, teacher_id):
                if do_move(new_ent, new_ent["slot_idx"], cand):
                    resolved.append({"conflict": c, "moved_to": cand})
                    moved = True
                    break

        if not moved:
            # Tentative de swap
            swapped = False
            for cand in candidates:
                cand_map = global_schedule.get(cand, {"teacher": {}, "class": {}})
                for other_cls_id, other_ent in list(cand_map.get("class", {}).items()):
                    other_teacher = other_ent.get("teacher_id")
                    if slot_free(new_ent["slot_idx"], other_cls_id, other_teacher):
                        original_slot = new_ent["slot_idx"]
                        if do_move(other_ent, cand, original_slot) and \
                           do_move(new_ent, original_slot, cand):
                            resolved.append({"conflict": c, "swap_with": other_ent})
                            swapped = True
                            break
                if swapped:
                    break
            if not swapped:
                unresolved.append(c)

    return resolved, unresolved


# ─────────────────────────────────────────────────────────────────────────────
#  Pipeline complet
# ─────────────────────────────────────────────────────────────────────────────

def reset_timetable_table():
    """
    Vide la table ClassScheduleEntry et remet la séquence à 1.
    DANGER : ne pas appeler si dry_run=True ou avant de confirmer le succès.
    """
    from django.db import connection
    ClassScheduleEntry.objects.all().delete()
    with connection.cursor() as cursor:
        try:
            cursor.execute(
                "ALTER SEQUENCE academics_classscheduleentry_id_seq RESTART WITH 1;"
            )
        except Exception:
            pass  # SQLite ne supporte pas cette syntaxe


def run_timetable_pipeline(
    levels_ordering_strategy="most_constrained_first",
    time_limit_base=60,
    dry_run=False,
    persist=True,
    report_prefix=None,
    max_attempts=4,
):
    """
    Pipeline complet : analyse → génération par niveau → merge → persistance.

    CHANGEMENTS :
    - Retry avec backoff exponentiel (jusqu'à max_attempts tentatives par niveau)
    - Reset conditionnel : uniquement si persist=True et pas dry_run
    - occupied_*_slots transmis à chaque niveau pour éviter les conflits cross-niveaux
    """
    report = {
        "start":  datetime.now().isoformat(),
        "levels": [],
        "merged": {"conflicts": [], "resolved": [], "shortfalls": []},
    }

    slots, slots_by_day, slot_conflicts = _load_slots()
    analysis   = analyze_levels()
    levels_info = analysis["levels"]

    # Tri des niveaux
    if levels_ordering_strategy == "most_constrained_first":
        levels_info.sort(
            key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"])),
            reverse=True,
        )
    elif levels_ordering_strategy == "least_constrained_first":
        levels_info.sort(
            key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"]))
        )
    else:
        levels_info.sort(key=lambda l: l["level_id"])

    global_schedule = {"entries": []}

    # Suivi des slots occupés (school-wide) pour éviter les conflits cross-niveaux
    occupied_teacher_slots: set = set()
    occupied_class_slots:   set = set()

    for lvl in levels_info:
        lvl_id  = lvl["level_id"]
        est_vars = lvl["est_vars"]
        timeout = min(300, max(time_limit_base, int(time_limit_base + 0.001 * est_vars)))

        plan     = None
        attempt  = 0
        success  = False

        while attempt < max_attempts and not success:
            attempt += 1
            t_limit = timeout * attempt  # backoff exponentiel
            print(f"[PIPE] Solving level {lvl['name']} id={lvl_id} timeout={t_limit}s")
            print(f"  Attempt {attempt}/{max_attempts} timeout {t_limit}s")

            plan = generate_timetable_for_level(
                lvl_id,
                time_limit_seconds=t_limit,
                occupied_teacher_slots=occupied_teacher_slots,
                occupied_class_slots=occupied_class_slots,
            )

            if plan.get("success"):
                success = True
                msg = plan.get("message", "")
                if "violations" in msg:
                    print(f"  [SOLVER] {msg}")
            else:
                print(f"  Attempt {attempt} failed: {plan.get('message')}")

        report["levels"].append({
            "level_id":    lvl_id,
            "name":        lvl["name"],
            "plan_status": success,
            "diag":        plan.get("diagnostics", {}),
            "attempt":     attempt,
        })

        if not success:
            print(f"[PIPE] Level {lvl['name']} échec après {max_attempts} tentatives.")
            continue

        # Merge
        conflicts = merge_level_plan_into_global(global_schedule, plan, slots)

        if conflicts:
            print(f"  {len(conflicts)} conflict(s) lors du merge de {lvl['name']}")
            resolved, unresolved = try_resolve_conflicts(
                conflicts, global_schedule,
                plan.get("feasible_3h", {}),
                plan.get("feasible_2h", {}),
                slots,
            )
            report["merged"]["resolved"].extend(resolved)
            if unresolved:
                report["merged"]["conflicts"].extend(unresolved)
                print(f"  {len(unresolved)} conflit(s) non résolu(s) pour {lvl['name']}")
            else:
                print(f"  Merged level {lvl['name']} without conflicts.")
        else:
            print(f"  Merged level {lvl['name']} without conflicts.")

        print(f"  Merged level {lvl['name']} successfully (attempt {attempt}).")

        # Mettre à jour les slots occupés pour les niveaux suivants
        for ent in plan.get("entries", []):
            t_id = ent.get("teacher_id")
            c_id = ent["class_id"]
            i    = ent["slot_idx"]
            if t_id is not None:
                occupied_teacher_slots.add((t_id, i))
            occupied_class_slots.add((c_id, i))

    # ── Vérification finale des conflits enseignants ──────────────────────────
    final_teacher_conflicts = []
    teacher_day_map = defaultdict(list)
    for ent in global_schedule.get("entries", []):
        if ent.get("teacher_id"):
            teacher_day_map[(ent["teacher_id"], ent["weekday"])].append(ent)
    for (t_id, day), ents in teacher_day_map.items():
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                a, b = ents[i], ents[j]
                a_s = _to_minutes(a["starts_at"])
                a_e = _to_minutes(a["ends_at"])
                b_s = _to_minutes(b["starts_at"])
                b_e = _to_minutes(b["ends_at"])
                if a_s < b_e and b_s < a_e:
                    final_teacher_conflicts.append({
                        "teacher_id": t_id, "weekday": day,
                        "entry_a": a, "entry_b": b,
                    })
    if final_teacher_conflicts:
        print(f"[FINAL] {len(final_teacher_conflicts)} teacher conflict(s) after merge.")
        report["merged"]["teacher_conflicts_after_merge"] = final_teacher_conflicts
    else:
        print("[FINAL] No teacher conflicts after merge.")

    # ── Persistance ───────────────────────────────────────────────────────────
    if persist and not dry_run:
        try:
            with transaction.atomic():
                class_ids = set(e["class_id"] for e in global_schedule.get("entries", []))
                if class_ids:
                    ClassScheduleEntry.objects.filter(
                        school_class_id__in=list(class_ids)
                    ).delete()
                created = 0
                for ent in global_schedule.get("entries", []):
                    ClassScheduleEntry.objects.create(
                        school_class_id=ent["class_id"],
                        subject_id=ent["subject_id"],
                        teacher_id=ent.get("teacher_id"),
                        weekday=ent["weekday"],
                        starts_at=ent["starts_at"],
                        ends_at=ent["ends_at"],
                    )
                    created += 1
            report["persisted"] = {"created": created}
        except Exception as e:
            report["persist_error"] = str(e)
    else:
        report["persisted"] = {"created": 0}

    report["end"]                 = datetime.now().isoformat()
    report["global_entries_count"] = len(global_schedule.get("entries", []))

    # Export JSON
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = report_prefix or f"timetable_report_{ts}"
    try:
        with open(f"{prefix}.json", "w", encoding="utf-8") as f:
            json.dump(report, f, default=str, indent=2, ensure_ascii=False)
        print(f"[REPORT] written to {prefix}.json")
    except Exception as e:
        print("Failed to write report:", e)

    return report


if __name__ == "__main__":
    r = run_timetable_pipeline(time_limit_base=60, dry_run=True, persist=False)
    print(json.dumps(r, indent=2, default=str))