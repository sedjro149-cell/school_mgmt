# -*- coding: utf-8 -*-
from academics.models import Level, SchoolClass, Subject, ClassSubject

# =========================
# Création des niveaux
# =========================
niveaux = ["6e", "5e", "4e", "3e", "Seconde", "Premiere", "Terminale"]
for nom in niveaux:
    Level.objects.get_or_create(name=nom)

# =========================
# Création des classes
# =========================
# 6e et 5e : 2 groupes A et B
for level_name in ["6e", "5e"]:
    level = Level.objects.get(name=level_name)
    for group in ["A", "B"]:
        SchoolClass.objects.get_or_create(name=f"{level_name} {group}", level=level)

# 4e et 3e : 2 groupes A et B
for level_name in ["4e", "3e"]:
    level = Level.objects.get(name=level_name)
    for group in ["A", "B"]:
        SchoolClass.objects.get_or_create(name=f"{level_name} {group}", level=level)

# Second cycle : Seconde, Premiere, Terminale : 4 groupes A B C D
for level_name in ["Seconde", "Premiere", "Terminale"]:
    level = Level.objects.get(name=level_name)
    for group in ["A", "B", "C", "D"]:
        SchoolClass.objects.get_or_create(name=f"{level_name} {group}", level=level)

# =========================
# Création des matières
# =========================
matieres = [
    "Anglais", "Francais", "Maths", "Physique-Chimie", "Histoire et Geographie",
    "Philosophie", "EPS", "Espagnol", "Allemand", "Economie", "Economie familiale",
    "Sciences de la Vie et de la Terre (SVT)", "Musique"
]

for nom in matieres:
    Subject.objects.get_or_create(name=nom)

# =========================
# Attribution des matières aux classes avec coefficients
# =========================
# Dictionnaire pour définir les coefficients pour chaque niveau et groupe
coeffs = {
    # Premier cycle
    "6e": {"Maths": 1, "Francais": 1, "Anglais":1, "Physique-Chimie":1, "Histoire et Geographie":1,
           "EPS":1, "Musique":1, "Economie familiale":1},
    "5e": {"Maths": 1, "Francais": 1, "Anglais":1, "Physique-Chimie":1, "Histoire et Geographie":1,
           "EPS":1, "Musique":1, "Economie familiale":1},
    "4e": {"Maths": 3, "Francais":2, "Anglais":2, "Physique-Chimie":2, "Histoire et Geographie":2,
           "EPS":1, "Musique":1, "Espagnol":2, "Economie familiale":1},
    "3e": {"Maths": 3, "Francais":2, "Anglais":2, "Physique-Chimie":2, "Histoire et Geographie":2,
           "EPS":1, "Musique":1, "Allemand":2, "Economie familiale":1},
    # Second cycle
    "Seconde": {
        "A": {"Maths":2, "Francais":4, "Anglais":4, "Physique-Chimie":2, "Histoire et Geographie":4,
              "EPS":1, "Musique":1, "Philosophie":2, "Economie":5, "Economie familiale":1},
        "B": {"Maths":2, "Francais":4, "Anglais":4, "Physique-Chimie":2, "Histoire et Geographie":4,
              "EPS":1, "Musique":1, "Philosophie":2, "Economie":5, "Economie familiale":1},
        "C": {"Maths":6, "Francais":2, "Anglais":2, "Physique-Chimie":5, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":2},
        "D": {"Maths":5, "Francais":2, "Anglais":2, "Physique-Chimie":4, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6},
    },
    "Premiere": {
        "A": {"Maths":6, "Francais":4, "Anglais":4, "Physique-Chimie":5, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6, "Economie":5, "Economie familiale":1},
        "B": {"Maths":6, "Francais":4, "Anglais":4, "Physique-Chimie":5, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6, "Economie":5, "Economie familiale":1},
        "C": {"Maths":6, "Francais":2, "Anglais":2, "Physique-Chimie":5, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6},
        "D": {"Maths":5, "Francais":2, "Anglais":2, "Physique-Chimie":4, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6},
    },
    "Terminale": {
        "A": {"Maths":6, "Francais":4, "Anglais":4, "Physique-Chimie":5, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6, "Economie":5, "Economie familiale":1},
        "B": {"Maths":6, "Francais":4, "Anglais":4, "Physique-Chimie":5, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6, "Economie":5, "Economie familiale":1},
        "C": {"Maths":6, "Francais":2, "Anglais":2, "Physique-Chimie":5, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6},
        "D": {"Maths":5, "Francais":2, "Anglais":2, "Physique-Chimie":4, "Histoire et Geographie":2,
              "EPS":1, "Musique":1, "Philosophie":6},
    }
}

# Boucle pour créer ClassSubject
for cls in SchoolClass.objects.all():
    lvl_name = cls.level.name
    group = cls.name.split()[-1]  # Extrait le groupe
    if lvl_name in coeffs:
        group_coeffs = coeffs[lvl_name].get(group, coeffs[lvl_name].get(lvl_name, {}))
        for mat_name, coef in group_coeffs.items():
            try:
                subject = Subject.objects.get(name=mat_name)
                ClassSubject.objects.get_or_create(school_class=cls, subject=subject, coefficient=coef)
            except Subject.DoesNotExist:
                print(f"Attention: la matière {mat_name} n'existe pas !")
