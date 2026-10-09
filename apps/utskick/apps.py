from django.apps import AppConfig


class UtskickConfig(AppConfig):
    """Flamingo 2.0: kontakter och utskick (README.md i den här mappen).

    Etiketten är "utskick". Ordet "Campaign" används aldrig här:
    flamingo.Campaign är Google Ads (beslut D1)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.utskick"
    label = "utskick"
    verbose_name = "Utskick"

    def ready(self):
        # apps/sms meddelar ändrade sms-lägen och frågar efter portalens
        # etiketter via sina krokar; sms-appen importerar inget härifrån
        # (README C.1).
        from apps.sms import hooks

        from . import smsbridge

        hooks.register_status_callback(smsbridge.sync_from_message)
        hooks.register_labeler(smsbridge.labels_for)
