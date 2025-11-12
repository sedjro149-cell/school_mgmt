# populate_parents.py
"""
Script to create Parent users and Parent objects and associate existing Students to them.

Behaviour:
- Groups students by their last name (student.user.last_name preferred).
- Each Parent may have at most 4 children. If more than 4 students share a last name,
  multiple Parent objects will be created (chunks of up to 4).
- For each Parent we create a Django User first (using create_user to hash password),
  then Parent object (Parent.user is OneToOne), then assign students to that Parent.
- The script auto-detects whether Student has a ForeignKey/OneToOne field or a
  ManyToMany field pointing to Parent and uses the appropriate assignment.

Usage:
- Edit DJANGO_SETTINGS_MODULE if necessary (default: school_mgmt.settings)
- Run: python populate_parents.py
- Optional: set DRY_RUN = True to perform a dry-run (no DB writes).

Notes:
- The script is defensive: it will skip students already associated with a parent
  (if the relation field is non-null or non-empty).
- Parent usernames/emails are made unique.
- Passwords for created parent users are randomly generated and printed in a
  small report at the end (if not a dry-run).

Make sure you have a DB backup or run first on staging.
"""

import os
import django
import random
import sys
from collections import defaultdict
from math import ceil
import secrets
from datetime import datetime

# ------ CONFIG (modifie si nécessaire) ------
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school_mgmt.settings")
django.setup()
# --------------------------------------------

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from core.models import Student
from core.models import Parent

User = get_user_model()

# ------- Configuration -------
DRY_RUN = False  # passe à True pour simuler sans écrire en DB
PARENT_PASSWORD_PREFIX = None  # si None -> mot de passe généré aléatoirement
MAX_CHILDREN = 4
EMAIL_DOMAINS = ["example.com", "school.test", "gmail.com", "yahoo.com"]

# ------- Helpers (inspired by your student script) -------

def unique_username(base):
    uname = base
    i = 0
    while User.objects.filter(username=uname).exists():
        i += 1
        uname = f"{base}{i}"
    return uname


def unique_email(local_part):
    domain = random.choice(EMAIL_DOMAINS)
    email = f"{local_part}@{domain}"
    i = 0
    while User.objects.filter(email=email).exists():
        i += 1
        email = f"{local_part}{i}@{domain}"
    return email


def make_parent_password():
    if PARENT_PASSWORD_PREFIX:
        # deterministic-ish but still with random suffix
        return f"{PARENT_PASSWORD_PREFIX}{secrets.token_urlsafe(6)}"
    return secrets.token_urlsafe(10)


# Detect how Student relates to Parent (FK or M2M), return a tuple (relation_type, field_name)
# relation_type: 'fk' | 'm2m' | None

def detect_parent_relation():
    for f in Student._meta.get_fields():
        # skip reverse relations from Parent side (we want fields defined *on* Student)
        if f.auto_created and not f.concrete:
            continue
        # Check relation to Parent model
        if getattr(f, 'related_model', None) is Parent:
            if f.many_to_one or getattr(f, 'one_to_one', False):
                return 'fk', f.name
            if f.many_to_many:
                return 'm2m', f.name
    # fallback common names
    for guess in ('parent', 'parents'):
        if hasattr(Student, guess):
            attr = getattr(Student, guess)
            # can't easily inspect here; assume fk
            return 'fk', guess
    return None, None


def chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def get_student_last_name(student):
    # prefer user's last_name if present
    try:
        if hasattr(student, 'user') and student.user and getattr(student.user, 'last_name', '').strip():
            return student.user.last_name.strip()
    except Exception:
        pass
    # fallback to student fields if any
    for candidate in ('last_name', 'surname', 'family_name'):
        if hasattr(student, candidate):
            val = getattr(student, candidate)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ''


def populate_parents(dry_run=DRY_RUN):
    relation_type, relation_field = detect_parent_relation()
    if relation_type is None:
        print("Aucune relation Student<->Parent détectée automatiquement."
              " Le script essaiera d'utiliser l'attribut 'parent' par défaut.")
        relation_type, relation_field = 'fk', 'parent'

    print(f"Relation détectée: {relation_type} via '{relation_field}'")

    students = list(Student.objects.all())
    if not students:
        print("Aucun Student trouvé. Crée d'abord des étudiants avant d'exécuter ce script.")
        return

    # Group students by last name (case-insensitive)
    groups = defaultdict(list)
    for s in students:
        last = get_student_last_name(s) or 'UNKNOWN'
        groups[last.strip().lower()].append(s)

    total_parents_created = 0
    total_students_assigned = 0
    parents_info = []  # list of dicts for report
    warnings = []

    for last_lower, studs in groups.items():
        last_display = studs[0].user.last_name.strip() if getattr(studs[0], 'user', None) else last_lower
        # Filter students that are already assigned (if fk) to avoid overwriting
        unassigned = []
        for s in studs:
            assigned = False
            if relation_type == 'fk':
                # if the fk attribute exists and is not None, consider assigned
                if hasattr(s, relation_field):
                    try:
                        val = getattr(s, relation_field)
                        if val is not None:
                            assigned = True
                    except Exception:
                        pass
            elif relation_type == 'm2m':
                try:
                    rel_qs = getattr(s, relation_field)
                    if rel_qs.exists():
                        assigned = True
                except Exception:
                    pass
            if not assigned:
                unassigned.append(s)

        if not unassigned:
            print(f"[{last_display}] Tous les étudiants sont déjà assignés. Skip.")
            continue

        # Determine how many parents needed for this family
        needed_parents = ceil(len(unassigned) / MAX_CHILDREN)

        # Create parents and fill them
        idx = 0
        for chunk in chunk_list(unassigned, MAX_CHILDREN):
            idx += 1
            # Build username base: parent.lastname.idx to reduce collisions
            base_local = f"parent.{last_display.replace(' ', '').lower()}"
            if needed_parents > 1:
                base_local = f"{base_local}{idx}"
            username_base = base_local
            username = unique_username(username_base)
            email = unique_email(username)
            pwd = make_parent_password()

            if dry_run:
                # simulate parent creation
                parent_user_info = {
                    'username': username,
                    'email': email,
                    'password': pwd,
                    'first_name': f"Parent of {last_display}",
                    'last_name': last_display,
                }
                parents_info.append({'last_name': last_display, 'username': username, 'created': False, 'assigned_student_ids': [s.id for s in chunk], 'password': pwd})
                total_parents_created += 0
                total_students_assigned += len(chunk)
                print(f"[DRY] Would create Parent user {username} and assign {len(chunk)} students for family '{last_display}'")
                continue

            try:
                with transaction.atomic():
                    # create User and Parent
                    user = User.objects.create_user(username=username, email=email, password=pwd)
                    # set nicer names if possible
                    user.first_name = f"Parent"
                    user.last_name = last_display
                    user.save()

                    parent = Parent.objects.create(user=user)

                    assigned_ids = []
                    # assign students in chunk
                    for s in chunk:
                        if relation_type == 'fk':
                            setattr(s, relation_field, parent)
                            s.save()
                        else:  # m2m
                            getattr(s, relation_field).add(parent)
                        assigned_ids.append(s.id)

                    parents_info.append({'last_name': last_display, 'username': username, 'created': True, 'assigned_student_ids': assigned_ids, 'password': pwd})
                    total_parents_created += 1
                    total_students_assigned += len(assigned_ids)
                    print(f"Created Parent {username} and assigned {len(assigned_ids)} students for family '{last_display}'")

            except Exception as e:
                msg = f"Erreur lors de la création du parent pour '{last_display}' (chunk #{idx}): {e}"
                print(msg)
                warnings.append(msg)
                continue

    # Summary
    print('\n--- Résumé ---')
    print(f"Total parents created: {total_parents_created}")
    print(f"Total students assigned: {total_students_assigned}")
    if parents_info:
        print('\nDétail parents créés (ou simulés):')
        for p in parents_info:
            print(f"- last_name={p['last_name']}, username={p['username']}, created={p['created']}, assigned_students={p['assigned_student_ids']}")
    if warnings:
        print('\nWarnings / erreurs:')
        for w in warnings:
            print(f"- {w}")

    print('\nFinished at', datetime.now())


if __name__ == '__main__':
    # allow optional command line flag --dry-run
    if '--dry-run' in sys.argv:
        DRY_RUN = True
    try:
        populate_parents(dry_run=DRY_RUN)
    except Exception as exc:
        print('Erreur lors de l exécution :', exc)
        sys.exit(1)
