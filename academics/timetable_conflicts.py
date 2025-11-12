# academics/services/timetable_conflicts.py
import random
import logging
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

from django.db import transaction

from academics.models import ClassScheduleEntry, TimeSlot, SchoolClass, ClassSubject
from core.models import Teacher

logger = logging.getLogger(__name__)


# ---------- Helpers to load time slots (same logic as in generator) ----------
def _to_minutes(t):
    return t.hour * 60 + t.minute


def _load_slots():
    """Retourne (slots, slots_by_day, slot_conflicts) identiques à timetable generator."""
    time_slots = list(TimeSlot.objects.all().order_by("day", "start_time"))
    slots = []
    for idx, slot in enumerate(time_slots):
        start_min = _to_minutes(slot.start_time)
        end_min = _to_minutes(slot.end_time)
        dur = end_min - start_min
        if dur <= 0:
            # ignore invalid slots but log
            logger.warning("TimeSlot id=%s a durée non positive, ignoré", getattr(slot, "id", None))
            continue
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


# ---------- Map DB entry -> slot_idx (if exact match) ----------
def map_entry_to_slot_idx(entry: ClassScheduleEntry, slots: List[dict]) -> Optional[int]:
    """Tente de retrouver index du TimeSlot correspondant en comparant weekday/start/end strictement."""
    for s in slots:
        if s["weekday"] == entry.weekday and s["db_obj"].start_time == entry.starts_at and s["db_obj"].end_time == entry.ends_at:
            return s["idx"]
    return None


# ---------- Detection ----------
def detect_teacher_conflicts() -> Dict:
    """
    Balaye tous les ClassScheduleEntry en base et renvoie :
      - teacher_conflicts: liste de dict {teacher_id, teacher_name, day, overlapping_pairs: [entry1_repr, entry2_repr, ...]}
      - class_conflicts: (optionnel: on signale si une classe a deux choses identiques)
      - meta: counts, sample rows, etc.
    """
    slots, slots_by_day, slot_conflicts = _load_slots()

    # Récupérer les entrées persistées
    entries = list(ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher").all())

    # Build per-teacher per-day list (use teacher_id to be robust)
    by_teacher_day = defaultdict(list)
    for e in entries:
        t_id = e.teacher_id  # peut être None
        if not t_id:
            continue
        by_teacher_day[(t_id, e.weekday)].append(e)

    teacher_conflicts = []
    class_conflicts = []

    # Check teacher overlaps by comparing time intervals (robuste si timeslot mapping absent)
    for (t_id, day), ents in by_teacher_day.items():
        # sort by start
        ents_sorted = sorted(ents, key=lambda x: x.starts_at)
        overlaps = []
        for i in range(len(ents_sorted) - 1):
            e1 = ents_sorted[i]; e2 = ents_sorted[i + 1]
            # chevauchement si e1.ends > e2.starts (time objects comparable)
            if e1.ends_at > e2.starts_at:
                overlaps.append((e1, e2))
        if overlaps:
            teacher_conflicts.append({
                "teacher_id": t_id,
                "teacher_name": str(ents_sorted[0].teacher) if getattr(ents_sorted[0], "teacher", None) else None,
                "weekday": day,
                "overlaps": [
                    {
                        "entry_ids": [o[0].id, o[1].id],
                        "class_ids": [o[0].school_class_id, o[1].school_class_id],
                        "class_names": [str(o[0].school_class), str(o[1].school_class)],
                        "subject_ids": [o[0].subject_id, o[1].subject_id],
                        "subject_names": [str(o[0].subject), str(o[1].subject)],
                        "times": [str(o[0].starts_at) + " - " + str(o[0].ends_at), str(o[1].starts_at) + " - " + str(o[1].ends_at)]
                    } for o in overlaps
                ]
            })

    # Check class overlaps too (defensive)
    by_class_day = defaultdict(list)
    for e in entries:
        by_class_day[(e.school_class_id, e.weekday)].append(e)
    for (cid, day), ents in by_class_day.items():
        ents_sorted = sorted(ents, key=lambda x: x.starts_at)
        overlaps = []
        for i in range(len(ents_sorted) - 1):
            e1 = ents_sorted[i]; e2 = ents_sorted[i + 1]
            if e1.ends_at > e2.starts_at:
                overlaps.append((e1, e2))
        if overlaps:
            class_conflicts.append({
                "class_id": cid,
                "class_name": str(ents_sorted[0].school_class),
                "weekday": day,
                "overlaps": [
                    {
                        "entry_ids": [o[0].id, o[1].id],
                        "subject_ids": [o[0].subject_id, o[1].subject_id],
                        "subject_names": [str(o[0].subject), str(o[1].subject)],
                        "teacher_ids": [o[0].teacher_id, o[1].teacher_id],
                        "teacher_names": [str(o[0].teacher), str(o[1].teacher)],
                        "times": [str(o[0].starts_at) + " - " + str(o[0].ends_at), str(o[1].starts_at) + " - " + str(o[1].ends_at)]
                    } for o in overlaps
                ]
            })

    result = {
        "teacher_conflicts": teacher_conflicts,
        "class_conflicts": class_conflicts,
        "meta": {
            "num_entries": len(entries),
            "num_teacher_conflicts": len(teacher_conflicts),
            "num_class_conflicts": len(class_conflicts)
        }
    }
    return result


# ---------- Simple resolver (greedy relocation + swap) ----------
def attempt_resolve_conflicts(dry_run: bool = True, persist: bool = False, max_tries: int = 200) -> Dict:
    """
    Tente de résoudre automatiquement les conflits enseignants détectés.
    - dry_run=True : renvoie les modifications proposées sans toucher la DB
    - persist=True : applique les changements dans une transaction (attention!)
    Retour : rapport contenant 'resolved', 'unresolved', 'proposals'
    """
    slots, slots_by_day, slot_conflicts = _load_slots()

    # build mapping slot index by (weekday, start_time, end_time) for quick lookup
    slot_idx_map = {}
    for s in slots:
        slot_idx_map[(s["weekday"], s["db_obj"].start_time, s["db_obj"].end_time)] = s["idx"]

    # load DB entries and index them
    entries = list(ClassScheduleEntry.objects.select_related("school_class", "subject", "teacher").all())
    entry_by_id = {e.id: e for e in entries}

    # build global schedule map: slot_idx -> {"teacher":{tid:entry_id}, "class":{cid:entry_id}}
    global_schedule = {}
    for e in entries:
        key = (e.weekday, e.starts_at, e.ends_at)
        slot_idx = slot_idx_map.get(key)
        # if slot_idx missing, we will still keep a fallback mapping by time-string to detect overlaps
        slot_key = slot_idx if slot_idx is not None else f"time::{e.weekday}::{e.starts_at}::{e.ends_at}"
        slot_map = global_schedule.setdefault(slot_key, {"teacher": {}, "class": {}, "entries": []})
        if e.teacher_id:
            slot_map["teacher"].setdefault(e.teacher_id, []).append(e.id)
        slot_map["class"].setdefault(e.school_class_id, []).append(e.id)
        slot_map["entries"].append(e.id)

    # detect teacher conflicts (list of pairs to resolve)
    conflicts = []
    teacher_day_map = defaultdict(list)
    for e in entries:
        if not e.teacher_id:
            continue
        teacher_day_map[(e.teacher_id, e.weekday)].append(e)
    for (tid, day), ents in teacher_day_map.items():
        ents_sorted = sorted(ents, key=lambda x: x.starts_at)
        for i in range(len(ents_sorted) - 1):
            a = ents_sorted[i]; b = ents_sorted[i + 1]
            if a.ends_at > b.starts_at:
                conflicts.append({"teacher_id": tid, "weekday": day, "pair": (a.id, b.id)})

    proposals = []
    resolved = []
    unresolved = []

    tries = 0

    # helper to check whether candidate slot is free for class & teacher
    def is_slot_free(slot_key, class_id, teacher_id):
        slot_map = global_schedule.get(slot_key, {"teacher": {}, "class": {}, "entries": []})
        # teacher or class already used
        if teacher_id and slot_map["teacher"].get(teacher_id):
            return False
        if slot_map["class"].get(class_id):
            return False
        return True

    # helper to find slot candidates for a given entry (we attempt slots with same duration)
    def find_candidate_slots_for_entry(entry):
        dur = (entry.ends_at.hour * 60 + entry.ends_at.minute) - (entry.starts_at.hour * 60 + entry.starts_at.minute)
        candidates = []
        # prefer same weekday different non-overlapping slots
        for s in slots:
            # skip identical original
            if s["weekday"] == entry.weekday and s["db_obj"].start_time == entry.starts_at and s["db_obj"].end_time == entry.ends_at:
                continue
            # require slot duration >= entry duration (allow equal or longer)
            if s["dur"] >= dur:
                candidates.append(s["idx"])
        # shuffle to diversify
        random.shuffle(candidates)
        return candidates

    # map slot idx back to slot_key used in global_schedule
    def slot_idx_to_key(idx):
        s = next((x for x in slots if x["idx"] == idx), None)
        if not s:
            return None
        return idx

    # Build a helper to get slot_key for a given (weekday,start,end)
    def make_slot_key_from_idx(idx):
        s = next((x for x in slots if x["idx"] == idx), None)
        if not s:
            return None
        return idx

    # MAIN loop: iterate conflicts and attempt greedy relocate then swap
    for conf in conflicts:
        if tries >= max_tries:
            break
        tries += 1
        a_id, b_id = conf["pair"]
        a = entry_by_id.get(a_id)
        b = entry_by_id.get(b_id)
        # pick the one with less "penalty" to move (heuristic: move the one that has more candidate slots)
        cand_a = find_candidate_slots_for_entry(a)
        cand_b = find_candidate_slots_for_entry(b)
        # try moving b first (later slot) then a
        moved = False
        for move_entry, cand_slots in ((b, cand_b), (a, cand_a)):
            if not cand_slots:
                continue
            for cand_idx in cand_slots:
                # convert cand_idx to slot_key used in schedule
                cand_slot = next((x for x in slots if x["idx"] == cand_idx), None)
                if not cand_slot:
                    continue
                cand_key = cand_idx
                # ensure slot doesn't conflict with other scheduled entries for the class or teacher
                if is_slot_free(cand_key, move_entry.school_class_id, move_entry.teacher_id):
                    # propose move: change starts_at/ends_at/weekday to candidate slot values
                    proposal = {
                        "entry_id": move_entry.id,
                        "from": {"weekday": move_entry.weekday, "starts_at": str(move_entry.starts_at), "ends_at": str(move_entry.ends_at)},
                        "to": {"weekday": cand_slot["weekday"], "starts_at": str(cand_slot["db_obj"].start_time), "ends_at": str(cand_slot["db_obj"].end_time)},
                        "method": "relocate"
                    }
                    proposals.append(proposal)
                    # update in-memory global_schedule (remove old, add new)
                    old_key = slot_idx_map.get((move_entry.weekday, move_entry.starts_at, move_entry.ends_at), f"time::{move_entry.weekday}::{move_entry.starts_at}::{move_entry.ends_at}")
                    # remove references
                    g_old = global_schedule.get(old_key)
                    if g_old:
                        g_old["class"].pop(move_entry.school_class_id, None)
                        if move_entry.teacher_id:
                            teacher_list = g_old["teacher"].get(move_entry.teacher_id, [])
                            if move_entry.id in teacher_list:
                                teacher_list.remove(move_entry.id)
                                if not teacher_list:
                                    g_old["teacher"].pop(move_entry.teacher_id, None)
                    # add to new slot map
                    g_new = global_schedule.setdefault(cand_key, {"teacher": {}, "class": {}, "entries": []})
                    g_new["class"].setdefault(move_entry.school_class_id, []).append(move_entry.id)
                    if move_entry.teacher_id:
                        g_new["teacher"].setdefault(move_entry.teacher_id, []).append(move_entry.id)
                    g_new["entries"].append(move_entry.id)

                    # apply to DB if persist (we will collect updates and apply after loop)
                    move_entry._proposed_new = {"weekday": cand_slot["weekday"], "starts_at": cand_slot["db_obj"].start_time, "ends_at": cand_slot["db_obj"].end_time}
                    moved = True
                    break
            if moved:
                resolved.append({"conflict": conf, "moved_entry": move_entry.id, "to_slot_idx": cand_idx})
                break

        if not moved:
            # try swap: for each candidate slot of b, see if occupant there can move into a's original slot
            swapped = False
            # build original slot keys
            a_slot_key = slot_idx_map.get((a.weekday, a.starts_at, a.ends_at), f"time::{a.weekday}::{a.starts_at}::{a.ends_at}")
            b_slot_key = slot_idx_map.get((b.weekday, b.starts_at, b.ends_at), f"time::{b.weekday}::{b.starts_at}::{b.ends_at}")
            for cand_idx in cand_b:
                cand_key = cand_idx
                slot_map = global_schedule.get(cand_key, {"entries": [], "teacher": {}, "class": {}})
                # try each occupant in that cand slot and see if it can move to b's original slot
                for occ_id in list(slot_map.get("entries", [])):
                    if occ_id in (a.id, b.id):
                        continue
                    occ = entry_by_id.get(occ_id)
                    # check if occ can occupy b's original slot (no class/teacher conflict)
                    if is_slot_free(b_slot_key, occ.school_class_id, occ.teacher_id):
                        # perform swap: move occ -> b_slot, and move b -> cand_idx (therefore freeing original b slot)
                        # propose two updates
                        proposals.append({
                            "entry_id": occ.id,
                            "from": {"weekday": occ.weekday, "starts_at": str(occ.starts_at), "ends_at": str(occ.ends_at)},
                            "to": {"weekday": b.weekday, "starts_at": str(b.starts_at), "ends_at": str(b.ends_at)},
                            "method": "swap-other"
                        })
                        proposals.append({
                            "entry_id": b.id,
                            "from": {"weekday": b.weekday, "starts_at": str(b.starts_at), "ends_at": str(b.ends_at)},
                            "to": {"weekday": slot_map and next((x["db_obj"].day for x in slots if x["idx"] == cand_idx), b.weekday),
                                   "starts_at": str(next((x["db_obj"].start_time for x in slots if x["idx"] == cand_idx), b.starts_at)),
                                   "ends_at": str(next((x["db_obj"].end_time for x in slots if x["idx"] == cand_idx), b.ends_at))},
                            "method": "swap-with-occ"
                        })
                        # update in-memory schedule accordingly
                        # remove occ from cand_key
                        slot_map["entries"].remove(occ.id)
                        slot_map["class"].get(occ.school_class_id, []).remove(occ.id)
                        slot_map["teacher"].get(occ.teacher_id, []).remove(occ.id)
                        # add occ to b_slot_key
                        g_bslot = global_schedule.setdefault(b_slot_key, {"entries": [], "class": {}, "teacher": {}})
                        g_bslot["entries"].append(occ.id)
                        g_bslot["class"].setdefault(occ.school_class_id, []).append(occ.id)
                        if occ.teacher_id:
                            g_bslot["teacher"].setdefault(occ.teacher_id, []).append(occ.id)
                        # add b to cand_key
                        slot_map["entries"].append(b.id)
                        slot_map["class"].setdefault(b.school_class_id, []).append(b.id)
                        if b.teacher_id:
                            slot_map["teacher"].setdefault(b.teacher_id, []).append(b.id)

                        # mark proposed updates
                        occ._proposed_new = {"weekday": b.weekday, "starts_at": b.starts_at, "ends_at": b.ends_at}
                        b._proposed_new = {"weekday": next((x["weekday"] for x in slots if x["idx"] == cand_idx), b.weekday),
                                           "starts_at": next((x["db_obj"].start_time for x in slots if x["idx"] == cand_idx), b.starts_at),
                                           "ends_at": next((x["db_obj"].end_time for x in slots if x["idx"] == cand_idx), b.ends_at)}
                        swapped = True
                        break
                if swapped:
                    resolved.append({"conflict": conf, "swap_with": occ.id})
                    break

            if not swapped:
                unresolved.append(conf)

    # Apply proposals to DB if requested
    applied = []
    errors = []
    if persist and not dry_run:
        try:
            with transaction.atomic():
                for e in entry_by_id.values():
                    if hasattr(e, "_proposed_new"):
                        new = e._proposed_new
                        # update fields
                        e.weekday = new["weekday"]
                        e.starts_at = new["starts_at"]
                        e.ends_at = new["ends_at"]
                        e.save()
                        applied.append(e.id)
        except Exception as exc:
            errors.append(str(exc))

    # build final report
    report = {
        "initial_conflicts_count": len(conflicts),
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "resolved": resolved,
        "unresolved": unresolved,
        "proposals": proposals,
        "applied": applied,
        "errors": errors,
    }
    return report


# ---------- Wrapper ----------
def detect_and_resolve(dry_run: bool = True, persist: bool = False, max_tries: int = 200) -> Dict:
    """
    Pipeline : détecte puis tente de résoudre.
    - dry_run: si True, ne touche pas la DB. persist=False signifie aussi dry-run.
    - persist: si True et dry_run=False, applique les changements.
    """
    result_detect = detect_teacher_conflicts()
    if result_detect["meta"]["num_teacher_conflicts"] == 0:
        return {"detected": result_detect, "resolve": {"skipped": True, "reason": "no conflicts"}}

    resolve_report = attempt_resolve_conflicts(dry_run=dry_run, persist=persist, max_tries=max_tries)
    # recompute detection after attempted resolution (if not dry_run and persist=True it reflects DB)
    detected_after = detect_teacher_conflicts()
    return {"detected_before": result_detect, "resolve_report": resolve_report, "detected_after": detected_after}
