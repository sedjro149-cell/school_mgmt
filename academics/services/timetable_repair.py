"""
Timetable repair utilities

Place this file in `academics/services/timetable_repair.py`.

Purpose:
 - Reuse your standalone detection logic to detect "same teacher + same class + same weekday" duplicates
 - Attempt local repairs (move, pair-swap, small k-cycle swaps) *without* breaking hard constraints
 - Return a report of resolved/unresolved duplicates and a small set of class ids that are good candidates
   for a limited global re-solve (so the caller -- timetable pipeline -- can invoke generate_timetable_for_levels
   if needed).

Design notes:
 - This module is intentionally self-contained and does *not* call back into the large timetable_by_level module
   to avoid circular import risk. It performs local repairs only. If repairs fail, it returns candidates for a
   targeted re-solve which the pipeline can run with `generate_timetable_for_levels`.
 - The functions operate on the `global_schedule` structure used in timetable_by_level: a dict where
   integer slot indices map to {"teacher":{tid:ent,...}, "class":{cid:ent,...}} and there's a global_schedule["entries"]
   list of entry dicts (each with keys like class_id, subject_id, teacher_id, slot_idx, weekday, starts_at, ends_at).

API:
 - detect_duplicates_in_global(global_schedule)
 - repair_duplicates_in_global(global_schedule, slots, slot_conflicts, slot_adjacent, feasible_slots_map=None, ...)

Return value of repair function: report dict with keys: resolved (list), unresolved (list), candidates_for_resolve (set), log (list)

"""
from collections import defaultdict, deque
import random
from copy import deepcopy

ALLOWED_SLOT_DURS = {120, 180}

# -----------------------------
# Detection
# -----------------------------

def detect_duplicates_in_global(global_schedule):
    """
    Find groups where the same (teacher_id, class_id, weekday) appears more than once.
    Returns list of groups: each group is dict {teacher_id, class_id, weekday, entries: [entry_dicts]}
    """
    by_key = defaultdict(list)
    for ent in global_schedule.get("entries", []):
        tid = ent.get("teacher_id")
        cid = ent.get("class_id")
        wd = ent.get("weekday")
        # only consider entries that have teacher, class and weekday
        if tid is None or cid is None or wd is None:
            continue
        by_key[(tid, cid, wd)].append(ent)

    groups = []
    for (tid, cid, wd), group in by_key.items():
        if len(group) > 1:
            groups.append({
                "teacher_id": tid,
                "class_id": cid,
                "weekday": wd,
                "entries": group,
            })
    return groups


# -----------------------------
# Helpers to manipulate global_schedule
# -----------------------------

def build_slot_maps_from_entries(entries):
    """Build slot map structure (slot_idx -> {"teacher":{...}, "class":{...}}) from entries list."""
    slot_maps = {}
    for ent in entries:
        sidx = ent.get("slot_idx")
        if sidx is None:
            continue
        sm = slot_maps.setdefault(sidx, {"teacher": {}, "class": {}})
        cid = ent.get("class_id")
        tid = ent.get("teacher_id")
        if cid is not None:
            sm["class"][cid] = ent
        if tid is not None:
            sm["teacher"][tid] = ent
    return slot_maps


def slot_free_for(global_slot_maps, slot_idx, class_id, teacher_id):
    sm = global_slot_maps.get(slot_idx, {"teacher": {}, "class": {}})
    if class_id in sm.get("class", {}):
        return False
    if teacher_id is not None and teacher_id in sm.get("teacher", {}):
        return False
    return True


def get_entry_slot_idx(ent):
    return ent.get("slot_idx")


def update_entry_to_slot(ent, new_slot_idx, slots):
    """Update the entry dict to reflect being at slot new_slot_idx (weekday, starts_at, ends_at updated).
    Assumes slots is the list returned by _load_slots (dict per slot)."""
    s = slots[new_slot_idx]
    ent["slot_idx"] = new_slot_idx
    ent["weekday"] = s["weekday"]
    ent["starts_at"] = s["db_obj"].start_time
    ent["ends_at"] = s["db_obj"].end_time


# -----------------------------
# Mobility heuristic
# -----------------------------

def mobility_score_for_entry(global_slot_maps, ent, slots):
    """Higher score = easier to move.
    Heuristic factors: number of free slots for teacher, number of free slots for class, whether current day is crowded.
    """
    teacher_id = ent.get("teacher_id")
    class_id = ent.get("class_id")
    free_teacher = 0
    free_class = 0
    for s in slots:
        idx = s["idx"]
        sm = global_slot_maps.get(idx, {"teacher": {}, "class": {}})
        if teacher_id not in sm.get("teacher", {}):
            free_teacher += 1
        if class_id not in sm.get("class", {}):
            free_class += 1
    # simple linear combination
    return free_teacher * 2 + free_class


# -----------------------------
# Move / Swap primitives
# -----------------------------

def check_move_valid(ent, target_idx, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries):
    """
    Check whether moving `ent` to `target_idx` would violate:
      - class/teacher overlap at target slot
      - adjacency/overlap of same (class, subject) with that class's other sessions
      - create same-day duplicate for same (teacher,class)
    Returns (True, reason) or (False, reason)
    """
    class_id = ent.get("class_id")
    teacher_id = ent.get("teacher_id")
    subject_id = ent.get("subject_id")

    # 1) slot free for class/teacher
    if not slot_free_for(global_slot_maps, target_idx, class_id, teacher_id):
        return False, "target_not_free_for_class_or_teacher"

    # 2) ensure duration allowed
    dur = slots[target_idx]["dur"]
    if dur not in ALLOWED_SLOT_DURS:
        return False, "slot_duration_not_allowed"

    # 3) adjacency/overlap with other sessions of same (class,subject)
    for other in global_schedule_entries:
        if other is ent:
            continue
        if other.get("class_id") != class_id or other.get("subject_id") != subject_id:
            continue
        oidx = other.get("slot_idx")
        if oidx is None:
            continue
        # adjacency or overlap forbidden
        if target_idx == oidx or target_idx in slot_conflicts.get(oidx, set()) or target_idx in slot_adjacent.get(oidx, set()):
            return False, "would_adjacent_or_overlap_same_class_subject"

    # 4) create same-day duplicate for same (teacher,class)
    # check for any other entry with same teacher and class on that weekday
    target_weekday = slots[target_idx]["weekday"]
    for other in global_schedule_entries:
        if other is ent:
            continue
        if other.get("teacher_id") == teacher_id and other.get("class_id") == class_id:
            if other.get("weekday") == target_weekday:
                return False, "would_create_same_day_duplicate"

    # 5) ensure not create consecutive-day violation for same (class,subject)
    # the code prohibits same (class,subject) on consecutive days; here we must avoid creating that.
    # collect other weekdays for same (class,subject)
    other_days = set()
    for other in global_schedule_entries:
        if other is ent:
            continue
        if other.get("class_id") == class_id and other.get("subject_id") == subject_id:
            wd = other.get("weekday")
            if wd is not None:
                other_days.add(wd)
    for d in other_days:
        if abs(d - target_weekday) == 1:
            return False, "would_create_consecutive_day_same_class_subject"

    return True, "ok"


def try_move_entry(ent, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries, max_candidates=50):
    """Try to move ent to a free compatible slot. If success, mutate ent and global_slot_maps and return (True, detail)
    Otherwise return (False, reason)
    """
    class_id = ent.get("class_id")
    teacher_id = ent.get("teacher_id")
    current_idx = ent.get("slot_idx")

    # generate candidate slot indices ordered by heuristic: prefer slots with allowed durations and where class and teacher free
    candidates = [s["idx"] for s in slots if s["dur"] in ALLOWED_SLOT_DURS and s["idx"] != current_idx]

    # shuffle to add slight randomization but keep reproducible-ish (seeded by random state if caller wants)
    random.shuffle(candidates)

    tried = 0
    for cand in candidates:
        if tried >= max_candidates:
            break
        tried += 1
        ok, reason = check_move_valid(ent, cand, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries)
        if not ok:
            continue
        # apply move: remove from old slot map, add to new slot map
        old_sm = global_slot_maps.get(current_idx, {"teacher": {}, "class": {}})
        # remove pointers (safe pop)
        old_sm.get("class", {}).pop(class_id, None)
        if teacher_id is not None:
            old_sm.get("teacher", {}).pop(teacher_id, None)
        # add to new slot map
        new_sm = global_slot_maps.setdefault(cand, {"teacher": {}, "class": {}})
        new_sm.setdefault("class", {})[class_id] = ent
        if teacher_id is not None:
            new_sm.setdefault("teacher", {})[teacher_id] = ent
        # update the entry dict
        update_entry_to_slot(ent, cand, slots)
        return True, {"move_to": cand}

    return False, "no_candidate_found"


def try_pair_swap(ent, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries, max_pairs=200):
    """Try swapping ent with a single occupying entry in another slot. Returns (True, detail) on success."""
    current_idx = ent.get("slot_idx")
    class_id = ent.get("class_id")
    teacher_id = ent.get("teacher_id")

    # search occupied slots that might host ent
    for cand_idx, sm in list(global_slot_maps.items()):
        if cand_idx == current_idx:
            continue
        # for each other class in that slot, attempt swap
        for other_cid, other_ent in list(sm.get("class", {}).items()):
            other_tid = other_ent.get("teacher_id")
            other_idx = other_ent.get("slot_idx")
            # can we move ent -> cand_idx ? and other_ent -> current_idx ?
            ok1, r1 = check_move_valid(ent, cand_idx, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries)
            if not ok1:
                continue
            # temporary mutate to allow checking other move (simulate ent removed from current_idx and occupying cand_idx)
            # we'll create shallow copies of slot maps to test swap validity
            temp_slot_maps = deepcopy(global_slot_maps)
            # remove ent from current slot
            temp_slot_maps.get(current_idx, {"teacher":{}, "class":{}}).get("class", {}).pop(class_id, None)
            if teacher_id is not None:
                temp_slot_maps.get(current_idx, {"teacher":{}, "class":{}}).get("teacher", {}).pop(teacher_id, None)
            # place ent in cand_idx
            temp_slot_maps.setdefault(cand_idx, {"teacher": {}, "class": {}}).setdefault("class", {})[class_id] = ent
            if teacher_id is not None:
                temp_slot_maps.setdefault(cand_idx, {"teacher": {}, "class": {}}).setdefault("teacher", {})[teacher_id] = ent

            ok2, r2 = check_move_valid(other_ent, current_idx, temp_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries)
            if not ok2:
                continue

            # swap is valid: perform actual swap on global_slot_maps
            # remove other_ent from cand_idx
            global_slot_maps.get(cand_idx, {"teacher":{}, "class":{}}).get("class", {}).pop(other_cid, None)
            if other_tid is not None:
                global_slot_maps.get(cand_idx, {"teacher":{}, "class":{}}).get("teacher", {}).pop(other_tid, None)
            # remove ent from current slot
            global_slot_maps.get(current_idx, {"teacher":{}, "class":{}}).get("class", {}).pop(class_id, None)
            if teacher_id is not None:
                global_slot_maps.get(current_idx, {"teacher":{}, "class":{}}).get("teacher", {}).pop(teacher_id, None)

            # place ent in cand_idx
            global_slot_maps.setdefault(cand_idx, {"teacher": {}, "class": {}}).setdefault("class", {})[class_id] = ent
            if teacher_id is not None:
                global_slot_maps.setdefault(cand_idx, {"teacher": {}, "class": {}}).setdefault("teacher", {})[teacher_id] = ent
            # place other_ent in current_idx
            global_slot_maps.setdefault(current_idx, {"teacher": {}, "class": {}}).setdefault("class", {})[other_cid] = other_ent
            if other_tid is not None:
                global_slot_maps.setdefault(current_idx, {"teacher": {}, "class": {}}).setdefault("teacher", {})[other_tid] = other_ent

            # update entry slot indices
            update_entry_to_slot(ent, cand_idx, slots)
            update_entry_to_slot(other_ent, current_idx, slots)
            return True, {"swap_with": other_ent}

    return False, "no_valid_pair_swap"


def try_k_cycle_swap(ent, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries, max_depth=3):
    """
    Attempt a small k-cycle of swaps: ent -> slot A (occupied by e1), e1 -> slot B (occupied by e2), ... e_k -> ent's original slot.
    Limited DFS with depth up to max_depth. Returns (True, path) on success.
    """
    start_idx = ent.get("slot_idx")
    class_id = ent.get("class_id")

    # helper: recursively try extend cycle
    path = []  # list of (moving_ent, target_idx)
    visited_slots = set()

    def dfs(current_ent, target_idx, depth):
        # current_ent wants to move to target_idx which is occupied by some other entry
        if depth > max_depth:
            return None
        sm = global_slot_maps.get(target_idx, {"teacher": {}, "class": {}})
        # if target free, we can place current_ent there and attempt to move chain back to start
        occupiers = list(sm.get("class", {}).values())
        if not occupiers:
            # we found a free slot -> now try to move the chain back by moving last occupant into start
            return [(current_ent, target_idx)]
        # otherwise for each occupant, attempt to push it further
        for other_ent in occupiers:
            other_idx = other_ent.get("slot_idx")
            if other_idx in visited_slots:
                continue
            visited_slots.add(other_idx)
            # simulate moving current_ent to target_idx: must be valid
            ok, reason = check_move_valid(current_ent, target_idx, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries)
            if not ok:
                continue
            # now attempt to move other_ent somewhere (recursively)
            # search candidate slots for other_ent (prefer 2h/3h slots)
            candidate_slots = [s["idx"] for s in slots if s["dur"] in ALLOWED_SLOT_DURS and s["idx"] != other_ent.get("slot_idx")]
            random.shuffle(candidate_slots)
            for cand in candidate_slots[:50]:
                # avoid cycles to same slot already in path
                if cand == target_idx:
                    continue
                # attempt to move other_ent to cand
                ok2, r2 = check_move_valid(other_ent, cand, global_slot_maps, slots, slot_conflicts, slot_adjacent, global_schedule_entries)
                if not ok2:
                    continue
                # recurse deeper: try to place whatever occupies cand further
                res = dfs(other_ent, cand, depth + 1)
                if res:
                    # prepend current move and return
                    return [(current_ent, target_idx)] + res
            # backtrack
            visited_slots.remove(other_idx)
        return None

    # Start by exploring candidate target slots for ent (where it's not allowed to stay)
    candidate_targets = [s["idx"] for s in slots if s["dur"] in ALLOWED_SLOT_DURS and s["idx"] != start_idx]
    random.shuffle(candidate_targets)
    for t in candidate_targets[:200]:
        visited_slots.clear()
        res = dfs(ent, t, 1)
        if res:
            # apply the cycle: res is list [(eA, tA), (eB, tB), ...]
            # we perform moves in order but carefull to rotate slot indices
            # capture original slot indices
            originals = {e.get("slot_idx"): e for e, _ in res}
            # apply moves: for chain length L, move last occupant last
            for moving_ent, target_idx in res:
                # remove moving_ent from its current slot
                cur = moving_ent.get("slot_idx")
                global_slot_maps.get(cur, {"teacher":{}, "class":{}}).get("class", {}).pop(moving_ent.get("class_id"), None)
                if moving_ent.get("teacher_id") is not None:
                    global_slot_maps.get(cur, {"teacher":{}, "class":{}}).get("teacher", {}).pop(moving_ent.get("teacher_id"), None)
                # place in target
                global_slot_maps.setdefault(target_idx, {"teacher": {}, "class": {}}).setdefault("class", {})[moving_ent.get("class_id")] = moving_ent
                if moving_ent.get("teacher_id") is not None:
                    global_slot_maps.setdefault(target_idx, {"teacher": {}, "class": {}}).setdefault("teacher", {})[moving_ent.get("teacher_id")] = moving_ent
                update_entry_to_slot(moving_ent, target_idx, slots)
            return True, {"cycle_len": len(res)}
    return False, "no_cycle_found"


# -----------------------------
# Top-level repair routine
# -----------------------------

def repair_duplicates_in_global(global_schedule, slots, slot_conflicts, slot_adjacent, feasible_slots_map=None,
                                 max_moves_per_duplicate=2, max_swap_attempts=50, max_cycle_depth=3, debug=False):
    """
    Attempt to repair same-day duplicates in `global_schedule` by local moves/swaps/cycles.

    Parameters:
      - feasible_slots_map: optional map (class_id, subject_id) -> [allowed_slot_idxs] produced by generators; used to
        restrict candidate slots search and speed up.

    Mutates global_schedule in-place if moves/swaps are applied.

    Returns dict report {
       resolved: list of {teacher_id,class_id,weekday,action,detail}
       unresolved: list of groups still in conflict
       candidates_for_resolve: set of class_ids (and teacher_ids optional) that should be included in a
                               targeted re-solve (caller may call generate_timetable_for_levels)
       log: list of human-friendly log strings
    }
    """
    report = {"resolved": [], "unresolved": [], "candidates_for_resolve": set(), "log": []}

    entries = global_schedule.get("entries", [])
    if not entries:
        return report

    # build slot maps and quick lookup
    slot_maps = build_slot_maps_from_entries(entries)

    groups = detect_duplicates_in_global(global_schedule)
    if not groups:
        return report

    # process groups; sort by "difficulty" - bigger groups and heavier classes first
    groups.sort(key=lambda g: (-len(g["entries"]), g["class_id"]))

    for g in groups:
        tid = g["teacher_id"]
        cid = g["class_id"]
        wd = g["weekday"]
        ents = g["entries"]
        if debug:
            report["log"].append(f"Processing duplicate teacher={tid} class={cid} weekday={wd} count={len(ents)}")

        # compute mobility score per entry and sort so we try moving easiest ones first
        scored = [(mobility_score_for_entry(slot_maps, e, slots), e) for e in ents]
        scored.sort(reverse=True, key=lambda x: x[0])
        # keep first as anchor, try to move others
        anchor = scored[0][1]
        to_move = [e for _, e in scored[1:]]

        moved_count = 0
        for ent in to_move:
            if moved_count >= max_moves_per_duplicate:
                break
            # try move
            ok, detail = try_move_entry(ent, slot_maps, slots, slot_conflicts, slot_adjacent, entries, max_candidates=50)
            if ok:
                moved_count += 1
                report["resolved"].append({"teacher_id": tid, "class_id": cid, "weekday": wd, "action": "move", "detail": detail})
                report["log"].append(f"Moved entry {ent.get('class_id')} to slot {detail.get('move_to')}")
                continue
            # try pair swap
            ok2, detail2 = try_pair_swap(ent, slot_maps, slots, slot_conflicts, slot_adjacent, entries)
            if ok2:
                moved_count += 1
                report["resolved"].append({"teacher_id": tid, "class_id": cid, "weekday": wd, "action": "pair_swap", "detail": detail2})
                report["log"].append(f"Pair swap for entry {ent.get('class_id')} swapped with {detail2.get('swap_with', {}).get('class_id')}")
                continue
            # try small cycles
            ok3, detail3 = try_k_cycle_swap(ent, slot_maps, slots, slot_conflicts, slot_adjacent, entries, max_depth=max_cycle_depth)
            if ok3:
                moved_count += 1
                report["resolved"].append({"teacher_id": tid, "class_id": cid, "weekday": wd, "action": "k_cycle", "detail": detail3})
                report["log"].append(f"Performed k-cycle for entry class {ent.get('class_id')} result={detail3}")
                continue
            # if none worked, mark candidate for targeted re-solve
            report["unresolved"].append({"teacher_id": tid, "class_id": cid, "weekday": wd, "entries": [e.copy() for e in ents]})
            report["candidates_for_resolve"].add(cid)
            # also add teacher id as candidate context (helpful for building subset)
            if tid is not None:
                report["candidates_for_resolve"].add(tid)
            if debug:
                report["log"].append(f"Could not repair duplicate teacher={tid} class={cid} weekday={wd}")

    # apply slot_maps back to global_schedule top-level maps
    # re-build top-level slot maps in global_schedule
    # clear numeric keys
    for k in list(global_schedule.keys()):
        if k == "entries":
            continue
        if isinstance(k, int):
            global_schedule.pop(k, None)
    # write back slot_maps
    for sidx, sm in slot_maps.items():
        global_schedule[sidx] = {"teacher": dict(sm.get("teacher", {})), "class": dict(sm.get("class", {}))}

    # entries already mutated in-place by update_entry_to_slot; ensure global_schedule["entries"] reflect current
    global_schedule["entries"] = entries

    return report


# -----------------------------
# Usage note (callers)
# -----------------------------
# Typical integration in timetable_by_level.run_timetable_pipeline:
#
#   from academics.services.timetable_repair import repair_duplicates_in_global
#   # after merging a level (or at the end of the pipeline):
#   repair_report = repair_duplicates_in_global(global_schedule, slots, slot_conflicts, slot_adjacent, feasible_slots_map=plan.get('feasible_slots'))
#   # if repair_report['unresolved'] not empty:
#   #    # build small subset of levels/classes and call generate_timetable_for_levels(subset)
#
# The function returns resolved/unresolved and candidates_for_resolve.
# Caller (the pipeline) should decide whether to call generate_timetable_for_levels
# on the candidate subset (recommended) and then merge the plan using the existing
# merge/resolve helpers in timetable_by_level.
#
# End of file
