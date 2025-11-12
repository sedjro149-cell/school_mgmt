# core/management/commands/seeds_superadmins.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

User = get_user_model()

class Command(BaseCommand):
    help = "Seed initial superadmin users (directors of the school)"

    def handle(self, *args, **kwargs):
        superadmins = [
            {"username": "director1", "email": "director1@school.com", "password": "admin123"},
            {"username": "director2", "email": "director2@school.com", "password": "admin123"},
            {"username": "director3", "email": "director3@school.com", "password": "admin123"},
        ]

        for data in superadmins:
            if User.objects.filter(username=data["username"]).exists():
                self.stdout.write(self.style.WARNING(f"Superadmin {data['username']} already exists"))
            else:
                User.objects.create_superuser(
                    username=data["username"],
                    email=data["email"],
                    password=data["password"]
                )
                self.stdout.write(self.style.SUCCESS(f"Created superadmin {data['username']}")))
