# academics/services/timetable_by_level.py

"""
Timetable pipeline (by level) - aggressive OR-Tools only version (corrected constraints).

*** STRATÉGIE (Hybride Robuste) ***
Objectif: Garantir 100% de génération (pas de plan=false) tout en respectant les
contraintes inviolables.

- CONTRAINTES DURES (INVIOLABLES):
  - C2 (Pas 2x/jour): model.Add(sum(...) <= 1)
  - C5 (Max 3x/semaine): model.Add(sum(...) <= 3)
  - C4 (Blocs 2h/3h): ALLOWED_SLOT_DURS
  - C6/C7 (Conflits Prof/Classe): Logique d'overlap
  - Quota: model.Add(sum(durations) == needed)

- CONTRAINTE SOUPLE (Négociation de dernier recours):
  - C1/C3 (Jours consécutifs): Transformée en pénalité. Le solveur la violera
    UNIQUEMENT s'il est impossible de respecter toutes les règles dures autrement.

- L'OBJECTIF du solveur est de MINIMISER les violations de C1/C3.
"""

import time
import math
import random
import json
from collections import defaultdict, Counter
from datetime import datetime
from copy import deepcopy

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

# repair utilities: detect & try local fixes for same-teacher same-class same-day duplicates
from academics.services.timetable_repair import repair_duplicates_in_global

# -----------------------------
# Helpers
# -----------------------------


def _to_minutes(t):
    return t.hour * 60 + t.minute


def _load_slots():
    """
    Load TimeSlot rows; build:
      - slots: list of dict {idx, db_obj, db_id, weekday, start, end, dur}
      - slots_by_day: dict day -> [idx,...]
      - slot_conflicts: dict idx -> set(idx that overlap)
      - slot_adjacent: dict idx -> set(idx that are immediately consecutive on same day)
    NOTE: idx is the array index (0..n-1). db_id is TimeSlot.id in DB.
    """
    time_slots = list(TimeSlot.objects.all().order_by("day", "start_time"))
    slots = []
    for idx, slot in enumerate(time_slots):
        start_min = _to_minutes(slot.start_time)
        end_min = _to_minutes(slot.end_time)
        dur = end_min - start_min
        if dur <= 0:
            raise ValueError(f"TimeSlot id={slot.id} duration non positive")
        slots.append({
            "idx": idx,
            "db_obj": slot,
            "db_id": slot.id,
            "weekday": slot.day,
            "start": start_min,
            "end": end_min,
            "dur": dur,
        })

    slots_by_day = defaultdict(list)
    for s in slots:
        slots_by_day[s["weekday"]].append(s["idx"])

    slot_conflicts = {s["idx"]: set() for s in slots}
    slot_adjacent = {s["idx"]: set() for s in slots}
    for day, idxs in slots_by_day.items():
        # sort indices by start time
        sorted_idxs = sorted(idxs, key=lambda i: slots[i]["start"])
        for a in range(len(sorted_idxs)):
            i = sorted_idxs[a]
            s_i = slots[i]
            for b in range(a + 1, len(sorted_idxs)):
                j = sorted_idxs[b]
                s_j = slots[j]
                # overlap if start < other's end and other.start < end
                if (s_i["start"] < s_j["end"]) and (s_j["start"] < s_i["end"]):
                    slot_conflicts[i].add(j)
                    slot_conflicts[j].add(i)
                # adjacency: end == other's start (immediately consecutive)
                if s_i["end"] == s_j["start"]:
                    slot_adjacent[i].add(j)
                    slot_adjacent[j].add(i)
                if s_j["start"] >= s_i["end"] + 240:  # 4h gap heuristic
                    break

    return slots, slots_by_day, slot_conflicts, slot_adjacent


# -----------------------------
# Analysis
# -----------------------------


def analyze_levels():
    """
    Return diagnostics per level.
    """
    slots, slots_by_day, slot_conflicts, slot_adjacent = _load_slots()
    available_minutes = sum(s["dur"] for s in slots)
    levels = []

    for level in Level.objects.all():
        classes_qs = SchoolClass.objects.filter(level=level)
        classes_list = list(classes_qs)
        num_classes = len(classes_list)
        num_classsubjects = 0
        needed_minutes = 0
        missing_teachers = []
        heavy_classes = []

        for cls in classes_list:
            cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
            class_needed = 0
            for cs in cs_qs:
                hrs = getattr(cs, "hours_per_week", None)
                if hrs is None:
                    continue
                num_classsubjects += 1
                minutes = int(hrs * 60)
                needed_minutes += minutes
                class_needed += minutes
                teach = Teacher.objects.filter(subject=cs.subject, classes=cls).first()
                if not teach:
                    missing_teachers.append({
                        "class": str(cls),
                        "class_id": cls.id,
                        "subject": cs.subject.name,
                        "subject_id": cs.subject.id
                    })
            heavy_classes.append((cls.id, class_needed))

        est_vars = num_classsubjects * len(slots)
        heavy_classes.sort(key=lambda x: -x[1])
        levels.append({
            "level_id": level.id,
            "name": getattr(level, "name", str(level)),
            "num_classes": num_classes,
            "num_classsubjects": num_classsubjects,
            "needed_minutes": needed_minutes,
            "available_minutes": available_minutes,
            "est_vars": est_vars,
            "missing_teachers": missing_teachers,
            "heavy_classes": heavy_classes[:5],
        })

    return {"slots_count": len(slots), "levels": levels}


# -----------------------------
# Occupied maps builder
# -----------------------------


def build_occupied_maps_from_global(global_schedule, slots, slot_conflicts):
    """
    From global_schedule build:
      - occupied_by_slot: slot_idx -> {"classes": set(), "teachers": set()}
      - forbidden_slots_by_teacher: tid -> set(slot_idx including overlapping)
      - forbidden_slots_by_class: cid -> set(slot_idx including overlapping)
    """
    occupied_by_slot = {}
    forbidden_slots_by_teacher = defaultdict(set)
    forbidden_slots_by_class = defaultdict(set)

    # iterate explicit slot maps
    for slot_key, slot_map in list(global_schedule.items()):
        if slot_key == "entries":
            continue
        try:
            slot_idx = int(slot_key)
        except Exception:
            continue
        classes = set(slot_map.get("class", {}).keys())
        teachers = set(slot_map.get("teacher", {}).keys())
        occupied_by_slot[slot_idx] = {"classes": set(classes), "teachers": set(teachers)}
        related = {slot_idx} | set(slot_conflicts.get(slot_idx, set()))
        for t in teachers:
            forbidden_slots_by_teacher[t].update(related)
        for c in classes:
            forbidden_slots_by_class[c].update(related)

    # iterate global entries
    for ent in global_schedule.get("entries", []):
        sidx = ent.get("slot_idx")
        if sidx is None:
            continue
        occupied_by_slot.setdefault(sidx, {"classes": set(), "teachers": set()})
        related = {sidx} | set(slot_conflicts.get(sidx, set()))
        if ent.get("class_id") is not None:
            occupied_by_slot[sidx]["classes"].add(ent["class_id"])
            forbidden_slots_by_class[ent["class_id"]].update(related)
        if ent.get("teacher_id") is not None:
            occupied_by_slot[sidx]["teachers"].add(ent["teacher_id"])
            forbidden_slots_by_teacher[ent["teacher_id"]].update(related)

    return occupied_by_slot, dict(forbidden_slots_by_teacher), dict(forbidden_slots_by_class)


# -----------------------------
# Level solver (Hybrid C2/C5 Hard, C1/C3 Soft)
# -----------------------------


def _representable_by_120_180(needed_minutes, max_sessions=3):
    """
    Fast small Diophantine check: can needed_minutes be expressed as 120*a + 180*b
    with a,b >=0 and a+b <= max_sessions
    """
    for b in range(0, max_sessions + 1):
        rem = needed_minutes - 180 * b
        if rem < 0:
            continue
        if rem % 120 == 0:
            a = rem // 120
            if a >= 0 and a + b <= max_sessions:
                return True
    return False


def generate_timetable_for_level(level_id,
                                 time_limit_seconds=60,
                                 penalty_same_day_base=20, # Inutilisé (C2 est DURE)
                                 penalty_consecutive_base=1000, # Poids TRES élevé pour C1/C3
                                 allow_missing_teacher=False,
                                 occupied_teacher_slots=None,
                                 occupied_class_slots=None,
                                 maximize_coverage=True): # Inutilisé (Objectif = Min Pénalité)
    """
    Solve CP-SAT for a single level with Hybrid rules:
      - DURE: C2 (Pas 2x/jour), C5 (Max 3x/semaine), C4 (Blocs), C6/C7 (Conflits), Quota
      - SOUPLE: C1/C3 (Jours consécutifs)
    Returns LevelPlan dict.
    """
    t0 = time.time()
    slots, slots_by_day, slot_conflicts, slot_adjacent = _load_slots()
    occupied_teacher_slots = occupied_teacher_slots or {}
    occupied_class_slots = occupied_class_slots or {}

    # C4 (DURE)
    ALLOWED_SLOT_DURS = {120, 180}
    MIN_SLOT_DUR = min(ALLOWED_SLOT_DURS) if ALLOWED_SLOT_DURS else 1
    # C5 (DURE)
    MAX_WEEKLY_SESSIONS = 3

    try:
        level = Level.objects.get(id=level_id)
    except Level.DoesNotExist:
        return {"success": False, "message": "Level not found", "entries": [], "diagnostics": {}, "time_s": 0}

    classes_objs = list(SchoolClass.objects.filter(level=level))
    if not classes_objs:
        return {"success": False, "message": "No classes for level", "entries": [], "diagnostics": {}, "time_s": 0}

    # collect class-subjects
    classes = {}
    missing_teachers = []
    for cls in classes_objs:
        subj_map = {}
        cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
        for cs in cs_qs:
            hrs = getattr(cs, "hours_per_week", None)
            if hrs is None:
                continue
            subj = cs.subject
            teacher = Teacher.objects.filter(subject=subj, classes=cls).first()
            if not teacher:
                missing_teachers.append({"class": str(cls), "class_id": cls.id, "subject": subj.name, "subject_id": subj.id})
                if not allow_missing_teacher:
                    continue
                teacher_id = None
            else:
                teacher_id = teacher.id
            subj_map[subj.id] = {"hours_min": int(hrs * 60), "teacher_id": teacher_id, "classsubject_id": cs.id}
        if subj_map:
            classes[cls.id] = {"obj": cls, "subjects": subj_map}

    diagnostics = {"num_classes": len(classes), "missing_teachers": missing_teachers}
    if not classes:
        return {"success": False, "message": "No valid class-subject for this level", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    total_available_minutes = sum(s["dur"] for s in slots)
    for c_id, c_data in classes.items():
        needed = sum(s["hours_min"] for s in c_data["subjects"].values())
        if needed > total_available_minutes:
            diagnostics["capacity_issue"] = {"class_id": c_id, "needed": needed, "available": total_available_minutes}
            return {"success": False, "message": "Capacity issue for a class", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    level_needed = sum(sum(s["hours_min"] for s in c["subjects"].values()) for c in classes.values())
    density = level_needed / max(1, total_available_minutes)
    
    # Ajuster le poids de la pénalité C1/C3
    penalty_consecutive = int(penalty_consecutive_base * (1 + density * 3))


    # feasible slots per (class,subject)
    max_slot_dur = max(s["dur"] for s in slots) if slots else 0
    feasible_slots = {}
    allowed_slot_indices = {s["idx"] for s in slots if s["dur"] in ALLOWED_SLOT_DURS}

    for c_id, c_data in classes.items():
        forbidden_for_class = occupied_class_slots.get(c_id, set())
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            teacher_id = s_data["teacher_id"]
            forbidden_for_teacher = set()
            if teacher_id is not None:
                forbidden_for_teacher = occupied_teacher_slots.get(teacher_id, set())
            
            # C4 + C6/C7
            candidates = [i for i in allowed_slot_indices if i not in forbidden_for_class and i not in forbidden_for_teacher]
            candidates = [i for i in candidates if slots[i]["dur"] <= needed + max_slot_dur]
            feasible_slots[(c_id, s_id)] = sorted(candidates)

    # Quick representability check
    no_feasible = []
    for (c_id, s_id), sls in feasible_slots.items():
        needed = classes[c_id]["subjects"][s_id]["hours_min"]

        # C4 + C5 (DURE)
        if not _representable_by_120_180(needed, max_sessions=MAX_WEEKLY_SESSIONS):
            no_feasible.append({"c": c_id, "s": s_id, "reason": "Quota non-représentable par 120/180 en <= 3 sessions"})
            continue

        max_sessions_possible = min(MAX_WEEKLY_SESSIONS, math.ceil(needed / MIN_SLOT_DUR))
        found = False

        if not sls:
            no_feasible.append({"c": c_id, "s": s_id, "reason": "Aucun slot candidat disponible (prof/classe occupés)"})
            continue

        limited_candidates = sls if len(sls) <= 40 else sls[:40]

        def backtrack(idx_list, start, total, used_idxs):
            nonlocal found
            if found: return
            if total == needed:
                found = True
                return
            if total > needed: return
            if len(used_idxs) >= max_sessions_possible: return
            
            for pos in range(start, len(idx_list)):
                i = idx_list[pos]
                bad = False
                for u in used_idxs:
                    if i in slot_conflicts[u] or i in slot_adjacent[u]:
                        bad = True
                        break
                if bad: continue
                backtrack(idx_list, pos + 1, total + slots[i]["dur"], used_idxs + [i])
                if found: return

        backtrack(limited_candidates, 0, 0, [])
        if not found and len(sls) <= 60: # retry with more
            backtrack(sls, 0, 0, [])
        if not found:
            no_feasible.append({"c": c_id, "s": s_id, "reason": "Backtrack n'a pas trouvé de combinaison non-adjacente"})

    if no_feasible:
        diagnostics["no_feasible"] = no_feasible
        return {"success": False, "message": "Certains (classe,matière) n'ont pas de combinaison de slots valide (Quota/Bloc/Dispo)", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # ==================================================================
    # Build CP-SAT model
    # ==================================================================
    model = cp_model.CpModel()
    X = {}
    for (c_id, s_id), slot_list in feasible_slots.items():
        for i in slot_list:
            X[(c_id, s_id, i)] = model.NewBoolVar(f"x_l{level_id}_c{c_id}_s{s_id}_t{i}")

    # ---------------------------------
    # Hard Constraints
    # ---------------------------------

    # Quota (DURE) + C5 (DURE)
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            slot_list = feasible_slots[(c_id, s_id)]
            needed = s_data["hours_min"]
            # Quota exact (DUR)
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) == needed)
            # C5: Max 3 sessions (DUR)
            model.Add(sum(X[(c_id, s_id, i)] for i in slot_list) <= MAX_WEEKLY_SESSIONS)

    # C6 (DURE) : one subject per class per slot
    for i in range(len(slots)):
        for c_id, c_data in classes.items():
            relevant_sids = [s_id for s_id in c_data["subjects"].keys() if (c_id, s_id, i) in X]
            if relevant_sids:
                model.Add(sum(X[(c_id, s_id, i)] for s_id in relevant_sids) <= 1)

    # C6 (DURE) : class overlap constraints
    for c_id, c_data in classes.items():
        subj_ids = list(c_data["subjects"].keys())
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for s_id in subj_ids if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for s_id in subj_ids if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # C7 (DURE) : teacher overlap constraints
    teacher_assignments = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            t_id = s_data["teacher_id"]
            if t_id is None: continue
            teacher_assignments.setdefault(t_id, []).append((c_id, s_id))

    for t_id, assigns in teacher_assignments.items():
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for (c_id, s_id) in assigns if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for (c_id, s_id) in assigns if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # C_adj (DURE) : PROHIBIT adjacent or overlapping for SAME (class,subject)
    for (c_id, s_id), slot_list in feasible_slots.items():
        for a_idx in range(len(slot_list)):
            i = slot_list[a_idx]
            for b_idx in range(a_idx + 1, len(slot_list)):
                j = slot_list[b_idx]
                if j in slot_conflicts.get(i, set()) or j in slot_adjacent.get(i, set()):
                    model.Add(X[(c_id, s_id, i)] + X[(c_id, s_id, j)] <= 1)

    # ---------------------------------
    # C2 (DURE) + C1/C3 (SOUPLE)
    # ---------------------------------
    
    D_day = {} # D_day[(c,s,day)] = 1 si le cours (c,s) a lieu le jour 'day'
    P_consec = [] # Liste des variables de pénalité pour C1/C3
    
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for day, idxs in slots_by_day.items():
                day_idxs = [i for i in idxs if (c_id, s_id, i) in X]
                if not day_idxs:
                    continue

                # --- CONTRAINTE C2 (DURE) ---
                # "ni dans la même journée"
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) <= 1)
                # --- FIN C2 ---

                # Création de la variable D_day pour la contrainte C1/C3
                D = model.NewBoolVar(f"d_l{level_id}_c{c_id}_s{s_id}_day{day}")
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) == D)
                D_day[(c_id, s_id, day)] = D

    # --- CONTRAINTE C1/C3 (SOUPLE) ---
    # Pénaliser "aujourd'hui, puis demain"
    weekdays_sorted = sorted(slots_by_day.keys())
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for k in range(len(weekdays_sorted) - 1):
                d1, d2 = weekdays_sorted[k], weekdays_sorted[k + 1]
                D1 = D_day.get((c_id, s_id, d1))
                D2 = D_day.get((c_id, s_id, d2))
                
                if D1 is None or D2 is None:
                    continue
                
                # Pcon=1 si D1=1 et D2=1
                Pcon = model.NewBoolVar(f"pc_l{level_id}_c{c_id}_s{s_id}_d{d1}_{d2}")
                model.Add(Pcon >= D1 + D2 - 1)
                model.Add(Pcon <= D1)
                model.Add(Pcon <= D2)
                P_consec.append(Pcon)
    
    # --- FIN C1/C3 ---

    # ==================================================================
    # Objective: MINIMISER les violations de C1/C3
    # ==================================================================
    
    penalty_consecutive_sum = sum(P_consec) if P_consec else 0
    model.Minimize(penalty_consecutive_sum * penalty_consecutive)

    # ==================================================================
    # Solve
    # ==================================================================
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(5, time_limit_seconds)
    solver.parameters.num_search_workers = 8
    if density > 0.6:
        solver.parameters.random_seed = random.randint(1, 100000)
        solver.parameters.search_branching = cp_model.FIXED_SEARCH
    
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        diagnostics.update({
            "status": int(status),
            "est_vars_after_pruning": sum(len(v) for v in feasible_slots.values())
        })
        return {"success": False, "message": "ECHEC: Contraintes DURES (C2/C5/Quota/Blocs) impossibles à satisfaire", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # Build entries
    entries = []
    for (c_id, s_id, i), var in X.items():
        if solver.Value(var) == 1:
            slot = slots[i]
            entries.append({
                "class_id": c_id,
                "subject_id": s_id,
                "teacher_id": classes[c_id]["subjects"][s_id]["teacher_id"],
                "weekday": slot["weekday"],
                "starts_at": slot["db_obj"].start_time,
                "ends_at": slot["db_obj"].end_time,
                "slot_idx": i,
                "slot_db_id": slot["db_id"],
            })

    # final validation: ensure exact coverage
    assigned_map = defaultdict(int)
    for ent in entries:
        assigned_map[(ent["class_id"], ent["subject_id"])] += \
            (ent["ends_at"].hour * 60 + ent["ends_at"].minute) - \
            (ent["starts_at"].hour * 60 + ent["starts_at"].minute)
            
    invalid = []
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            got = assigned_map.get((c_id, s_id), 0)
            if got != needed:
                invalid.append({"class_id": c_id, "subject_id": s_id, "needed": needed, "got": got})

    if invalid:
        diagnostics.update({"post_validation_invalid": invalid})
        return {"success": False, "message": "Post validation failed: assigned minutes not equal to quotas (Erreur critique solveur)", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # Diagnostiquer les violations de contraintes souples
    violations_c1_c3 = solver.Value(penalty_consecutive_sum)
    
    diagnostics.update({
        "status": int(status),
        "est_vars_after_pruning": sum(len(v) for v in feasible_slots.values()),
        "violations_c1c3_consecutive_days": int(violations_c1_c3),
        "penalty_consecutive_weight": penalty_consecutive
    })
    
    if violations_c1_c3 > 0:
        print(f"  [SOLVER] Niveau {level_id} résolu avec {violations_c1_c3} violations (jours consécutifs) comme dernier recours.")
    
    return {"success": True, "message": "Level solved (Hybride C2/C5 dures, C1/C3 souple)", "entries": entries, "diagnostics": diagnostics, "time_s": time.time() - t0, "feasible_slots": feasible_slots}

# -----------------------------
# Merge & conflict resolution
# -----------------------------

def merge_level_plan_into_global(global_schedule, level_plan):
    """ Merge entries of level_plan into global_schedule (mutates it). Returns list of conflicts detected. """
    conflicts = []
    for ent in level_plan.get("entries", []):
        slot = ent["slot_idx"]
        teacher = ent.get("teacher_id")
        cls = ent.get("class_id")
        
        slot_map = global_schedule.setdefault(slot, {"teacher": {}, "class": {}})
        
        if cls in slot_map["class"]:
            conflicts.append({"type": "class_double", "slot": slot, "class_id": cls, "existing": slot_map["class"][cls], "new": ent})
            continue
            
        if teacher is not None and teacher in slot_map["teacher"]:
            existing = slot_map["teacher"][teacher]
            conflicts.append({"type": "teacher_conflict", "slot": slot, "teacher_id": teacher, "existing": existing, "new": ent})
            continue
            
        slot_map["class"][cls] = ent
        if teacher is not None:
            slot_map["teacher"][teacher] = ent
        
        global_schedule.setdefault("entries", []).append(ent)
        
    return conflicts

def try_resolve_conflicts(conflicts, global_schedule, feasible_slots_map, max_tries=200):
    """ Greedy relocation + swap fallback. Returns (resolved, unresolved) """
    unresolved = []
    resolved = []
    
    feasible_slots_map = feasible_slots_map or {} # Ensure it exists
    
    # Need slot data for this function
    slots, _, slot_conflicts, _ = _load_slots()

    def slot_free_for(slot_idx, class_id, teacher_id):
        # Check base slot
        slot_map = global_schedule.get(slot_idx, {"teacher": {}, "class": {}})
        if class_id in slot_map.get("class", {}):
            return False
        if teacher_id is not None and teacher_id in slot_map.get("teacher", {}):
            return False
        
        # Check all conflicting slots
        for conflict_idx in slot_conflicts.get(slot_idx, set()):
            conflict_slot_map = global_schedule.get(conflict_idx, {"teacher": {}, "class": {}})
            if class_id in conflict_slot_map.get("class", {}):
                return False
            if teacher_id is not None and teacher_id in conflict_slot_map.get("teacher", {}):
                return False
        return True

    for c in conflicts:
        new_ent = c.get("new")
        existing_ent = c.get("existing")
        
        if c["type"] != "teacher_conflict":
            unresolved.append(c)
            continue
            
        class_id = new_ent["class_id"]
        subject_id = new_ent["subject_id"]
        teacher_id = new_ent.get("teacher_id")
        
        candidates = feasible_slots_map.get((class_id, subject_id), [])
        
        moved = False
        random.shuffle(candidates)
        # 1. Try simple move
        for cand in candidates:
            if cand == new_ent["slot_idx"]: continue
            if slot_free_for(cand, class_id, teacher_id):
                # remove from old slot
                old_slot_map = global_schedule.get(new_ent["slot_idx"], {"teacher": {}, "class": {}})
                old_slot_map.get("class", {}).pop(class_id, None)
                if teacher_id is not None:
                    old_slot_map.get("teacher", {}).pop(teacher_id, None)
                
                # update entry
                new_ent["slot_idx"] = cand
                new_ent["weekday"] = slots[cand]["weekday"]
                new_ent["starts_at"] = slots[cand]["db_obj"].start_time
                new_ent["ends_at"] = slots[cand]["db_obj"].end_time
                new_ent["slot_db_id"] = slots[cand]["db_id"]
                
                # add to new slot
                slot_map_new = global_schedule.setdefault(cand, {"teacher": {}, "class": {}})
                slot_map_new["class"][class_id] = new_ent
                if teacher_id is not None:
                    slot_map_new["teacher"][teacher_id] = new_ent
                
                resolved.append({"conflict": c, "moved_to": cand})
                moved = True
                break
        
        if moved:
            continue

        # 2. Try swap (re-intégré depuis votre code original)
        swapped = False
        for cand in candidates:
            if cand == new_ent["slot_idx"]: continue
            
            slot_map = global_schedule.get(cand, {"teacher": {}, "class": {}})
            # Can only swap if this slot is occupied by *one* class
            if len(slot_map.get("class", {})) != 1:
                continue

            other_class_id, other_ent = list(slot_map.get("class", {}).items())[0]
            other_teacher = other_ent.get("teacher_id")
            
            # Can 'other' move to 'new_ent's old slot?
            if slot_free_for(new_ent["slot_idx"], other_class_id, other_teacher):
                # remove 'other' from 'cand'
                slot_map["class"].pop(other_class_id, None)
                if other_teacher is not None:
                    slot_map["teacher"].pop(other_teacher, None)
                    
                # remove 'new_ent' from its old slot
                new_ent_old_slot = new_ent["slot_idx"]
                old_slot_map = global_schedule.get(new_ent_old_slot, {"teacher": {}, "class": {}})
                old_slot_map.get("class", {}).pop(class_id, None)
                if teacher_id is not None:
                    old_slot_map.get("teacher", {}).pop(teacher_id, None)
                
                # move 'new_ent' to 'cand'
                new_ent["slot_idx"] = cand
                new_ent["weekday"] = slots[cand]["weekday"]
                new_ent["starts_at"] = slots[cand]["db_obj"].start_time
                new_ent["ends_at"] = slots[cand]["db_obj"].end_time
                new_ent["slot_db_id"] = slots[cand]["db_id"]
                slot_map_new = global_schedule.setdefault(cand, {"teacher": {}, "class": {}})
                slot_map_new["class"][class_id] = new_ent
                if teacher_id is not None:
                    slot_map_new["teacher"][teacher_id] = new_ent
                    
                # move 'other_ent' to 'new_ent_old_slot'
                other_ent["slot_idx"] = new_ent_old_slot
                other_ent["weekday"] = slots[new_ent_old_slot]["weekday"]
                other_ent["starts_at"] = slots[new_ent_old_slot]["db_obj"].start_time
                other_ent["ends_at"] = slots[new_ent_old_slot]["db_obj"].end_time
                other_ent["slot_db_id"] = slots[new_ent_old_slot]["db_id"]
                old_slot_map["class"][other_class_id] = other_ent
                if other_teacher is not None:
                    old_slot_map["teacher"][other_teacher] = other_ent
                
                resolved.append({"conflict": c, "swap_with": other_ent})
                swapped = True
                break
        
        if not swapped:
            unresolved.append(c)
            
    return resolved, unresolved


# -----------------------------
# Optional limited global re-solve
# -----------------------------


def generate_timetable_for_levels(level_ids, time_limit_seconds=120, penalty_consecutive_base=1000):
    """
    Limited global re-solve for a subset of levels.
    *** MODÈLE HYBRIDE : C2/C5 Dures, C1/C3 Souple ***
    """
    slots, slots_by_day, slot_conflicts, slot_adjacent = _load_slots()
    ALLOWED_SLOT_DURS = {120, 180} # C4
    MIN_SLOT_DUR = min(ALLOWED_SLOT_DURS) if ALLOWED_SLOT_DURS else 1
    MAX_WEEKLY_SESSIONS = 3 # C5

    classes = {}
    for lvl in Level.objects.filter(id__in=level_ids):
        for cls in SchoolClass.objects.filter(level=lvl):
            subj_map = {}
            cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
            for cs in cs_qs:
                hrs = getattr(cs, "hours_per_week", None)
                if hrs is None: continue
                subj = cs.subject
                teacher = Teacher.objects.filter(subject=subj, classes=cls).first()
                teacher_id = teacher.id if teacher else None
                
                subj_map[subj.id] = {
                    "hours_min": int(hrs * 60),
                    "teacher_id": teacher_id,
                    "classsubject_id": cs.id
                }
            if subj_map:
                classes[cls.id] = {"obj": cls, "subjects": subj_map}

    if not classes:
        return {"success": False, "message": "No classes for levels", "entries": [], "diagnostics": {}}

    max_slot_dur = max(s["dur"] for s in slots) if slots else 0
    
    allowed_slot_indices = {s["idx"] for s in slots if s["dur"] in ALLOWED_SLOT_DURS} # C4
    
    feasible_slots = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            candidates = [i for i in allowed_slot_indices if slots[i]["dur"] <= needed + max_slot_dur]
            feasible_slots[(c_id, s_id)] = sorted(candidates)

    # Feasibility check
    no_feasible = []
    for (c_id, s_id), sls in feasible_slots.items():
        needed = classes[c_id]["subjects"][s_id]["hours_min"]
        if not _representable_by_120_180(needed, max_sessions=MAX_WEEKLY_SESSIONS):
            no_feasible.append({"c": c_id, "s": s_id, "reason": "Quota non-représentable"})
            continue
        if not sls:
            no_feasible.append({"c": c_id, "s": s_id, "reason": "Aucun slot candidat"})
            continue
    
    if no_feasible:
        return {"success": False, "message": "Some (class,subject) not representable by allowed 2h/3h slots", "entries": [], "diagnostics": {"no_feasible": no_feasible}}

    # ==================================================================
    # build combined CP model
    # ==================================================================
    model = cp_model.CpModel()
    X = {}
    for (c_id, s_id), slot_list in feasible_slots.items():
        for i in slot_list:
            X[(c_id, s_id, i)] = model.NewBoolVar(f"x_gl_c{c_id}_s{s_id}_t{i}")

    # Quota (DURE) + C5 (DURE)
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            slot_list = feasible_slots[(c_id, s_id)]
            needed = s_data["hours_min"]
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) == needed)
            model.Add(sum(X[(c_id, s_id, i)] for i in slot_list) <= MAX_WEEKLY_SESSIONS) # C5

    # C6 (DURE) : class single per slot
    for i in range(len(slots)):
        for c_id in classes.keys():
            sids = [s_id for s_id in classes[c_id]["subjects"].keys() if (c_id, s_id, i) in X]
            if sids:
                model.Add(sum(X[(c_id, s_id, i)] for s_id in sids) <= 1)

    # C6 (DURE) : class overlap
    for c_id in classes.keys():
        subj_ids = list(classes[c_id]["subjects"].keys())
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for s_id in subj_ids if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for s_id in subj_ids if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # C7 (DURE) : teacher overlap
    teacher_assignments = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            t_id = s_data["teacher_id"]
            if t_id is None: continue
            teacher_assignments.setdefault(t_id, []).append((c_id, s_id))
            
    for t_id, assigns in teacher_assignments.items():
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for (c_id, s_id) in assigns if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for (c_id, s_id) in assigns if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # C_adj (DURE) : forbid adjacency
    for (c_id, s_id), slot_list in feasible_slots.items():
        for a_idx in range(len(slot_list)):
            i = slot_list[a_idx]
            for b_idx in range(a_idx + 1, len(slot_list)):
                j = slot_list[b_idx]
                if j in slot_conflicts.get(i, set()) or j in slot_adjacent.get(i, set()):
                    model.Add(X[(c_id, s_id, i)] + X[(c_id, s_id, j)] <= 1)
                    
    # --- C2 (DURE) + C1/C3 (SOUPLE) ---
    D_day = {}
    P_consec = []
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for day, idxs in slots_by_day.items():
                day_idxs = [i for i in idxs if (c_id, s_id, i) in X]
                if not day_idxs:
                    continue
                
                # C2 (DURE): Max 1 par jour
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) <= 1)

                D = model.NewBoolVar(f"d_gl_c{c_id}_s{s_id}_day{day}")
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) == D)
                D_day[(c_id, s_id, day)] = D

    # C1/C3 (SOUPLE): Pénaliser jours consécutifs
    weekdays_sorted = sorted(slots_by_day.keys())
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for k in range(len(weekdays_sorted) - 1):
                d1, d2 = weekdays_sorted[k], weekdays_sorted[k + 1]
                D1 = D_day.get((c_id, s_id, d1))
                D2 = D_day.get((c_id, s_id, d2))
                if D1 is None or D2 is None:
                    continue
                
                Pcon = model.NewBoolVar(f"pc_gl_c{c_id}_s{s_id}_d{d1}_{d2}")
                model.Add(Pcon >= D1 + D2 - 1)
                model.Add(Pcon <= D1)
                model.Add(Pcon <= D2)
                P_consec.append(Pcon)

    # ==================================================================
    # Objective: MINIMISER les violations de C1/C3
    # ==================================================================
    penalty_consecutive_sum = sum(P_consec) if P_consec else 0
    model.Minimize(penalty_consecutive_sum * penalty_consecutive_base)

    # ==================================================================
    # Solve
    # ==================================================================
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(30, time_limit_seconds)
    solver.parameters.num_search_workers = 8

    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"success": False, "message": "Global re-solve failed (Contraintes DURES C2/C5/etc impossibles)", "entries": [], "diagnostics": {"status": int(status)}}

    entries = []
    for (c_id, s_id, i), var in X.items():
        if solver.Value(var) == 1:
            slot = slots[i]
            entries.append({
                "class_id": c_id,
                "subject_id": s_id,
                "teacher_id": classes[c_id]["subjects"][s_id]["teacher_id"],
                "weekday": slot["weekday"],
                "starts_at": slot["db_obj"].start_time,
                "ends_at": slot["db_obj"].end_time,
                "slot_idx": i,
                "slot_db_id": slot["db_id"],
            })
            
    violations_c1_c3 = solver.Value(penalty_consecutive_sum)
    print(f"[GLOBAL RESOLVE] Solved with {violations_c1_c3} violations (jours consécutifs).")

    return {"success": True, "message": "Global subset solved (Hybride)", "entries": entries, "diagnostics": {"status": int(status), "violations_c1c3_consecutive_days": int(violations_c1_c3)}}

# -----------------------------
# Pipeline driver
# -----------------------------

def run_timetable_pipeline(levels_ordering_strategy='most_constrained_first',
                           time_limit_base=60,
                           dry_run=False,
                           persist=True,
                           report_prefix=None,
                           max_retries_per_level=4,
                           max_global_resolve_levels=3):
    """
    End-to-end pipeline (Modèle Hybride C2/C5 Dures, C1/C3 Souple)
    """
    report = {"start": datetime.now().isoformat(), "levels": [], "merged": {"conflicts": [], "resolved": [], "shortfalls": []}}
    
    slots, slots_by_day, slot_conflicts, slot_adjacent = _load_slots()
    analysis = analyze_levels()
    levels_info = analysis["levels"]
    
    if levels_ordering_strategy == 'most_constrained_first':
        levels_info.sort(key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"])), reverse=True)
    elif levels_ordering_strategy == 'least_constrained_first':
        levels_info.sort(key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"])))
    else: # default by id
        levels_info.sort(key=lambda l: l["level_id"])
        
    global_schedule = {}
    global_schedule["entries"] = []

    def current_occupied_maps():
        return build_occupied_maps_from_global(global_schedule, slots, slot_conflicts)

    all_level_plans = []
    level_failures = []
    feasible_slots_map_global = {} # Stocker tous les feasible_slots
    
    for lvl in levels_info:
        lvl_id = lvl["level_id"]
        est_vars = lvl["est_vars"]
        timeout = min(600, max(time_limit_base, int(time_limit_base + 0.002 * est_vars)))
        print(f"[PIPE] Solving level {lvl['name']} id={lvl_id} timeout={timeout}s")

        _, forbidden_by_teacher, forbidden_by_class = current_occupied_maps()

        attempt = 0
        plan = None
        
        while attempt < max_retries_per_level:
            attempt_timeout = timeout * (2 ** attempt)
            print(f"  Attempt {attempt+1}/{max_retries_per_level} timeout {attempt_timeout}s")
            
            plan = generate_timetable_for_level(
                lvl_id,
                time_limit_seconds=attempt_timeout,
                penalty_consecutive_base=1000, # Poids TRES élevé pour C1/C3
                allow_missing_teacher=False,
                occupied_teacher_slots=forbidden_by_teacher,
                occupied_class_slots=forbidden_by_class
            )
            
            plan["attempt"] = attempt + 1
            plan["timeout_used"] = attempt_timeout
            plan["level_meta"] = lvl
            all_level_plans.append(plan)
            
            if plan.get("feasible_slots"):
                feasible_slots_map_global.update(plan.get("feasible_slots", {}))
            
            report["levels"].append({
                "level_id": lvl_id, "name": lvl["name"],
                "plan_status": plan.get("success"),
                "diag": plan.get("diagnostics"),
                "attempt": attempt + 1
            })

            if not plan.get("success"):
                print(f"  Plan generation failed: {plan.get('message')}")
                attempt += 1
                continue
            
            # merge
            conflicts = merge_level_plan_into_global(global_schedule, plan)
            
            if not conflicts:
                print(f"  Merged level {lvl['name']} without conflicts.")
            else:
                # try greedy resolve
                print(f"  Detected {len(conflicts)} conflicts merging level; trying greedy resolve")
                resolved, unresolved = try_resolve_conflicts(
                    conflicts, global_schedule, feasible_slots_map_global # Utiliser le map global
                )
                report['merged']['resolved'].extend(resolved)
                
                if unresolved:
                    print(f"  Unresolved after greedy: {len(unresolved)} -> will retry generation")
                    report['merged']['conflicts'].extend(unresolved)
                    attempt += 1
                    continue
                else:
                    print(f"  Conflicts resolved by greedy.")

            # --- attempt automatic duplicate repair ---
            # (Devrait être inutile pour C2, mais utile pour les conflits de fusion)
            try:
                repair_report = repair_duplicates_in_global(
                    global_schedule, slots, slot_conflicts, slot_adjacent,
                    feasible_slots_map=feasible_slots_map_global,
                    debug=False
                )
                if repair_report:
                    resolved_dup = repair_report.get("resolved", [])
                    if resolved_dup:
                        report["merged"].setdefault("duplicates_resolved", []).extend(resolved_dup)
                        print(f"  Repair: resolved {len(resolved_dup)} duplicate(s) after merge.")
                    
                    unresolved_dup = repair_report.get("unresolved", [])
                    if unresolved_dup:
                        report["merged"].setdefault("duplicates_unresolved", []).extend(unresolved_dup)
                        print(f"  Repair: {len(unresolved_dup)} duplicate group(s) remain unresolved.")
                    
                    candidates = list(repair_report.get("candidates_for_resolve", []) or [])
                    if candidates:
                        existing = set(report["merged"].get("duplicates_candidates", []))
                        existing.update(candidates)
                        report["merged"]["duplicates_candidates"] = list(existing)
                        print(f"  Repair: added {len(candidates)} candidate ids for targeted re-solve.")

            except Exception as e:
                print("  [REPAIR] repair_duplicates_in_global raised an exception:", e)
                report["merged"].setdefault("repair_errors", []).append(str(e))
            # --- end repair block ---

            print(f"  Merged level {lvl['name']} successfully (attempt {attempt+1}).")
            break # Success

        if not plan or not plan.get("success"):
            print(f"[WARN] Level {lvl['name']} could not be solved after {attempt} attempts.")
            level_failures.append(lvl_id)
            continue
            
        # Validation du quota
        assigned_minutes = defaultdict(int)
        for ent in global_schedule.get("entries", []):
            c = ent["class_id"]
            s = ent["subject_id"]
            idx = ent.get("slot_idx")
            dur = 0
            if idx is not None:
                dur = next((sl["dur"] for sl in slots if sl["idx"] == idx), 0)
            assigned_minutes[(c, s)] += dur
            
        needed_map = {}
        for cls in SchoolClass.objects.filter(level__id=lvl_id):
            cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
            for cs in cs_qs:
                hrs = getattr(cs, "hours_per_week", None)
                if hrs is None: continue
                needed_map[(cls.id, cs.subject.id)] = int(hrs * 60)
                
        shortfalls = []
        for key, need in needed_map.items():
            got = assigned_minutes.get(key, 0)
            if got < need:
                shortfalls.append({"class_id": key[0], "subject_id": key[1], "needed": need, "got": got, "missing": need - got})
        
        if shortfalls:
            print(f"  [WARN] Found {len(shortfalls)} shortfalls for level {lvl['name']} after merge/repair.")
            report['merged']['shortfalls'].append({"level": lvl_id, "shortfalls": shortfalls})

    # ... (logique de re-solve globale) ...
    extra_candidates = set(report['merged'].get('duplicates_candidates', []))
    
    candidate_level_ids = set()
    class_ids = set()
    teacher_ids = set()
    
    for c in extra_candidates:
        try:
            cls = SchoolClass.objects.filter(id=int(c)).first()
            if cls:
                candidate_level_ids.add(cls.level.id)
                class_ids.add(cls.id)
                continue
        except Exception: pass
        try:
            t = Teacher.objects.filter(id=int(c)).first()
            if t:
                taught_classes = list(t.classes.all()[:10])
                for tc in taught_classes:
                    candidate_level_ids.add(tc.level.id)
                    class_ids.add(tc.id)
                teacher_ids.add(t.id)
                continue
        except Exception: pass

    for lf in level_failures:
        candidate_level_ids.add(lf)

    candidate_level_ids_list = sorted(candidate_level_ids)[:max_global_resolve_levels]

    if candidate_level_ids_list:
        print(f"[GLOBAL RESOLVE] Attempting targeted global re-solve for levels (candidates): {candidate_level_ids_list}")
        
        global_plan = generate_timetable_for_levels(
            candidate_level_ids_list,
            time_limit_seconds=180,
            penalty_consecutive_base=1000
        )
        
        if global_plan.get("success"):
            # (Logique de merge du plan global... reste identique)
            backup_entries = global_schedule.get("entries", []).copy()
            backup_slot_maps = {k: deepcopy(v) for k, v in global_schedule.items() if k != "entries"}

            level_class_ids = set()
            for lvlid in candidate_level_ids_list:
                for cls in SchoolClass.objects.filter(level__id=lvlid):
                    level_class_ids.add(cls.id)
            
            new_entries = [e for e in global_schedule.get("entries", []) if e["class_id"] not in level_class_ids]
            global_schedule["entries"] = new_entries
            
            new_slot_maps = {}
            for ent in global_schedule.get("entries", []):
                sidx = ent["slot_idx"]
                sm = new_slot_maps.setdefault(sidx, {"teacher": {}, "class": {}})
                sm["class"][ent["class_id"]] = ent
                if ent.get("teacher_id") is not None:
                    sm["teacher"][ent["teacher_id"]] = ent
            for k in list(global_schedule.keys()):
                if k != "entries":
                    global_schedule.pop(k, None)
            for k, v in new_slot_maps.items():
                global_schedule[k] = v
            
            merge_conflicts = merge_level_plan_into_global(global_schedule, global_plan)
            
            if merge_conflicts:
                res, unr = try_resolve_conflicts(merge_conflicts, global_schedule, feasible_slots_map_global)
                report['merged']['resolved'].extend(res)
                if unr:
                    print("[GLOBAL RESOLVE] Conflicts after merge: reverting to backup")
                    global_schedule.clear()
                    global_schedule.update(backup_slot_maps)
                    global_schedule["entries"] = backup_entries
                else:
                    print("[GLOBAL RESOLVE] Merge succeeded and conflicts resolved.")
            else:
                print("[GLOBAL RESOLVE] Merge succeeded without conflicts.")
        else:
            print(f"[GLOBAL RESOLVE] Failed to solve subset: {global_plan.get('message')}")

    # Final teacher conflict check
    final_conflicts = []
    by_teacher_day = defaultdict(list)
    for ent in global_schedule.get("entries", []):
        t_id = ent.get("teacher_id")
        if not t_id: continue
        by_teacher_day[(t_id, ent["weekday"])].append(ent)
        
    for (t_id, day), ents in by_teacher_day.items():
        ents.sort(key=lambda e: e["starts_at"])
        for i in range(len(ents) - 1):
            e1, e2 = ents[i], ents[i + 1]
            # Check overlap
            if e1["ends_at"].hour * 60 + e1["ends_at"].minute > e2["starts_at"].hour * 60 + e2["starts_at"].minute:
                final_conflicts.append({
                    "teacher_id": t_id, "day": day,
                    "conflict_between": [e1, e2]
                })

    if final_conflicts:
        print(f"[FINAL] Teacher conflicts remain: {len(final_conflicts)}")
        report.setdefault("merged", {}).setdefault("teacher_conflicts_after_merge", final_conflicts)
    else:
        print("[FINAL] No teacher conflicts after merge.")
        
    # persist if requested
    if persist and not dry_run:
        try:
            with transaction.atomic():
                class_ids = set(ent['class_id'] for ent in global_schedule.get('entries', []))
                if class_ids:
                    ClassScheduleEntry.objects.filter(school_class_id__in=list(class_ids)).delete()
                
                created = 0
                for ent in global_schedule.get('entries', []):
                    ClassScheduleEntry.objects.create(
                        school_class_id=ent['class_id'],
                        subject_id=ent['subject_id'],
                        teacher_id=ent.get('teacher_id'),
                        weekday=ent['weekday'],
                        starts_at=ent['starts_at'],
                        ends_at=ent['ends_at'],
                    )
                    created += 1
                report['persisted'] = {'created': created}
        except Exception as e:
            report['persist_error'] = str(e)
    else:
        report['persisted'] = {'created': 0}
            
    report['end'] = datetime.now().isoformat()
    report['global_entries_count'] = len(global_schedule.get('entries', []))
    
    # export report file
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    prefix = report_prefix or f"timetable_report_{ts}"
    try:
        with open(f"{prefix}.json", 'w', encoding='utf-8') as f:
            json.dump(report, f, default=str, indent=2, ensure_ascii=False)
        print(f"[REPORT] written to {prefix}.json")
    except Exception as e:
        print("[REPORT] failed to write json:", e)

    return report

# -----------------------------
# If run as script
# -----------------------------
if __name__ == '__main__':
    print("Run aggressive timetable pipeline (dry-run, Hybride C2/C5 Dures, C1/C3 Souple)")
    r = run_timetable_pipeline(time_limit_base=30, dry_run=True, persist=False)
    print("Done. Summary:")
    print(json.dumps(r, indent=2, default=str))