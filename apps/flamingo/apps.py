from django.apps import AppConfig


class FlamingoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.flamingo"
    verbose_name = "ADX Flamingo"

    def ready(self):
        # Mediaarkivet kopplar sin signal: en ny landningssida får färgerna
        # ur kontots logotyp (media._new_page_logo_colors).
        from . import media  # noqa: F401
