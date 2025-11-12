# fees/apps.py
from django.apps import AppConfig

class FeesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = "fees"

    def ready(self):
        # Import des signaux
        from . import signals  # noqa

        # Optional: import filters juste pour s'assurer qu'ils sont chargés
        from . import filters  # noqa
