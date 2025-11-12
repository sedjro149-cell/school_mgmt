#!/usr/bin/env python3
"""
Standalone schedule checker.

Place this file in your project (e.g. scripts/check_teacher_schedule.py)
Run from project root:

  DJANGO_SETTINGS_MODULE=myproject.settings python scripts/check_teacher_schedule.py
  DJANGO_SETTINGS_MODULE=myproject.settings python scripts/check_teacher_schedule.py --dump report.json

Options:
  --class-id ID   : check only entries for that school_class id
  --limit N       : show at most N example conflicts per type (default 10)
  --dump PATH     : write JSON report to PATH
  --verbose       : print full lists (careful: can be large)
"""
import os
import sys
import argparse
import json
from collections import defaultdict
from datetime import datetime

# OPTION: default settings module (change "myproject.settings" to your settings module if you prefer)
DEFAULT_SETTINGS = os.environ.get("DJANGO_SETTINGS_MODULE", "school_mgmt.settings")

# Try to ensure project root is on sys.path (script placed in project root or scripts/)
PROJECT_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__))) if os.path.basename(__file__) == "check_teacher_schedule.py" else os.getcwd()
sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", DEFAULT_SETTINGS)
try:
    import django
    django.setup()
except Exception as e:
    print("Erreur lors de l'initialisation Django. Vérifie DJANGO_SETTINGS_MODULE.")
    raise

from academics.models import ClassScheduleEntry
# core.models import Teacher/SchoolClass only for pretty printing (optional)
from core.models import Teacher, Student  # Student import harmless if unused

def teacher_repr(entry):
    # try a few ways to print teacher: prefer username or str()
    t = getattr(entry, "teacher", None)
    if t is None:
        return str(getattr(entry, "teacher_id", None))
    # try common attributes safely
    for attr in ("user", "username", "name", "short_name"):
        val = getattr(t, attr, None)
        if val:
            # if user object, try its username/last_name
            if hasattr(val, "username"):
                return getattr(val, "username")
            return str(val)
    return str(t)

def class_repr(entry):
    c = getattr(entry, "school_class", None)
    if c is None:
        return str(getattr(entry, "school_class_id", None))
    return getattr(c, "name", str(c))

def format_entry_short(e):
    return {
        "id": getattr(e, "id", None),
        "class_id": getattr(e, "school_class_id", None),
        "class_name": class_repr(e),
        "teacher_id": getattr(e, "teacher_id", None),
        "teacher": teacher_repr(e),
        "weekday": getattr(e, "weekday", None),
        "starts_at": getattr(e, "starts_at", None),
        "ends_at": getattr(e, "ends_at", None),
    }

def run_check(class_id=None, limit=10, verbose=False):
    """
    Returns a report dict with:
      - same_day_same_class_conflicts: list of groups where same (teacher,class,weekday) has >1 entries
      - consecutive_day_same_class_conflicts: list of entries where the same teacher has same class on consecutive weekdays
    """
    qs = ClassScheduleEntry.objects.all().select_related("school_class", "teacher")
    if class_id:
        qs = qs.filter(school_class_id=class_id)

    entries = list(qs)
    if not entries:
        return {"ok": True, "message": "No ClassScheduleEntry found (after optional filter).", "counts": {}, "same_day_conflicts": [], "consec_conflicts": []}

    # Map (teacher_id, class_id, weekday) -> [entries]
    same_day_map = defaultdict(list)
    # Map (teacher_id, class_id) -> set(weekdays)
    teacher_class_days = defaultdict(set)

    for e in entries:
        tid = getattr(e, "teacher_id", None)
        cid = getattr(e, "school_class_id", None)
        wd = getattr(e, "weekday", None)
        # skip entries w/o teacher or class or weekday (unlikely)
        if tid is None or cid is None or wd is None:
            continue
        same_day_map[(tid, cid, wd)].append(e)
        teacher_class_days[(tid, cid)].add(wd)

    same_day_conflicts = []
    for key, group in same_day_map.items():
        if len(group) > 1:
            tid, cid, wd = key
            group_short = [format_entry_short(e) for e in group]
            same_day_conflicts.append({
                "teacher_id": tid,
                "teacher": teacher_repr(group[0]),
                "class_id": cid,
                "class_name": class_repr(group[0]),
                "weekday": wd,
                "count": len(group),
                "entries": group_short[:limit] if not verbose else group_short,
            })

    # Consecutive-day detection for same teacher & same class
    consec_conflicts = []
    for (tid, cid), days in teacher_class_days.items():
        if not days:
            continue
        sorted_days = sorted(days)
        for i in range(len(sorted_days) - 1):
            if sorted_days[i + 1] - sorted_days[i] == 1:
                # find example entries for those days
                ents_d1 = same_day_map.get((tid, cid, sorted_days[i]), [])
                ents_d2 = same_day_map.get((tid, cid, sorted_days[i+1]), [])
                sample = []
                if ents_d1:
                    sample.append(format_entry_short(ents_d1[0]))
                if ents_d2:
                    sample.append(format_entry_short(ents_d2[0]))
                consec_conflicts.append({
                    "teacher_id": tid,
                    "teacher": teacher_repr(ents_d1[0] if ents_d1 else (ents_d2[0] if ents_d2 else None)),
                    "class_id": cid,
                    "class_name": class_repr(ents_d1[0] if ents_d1 else (ents_d2[0] if ents_d2 else None)),
                    "days": (sorted_days[i], sorted_days[i+1]),
                    "examples": sample,
                })

    report = {
        "timestamp": datetime.now().isoformat(),
        "total_entries": len(entries),
        "same_day_conflict_count": len(same_day_conflicts),
        "consecutive_day_conflict_count": len(consec_conflicts),
        "same_day_conflicts": same_day_conflicts[:limit] if not verbose else same_day_conflicts,
        "consecutive_day_conflicts": consec_conflicts[:limit] if not verbose else consec_conflicts,
    }
    return report

def main():
    p = argparse.ArgumentParser(description="Check teacher/class schedule conflicts (same day same class, consecutive days same class)")
    p.add_argument("--class-id", type=int, help="Optional: only check this school_class id")
    p.add_argument("--dump", help="Optional: path to JSON file to write full report")
    p.add_argument("--limit", type=int, default=10, help="Max examples to show per conflict type")
    p.add_argument("--verbose", action="store_true", help="Show full conflict lists (may be large)")
    args = p.parse_args()

    print("Schedule checker starting...")
    print("DJANGO_SETTINGS_MODULE =", os.environ.get("DJANGO_SETTINGS_MODULE"))
    try:
        report = run_check(class_id=args.class_id, limit=args.limit, verbose=args.verbose)
    except Exception as e:
        print("Erreur lors de l'analyse :", str(e))
        raise

    # Pretty print summary
    print("\n=== CHECK SUMMARY ===")
    print("Total schedule entries scanned:", report.get("total_entries", 0))
    print("Same-day (same class) conflict groups:", report.get("same_day_conflict_count", 0))
    print("Consecutive-day (same class) conflicts:", report.get("consecutive_day_conflict_count", 0))

    if report.get("same_day_conflict_count", 0) > 0:
        print("\nSample same-day conflicts:")
        for item in report.get("same_day_conflicts", []):
            print(f" - teacher={item['teacher']} ({item['teacher_id']}), class={item['class_name']} ({item['class_id']}), weekday={item['weekday']}, count={item['count']}")
            for ent in item.get("entries", []):
                print(f"    entry id={ent['id']} starts={ent['starts_at']} ends={ent['ends_at']}")
    if report.get("consecutive_day_conflict_count", 0) > 0:
        print("\nSample consecutive-day conflicts:")
        for item in report.get("consecutive_day_conflicts", []):
            print(f" - teacher={item['teacher']} ({item['teacher_id']}), class={item['class_name']} ({item['class_id']}), days={item['days']}")
            for ex in item.get("examples", []):
                print(f"    example id={ex['id']} day={ex['weekday']} starts={ex['starts_at']} ends={ex['ends_at']}")

    if args.dump:
        try:
            with open(args.dump, "w", encoding="utf-8") as f:
                json.dump(report, f, default=str, indent=2, ensure_ascii=False)
            print(f"\nReport dumped to {args.dump}")
        except Exception as e:
            print("Impossible d'écrire le fichier dump:", e)

    print("\nDone.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
