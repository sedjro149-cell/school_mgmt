"""
Timetable pipeline (by level) for Django project.

Place this file in your project (e.g. academics/services/timetable_by_level.py)
Run from manage.py context or import functions.

Functionality:
- analyze_levels(): diagnostics per Level
- generate_timetable_for_level(level_id,...): builds and solves CP-SAT for that level
- merge_and_resolve(): merges level plans into a global schedule and attempts to resolve conflicts
- run_timetable_pipeline(): end-to-end driver (with dry-run and persistence options)

This file reuses the pruning and bounding heuristics used previously.
"""

import time
import math
import random
import json
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

# -----------------------------
# Helpers / data structures
# -----------------------------

def _to_minutes(t):
    return t.hour * 60 + t.minute


def _load_slots():
    """Load TimeSlot objects and build useful indices."""
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

    # build conflicts
    slot_conflicts = {s["idx"]: set() for s in slots}
    for day, idxs in slots_by_day.items():
        for a in range(len(idxs)):
            i = idxs[a]
            s_i = slots[i]
            for b in range(a + 1, len(idxs)):
                j = idxs[b]
                s_j = slots[j]
                if (s_i["start"] < s_j["end"]) and (s_j["start"] < s_i["end"]):
                    slot_conflicts[i].add(j)
                    slot_conflicts[j].add(i)

    return slots, slots_by_day, slot_conflicts


# -----------------------------
# Analysis: per-level diagnostics
# -----------------------------

def analyze_levels():
    """Return diagnostics per level to guide ordering and timeouts.

    Returns dict with:
      levels: list of {level_id, name, num_classes, num_classsubjects, needed_minutes, available_minutes, est_vars}
    """
    slots, slots_by_day, slot_conflicts = _load_slots()
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
                # detect missing teacher
                teach = Teacher.objects.filter(subject=cs.subject, classes=cls).first()
                if not teach:
                    missing_teachers.append({"class": str(cls), "class_id": cls.id, "subject": cs.subject.name, "subject_id": cs.subject.id})
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
# Core: generate timetable for a single level
# -----------------------------

def generate_timetable_for_level(level_id, time_limit_seconds=60, penalty_same_day=20, penalty_consecutive=50, allow_missing_teacher=False):
    """Solve CP-SAT for a single Level. Returns a dict LevelPlan.

    LevelPlan keys:
      success (bool), message, entries (list), diagnostics (dict), time_s
    entries: list of dicts with keys: class_id, subject_id, teacher_id, weekday, starts_at, ends_at, slot_idx
    """
    t0 = time.time()
    slots, slots_by_day, slot_conflicts = _load_slots()

    # collect classes for this level
    try:
        level = Level.objects.get(id=level_id)
    except Level.DoesNotExist:
        return {"success": False, "message": "Level not found", "entries": [], "diagnostics": {}, "time_s": 0}

    classes_objs = list(SchoolClass.objects.filter(level=level))
    if not classes_objs:
        return {"success": False, "message": "No classes for level", "entries": [], "diagnostics": {}, "time_s": 0}

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

    # pre-check capacity
    total_available_minutes = sum(s["dur"] for s in slots)
    for c_id, c_data in classes.items():
        needed = sum(s["hours_min"] for s in c_data["subjects"].values())
        if needed > total_available_minutes:
            diagnostics["capacity_issue"] = {"class_id": c_id, "needed": needed, "available": total_available_minutes}
            return {"success": False, "message": "Capacity issue for a class", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # pruning feasible slots per (c,s)
    min_slot_dur = min(s["dur"] for s in slots)
    max_slot_dur = max(s["dur"] for s in slots)
    feasible_slots = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            candidates = [i for i in range(len(slots)) if slots[i]["dur"] <= needed + max_slot_dur]
            if not candidates:
                candidates = [i for i in range(len(slots)) if slots[i]["dur"] <= max(needed * 2, max_slot_dur)]
            feasible_slots[(c_id, s_id)] = candidates

    no_feasible = [(c_id, s_id) for (c_id, s_id), sls in feasible_slots.items() if not sls]
    if no_feasible:
        diagnostics["no_feasible"] = no_feasible
        return {"success": False, "message": "Some (class,subject) have no feasible slots", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # build CP model
    model = cp_model.CpModel()
    X = {}
    for (c_id, s_id), slot_list in feasible_slots.items():
        for i in slot_list:
            X[(c_id, s_id, i)] = model.NewBoolVar(f"x_l{level_id}_c{c_id}_s{s_id}_t{i}")

    # hard constraints: min minutes and upper bounds
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            slot_list = feasible_slots[(c_id, s_id)]
            needed = s_data["hours_min"]
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) >= needed)
            max_sessions = math.ceil(needed / min_slot_dur) if min_slot_dur > 0 else len(slot_list)
            model.Add(sum(X[(c_id, s_id, i)] for i in slot_list) <= max_sessions)
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) <= needed + max_slot_dur)

    # one subject per class per slot
    for i in range(len(slots)):
        for c_id, c_data in classes.items():
            relevant_sids = [s_id for s_id in c_data["subjects"].keys() if (c_id, s_id, i) in X]
            if relevant_sids:
                model.Add(sum(X[(c_id, s_id, i)] for s_id in relevant_sids) <= 1)

    # class overlap constraints using slot_conflicts
    for c_id, c_data in classes.items():
        subj_ids = list(c_data["subjects"].keys())
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                terms_i = [X[(c_id, s_id, i)] for s_id in subj_ids if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for s_id in subj_ids if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # teacher overlap constraints
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

    # soft constraints
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
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) <= 1 + len(day_idxs) * P)
                P_same.append(P)
    weekdays_sorted = sorted(slots_by_day.keys())
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for k in range(len(weekdays_sorted) - 1):
                d1 = weekdays_sorted[k]
                d2 = weekdays_sorted[k + 1]
                D1 = D_day.get((c_id, s_id, d1))
                D2 = D_day.get((c_id, s_id, d2))
                if D1 is None or D2 is None:
                    continue
                Pcon = model.NewBoolVar(f"pc_l{level_id}_c{c_id}_s{s_id}_d{d1}_{d2}")
                model.Add(Pcon >= D1 + D2 - 1)
                model.Add(Pcon <= D1)
                model.Add(Pcon <= D2)
                P_consec.append(Pcon)

    total_minutes_used = sum(X[k] * next(s["dur"] for s in slots if s["idx"] == k[2]) for k in X.keys())
    objective = total_minutes_used + penalty_same_day * sum(P_same) + penalty_consecutive * sum(P_consec)
    model.Minimize(objective)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(time_limit_seconds, 60)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        diagnostics.update({"status": int(status), "est_vars_after_pruning": sum(len(v) for v in feasible_slots.values())})
        return {"success": False, "message": "No solution for level", "entries": [], "diagnostics": diagnostics, "time_s": time.time() - t0}

    # collect entries
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

    diagnostics.update({"status": int(status), "est_vars_after_pruning": sum(len(v) for v in feasible_slots.values())})
    return {"success": True, "message": "Level solved", "entries": entries, "diagnostics": diagnostics, "time_s": time.time() - t0, "feasible_slots": feasible_slots}


# -----------------------------
# Merge and conflict resolution
# -----------------------------

def merge_level_plan_into_global(global_schedule, level_plan):
    """Insert entries into global_schedule and detect conflicts.

    global_schedule structure:
      - by_slot: dict slot_idx -> {"teacher": {teacher_id: (entry)}, "class": {class_id: entry}}
      - entries: list of entries placed
    Returns conflicts list (each conflict is list of conflicting assignments)
    """
    conflicts = []
    for ent in level_plan.get("entries", []):
        slot = ent["slot_idx"]
        teacher = ent.get("teacher_id")
        cls = ent.get("class_id")
        slot_map = global_schedule.setdefault(slot, {"teacher": {}, "class": {}})

        # class double booking (very unlikely because level plan ensures it) but check
        if cls in slot_map["class"]:
            conflicts.append({"type": "class_double", "slot": slot, "class_id": cls, "existing": slot_map["class"][cls], "new": ent})
            continue
        # teacher conflict
        if teacher is not None and teacher in slot_map["teacher"]:
            # conflict between existing and new
            existing = slot_map["teacher"][teacher]
            conflicts.append({"type": "teacher_conflict", "slot": slot, "teacher_id": teacher, "existing": existing, "new": ent})
            continue

        # no conflict -> commit in memory
        slot_map["class"][cls] = ent
        if teacher is not None:
            slot_map["teacher"][teacher] = ent
        # also append to global entries list
        global_schedule.setdefault("entries", []).append(ent)

    return conflicts


def try_resolve_conflicts(conflicts, global_schedule, feasible_slots_map, max_tries=100):
    """Attempt to resolve conflicts using greedy relocation and simple swap.
    feasible_slots_map is a dict like returned in level_plan['feasible_slots'] keyed by (class_id, subject_id).
    Returns resolved_conflicts, unresolved_conflicts
    """
    unresolved = []
    resolved = []

    # Helper to check if slot is free for class and teacher
    def slot_free_for(slot_idx, class_id, teacher_id):
        slot_map = global_schedule.get(slot_idx, {"teacher": {}, "class": {}})
        if class_id in slot_map.get("class", {}):
            return False
        if teacher_id is not None and teacher_id in slot_map.get("teacher", {}):
            return False
        return True

    # Greedy relocation: for each conflict try to move the 'new' entry to another feasible slot
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
                # perform move: remove old from global_schedule, add new at cand
                # remove new_ent at old slot if present
                old_slot_map = global_schedule.get(new_ent["slot_idx"], {"teacher": {}, "class": {}})
                old_slot_map.get("class", {}).pop(class_id, None)
                if teacher_id is not None:
                    old_slot_map.get("teacher", {}).pop(teacher_id, None)
                # place at new
                new_ent["slot_idx"] = cand
                slot_map_new = global_schedule.setdefault(cand, {"teacher": {}, "class": {}})
                slot_map_new["class"][class_id] = new_ent
                if teacher_id is not None:
                    slot_map_new["teacher"][teacher_id] = new_ent
                resolved.append({"conflict": c, "moved_to": cand})
                moved = True
                break
        if not moved:
            # try swap with another entry in candidate slot if that entry can move
            swapped = False
            for cand in candidates:
                slot_map = global_schedule.get(cand, {"teacher": {}, "class": {}})
                # pick any entry in cand that is not conflicting type
                for other_class_id, other_ent in list(slot_map.get("class", {}).items()):
                    other_teacher = other_ent.get("teacher_id")
                    # check if other_ent can move to new_ent's original slot
                    if slot_free_for(new_ent["slot_idx"], other_class_id, other_teacher):
                        # swap
                        # remove other from cand
                        slot_map["class"].pop(other_class_id, None)
                        if other_teacher is not None:
                            slot_map["teacher"].pop(other_teacher, None)
                        # place new_ent in cand
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
                        # move other_ent to original slot
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
# Pipeline driver
# -----------------------------

def run_timetable_pipeline(levels_ordering_strategy='most_constrained_first', time_limit_base=60, dry_run=False, persist=True, report_prefix=None):
    """End-to-end pipeline.

    - analyze levels
    - order levels
    - generate per level
    - merge progressively and try to resolve conflicts
    - persist final schedule (if persist and no/unresolved conflicts policy)
    Returns a big report dict.
    """
    report = {"start": datetime.now().isoformat(), "levels": [], "merged": {"conflicts": [], "resolved": []}}

    slots, slots_by_day, slot_conflicts = _load_slots()
    analysis = analyze_levels()
    levels_info = analysis["levels"]

    # order levels
    if levels_ordering_strategy == 'most_constrained_first':
        levels_info.sort(key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"])), reverse=True)
    elif levels_ordering_strategy == 'least_constrained_first':
        levels_info.sort(key=lambda l: (l["needed_minutes"] / max(1, l["available_minutes"])))
    else:
        levels_info.sort(key=lambda l: l["level_id"])  # default deterministic

    global_schedule = {}  # slot_idx -> {"teacher":{tid:ent}, "class":{cid:ent}} + 'entries' list
    global_schedule["entries"] = []

    all_level_plans = []

    for lvl in levels_info:
        lvl_id = lvl["level_id"]
        # adapt timeout by estimated vars roughly
        est_vars = lvl["est_vars"]
        # heuristic: base + 0.001 * est_vars seconds, capped
        timeout = min(300, max(time_limit_base, int(time_limit_base + 0.001 * est_vars)))
        print(f"Solving level {lvl['name']} (id={lvl_id}) with timeout {timeout}s ...")
        plan = generate_timetable_for_level(lvl_id, time_limit_seconds=timeout)
        plan["level_meta"] = lvl
        all_level_plans.append(plan)
        report["levels"].append({"level_id": lvl_id, "name": lvl["name"], "plan_status": plan.get("success"), "diag": plan.get("diagnostics")})

        if not plan.get("success"):
            print(f"Level {lvl['name']} failed to produce plan: {plan.get('message')}")
            # choose to continue or abort: we'll continue but report
            continue

        # merge
        conflicts = merge_level_plan_into_global(global_schedule, plan)
        if conflicts:
            print(f"Detected {len(conflicts)} conflicts when merging level {lvl['name']}")
            # try to resolve using greedy/swap on these conflicts
            resolved, unresolved = try_resolve_conflicts(conflicts, global_schedule, plan.get('feasible_slots', {}))
            report['merged']['resolved'].extend(resolved)
            if unresolved:
                report['merged']['conflicts'].extend(unresolved)
                print(f"Unresolved conflicts remain for level {lvl['name']}: {len(unresolved)}")
            else:
                print(f"All conflicts resolved for level {lvl['name']}")

    # end loop levels

    # final persistence
    if persist and not dry_run:
        # policy: persist what we have, and write a conflicts report
        try:
            with transaction.atomic():
                # clear existing entries for classes included
                class_ids = set(ent['class_id'] for ent in global_schedule.get('entries', []))
                if class_ids:
                    ClassScheduleEntry.objects.filter(school_class_id__in=list(class_ids)).delete()
                # bulk create
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

    # export report files
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    prefix = report_prefix or f"timetable_report_{ts}"
    try:
        with open(f"{prefix}.json", 'w', encoding='utf-8') as f:
            json.dump(report, f, default=str, indent=2, ensure_ascii=False)
        print(f"Report written to {prefix}.json")
    except Exception as e:
        print("Failed to write report json:", e)

    return report


# -----------------------------
# If run as script
# -----------------------------
if __name__ == '__main__':
    print("Run analysis and pipeline (dry-run)")
    r = run_timetable_pipeline(time_limit_base=60, dry_run=True, persist=False)
    print("Done. Summary:")
    print(json.dumps(r, indent=2, default=str))
