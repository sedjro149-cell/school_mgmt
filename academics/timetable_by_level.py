# academics/services/timetable_by_level.py
"""
Timetable pipeline (by level) - aggressive OR-Tools only version (corrected constraints).

Principales corrections:
- Only slot durations 120 or 180 minutes are allowed for course assignments.
- For each (class, subject) the total assigned minutes must equal exactly the weekly quota.
- Prevent splitting a 3h quota into multiple smaller pieces (e.g. 2+1) by restricting allowed slot sizes.
- Prevent the same (class,subject,teacher) from being assigned to consecutive slots (no back-to-back).
- No overshoot: the solver cannot give more minutes than required.
- Same rules applied to limited global re-solve.

Usage:
    from academics.timetable_by_level import run_timetable_pipeline
    run_timetable_pipeline(dry_run=True, persist=False)
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
      - slots: list of dict {idx, db_obj, weekday, start, end, dur}
      - slots_by_day: dict day -> [idx,...]
      - slot_conflicts: dict idx -> set(idx that overlap)
      - slot_adjacent: dict idx -> set(idx that are immediately consecutive on same day)
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
        # sort indices by start time to optimize overlapping and adjacency search
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
                # optimization: if next slot starts far after this end -> break inner loop
                if s_j["start"] >= s_i["end"] + 240:  # 4h gap heuristic
                    break

    return slots, slots_by_day, slot_conflicts, slot_adjacent


# -----------------------------
# Analysis
# -----------------------------


def analyze_levels():
    """
    Return diagnostics per level:
      - level_id, name, num_classes, num_classsubjects, needed_minutes, available_minutes, est_vars, missing_teachers
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
    global_schedule is expected to contain slot_idx keys (ints) mapping to {"teacher":{}, "class":{}} and an 'entries' list.
    """
    occupied_by_slot = {}
    forbidden_slots_by_teacher = defaultdict(set)
    forbidden_slots_by_class = defaultdict(set)

    # iterate explicit slot maps
    for slot_idx, slot_map in list(global_schedule.items()):
        if slot_idx == "entries":
            continue
        classes = set(slot_map.get("class", {}).keys())
        teachers = set(slot_map.get("teacher", {}).keys())
        occupied_by_slot[slot_idx] = {"classes": classes, "teachers": teachers}
        related = {slot_idx} | set(slot_conflicts.get(slot_idx, set()))
        for t in teachers:
            forbidden_slots_by_teacher[t].update(related)
        for c in classes:
            forbidden_slots_by_class[c].update(related)

    # iterate global entries in case of entries-only representation
    for ent in global_schedule.get("entries", []):
        sidx = ent.get("slot_idx")
        if sidx is None:
            continue
        occupied_by_slot.setdefault(sidx, {"classes": set(), "teachers": set()})
        if ent.get("class_id") is not None:
            occupied_by_slot[sidx]["classes"].add(ent["class_id"])
            forbidden_slots_by_class[ent["class_id"]].update({sidx} | set(slot_conflicts.get(sidx, set())))
        if ent.get("teacher_id") is not None:
            occupied_by_slot[sidx]["teachers"].add(ent["teacher_id"])
            forbidden_slots_by_teacher[ent["teacher_id"]].update({sidx} | set(slot_conflicts.get(sidx, set())))

    return occupied_by_slot, dict(forbidden_slots_by_teacher), dict(forbidden_slots_by_class)


# -----------------------------
# Level solver (aggressive + strict duration rules)
# -----------------------------


def generate_timetable_for_level(level_id,
                                 time_limit_seconds=60,
                                 penalty_same_day_base=20,
                                 penalty_consecutive_base=50,
                                 allow_missing_teacher=False,
                                 occupied_teacher_slots=None,
                                 occupied_class_slots=None,
                                 maximize_coverage=True):
    """
    Solve CP-SAT for a single level with strict rules:
      - only slot durations of 120 or 180 are allowed to satisfy subject quotas
      - the sum of durations assigned to a (class,subject) must equal exactly the weekly quota
      - prevent selection of two consecutive slots for the same (class,subject,teacher)
    Returns LevelPlan dict:
      success, message, entries, diagnostics, time_s, feasible_slots
    """
    t0 = time.time()
    slots, slots_by_day, slot_conflicts, slot_adjacent = _load_slots()
    occupied_teacher_slots = occupied_teacher_slots or {}
    occupied_class_slots = occupied_class_slots or {}

    # allowed atomic session durations (minutes)
    ALLOWED_SLOT_DURS = {120, 180}

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
    # quick capacity check
    for c_id, c_data in classes.items():
        needed = sum(s["hours_min"] for s in c_data["subjects"].values())
        if needed > total_available_minutes:
            diagnostics["capacity_issue"] = {"class_id": c_id, "needed": needed, "available": total_available_minutes}
            return {"success": False, "message": "Capacity issue for a class", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # compute dynamic penalties: scale by density (needed_minutes / available_minutes)
    level_needed = sum(sum(s["hours_min"] for s in c["subjects"].values()) for c in classes.values())
    density = level_needed / max(1, total_available_minutes)
    # scale penalties with density
    penalty_same_day = int(penalty_same_day_base * (1 + density * 4))
    penalty_consecutive = int(penalty_consecutive_base * (1 + density * 3))

    # feasible slots per (class,subject) considering occupied slots + allowed durations
    min_slot_dur = min(s["dur"] for s in slots)
    max_slot_dur = max(s["dur"] for s in slots)
    feasible_slots = {}

    # Precompute slot durations map and allowed-slot set indices
    allowed_slot_indices = {s["idx"] for s in slots if s["dur"] in ALLOWED_SLOT_DURS}

    for c_id, c_data in classes.items():
        forbidden_for_class = occupied_class_slots.get(c_id, set())
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            teacher_id = s_data["teacher_id"]
            forbidden_for_teacher = set()
            if teacher_id is not None:
                forbidden_for_teacher = occupied_teacher_slots.get(teacher_id, set())

            # Candidate slots must:
            #  - have duration in ALLOWED_SLOT_DURS
            #  - not be forbidden for the class or teacher
            candidates = [i for i in allowed_slot_indices if i not in forbidden_for_class and i not in forbidden_for_teacher]

            # For performance, further filter slots that are not ridiculously small/big relative to needed:
            # keep slots with dur <= needed + max_slot_dur (like before)
            candidates = [i for i in candidates if slots[i]["dur"] <= needed + max_slot_dur]

            # Final candidate list
            feasible_slots[(c_id, s_id)] = sorted(candidates)

    # Now **validate** that each (c,s) has at least one combination of allowed slots summing exactly to needed,
    # respecting adjacency prohibition (no two selected slots for same (c,s) that are adjacent or overlapping).
    # This is a combinational check — to keep it fast, we do a small feasibility pruning:
    # try to find combinations up to a reasonable number of sessions (needed // min_allowed_slot_dur)
    no_feasible = []
    for (c_id, s_id), sls in feasible_slots.items():
        needed = classes[c_id]["subjects"][s_id]["hours_min"]
        # compute max sessions feasible given minimal allowed slot (120)
        max_sessions_possible = math.ceil(needed / min({120, 180}))
        # simple DP subset-sum limited to max_sessions_possible and forbidding adjacency
        # We'll attempt to see if at least one non-adjacent subset sums exactly to needed.
        found = False
        # small backtracking search (bounded) to detect trivial impossibilities
        def backtrack(idx_list, start, total, used_idxs):
            nonlocal found
            if found:
                return
            if total == needed:
                found = True
                return
            if total > needed:
                return
            if len(used_idxs) >= max_sessions_possible:
                return
            for pos in range(start, len(idx_list)):
                i = idx_list[pos]
                # forbid adjacency/overlap with already used slots
                bad = False
                for u in used_idxs:
                    if i in slot_conflicts[u] or i in slot_adjacent[u]:
                        bad = True
                        break
                if bad:
                    continue
                backtrack(idx_list, pos + 1, total + slots[i]["dur"], used_idxs + [i])
                if found:
                    return

        # small guard: if no candidates, impossible
        if not sls:
            no_feasible.append((c_id, s_id))
            continue
        # perform backtracking with early stop if too many candidates
        limited_candidates = sls if len(sls) <= 20 else sls[:20]  # limit search width for speed
        backtrack(limited_candidates, 0, 0, [])
        if not found:
            # As fallback try a full but bounded search up to reasonable depth
            if len(sls) <= 30:
                backtrack(sls, 0, 0, [])
        if not found:
            no_feasible.append((c_id, s_id))

    if no_feasible:
        diagnostics["no_feasible"] = no_feasible
        return {"success": False, "message": "Some (class,subject) have no feasible exact-slot combination given current occupancy and 2h/3h rule", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # Build CP-SAT model
    model = cp_model.CpModel()
    X = {}
    # create boolean vars only for feasible indices (already filtered)
    for (c_id, s_id), slot_list in feasible_slots.items():
        for i in slot_list:
            X[(c_id, s_id, i)] = model.NewBoolVar(f"x_l{level_id}_c{c_id}_s{s_id}_t{i}")

    # Hard: sum durations == needed (exact equality)
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            slot_list = feasible_slots[(c_id, s_id)]
            needed = s_data["hours_min"]
            # equality: exact coverage
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) == needed)
            # limit number of sessions to reasonable maximum (prevents huge splits)
            max_sessions = math.ceil(needed / min({120, 180}))
            model.Add(sum(X[(c_id, s_id, i)] for i in slot_list) <= max_sessions)

    # one subject per class per slot (no double booking of class)
    for i in range(len(slots)):
        for c_id, c_data in classes.items():
            relevant_sids = [s_id for s_id in c_data["subjects"].keys() if (c_id, s_id, i) in X]
            if relevant_sids:
                model.Add(sum(X[(c_id, s_id, i)] for s_id in relevant_sids) <= 1)

    # class overlap constraints using slot_conflicts (no class on overlapping slots)
    for c_id, c_data in classes.items():
        subj_ids = list(c_data["subjects"].keys())
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for s_id in subj_ids if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for s_id in subj_ids if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # teacher overlap constraints inside this level (pairwise on overlapping slots)
    teacher_assignments = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            t_id = s_data["teacher_id"]
            if t_id is None:
                continue
            teacher_assignments.setdefault(t_id, []).append((c_id, s_id))

    for t_id, assigns in teacher_assignments.items():
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for (c_id, s_id) in assigns if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for (c_id, s_id) in assigns if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # PROHIBIT selecting two slots that are adjacent or overlapping for the SAME (class,subject)
    # This enforces "le même cours ... ne se suivent pas"
    for (c_id, s_id), slot_list in feasible_slots.items():
        # for every pair (i,j) where either overlap or adjacency exists, disallow both
        for a_idx in range(len(slot_list)):
            i = slot_list[a_idx]
            # overlapping handled elsewhere, but we also include adjacency
            # check j over subsequent indices
            for b_idx in range(a_idx + 1, len(slot_list)):
                j = slot_list[b_idx]
                if j in slot_conflicts.get(i, set()) or j in slot_adjacent.get(i, set()):
                    model.Add(X[(c_id, s_id, i)] + X[(c_id, s_id, j)] <= 1)

    # Soft constructs: discourage same-day multiple sessions for same (class,subject)
    P_same = []
    D_day = {}
    P_consec = []
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for day, idxs in slots_by_day.items():
                day_idxs = [i for i in idxs if (c_id, s_id, i) in X]
                if not day_idxs:
                    continue
                D = model.NewBoolVar(f"d_l{level_id}_c{c_id}_s{s_id}_day{day}")
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) >= D)
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) <= len(day_idxs) * D)
                D_day[(c_id, s_id, day)] = D
                P = model.NewBoolVar(f"p_l{level_id}_c{c_id}_s{s_id}_day{day}")
                # allow at most 1 per day without penalty (we already prevent adjacency)
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) <= 1 + len(day_idxs) * P)
                P_same.append(P)

    weekdays_sorted = sorted(slots_by_day.keys())
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for k in range(len(weekdays_sorted) - 1):
                d1, d2 = weekdays_sorted[k], weekdays_sorted[k + 1]
                D1 = D_day.get((c_id, s_id, d1))
                D2 = D_day.get((c_id, s_id, d2))
                if D1 is None or D2 is None:
                    continue
                Pcon = model.NewBoolVar(f"pc_l{level_id}_c{c_id}_s{s_id}_d{d1}_{d2}")
                model.Add(Pcon >= D1 + D2 - 1)
                model.Add(Pcon <= D1)
                model.Add(Pcon <= D2)
                P_consec.append(Pcon)

    # Objective: maximize assigned minutes minus scaled penalties (aggressive)
    total_minutes_assigned = sum(X[(c, s, i)] * slots[i]["dur"] for (c, s, i) in X.keys())
    penalty_same = sum(P_same) if P_same else 0
    penalty_consec = sum(P_consec) if P_consec else 0
    weight_coverage = int(1000 * (1 + density)) if 'density' in locals() else 1000
    model.Maximize(weight_coverage * total_minutes_assigned - penalty_same_day * penalty_same - penalty_consecutive * penalty_consec)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(5, time_limit_seconds)
    solver.parameters.num_search_workers = 8
    if density > 0.6:
        solver.parameters.random_seed = random.randint(1, 100000)
        solver.parameters.search_branching = cp_model.FIXED_SEARCH
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        diagnostics.update({"status": int(status), "est_vars_after_pruning": sum(len(v) for v in feasible_slots.values())})
        return {"success": False, "message": "No solution for level (strict 2h/3h rules)", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

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
            })

    # final validation: ensure exact coverage
    assigned_map = defaultdict(int)
    for ent in entries:
        assigned_map[(ent["class_id"], ent["subject_id"])] += (ent["ends_at"].hour * 60 + ent["ends_at"].minute) - (ent["starts_at"].hour * 60 + ent["starts_at"].minute)
    invalid = []
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            got = assigned_map.get((c_id, s_id), 0)
            if got != needed:
                invalid.append({"class_id": c_id, "subject_id": s_id, "needed": needed, "got": got})
    if invalid:
        diagnostics.update({"post_validation_invalid": invalid})
        return {"success": False, "message": "Post validation failed: assigned minutes not equal to quotas", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    diagnostics.update({"status": int(status), "est_vars_after_pruning": sum(len(v) for v in feasible_slots.values()), "penalty_same_day": penalty_same_day, "penalty_consecutive": penalty_consecutive})
    return {"success": True, "message": "Level solved (strict)", "entries": entries, "diagnostics": diagnostics, "time_s": time.time() - t0, "feasible_slots": feasible_slots}


# -----------------------------
# Merge & conflict resolution (greedy fallback)
# -----------------------------


def merge_level_plan_into_global(global_schedule, level_plan):
    """
    Merge entries of level_plan into global_schedule (mutates it).
    Returns list of conflicts detected.
    """
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
    """
    Greedy relocation + swap fallback (kept as last resort).
    Returns (resolved, unresolved)
    """
    unresolved = []
    resolved = []

    def slot_free_for(slot_idx, class_id, teacher_id):
        slot_map = global_schedule.get(slot_idx, {"teacher": {}, "class": {}})
        if class_id in slot_map.get("class", {}):
            return False
        if teacher_id is not None and teacher_id in slot_map.get("teacher", {}):
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
        for cand in candidates:
            if cand == new_ent["slot_idx"]:
                continue
            if slot_free_for(cand, class_id, teacher_id):
                old_slot_map = global_schedule.get(new_ent["slot_idx"], {"teacher": {}, "class": {}})
                old_slot_map.get("class", {}).pop(class_id, None)
                if teacher_id is not None:
                    old_slot_map.get("teacher", {}).pop(teacher_id, None)
                new_ent["slot_idx"] = cand
                slot_map_new = global_schedule.setdefault(cand, {"teacher": {}, "class": {}})
                slot_map_new["class"][class_id] = new_ent
                if teacher_id is not None:
                    slot_map_new["teacher"][teacher_id] = new_ent
                resolved.append({"conflict": c, "moved_to": cand})
                moved = True
                break
        if moved:
            continue

        # try swap
        swapped = False
        for cand in candidates:
            slot_map = global_schedule.get(cand, {"teacher": {}, "class": {}})
            for other_class_id, other_ent in list(slot_map.get("class", {}).items()):
                other_teacher = other_ent.get("teacher_id")
                if slot_free_for(new_ent["slot_idx"], other_class_id, other_teacher):
                    slot_map["class"].pop(other_class_id, None)
                    if other_teacher is not None:
                        slot_map["teacher"].pop(other_teacher, None)
                    new_ent_old_slot = new_ent["slot_idx"]
                    old_slot_map = global_schedule.get(new_ent_old_slot, {"teacher": {}, "class": {}})
                    old_slot_map.get("class", {}).pop(class_id, None)
                    if teacher_id is not None:
                        old_slot_map.get("teacher", {}).pop(teacher_id, None)
                    new_ent["slot_idx"] = cand
                    slot_map_new = global_schedule.setdefault(cand, {"teacher": {}, "class": {}})
                    slot_map_new["class"][class_id] = new_ent
                    if teacher_id is not None:
                        slot_map_new["teacher"][teacher_id] = new_ent
                    other_ent["slot_idx"] = new_ent_old_slot
                    old_slot_map["class"][other_class_id] = other_ent
                    if other_teacher is not None:
                        old_slot_map["teacher"][other_teacher] = other_ent
                    resolved.append({"conflict": c, "swap_with": other_ent})
                    swapped = True
                    break
            if swapped:
                break
        if not swapped:
            unresolved.append(c)

    return resolved, unresolved


# -----------------------------
# Optional limited global re-solve (on subset of levels)
# -----------------------------


def generate_timetable_for_levels(level_ids, time_limit_seconds=120):
    """
    Limited global re-solve for a subset of levels (combine classes).
    WARNING: can become heavy; intended for small subsets (K <= 4-6).
    Returns similar structure to per-level but combined.
    This version also enforces 2h/3h slot use and exact quota coverage.
    """
    slots, slots_by_day, slot_conflicts, slot_adjacent = _load_slots()

    ALLOWED_SLOT_DURS = {120, 180}

    classes = {}
    for lvl in Level.objects.filter(id__in=level_ids):
        for cls in SchoolClass.objects.filter(level=lvl):
            subj_map = {}
            cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
            for cs in cs_qs:
                hrs = getattr(cs, "hours_per_week", None)
                if hrs is None:
                    continue
                subj = cs.subject
                teacher = Teacher.objects.filter(subject=subj, classes=cls).first()
                teacher_id = teacher.id if teacher else None
                subj_map[subj.id] = {"hours_min": int(hrs * 60), "teacher_id": teacher_id, "classsubject_id": cs.id}
            if subj_map:
                classes[cls.id] = {"obj": cls, "subjects": subj_map}

    if not classes:
        return {"success": False, "message": "No classes for levels", "entries": [], "diagnostics": {}}

    min_slot_dur = min(s["dur"] for s in slots)
    max_slot_dur = max(s["dur"] for s in slots)
    # allowed indices
    allowed_slot_indices = {s["idx"] for s in slots if s["dur"] in ALLOWED_SLOT_DURS}
    feasible_slots = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            candidates = [i for i in allowed_slot_indices if slots[i]["dur"] <= needed + max_slot_dur]
            feasible_slots[(c_id, s_id)] = sorted(candidates)

    # quick combinational feasibility check (like per-level)
    no_feasible = []
    for (c_id, s_id), sls in feasible_slots.items():
        needed = classes[c_id]["subjects"][s_id]["hours_min"]
        if not sls:
            no_feasible.append((c_id, s_id))
            continue
        found = False
        max_sessions_possible = math.ceil(needed / min({120, 180}))
        limited_candidates = sls if len(sls) <= 30 else sls[:30]
        def backtrack(idx_list, start, total, used_idxs):
            nonlocal found
            if found:
                return
            if total == needed:
                found = True
                return
            if total > needed:
                return
            if len(used_idxs) >= max_sessions_possible:
                return
            for pos in range(start, len(idx_list)):
                i = idx_list[pos]
                bad = False
                for u in used_idxs:
                    if i in slot_conflicts[u] or i in slot_adjacent[u]:
                        bad = True
                        break
                if bad:
                    continue
                backtrack(idx_list, pos + 1, total + slots[i]["dur"], used_idxs + [i])
                if found:
                    return
        backtrack(limited_candidates, 0, 0, [])
        if not found:
            no_feasible.append((c_id, s_id))
    if no_feasible:
        return {"success": False, "message": "Some (class,subject) not representable by allowed 2h/3h slots", "entries": [], "diagnostics": {"no_feasible": no_feasible}}

    # build combined CP model (similar to per-level but with classes across levels)
    model = cp_model.CpModel()
    X = {}
    for (c_id, s_id), slot_list in feasible_slots.items():
        for i in slot_list:
            X[(c_id, s_id, i)] = model.NewBoolVar(f"x_gl_c{c_id}_s{s_id}_t{i}")

    # quotas equality
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            slot_list = feasible_slots[(c_id, s_id)]
            needed = s_data["hours_min"]
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) == needed)
            max_sessions = math.ceil(needed / min({120, 180}))
            model.Add(sum(X[(c_id, s_id, i)] for i in slot_list) <= max_sessions)

    # class single per slot
    for i in range(len(slots)):
        for c_id in classes.keys():
            sids = [s_id for s_id in classes[c_id]["subjects"].keys() if (c_id, s_id, i) in X]
            if sids:
                model.Add(sum(X[(c_id, s_id, i)] for s_id in sids) <= 1)

    # class overlap via slot_conflicts
    for c_id in classes.keys():
        subj_ids = list(classes[c_id]["subjects"].keys())
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for s_id in subj_ids if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for s_id in subj_ids if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # teacher overlap across all classes
    teacher_assignments = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            t_id = s_data["teacher_id"]
            if t_id is None:
                continue
            teacher_assignments.setdefault(t_id, []).append((c_id, s_id))

    for t_id, assigns in teacher_assignments.items():
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for (c_id, s_id) in assigns if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for (c_id, s_id) in assigns if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # forbid adjacency for same (class,subject)
    for (c_id, s_id), slot_list in feasible_slots.items():
        for a_idx in range(len(slot_list)):
            i = slot_list[a_idx]
            for b_idx in range(a_idx + 1, len(slot_list)):
                j = slot_list[b_idx]
                if j in slot_conflicts.get(i, set()) or j in slot_adjacent.get(i, set()):
                    model.Add(X[(c_id, s_id, i)] + X[(c_id, s_id, j)] <= 1)

    total_minutes = sum(X[(c, s, i)] * slots[i]["dur"] for (c, s, i) in X.keys())
    model.Maximize(total_minutes)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(30, time_limit_seconds)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"success": False, "message": "Global re-solve failed", "entries": [], "diagnostics": {"status": int(status)}}

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
            })

    return {"success": True, "message": "Global subset solved (strict)", "entries": entries, "diagnostics": {"status": int(status)}}


# -----------------------------
# Pipeline driver (aggressive + strict)
# -----------------------------


def run_timetable_pipeline(levels_ordering_strategy='most_constrained_first',
                           time_limit_base=60,
                           dry_run=False,
                           persist=True,
                           report_prefix=None,
                           max_retries_per_level=4,
                           max_global_resolve_levels=3):
    """
    End-to-end aggressive pipeline with strict 2h/3h session rules.
    """
    report = {"start": datetime.now().isoformat(), "levels": [], "merged": {"conflicts": [], "resolved": [], "shortfalls": []}}

    slots, slots_by_day, slot_conflicts, slot_adjacent = _load_slots()
    analysis = analyze_levels()
    levels_info = analysis["levels"]

    # ordering
    if levels_ordering_strategy == 'most_constrained_first':
        levels_info.sort(key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"])), reverse=True)
    elif levels_ordering_strategy == 'least_constrained_first':
        levels_info.sort(key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"])))
    else:
        levels_info.sort(key=lambda l: l["level_id"])

    global_schedule = {}
    global_schedule["entries"] = []

    def current_occupied_maps():
        return build_occupied_maps_from_global(global_schedule, slots, slot_conflicts)

    all_level_plans = []
    level_failures = []

    for lvl in levels_info:
        lvl_id = lvl["level_id"]
        est_vars = lvl["est_vars"]
        timeout = min(600, max(time_limit_base, int(time_limit_base + 0.002 * est_vars)))
        print(f"[PIPE] Solving level {lvl['name']} id={lvl_id} timeout={timeout}s")

        # compute occupied maps from current global schedule
        _, forbidden_by_teacher, forbidden_by_class = current_occupied_maps()

        attempt = 0
        plan = None
        while attempt < max_retries_per_level:
            attempt_timeout = timeout * (2 ** attempt)
            print(f"  Attempt {attempt+1}/{max_retries_per_level} timeout {attempt_timeout}s")
            plan = generate_timetable_for_level(
                lvl_id,
                time_limit_seconds=attempt_timeout,
                penalty_same_day_base=20,
                penalty_consecutive_base=50,
                allow_missing_teacher=False,
                occupied_teacher_slots=forbidden_by_teacher,
                occupied_class_slots=forbidden_by_class
            )
            plan["attempt"] = attempt + 1
            plan["timeout_used"] = attempt_timeout
            plan["level_meta"] = lvl
            all_level_plans.append(plan)
            report["levels"].append({"level_id": lvl_id, "name": lvl["name"], "plan_status": plan.get("success"), "diag": plan.get("diagnostics"), "attempt": attempt + 1})

            if not plan.get("success"):
                print(f"    Plan generation failed: {plan.get('message')}")
                attempt += 1
                continue

            # merge
            conflicts = merge_level_plan_into_global(global_schedule, plan)
            if not conflicts:
                print(f"    Merged level {lvl['name']} without conflicts.")
            else:
                # try greedy resolve
                print(f"    Detected {len(conflicts)} conflicts merging level; trying greedy resolve")
                resolved, unresolved = try_resolve_conflicts(conflicts, global_schedule, plan.get("feasible_slots", {}))
                report['merged']['resolved'].extend(resolved)
                if unresolved:
                    print(f"    Unresolved after greedy: {len(unresolved)} -> will retry generation with larger timeout")
                    report['merged']['conflicts'].extend(unresolved)
                    attempt += 1
                    continue
                else:
                    print(f"    Conflicts resolved by greedy.")

            # --- attempt automatic duplicate repair (same teacher + same class on same weekday) ---
            try:
                repair_report = repair_duplicates_in_global(
                    global_schedule,
                    slots,
                    slot_conflicts,
                    slot_adjacent,
                    feasible_slots_map=plan.get("feasible_slots") if plan is not None else None,
                    debug=False
                )

                if repair_report:
                    resolved_dup = repair_report.get("resolved", [])
                    if resolved_dup:
                        report["merged"].setdefault("duplicates_resolved", []).extend(resolved_dup)
                        print(f"    Repair: resolved {len(resolved_dup)} duplicate(s) after merge.")

                    unresolved_dup = repair_report.get("unresolved", [])
                    if unresolved_dup:
                        report["merged"].setdefault("duplicates_unresolved", []).extend(unresolved_dup)
                        print(f"    Repair: {len(unresolved_dup)} duplicate group(s) remain unresolved and flagged for review.")

                    candidates = list(repair_report.get("candidates_for_resolve", []) or [])
                    if candidates:
                        existing = set(report["merged"].get("duplicates_candidates", []))
                        existing.update(candidates)
                        report["merged"]["duplicates_candidates"] = list(existing)
                        print(f"    Repair: added {len(candidates)} candidate ids for targeted re-solve.")
            except Exception as e:
                print("    [REPAIR] repair_duplicates_in_global raised an exception:", e)
                report["merged"].setdefault("repair_errors", []).append(str(e))
            # --- end repair block ---

            print(f"    Merged level {lvl['name']} successfully (attempt {attempt+1}).")
            break

        if not plan or not plan.get("success"):
            print(f"[WARN] Level {lvl['name']} could not be solved after {attempt} attempts.")
            level_failures.append(lvl_id)
            continue

        # compute assigned minutes for classes in this level (global view)
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
                if hrs is None:
                    continue
                needed_map[(cls.id, cs.subject.id)] = int(hrs * 60)

        shortfalls = []
        for key, need in needed_map.items():
            got = assigned_minutes.get(key, 0)
            if got < need:
                shortfalls.append({"class_id": key[0], "subject_id": key[1], "needed": need, "got": got, "missing": need - got})

        if shortfalls:
            print(f"  Found {len(shortfalls)} shortfalls for level {lvl['name']}. Attempting targeted repair...")
            # Attempt limited repairs: re-solve this level after freeing its entries from global
            repaired = False
            repair_attempt = 0
            while repair_attempt < 2 and shortfalls:
                # backup global slot maps and entries
                backup_entries = global_schedule.get("entries", []).copy()
                backup_slot_maps = {k: deepcopy(v) for k, v in global_schedule.items() if k != "entries"}

                # remove this level's entries from global
                level_class_ids = set(cls.id for cls in SchoolClass.objects.filter(level__id=lvl_id))
                new_entries = [e for e in global_schedule.get("entries", []) if e["class_id"] not in level_class_ids]
                global_schedule["entries"] = new_entries
                # rebuild slot maps
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

                # rebuild occupied maps
                _, forbidden_by_teacher2, forbidden_by_class2 = current_occupied_maps()
                repair_timeout = timeout * 3
                print(f"    Repair attempt {repair_attempt+1} with timeout {repair_timeout}s")
                repair_plan = generate_timetable_for_level(
                    lvl_id,
                    time_limit_seconds=repair_timeout,
                    penalty_same_day_base=10,
                    penalty_consecutive_base=30,
                    allow_missing_teacher=False,
                    occupied_teacher_slots=forbidden_by_teacher2,
                    occupied_class_slots=forbidden_by_class2
                )
                if repair_plan.get("success"):
                    merge_conflicts = merge_level_plan_into_global(global_schedule, repair_plan)
                    if merge_conflicts:
                        res, unr = try_resolve_conflicts(merge_conflicts, global_schedule, repair_plan.get("feasible_slots", {}))
                        report['merged']['resolved'].extend(res)
                        if unr:
                            print(f"    Repair conflicts couldn't be fully resolved: reverting")
                            global_schedule.clear()
                            global_schedule.update(backup_slot_maps)
                            global_schedule["entries"] = backup_entries
                        else:
                            print(f"    Repair succeeded and merged.")
                            repaired = True
                            shortfalls = []
                            break
                    else:
                        print(f"    Repair merged without conflicts.")
                        repaired = True
                        shortfalls = []
                        break
                else:
                    print(f"    Repair generation failed: {repair_plan.get('message')}")
                    # revert
                    global_schedule.clear()
                    global_schedule.update(backup_slot_maps)
                    global_schedule["entries"] = backup_entries
                repair_attempt += 1

            if not repaired and shortfalls:
                print(f"  Shortfalls remain after repair attempts for level {lvl['name']}. Recording.")
                report['merged']['shortfalls'].append({"level": lvl_id, "shortfalls": shortfalls})

    # After processing all levels, if repair produced candidates, attempt a limited targeted global re-solve
    # gather candidate level ids from report['merged']['duplicates_candidates'] (if present) and from level_failures/shortfalls
    extra_candidates = set(report['merged'].get('duplicates_candidates', []))
    # candidates might include teacher ids and class ids; convert class ids to their levels
    candidate_level_ids = set()
    class_ids = set()
    teacher_ids = set()
    for c in extra_candidates:
        # heuristic: class ids are positive ints; teachers also ints - we try both lookups
        try:
            cls = SchoolClass.objects.filter(id=int(c)).first()
            if cls:
                candidate_level_ids.add(cls.level.id)
                class_ids.add(cls.id)
                continue
        except Exception:
            pass
        try:
            t = Teacher.objects.filter(id=int(c)).first()
            if t:
                # include levels of classes taught by this teacher (limited set)
                taught_classes = list(t.classes.all()[:10])
                for tc in taught_classes:
                    candidate_level_ids.add(tc.level.id)
                    class_ids.add(tc.id)
                teacher_ids.add(t.id)
                continue
        except Exception:
            pass

    # also add levels that failed earlier
    for lf in level_failures:
        candidate_level_ids.add(lf)

    # cap to max_global_resolve_levels
    candidate_level_ids_list = sorted(candidate_level_ids)[:max_global_resolve_levels]
    if candidate_level_ids_list:
        print(f"[GLOBAL RESOLVE] Attempting targeted global re-solve for levels (candidates): {candidate_level_ids_list}")
        global_plan = generate_timetable_for_levels(candidate_level_ids_list, time_limit_seconds=180)
        if global_plan.get("success"):
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
                res, unr = try_resolve_conflicts(merge_conflicts, global_schedule, global_plan.get("feasible_slots", {}))
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
        if not t_id:
            continue
        by_teacher_day[(t_id, ent["weekday"])].append(ent)

    for (t_id, day), ents in by_teacher_day.items():
        ents.sort(key=lambda e: e["starts_at"])
        for i in range(len(ents) - 1):
            e1, e2 = ents[i], ents[i + 1]
            if e1["ends_at"] > e2["starts_at"]:
                final_conflicts.append({
                    "teacher_id": t_id,
                    "day": day,
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
    print("Run aggressive timetable pipeline (dry-run, strict 2h/3h rules)")
    r = run_timetable_pipeline(time_limit_base=30, dry_run=True, persist=False)
    print("Done. Summary:")
    print(json.dumps(r, indent=2, default=str))
