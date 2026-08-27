from django.apps import AppConfig


class AssistantConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.assistant"
    verbose_name = "AI-redaktören"

    def ready(self):
        # Registers every content model with django-reversion. Central here
        # rather than spread over each app's apps.py, so the list of what is
        # versioned lives in one place.
        from . import revisions

        revisions.register_models()
