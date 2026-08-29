"""
Built-in privacy-aware web analytics.

Data is collected silently for later productization. There is NO /manage/
stats UI - staff inspect raw data in Django admin (/admin/). The only
customer-facing surface is the traffic-source snapshot shown on an Inquiry.

Model hierarchy:
    Visitor  (permanent identity, 2-year cookie)
      └─ Session  (one visit; 30-min inactivity window)
           ├─ PageView  (each page hit)
           └─ Event     (tel_click, email_click, booking, ...)

Design notes:
- First referrer / first landing page are stored permanently on Visitor and
  never overwritten - that's the "where did they originally come from" signal.
- Session stores the per-visit source so returning visits keep their own
  attribution.
- IPs are anonymised (last octet / last 80 bits masked) before saving.
- Engaged time is tracked separately from wall-clock duration. Wall-clock
  (last_activity - started_at) reads 0 for single-pageview visits, so it can't
  answer "how long did they stay"; engaged_seconds accumulates only the time
  the tab was actually visible.
"""

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def format_seconds(secs) -> str:
    """Render a second count as '42s' or '3m 07s'. Shared by models + reports."""
    secs = int(secs or 0)
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60:02d}s"


class TrafficSource(models.TextChoices):
    DIRECT = "direct", _("Direkt")
    ORGANIC = "organic", _("Sökmotor")
    SOCIAL = "social", _("Sociala medier")
    REFERRAL = "referral", _("Hänvisning")
    EMAIL = "email", _("E-post")
    PAID = "paid", _("Annons")
    INTERNAL = "internal", _("Intern")


class DeviceType(models.TextChoices):
    DESKTOP = "desktop", _("Dator")
    MOBILE = "mobile", _("Mobil")
    TABLET = "tablet", _("Surfplatta")
    BOT = "bot", _("Bot")
    UNKNOWN = "unknown", _("Okänd")


class Placement(models.TextChoices):
    """
    Where on the page an interaction happened.

    Resolved client-side by walking up the DOM (see static/js/analytics.js) and
    validated against this list server-side before saving - the beacon payload
    is untrusted input. Lets us answer "which button actually drives calls",
    which matters because most contact links are CMS-managed menu items rather
    than hardcoded markup.
    """

    HEADER = "header", _("Sidhuvud")
    MOBILE_NAV = "mobile_nav", _("Mobilmeny")
    HERO = "hero", _("Hero-sektion")
    CTA = "cta", _("CTA-sektion")
    QUOTE_CTA = "quote_cta", _("Offert-CTA")
    STICKY = "sticky", _("Fast knapp")
    FOOTER = "footer", _("Sidfot")
    CONTENT = "content", _("I löpande text")
    OTHER = "other", _("Övrigt")


class Visitor(models.Model):
    """
    A unique visitor, identified by a long-lived first-party cookie.

    First-touch attribution lives here and is written exactly once.
    """

    uuid = models.UUIDField(_("Besökar-ID"), unique=True, editable=False, db_index=True)

    # First-touch attribution - set once, never overwritten.
    first_seen = models.DateTimeField(_("Först sedd"), default=timezone.now)
    first_referrer = models.URLField(_("Första referrer"), max_length=1000, blank=True)
    first_source = models.CharField(
        _("Första källa"),
        max_length=20,
        choices=TrafficSource.choices,
        default=TrafficSource.DIRECT,
    )
    first_source_detail = models.CharField(
        _("Första källa (detalj)"),
        max_length=100,
        blank=True,
        help_text=_("T.ex. google, facebook, newsletter."),
    )
    first_landing_page = models.CharField(_("Första landningssida"), max_length=500, blank=True)

    last_seen = models.DateTimeField(_("Senast sedd"), default=timezone.now)
    session_count = models.PositiveIntegerField(_("Antal besök"), default=0)

    class Meta:
        verbose_name = _("Besökare")
        verbose_name_plural = _("Besökare")
        ordering = ["-last_seen"]
        indexes = [
            # Default ordering + "active visitors" queries hit last_seen;
            # first_seen backs the admin date_hierarchy and new-visitor charts.
            models.Index(fields=["-last_seen"]),
            models.Index(fields=["first_seen"]),
            models.Index(fields=["first_source"]),
        ]

    def __str__(self):
        return f"{self.uuid} ({self.get_first_source_display()})"


class Session(models.Model):
    """A single visit. A new session starts after 30 min of inactivity."""

    uuid = models.UUIDField(_("Sessions-ID"), unique=True, editable=False, db_index=True)
    visitor = models.ForeignKey(
        Visitor,
        on_delete=models.CASCADE,
        related_name="sessions",
        verbose_name=_("Besökare"),
    )

    started_at = models.DateTimeField(_("Startade"), default=timezone.now)
    last_activity = models.DateTimeField(_("Senaste aktivitet"), default=timezone.now)
    pageview_count = models.PositiveIntegerField(_("Antal sidvisningar"), default=0)

    # Per-visit attribution
    referrer = models.URLField(_("Referrer"), max_length=1000, blank=True)
    source = models.CharField(
        _("Källa"),
        max_length=20,
        choices=TrafficSource.choices,
        default=TrafficSource.DIRECT,
    )
    source_detail = models.CharField(_("Källa (detalj)"), max_length=100, blank=True)
    landing_page = models.CharField(_("Landningssida"), max_length=500, blank=True)

    # UTM campaign tracking. term/content are needed to tell two ads inside the
    # same campaign apart - without them a campaign is a single opaque number.
    utm_source = models.CharField(_("UTM source"), max_length=100, blank=True)
    utm_medium = models.CharField(_("UTM medium"), max_length=100, blank=True)
    utm_campaign = models.CharField(_("UTM campaign"), max_length=100, blank=True)
    utm_term = models.CharField(_("UTM term"), max_length=100, blank=True)
    utm_content = models.CharField(_("UTM content"), max_length=100, blank=True)

    # Engaged time: seconds the visitor was actually active on the site, summed
    # from the pageviews below. Unlike last_activity - started_at this does not
    # collapse to 0 for a single-pageview visit.
    engaged_seconds = models.PositiveIntegerField(_("Engagerad tid (s)"), default=0)

    # Client/device
    device_type = models.CharField(
        _("Enhetstyp"),
        max_length=10,
        choices=DeviceType.choices,
        default=DeviceType.UNKNOWN,
    )
    os = models.CharField(_("Operativsystem"), max_length=60, blank=True)
    browser = models.CharField(_("Webbläsare"), max_length=60, blank=True)
    screen_resolution = models.CharField(_("Skärmupplösning"), max_length=20, blank=True)
    # Viewport is what CSS breakpoints actually respond to, so it is the useful
    # number when deciding layout - screen resolution can be far larger.
    viewport_width = models.PositiveIntegerField(_("Vyportbredd"), null=True, blank=True)
    viewport_height = models.PositiveIntegerField(_("Vyporthöjd"), null=True, blank=True)
    user_agent = models.TextField(_("User agent"), blank=True)

    # Anonymised network info
    ip_address = models.GenericIPAddressField(_("IP (anonymiserad)"), null=True, blank=True)
    country = models.CharField(_("Land"), max_length=2, blank=True)

    class Meta:
        verbose_name = _("Session")
        verbose_name_plural = _("Sessioner")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["-started_at"]),
            models.Index(fields=["source"]),
        ]

    def __str__(self):
        return f"{self.uuid} - {self.get_source_display()} ({self.started_at:%Y-%m-%d %H:%M})"

    @property
    def duration_seconds(self):
        """
        Wall-clock span of the session in seconds.

        Note this is last_activity - started_at, so a visit with a single
        pageview reads 0 even if the visitor stayed a while. Prefer
        engaged_seconds when reporting "how long did they stay".
        """
        return int((self.last_activity - self.started_at).total_seconds())

    @property
    def duration_display(self):
        return format_seconds(self.duration_seconds)

    @property
    def engaged_display(self):
        return format_seconds(self.engaged_seconds)

    @property
    def viewport_display(self):
        if self.viewport_width and self.viewport_height:
            return f"{self.viewport_width}\u00d7{self.viewport_height}"
        return ""


class PageView(models.Model):
    """A single page view within a session."""

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="pageviews",
        verbose_name=_("Session"),
    )
    path = models.CharField(_("Sökväg"), max_length=500)
    title = models.CharField(_("Sidtitel"), max_length=255, blank=True)
    viewed_at = models.DateTimeField(_("Tidpunkt"), default=timezone.now)
    # Accumulated from beacon heartbeats; only counts time the tab was visible.
    engaged_seconds = models.PositiveIntegerField(_("Engagerad tid (s)"), default=0)

    class Meta:
        verbose_name = _("Sidvisning")
        verbose_name_plural = _("Sidvisningar")
        ordering = ["viewed_at"]
        indexes = [
            models.Index(fields=["path"]),
            models.Index(fields=["viewed_at"]),
            # Resolving "latest pageview for this session+path" on every
            # engagement heartbeat.
            models.Index(fields=["session", "path", "-viewed_at"]),
            # Per-session journeys and the exit-page report, which both need
            # the newest pageview within a session.
            models.Index(fields=["session", "-viewed_at"]),
        ]

    def __str__(self):
        return f"{self.path} ({self.viewed_at:%H:%M:%S})"

    @property
    def engaged_display(self):
        return format_seconds(self.engaged_seconds)


class EventType(models.TextChoices):
    TEL_CLICK = "tel_click", _("Telefonklick")
    EMAIL_CLICK = "email_click", _("E-postklick")
    BOOKING = "booking", _("Förfrågan/bokning")
    OUTBOUND = "outbound", _("Extern länk")
    # Why people fall out of the inquiry wizard. FORM_ERROR is recorded
    # server-side (the only place that knows which field failed validation);
    # FORM_ABANDON comes from the beacon on pagehide.
    FORM_ERROR = "form_error", _("Formulärfel")
    FORM_ABANDON = "form_abandon", _("Formuläravhopp")
    OTHER = "other", _("Övrigt")


class Event(models.Model):
    """A tracked interaction (tel: click, booking, ...)."""

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("Session"),
    )
    event_type = models.CharField(
        _("Händelsetyp"),
        max_length=20,
        choices=EventType.choices,
    )
    label = models.CharField(_("Etikett"), max_length=255, blank=True)
    path = models.CharField(_("Sökväg"), max_length=500, blank=True)
    placement = models.CharField(
        _("Placering"),
        max_length=20,
        choices=Placement.choices,
        blank=True,
        help_text=_("Var på sidan interaktionen skedde, t.ex. sidhuvud eller sidfot."),
    )
    created_at = models.DateTimeField(_("Tidpunkt"), default=timezone.now)

    class Meta:
        verbose_name = _("Händelse")
        verbose_name_plural = _("Händelser")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["event_type"]),
            # Every dashboard panel filters events by date range, and the admin
            # date_hierarchy scans this column too.
            models.Index(fields=["-created_at"]),
            models.Index(fields=["event_type", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.label}"
