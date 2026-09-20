"""
Övervakning av kundernas sajter, visad i kundportalen.

Tre saker att hålla isär:

- MonitorSettings: VAD kunden får se. En bool per övervakningstyp, styrs
  från kundkortet i /manage/. Avstängt = varken insamlat eller visat.
- MonitoredDomain: VAR vi tittar. En kund kan ha flera domäner; en av dem
  är primär. Pluskunder (egen server med vår plattform) har dessutom ett
  statusendpoint (/status/adx/) som ger serverdata, backup, besök.
- Check: VAD vi såg, tidsstämplat. Snabbkontrollen (var 5:e minut) ger
  drifttid och svarstid; dygnskontrollen ger certifikat, registrar,
  e-posthälsa, prestanda, säkerhet och fel i Sentry.

Larm går BARA till byrån (emails.py). Kunden ser läget i portalen och får
aldrig automatiska mejl härifrån.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.projects.models import Customer


class Kind(models.TextChoices):
    UPTIME = "uptime", "Drifttid och svarstid"
    SSL = "ssl", "HTTPS-certifikat"
    DOMAIN = "domain", "Domän och registrar"
    EMAIL = "email", "E-postens hälsa"
    PERFORMANCE = "performance", "Prestanda"
    SECURITY = "security", "Säkerhet"
    SNAPSHOT = "snapshot", "Server (statusendpoint)"
    ERRORS = "errors", "Fel i Sentry"


#: Vilka kontroller som körs var 5:e minut respektive en gång per dygn.
QUICK_KINDS = (Kind.UPTIME, Kind.SNAPSHOT)
DAILY_KINDS = (Kind.SSL, Kind.DOMAIN, Kind.EMAIL, Kind.SECURITY, Kind.PERFORMANCE, Kind.ERRORS)


class MonitorSettings(models.Model):
    """Kundens övervakning: av/på per typ, och byråns anteckning överst på sidan."""

    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name="monitor")
    show_uptime = models.BooleanField("Drifttid (online-status)", default=True)
    show_response = models.BooleanField("Svarstid", default=True)
    show_ssl = models.BooleanField("HTTPS-certifikat", default=True)
    show_domain = models.BooleanField("Domän och registrar", default=True)
    show_email = models.BooleanField("E-postens hälsa", default=True)
    show_performance = models.BooleanField("Prestanda (PageSpeed)", default=True)
    show_security = models.BooleanField("Säkerhet", default=True)
    show_visits = models.BooleanField("Besök (kräver statusendpoint)", default=False)
    show_server = models.BooleanField("Server (kräver statusendpoint)", default=False)
    show_errors = models.BooleanField("Fel i Sentry", default=False)
    show_gbp = models.BooleanField("Google Business Profile", default=False)
    show_search = models.BooleanField("Sökpositioner (Search Console)", default=False)
    show_events = models.BooleanField("Händelser (tidslinje)", default=True)
    note = models.TextField("Anteckning från ADX", blank=True, max_length=1000)
    # Sentry-projektets slug i byråns organisation (SENTRY_ORG_SLUG i env).
    sentry_project = models.CharField("Sentry-projekt", max_length=80, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Övervakningsinställning"
        verbose_name_plural = "Övervakningsinställningar"

    def __str__(self):
        return f"Övervakning: {self.customer}"

    #: Vilken kontroll som ger data till vilken panel.
    KIND_FOR = {
        "show_uptime": Kind.UPTIME,
        "show_response": Kind.UPTIME,
        "show_ssl": Kind.SSL,
        "show_domain": Kind.DOMAIN,
        "show_email": Kind.EMAIL,
        "show_performance": Kind.PERFORMANCE,
        "show_security": Kind.SECURITY,
        "show_visits": Kind.SNAPSHOT,
        "show_server": Kind.SNAPSHOT,
        "show_errors": Kind.ERRORS,
    }

    @classmethod
    def toggle_fields(cls):
        return [f.name for f in cls._meta.fields if f.name.startswith("show_")]

    def enabled_kinds(self):
        """Kontroller som ska köras: bara det kunden faktiskt får se."""
        kinds = set()
        for field, kind in self.KIND_FOR.items():
            if getattr(self, field):
                kinds.add(kind)
        return kinds

    @property
    def anything_enabled(self):
        return any(getattr(self, f) for f in self.toggle_fields())


class MonitoredDomain(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="domains")
    name = models.CharField("Domän", max_length=253, unique=True, help_text="t.ex. nordanbygg.se")
    is_primary = models.BooleanField("Primär", default=False)
    is_active = models.BooleanField("Övervakas", default=True)
    # Pluskund: vår plattform på egen server exponerar /status/adx/ med den
    # delade nyckeln (ADX_STATUS_KEY). Tomt = ingen serverdata.
    status_url = models.URLField("Statusendpoint", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_primary", "name"]
        verbose_name = "Övervakad domän"
        verbose_name_plural = "Övervakade domäner"

    def __str__(self):
        return self.name

    @property
    def url(self):
        return f"https://{self.name}/"

    def latest(self, kind):
        return self.checks.filter(kind=kind).order_by("-checked_at").first()

    def open_incident(self):
        return self.incidents.filter(ended_at__isnull=True).first()


class Check(models.Model):
    """En observation. `ok` är sammanfattningen, `data` detaljerna."""

    domain = models.ForeignKey(MonitoredDomain, on_delete=models.CASCADE, related_name="checks")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    checked_at = models.DateTimeField(default=timezone.now, db_index=True)
    ok = models.BooleanField(default=True)
    ms = models.PositiveIntegerField(null=True, blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-checked_at"]
        indexes = [models.Index(fields=["domain", "kind", "-checked_at"])]
        verbose_name = "Kontroll"
        verbose_name_plural = "Kontroller"

    def __str__(self):
        state = "ok" if self.ok else "FEL"
        return f"{self.domain} {self.kind} {self.checked_at:%Y-%m-%d %H:%M} {state}"


class Incident(models.Model):
    """Ett avbrott: öppnas vid andra misslyckade kontrollen i rad, stängs vid nästa lyckade."""

    domain = models.ForeignKey(MonitoredDomain, on_delete=models.CASCADE, related_name="incidents")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Avbrott"
        verbose_name_plural = "Avbrott"

    def __str__(self):
        return f"{self.domain} {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def duration_minutes(self):
        end = self.ended_at or timezone.now()
        return max(1, int((end - self.started_at).total_seconds() // 60))


def settings_for(customer):
    obj, _ = MonitorSettings.objects.get_or_create(customer=customer)
    return obj


def status_key_configured():
    return bool(getattr(settings, "ADX_STATUS_KEY", ""))
