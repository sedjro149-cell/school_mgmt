# Remplacez la fonction generate_timetable existante par celle-ci
from ortools.sat.python import cp_model
from django.db import transaction
from academics.models import SchoolClass, ClassSubject, TimeSlot, ClassScheduleEntry
from core.models import Teacher
import time
import math

def _to_minutes(t):
    return t.hour * 60 + t.minute

def generate_timetable(time_limit_seconds: int = 30,
                       penalty_same_day: int = 20,
                       penalty_consecutive: int = 50,
                       allow_missing_teacher: bool = False):
    """
    Générateur d'emplois du temps utilisant OR-Tools CP-SAT.
    Améliorations ajoutées :
    - pruning des slots inutiles (feasible_slots) pour chaque (class,subject)
    - bornes supérieures sur nombre de sessions et minutes (évite over-scheduling)
    - diagnostics imprimés (taille modèle estimée)
    - timeout par défaut augmenté si time_limit_seconds trop bas
    """
    t_start = time.time()
    warnings = []
    diagnostics = {}

    # --------------------------
    # 1) Charger et préparer les slots
    # --------------------------
    time_slots = list(TimeSlot.objects.all().order_by("day", "start_time"))
    if not time_slots:
        return {"success": False, "message": "Aucun TimeSlot défini.", "warnings": [], "diagnostics": {}, "time_s": time.time() - t_start}

    slots = []
    for idx, slot in enumerate(time_slots):
        start_min = _to_minutes(slot.start_time)
        end_min = _to_minutes(slot.end_time)
        dur = end_min - start_min
        if dur <= 0:
            return {"success": False, "message": f"TimeSlot id={slot.id} a une durée non positive.", "warnings": [], "diagnostics": {}, "time_s": time.time() - t_start}
        slots.append({
            "idx": idx,
            "db_obj": slot,
            "weekday": slot.day,
            "start": start_min,
            "end": end_min,
            "dur": dur
        })

    # map day -> list of slot indices
    slots_by_day = {}
    for s in slots:
        slots_by_day.setdefault(s["weekday"], []).append(s["idx"])

    # compute maximum slots on any day (for big-M)
    max_slots_per_day = max((len(idxs) for idxs in slots_by_day.values())) if slots_by_day else 0
    if max_slots_per_day <= 0:
        return {"success": False, "message": "Aucun créneau valide par jour.", "warnings": warnings, "diagnostics": {}, "time_s": time.time() - t_start}

    # build conflict sets: overlapping slots on same weekday
    slot_conflicts = {s["idx"]: set() for s in slots}
    for day, idxs in slots_by_day.items():
        for a in range(len(idxs)):
            i = idxs[a]
            s_i = slots[i]
            for b in range(a + 1, len(idxs)):
                j = idxs[b]
                s_j = slots[j]
                # overlapping if start < other's end and other start < end
                if (s_i["start"] < s_j["end"]) and (s_j["start"] < s_i["end"]):
                    slot_conflicts[i].add(j)
                    slot_conflicts[j].add(i)

    # consecutive pairs (small gap, touching)
    consecutive_pairs = set()
    for day, idxs in slots_by_day.items():
        sorted_idxs = sorted(idxs, key=lambda _i: slots[_i]["start"])
        for a in range(len(sorted_idxs)):
            for b in range(a+1, len(sorted_idxs)):
                i = sorted_idxs[a]; j = sorted_idxs[b]
                if slots[j]["start"] >= slots[i]["end"]:
                    gap = slots[j]["start"] - slots[i]["end"]
                    # consecutive-ish threshold: <= 15 minutes treated as consecutive
                    if gap <= 15:
                        consecutive_pairs.add((i, j))
                    if gap > 60:
                        break

    # --------------------------
    # 2) Charger classes, subjects et teachers
    # --------------------------
    classes = {}  # cls_id -> {"obj": cls, "subjects": {subj_id: {hours_min, teacher_id, classsubject_id}}}
    missing_teachers = []
    for cls in SchoolClass.objects.all():
        cs_qs = ClassSubject.objects.filter(school_class=cls).select_related("subject")
        subj_map = {}
        for cs in cs_qs:
            subj = cs.subject
            subj_id = subj.id
            hours_per_week = getattr(cs, "hours_per_week", None)
            if hours_per_week is None:
                warnings.append(f"ClassSubject id={cs.id} (class={cls}) sans hours_per_week, ignoré.")
                continue
            teacher = Teacher.objects.filter(subject=subj, classes=cls).first()
            if not teacher:
                missing_teachers.append({"class": str(cls), "class_id": cls.id, "subject": subj.name, "subject_id": subj_id})
                if not allow_missing_teacher:
                    # skip adding this subject because no teacher; user asked de zaper => on laisse skip
                    continue
                teacher_id = None
            else:
                teacher_id = teacher.id

            subj_map[subj_id] = {
                "hours_min": int(hours_per_week * 60),
                "teacher_id": teacher_id,
                "classsubject_id": cs.id
            }

        if subj_map:
            classes[cls.id] = {"obj": cls, "subjects": subj_map}
        else:
            warnings.append(f"Classe {cls} (id={cls.id}) n'a aucune matière utilisable (avec prof si requis).")

    if not classes:
        return {"success": False, "message": "Aucune classe valide (avec matière+prof) trouvée.", "warnings": warnings, "diagnostics": {}, "time_s": time.time() - t_start}

    if missing_teachers:
        warnings.append(f"{len(missing_teachers)} ClassSubject(s) sans teacher (voir diagnostics['missing_teachers']).")

    diagnostics["num_classes_included"] = len(classes)
    diagnostics["num_slots"] = len(slots)
    diagnostics["missing_teachers"] = missing_teachers

    # Pre-check capacity per class (coarse)
    total_available_minutes = sum(s["dur"] for s in slots)
    capacity_issues = []
    for c_id, c_data in classes.items():
        needed = sum(s["hours_min"] for s in c_data["subjects"].values())
        if needed > total_available_minutes:
            capacity_issues.append({"class_id": c_id, "needed": needed, "available": total_available_minutes})
    if capacity_issues:
        return {"success": False, "message": "Capacité horaire insuffisante pour au moins une classe.", "details": capacity_issues, "warnings": warnings, "diagnostics": diagnostics, "time_s": time.time() - t_start}

    # --------------------------
    # Diagnostic imprimé (taille modèle estimée)
    # --------------------------
    num_classsubjects = sum(len(c["subjects"]) for c in classes.values())
    estimated_vars = num_classsubjects * len(slots)
    print("DIAGNOSTIC: classes:", len(classes), "classsubjects:", num_classsubjects, "slots:", len(slots),
          "estimated X variables (before pruning):", estimated_vars)

    # --------------------------
    # 3) PRUNING: calculer feasible_slots par (c, s)
    # --------------------------
    min_slot_dur = min(s["dur"] for s in slots)
    max_slot_dur = max(s["dur"] for s in slots)

    feasible_slots = {}
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            # Heuristique : n'autoriser que les slots dont durée <= needed + max_slot_dur
            # (on autorise un slot plus gros que needed pour combler la dernière portion)
            cand = [i for i in range(len(slots)) if slots[i]["dur"] <= needed + max_slot_dur]
            # si aucune candidate, alors on autorise au moins les slots dont dur <= needed*2 (fallback)
            if not cand:
                cand = [i for i in range(len(slots)) if slots[i]["dur"] <= max(needed * 2, max_slot_dur)]
            feasible_slots[(c_id, s_id)] = cand

    # Recompute estimated variables after pruning
    est_after_pruning = sum(len(v) for v in feasible_slots.values())
    print("DIAGNOSTIC: estimated X variables AFTER pruning:", est_after_pruning)

    # Quick infeasibility check: any (c,s) without feasible slots => impossible
    no_feasible = [(c_id, s_id) for (c_id, s_id), sls in feasible_slots.items() if not sls]
    if no_feasible:
        warnings.append(f"{len(no_feasible)} class-subject pairs n'ont aucun créneau feasible. Voir diagnostics.")
        diagnostics["no_feasible_pairs"] = no_feasible
        return {"success": False, "message": "Certains (class,subject) n'ont aucun slot feasible.", "warnings": warnings, "diagnostics": diagnostics, "time_s": time.time() - t_start}

    # --------------------------
    # 4) Construire le modèle CP-SAT (avec X uniquement sur feasible_slots)
    # --------------------------
    model = cp_model.CpModel()

    # X[(c,s,i)] boolean: assign subject s for class c at slot index i (only for feasible slots)
    X = {}
    for (c_id, s_id), slot_list in feasible_slots.items():
        for i in slot_list:
            X[(c_id, s_id, i)] = model.NewBoolVar(f"x_c{c_id}_s{s_id}_t{i}")

    # --------------------------
    # 5) Hard constraints (avec bornes supérieures)
    # --------------------------
    # 5.a Quota horaire minimal (minutes) and upper bounds
    for c_id, c_data in classes.items():
        for s_id, s_data in c_data["subjects"].items():
            needed = s_data["hours_min"]
            slot_list = feasible_slots[(c_id, s_id)]
            # min minutes
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) >= needed)
            # upper bound on number of sessions (no more than ceil(needed/min_slot_dur))
            max_sessions = math.ceil(needed / min_slot_dur) if min_slot_dur > 0 else len(slot_list)
            model.Add(sum(X[(c_id, s_id, i)] for i in slot_list) <= max_sessions)
            # upper bound minutes to avoid large overfill (tolerance max_slot_dur)
            model.Add(sum(X[(c_id, s_id, i)] * slots[i]["dur"] for i in slot_list) <= needed + max_slot_dur)

    # 5.b Une seule matière par classe pour un même slot (pas de double booking de classe)
    for i in range(len(slots)):
        for c_id, c_data in classes.items():
            # sum only over subject ids s_id for which i in feasible_slots[(c_id,s_id)]
            relevant_sids = [s_id for s_id in c_data["subjects"].keys() if i in feasible_slots.get((c_id, s_id), [])]
            if not relevant_sids:
                continue
            model.Add(sum(X[(c_id, s_id, i)] for s_id in relevant_sids) <= 1)

    # 5.c Pas de chevauchement réel pour une même classe (paires de slots qui se chevauchent)
    for c_id, c_data in classes.items():
        subj_ids = list(c_data["subjects"].keys())
        for i in range(len(slots)):
            for j in slot_conflicts[i]:
                # build sums only where (c_id,s_id,i) exists in X
                terms_i = [X[(c_id, s_id, i)] for s_id in subj_ids if (c_id, s_id, i) in X]
                terms_j = [X[(c_id, s_id, j)] for s_id in subj_ids if (c_id, s_id, j) in X]
                if terms_i or terms_j:
                    model.Add(sum(terms_i) + sum(terms_j) <= 1)

    # 5.d Pas de chevauchement réel pour un même teacher (pairwise over conflicting slots)
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

    # --------------------------
    # 6) Soft constraints: same-day multi-session & consecutive-day presence
    # --------------------------
    P_same_day_vars = []
    D_day = {}  # (c_id, s_id, day) -> BoolVar
    P_consec_vars = []

    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for day, idxs in slots_by_day.items():
                # restrict idxs to those that are feasible for (c,s)
                day_idxs = [i for i in idxs if (c_id, s_id, i) in X]
                if not day_idxs:
                    continue
                # D_day: whether subject appears at least once that day
                D = model.NewBoolVar(f"d_c{c_id}_s{s_id}_day{day}")
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) >= D)
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) <= max_slots_per_day * D)
                D_day[(c_id, s_id, day)] = D

                P_same = model.NewBoolVar(f"p_same_c{c_id}_s{s_id}_day{day}")
                model.Add(sum(X[(c_id, s_id, i)] for i in day_idxs) <= 1 + max_slots_per_day * P_same)
                P_same_day_vars.append(P_same)

    weekdays_sorted = sorted(slots_by_day.keys())
    for c_id, c_data in classes.items():
        for s_id in c_data["subjects"].keys():
            for iwd in range(len(weekdays_sorted) - 1):
                d1 = weekdays_sorted[iwd]
                d2 = weekdays_sorted[iwd + 1]
                D1 = D_day.get((c_id, s_id, d1), None)
                D2 = D_day.get((c_id, s_id, d2), None)
                if D1 is None or D2 is None:
                    continue
                Pcon = model.NewBoolVar(f"p_consec_c{c_id}_s{s_id}_d{d1}_{d2}")
                model.Add(Pcon >= D1 + D2 - 1)
                model.Add(Pcon <= D1)
                model.Add(Pcon <= D2)
                P_consec_vars.append(Pcon)

    # --------------------------
    # 7) Objective (minimize minutes used + penalties)
    # --------------------------
    total_minutes_used = sum(X[(c_id, s_id, i)] * slots[i]["dur"]
                             for (c_id, s_id, i) in X.keys())

    sum_same = sum(P_same_day_vars) if P_same_day_vars else 0
    sum_consec = sum(P_consec_vars) if P_consec_vars else 0

    objective = total_minutes_used + penalty_same_day * sum_same + penalty_consecutive * sum_consec
    model.Minimize(objective)

    # --------------------------
    # 8) Solve
    # --------------------------
    solver = cp_model.CpSolver()
    # raise time limit if too small; pour les gros problèmes, 120s est un bon début
    solver.parameters.max_time_in_seconds = max(time_limit_seconds, 120)
    solver.parameters.num_search_workers = 8
    # solver.parameters.log_search_progress = True  # activer si tu veux la sortie de log OR-Tools
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"success": False,
                "message": "Aucune solution trouvée (contrariantes trop fortes / timeout).",
                "status": int(status),
                "warnings": warnings,
                "diagnostics": diagnostics,
                "time_s": time.time() - t_start}

    # --------------------------
    # 9) Stockage atomique
    # --------------------------
    created = 0
    try:
        with transaction.atomic():
            ClassScheduleEntry.objects.filter(school_class_id__in=list(classes.keys())).delete()
            for (c_id, s_id, i), var in list(X.items()):
                if solver.Value(var) == 1:
                    ClassScheduleEntry.objects.create(
                        school_class_id=c_id,
                        subject_id=s_id,
                        teacher_id=classes[c_id]["subjects"][s_id]["teacher_id"],
                        weekday=slots[i]["weekday"],
                        starts_at=slots[i]["db_obj"].start_time,
                        ends_at=slots[i]["db_obj"].end_time,
                    )
                    created += 1
    except Exception as e:
        return {"success": False, "message": f"Erreur sauvegarde: {e}", "warnings": warnings, "diagnostics": diagnostics, "time_s": time.time() - t_start}

    elapsed = time.time() - t_start
    diagnostics["solver_status"] = int(status)
    diagnostics["num_created"] = created
    diagnostics["num_classes_included"] = len(classes)
    diagnostics["estimated_vars_after_pruning"] = est_after_pruning

    return {
        "success": True,
        "message": "Emplois du temps générés.",
        "created": created,
        "warnings": warnings,
        "diagnostics": diagnostics,
        "time_s": elapsed
    }
