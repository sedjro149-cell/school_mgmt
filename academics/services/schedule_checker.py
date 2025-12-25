# academics/services/schedule_checker.py
from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, Optional

from academics.models import ClassScheduleEntry
from core.models import Teacher  # optional for pretty printing if you want

def _teacher_repr(entry):
    t = getattr(entry, "teacher", None)
    if t is None:
        return str(getattr(entry, "teacher_id", None))
    for attr in ("user", "username", "name", "short_name"):
        val = getattr(t, attr, None)
        if val:
            if hasattr(val, "username"):
                return getattr(val, "username")
            return str(val)
    return str(t)

def _class_repr(entry):
    c = getattr(entry, "school_class", None)
    if c is None:
        return str(getattr(entry, "school_class_id", None))
    return getattr(c, "name", str(c))

def _format_entry_short(e):
    return {
        "id": getattr(e, "id", None),
        "class_id": getattr(e, "school_class_id", None),
        "class_name": _class_repr(e),
        "teacher_id": getattr(e, "teacher_id", None),
        "teacher": _teacher_repr(e),
        "weekday": getattr(e, "weekday", None),
        "starts_at": getattr(e, "starts_at", None),
        "ends_at": getattr(e, "ends_at", None),
    }

def run_check(class_id: Optional[int] = None, limit: int = 10, verbose: bool = False) -> Dict[str, Any]:
    """
    Analyse les ClassScheduleEntry en base et renvoie un rapport dict:
    - same_day_conflicts : mêmes teacher+class+weekday > 1
    - consecutive_day_conflicts : mêmes teacher+class sur jours consécutifs
    """
    qs = ClassScheduleEntry.objects.all().select_related("school_class", "teacher")
    if class_id:
        qs = qs.filter(school_class_id=class_id)

    entries = list(qs)
    if not entries:
        return {
            "timestamp": datetime.now().isoformat(),
            "total_entries": 0,
            "same_day_conflict_count": 0,
            "consecutive_day_conflict_count": 0,
            "same_day_conflicts": [],
            "consecutive_day_conflicts": [],
            "ok": True,
            "message": "No ClassScheduleEntry found (after optional filter)."
        }

    same_day_map = defaultdict(list)
    teacher_class_days = defaultdict(set)

    for e in entries:
        tid = getattr(e, "teacher_id", None)
        cid = getattr(e, "school_class_id", None)
        wd = getattr(e, "weekday", None)
        if tid is None or cid is None or wd is None:
            continue
        same_day_map[(tid, cid, wd)].append(e)
        teacher_class_days[(tid, cid)].add(wd)

    same_day_conflicts = []
    for (tid, cid, wd), group in same_day_map.items():
        if len(group) > 1:
            group_short = [_format_entry_short(e) for e in group]
            same_day_conflicts.append({
                "teacher_id": tid,
                "teacher": _teacher_repr(group[0]),
                "class_id": cid,
                "class_name": _class_repr(group[0]),
                "weekday": wd,
                "count": len(group),
                "entries": group_short if verbose else group_short[:limit],
            })

    consec_conflicts = []
    for (tid, cid), days in teacher_class_days.items():
        if not days:
            continue
        sorted_days = sorted(days)
        for i in range(len(sorted_days) - 1):
            if sorted_days[i + 1] - sorted_days[i] == 1:
                ents_d1 = same_day_map.get((tid, cid, sorted_days[i]), [])
                ents_d2 = same_day_map.get((tid, cid, sorted_days[i+1]), [])
                sample = []
                if ents_d1:
                    sample.append(_format_entry_short(ents_d1[0]))
                if ents_d2:
                    sample.append(_format_entry_short(ents_d2[0]))
                consec_conflicts.append({
                    "teacher_id": tid,
                    "teacher": _teacher_repr(ents_d1[0] if ents_d1 else (ents_d2[0] if ents_d2 else None)),
                    "class_id": cid,
                    "class_name": _class_repr(ents_d1[0] if ents_d1 else (ents_d2[0] if ents_d2 else None)),
                    "days": (sorted_days[i], sorted_days[i+1]),
                    "examples": sample,
                })

    report = {
        "timestamp": datetime.now().isoformat(),
        "total_entries": len(entries),
        "same_day_conflict_count": len(same_day_conflicts),
        "consecutive_day_conflict_count": len(consec_conflicts),
        "same_day_conflicts": same_day_conflicts if verbose else same_day_conflicts[:limit],
        "consecutive_day_conflicts": consec_conflicts if verbose else consec_conflicts[:limit],
        "ok": True,
    }
    return report
