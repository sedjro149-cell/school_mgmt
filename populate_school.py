# populate_school.py
import os
import django
import random
import sys
from datetime import date, timedelta
from decimal import Decimal

# ------ CONFIG (modifie si nécessaire) ------
# Assure-toi que le module settings correspond au nom de ton projet
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school_mgmt.settings")
django.setup()
# --------------------------------------------

from django.contrib.auth import get_user_model
from django.db import transaction
from core.models import Student  # ton modèle Student
from academics.models import SchoolClass  # classes existantes

User = get_user_model()

# ---------- Lists of names (Beninese / West African + pan-african) ----------
MALE_FIRST = [
    "Koffi", "Yannick", "Gildas", "Kodjo", "Togbé", "Franck", "Kossi", "Anani",
    "Eudes", "Claudin", "Nicolas", "Ismaël", "Alassane", "Ousmane", "Emmanuel",
    "Saliou", "Blaise", "Serge", "Yves", "Kevin", "Rachid", "Mohamed", "Issa",
    "Romaric", "Ulrich", "Samson", "Wilfried", "Landry", "Brice", "Firmin", "Stéphane",
    "Jean", "Pascal", "Rodrigue", "Fabrice", "Arnaud", "Richard", "Loïc", "Cyrille",
    "Ghislain", "Hervé", "Jules", "Théophile", "Barnabé", "Joël", "Raphaël", "Didier",
    "Ephrem", "Roméo", "Boris", "André", "Jacob", "Josué", "Daniel", "Samuel",
    "Yacoubou", "Souleymane", "Idriss", "Abdoulaye", "Hamidou", "Moussa", "Daouda",
    "Adama", "Cheikh", "Karim", "Aboubacar", "Malik", "Ali", "Youssouf", "Nourou",
    "Cédric", "Lionel", "Prince", "Gaëtan", "Laurent"
]

FEMALE_FIRST = [
    "Aïcha", "Adéla", "Mariam", "Aminata", "Nathalie", "Assétou", "Nadine", "Kokou",
    "Olivia", "Estelle", "Fanta", "Sita", "Rita", "Brigitte", "Claudine", "Awa",
    "Nora", "Grace", "Evelyne", "Hawa", "Fatou", "Chantal", "Victoire", "Prisca",
    "Naomi", "Sylvie", "Inès", "Patricia", "Bénédicte", "Cynthia", "Déborah", "Laure",
    "Clarisse", "Flora", "Mireille", "Irène", "Tatiana", "Angélique", "Astrid", "Paola",
    "Séverine", "Christelle", "Noëlla", "Ariane", "Ornella", "Josiane", "Justine", "Anne",
    "Thérèse", "Lucie", "Rosine", "Colette", "Pélagie", "Carine", "Isabelle", "Fati",
    "Oumou", "Ramatou", "Aminata", "Hadjara", "Khadija", "Zeinabou", "Safiatou",
    "Adjowa", "Adjo", "Akofa", "Essi", "Désirée", "Myriam", "Sarah", "Rachel"
]


LAST_NAMES = [
    "Adjovi", "Gnonlonfoun", "Agossa", "Togbé", "Houngbédji", "Yabi", "Koutché",
    "Soglo", "Ahouansou", "Kpodar", "Dossou", "Dah", "Anago", "Akakpo", "Koffi",
    "Mensah", "Ouedraogo", "Diallo", "Traoré", "N'diaye", "Mabiala", "Okoye",

    "Gnandi", "Hounkpati", "Kouassi", "Kouame", "Kossi", "Kodjo", "Ahouansou",
    "Agbo", "Koura", "Houngbo", "Kougnin", "Kpogue", "Kpovi", "Sossou", "Houssou",
    "Zossou", "Avo", "Baba", "Bello", "Bamba", "Bah", "Balde", "Bangoura",
    "Camara", "Coulibaly", "Cissé", "Coumba", "Diabaté", "Diarra", "Fofana",

    "Koné", "Koulibaly", "Kone", "Kouyaté", "Kouassi", "Mendy", "Mbaye", "Mbengue",
    "Mohammed", "Moussa", "Ndiaye", "Niang", "Nguessan", "N’Guessan", "Oumar",
    "Sarr", "Sankara", "Sawadogo", "Souleyman", "Sow", "Sylla", "Tamba", "Traore",
    "Toure", "Yamba", "Yeo", "Zongo", "Zoungrana",

    "Akakpo", "Akindo", "Akindele", "Adeyemi", "Adebayo", "Adebisi", "Adeyinka",
    "Adekunle", "Adesina", "Afolabi", "Ajayi", "Akanbi", "Akpan", "Amadou",
    "Aminu", "Anan", "Annan", "Antoine", "Assogba", "Atcholi", "Atakora",

    "Balogun", "Bankole", "Boahen", "Chukwu", "Ebo", "Ekpo", "Etim", "Fofana",
    "Gbessi", "Guedegbe", "Idriss", "Ismail", "Jalloh", "Kabba", "Kaboré",
    "Kakpo", "Kanzi", "Karamoko", "Kibaki", "Kouadio", "Koulibaly", "Kwame",
    "Lamine", "Mamadou", "Mbakop", "Moyo", "Nwankwo", "Nwachukwu", "Obi",

    "Okechukwu", "Ola", "Olusola", "Onyango", "Owusu", "Saïdou", "Sekou",
    "Senyo", "Seydou", "Tcham'", "Tchao", "Tchibota", "Tiémoko", "Yao", "Yem",
    "Yendé", "Zanou", "Zinédine"
]


EMAIL_DOMAINS = ["example.com", "school.test", "gmail.com", "yahoo.com"]  # last two ok in dev

# ---------- helpers ----------
def rnd_choice_name(sex):
    if sex == "M":
        return random.choice(MALE_FIRST)
    return random.choice(FEMALE_FIRST)

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

def random_dob(min_age=9, max_age=21):
    today = date.today()
    age = random.randint(min_age, max_age)
    # choose a random day within that year
    start = date(today.year - age, 1, 1)
    end = date(today.year - age, 12, 31)
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))

def create_user_and_student(first_name, last_name, sex, dob, school_class, password="ChangeMe123!"):
    base_username = f"{first_name.lower()}.{last_name.lower()}".replace(" ", "").replace("'", "")
    username = unique_username(base_username)
    local_part = username  # simple email local part
    email = unique_email(local_part)
    with transaction.atomic():
        user = User.objects.create(username=username, first_name=first_name, last_name=last_name, email=email)
        user.set_password(password)
        user.save()

        # Create Student - Student.id generates automatically via default generate_student_id
        student = Student.objects.create(
            user=user,
            date_of_birth=dob,
            sex=sex,
            school_class=school_class
        )
    return user, student

def populate():
    classes = list(SchoolClass.objects.all().order_by("id"))
    if not classes:
        print("Aucune SchoolClass trouvée. Crée d'abord des classes dans 'academics.SchoolClass'.")
        return

    total_created = 0
    summary = []

    print(f"Found {len(classes)} classes. Populating each with 35-45 students...")

    for cls in classes:
        existing = Student.objects.filter(school_class=cls).count()
        target = random.randint(35, 45)
        to_create = max(0, target - existing)
        created_ids = []
        print(f"Class {cls.id} ({getattr(cls, 'name', 'N/A')}): existing={existing}, target={target}, will_create={to_create}")

        for _ in range(to_create):
            sex = random.choice(["M", "F"])
            first = rnd_choice_name(sex)
            last = random.choice(LAST_NAMES)
            dob = random_dob(min_age=6, max_age=18)
            # ensure username uniqueness: maybe combine with random digits sometimes
            try:
                user, student = create_user_and_student(first, last, sex, dob, cls)
                created_ids.append(student.id)
                total_created += 1
            except Exception as e:
                # log and continue (very defensive)
                print(f"Erreur création pour {first} {last} in class {cls.id}: {e}")
                continue

        summary.append({
            "class_id": cls.id,
            "class_name": getattr(cls, "name", None),
            "created": len(created_ids),
            "new_student_ids": created_ids
        })

    print("\n--- Résumé ---")
    for s in summary:
        print(f"Class {s['class_id']} ({s['class_name']}): created {s['created']} students")
    print(f"Total students created: {total_created}")
    print("Finished.")

if __name__ == "__main__":
    try:
        populate()
    except Exception as e:
        print("Erreur lors du peuplement :", e)
        sys.exit(1)
