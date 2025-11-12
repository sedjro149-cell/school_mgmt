# populate_teachers.py
"""
Script to create Teacher users and Teacher objects and assign them to subjects and classes.

Behavior:
- For each Subject in academics.Subject, create 15 Teacher users.
- Each Teacher is assigned exactly ONE Subject (Teacher.subject FK).
- For each created Teacher, assign between 1 and 3 SchoolClass instances randomly,
  but only classes that include the Subject in their programme (via ClassSubject).
- DO NOT assign a SchoolClass to a Teacher for a Subject if that class already has
  another Teacher assigned for the same Subject (enforces uniqueness (class,subject) -> one teacher).
- The script creates the Django User first (using get_user_model().objects.create_user),
  then Teacher object, then assigns classes.
- Transactional: each Teacher creation + assignments is done inside a transaction.
- Supports --dry-run to simulate without writing to DB. Default is write-mode.
- Exports created credentials to CSV 'teachers_credentials.csv' in the current folder.

Usage:
- Place this file at the root of your Django project (same folder as manage.py)
- Adjust DJANGO_SETTINGS_MODULE if necessary
- Run (create for real): python populate_teachers.py
- Run dry-run: python populate_teachers.py --dry-run

IMPORTANT: Run on staging / backup DB first if you're unsure. This script WILL create users and teachers.
"""

import os
import django
import random
import sys
import csv
import secrets
from collections import defaultdict
from datetime import datetime

# ------ CONFIG (modifie si nécessaire) ------
# Assure-toi de mettre le bon module settings de ton projet
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school_mgmt.settings")
django.setup()
# --------------------------------------------

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.text import slugify

# Import the models
from core.models import Teacher
from academics.models import Subject, SchoolClass, ClassSubject

User = get_user_model()

# ------- Small name pools (recycled from student script) -------
MALE_FIRST = [
    "Koffi", "Yannick", "Gildas", "Kodjo", "Togbé", "Franck", "Kossi", "Anani",
    "Eudes", "Claudin", "Nicolas", "Ismaël", "Alassane", "Ousmane", "Emmanuel",
    "Saliou", "Blaise", "Serge", "Yves", "Kevin", "Rachid", "Mohamed", "Issa",
    "Romaric", "Ulrich", "Samson", "Wilfried", "Landry", "Brice", "Firmin", "Stéphane",
    "Jean", "Pascal", "Rodrigue", "Fabrice", "Arnaud", "Richard"
]

FEMALE_FIRST = [
    "Aïcha", "Adéla", "Mariam", "Aminata", "Nathalie", "Assétou", "Nadine", "Kokou",
    "Olivia", "Estelle", "Fanta", "Sita", "Rita", "Brigitte", "Claudine", "Awa",
    "Nora", "Grace", "Evelyne", "Hawa", "Fatou", "Chantal", "Victoire", "Prisca"
]

LAST_NAMES = [
    "Adjovi", "Gnonlonfoun", "Agossa", "Togbé", "Houngbédji", "Yabi", "Koutché",
    "Soglo", "Ahouansou", "Kpodar", "Dossou", "Dah", "Anago", "Akakpo", "Koffi",
    "Mensah", "Ouedraogo", "Diallo", "Traoré", "N'diaye", "Mabiala", "Okoye"
]

EMAIL_DOMAINS = ["example.com", "school.test", "gmail.com", "yahoo.com"]
TEACHERS_PER_SUBJECT = 15
MAX_CLASSES_PER_TEACHER = 3

# ------- Helpers -------

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


def make_password():
    return secrets.token_urlsafe(10)


def pick_name():
    sex = random.choice(["M", "F"])  # random gender for teacher name generation
    first = random.choice(MALE_FIRST) if sex == "M" else random.choice(FEMALE_FIRST)
    last = random.choice(LAST_NAMES)
    return first, last


# ------- Core function -------

def populate_teachers(dry_run=False, credentials_csv_path="teachers_credentials.csv"):
    subjects = list(Subject.objects.all())
    if not subjects:
        print("Aucune matière trouvée dans academics.Subject. Crée d'abord des matières.")
        return

    total_created = 0
    total_assigned_classes = 0
    warnings = []
    created_records = []  # for CSV

    print(f"Found {len(subjects)} subjects. Creating {TEACHERS_PER_SUBJECT} teachers per subject...")

    for subject in subjects:
        # get SchoolClass instances that include this subject via ClassSubject
        class_subject_qs = ClassSubject.objects.filter(subject=subject)
        school_classes = [cs.school_class for cs in class_subject_qs]

        if not school_classes:
            msg = f"Subject '{subject.name}' has no classes linked via ClassSubject. Teachers will be created but have no classes."
            warnings.append(msg)
            print("WARNING:", msg)

        for idx in range(1, TEACHERS_PER_SUBJECT + 1):
            first_name, last_name = pick_name()
            username_base = f"teacher.{slugify(subject.name)}.{idx}"
            username = unique_username(username_base)
            email = unique_email(username)
            pwd = make_password()

            # Choose desired number of classes for this teacher
            desired = random.randint(1, MAX_CLASSES_PER_TEACHER)

            if dry_run:
                # simulate selection of compatible classes while enforcing (class,subject) uniqueness
                available = []
                for cls in school_classes:
                    # skip if class already has a teacher for this subject
                    exists = Teacher.objects.filter(classes=cls, subject=subject).exists()
                    if not exists:
                        available.append(cls)
                chosen = random.sample(available, min(desired, len(available))) if available else []
                created_records.append({
                    "username": username,
                    "email": email,
                    "password": pwd,
                    "first_name": first_name,
                    "last_name": last_name,
                    "subject": subject.name,
                    "assigned_class_ids": [c.id for c in chosen]
                })
                print(f"[DRY] Would create teacher {username} for subject '{subject.name}' assigned to {len(chosen)} classes")
                continue

            # Real creation
            try:
                with transaction.atomic():
                    user = User.objects.create_user(username=username, email=email, password=pwd)
                    user.first_name = first_name
                    user.last_name = last_name
                    user.save()

                    teacher = Teacher.objects.create(user=user, subject=subject)

                    assigned = []
                    # Shuffle candidate classes and attempt to pick up to 'desired' ones
                    candidates = school_classes.copy()
                    random.shuffle(candidates)
                    for cls in candidates:
                        if len(assigned) >= desired:
                            break
                        # check if this class already has a teacher for this subject
                        conflict = Teacher.objects.filter(classes=cls, subject=subject).exists()
                        if conflict:
                            continue
                        teacher.classes.add(cls)
                        assigned.append(cls)

                    total_created += 1
                    total_assigned_classes += len(assigned)

                    created_records.append({
                        "username": username,
                        "email": email,
                        "password": pwd,
                        "first_name": first_name,
                        "last_name": last_name,
                        "subject": subject.name,
                        "assigned_class_ids": [c.id for c in assigned]
                    })

                    print(f"Created Teacher {username} (subject='{subject.name}') and assigned {len(assigned)} classes")

            except Exception as e:
                msg = f"Erreur lors de la création du teacher {username} (subject={subject.name}): {e}"
                print(msg)
                warnings.append(msg)
                continue

    # write CSV
    if not dry_run and created_records:
        try:
            with open(credentials_csv_path, "w", newline="", encoding="utf-8") as csvfile:
                fieldnames = ["username", "email", "password", "first_name", "last_name", "subject", "assigned_class_ids"]
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                for r in created_records:
                    # convert assigned_class_ids to semicolon-separated string for readability
                    r_out = r.copy()
                    r_out["assigned_class_ids"] = ";".join([str(x) for x in r_out["assigned_class_ids"]])
                    writer.writerow(r_out)
            print(f"Credentials exported to {credentials_csv_path}")
        except Exception as e:
            print(f"Warning: impossible d'écrire le CSV des credentials: {e}")

    # Summary
    print('\n--- Résumé ---')
    print(f"Total teachers created: {total_created}")
    print(f"Total class assignments made: {total_assigned_classes}")
    if warnings:
        print('\nWarnings:')
        for w in warnings:
            print(f"- {w}")
    print("Finished at", datetime.now())


if __name__ == '__main__':
    # check for --dry-run flag
    DRY_RUN = '--dry-run' in sys.argv
    if DRY_RUN:
        print("Running in DRY-RUN mode (no DB writes). Use without --dry-run to write.)")
    try:
        populate_teachers(dry_run=DRY_RUN)
    except Exception as exc:
        print('Erreur lors de l exécution :', exc)
        sys.exit(1)
