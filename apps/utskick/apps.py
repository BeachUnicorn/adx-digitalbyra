from django.apps import AppConfig


class UtskickConfig(AppConfig):
    """Flamingo 2.0: kontakter och utskick (README.md i den här mappen).

    Etiketten är "utskick". Ordet "Campaign" används aldrig här:
    flamingo.Campaign är Google Ads (beslut D1)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.utskick"
    label = "utskick"
    verbose_name = "Utskick"
