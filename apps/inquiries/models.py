"""
Förfrågningar (inquiries) - non-binding quote/contact requests.

Models:
- Inquiry      : The main inquiry record (customer details + description)
- InquiryImage : Images attached by the customer (stored in MEDIA_ROOT)
"""

import secrets
import string

from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel


def _generate_reference():
    """
    Generate a random 8-character alphanumeric reference.

    Does not reveal submission count. Format: ADX-XXXXXXXX
    Uses uppercase letters + digits, excluding ambiguous chars (0/O, 1/I/L).
    """
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "").replace("L", "")
    alphabet += string.digits.replace("0", "").replace("1", "")
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"ADX-{code}"


class CustomerType(models.TextChoices):
    COMPANY = "company", _("Företag")
    ORG = "org", _("Organisation / Förening")
    PRIVATE = "private", _("Privatperson")


class InquiryBudget(models.TextChoices):
    """Ungefärligt spann - hjälper byrån prioritera, aldrig ett krav."""

    UNDER_25 = "under_25", _("Under 25 000 kr")
    B25_100 = "25_100", _("25 000 – 100 000 kr")
    B100_250 = "100_250", _("100 000 – 250 000 kr")
    OVER_250 = "over_250", _("Över 250 000 kr")
    MONTHLY = "monthly", _("Löpande månadsbudget")
    UNKNOWN = "unknown", _("Vet inte än")


class InquiryTimeline(models.TextChoices):
    ASAP = "asap", _("Så snart som möjligt")
    QUARTER = "quarter", _("Inom 1–3 månader")
    LATER = "later", _("Senare i år")
    EXPLORING = "exploring", _("Utforskar bara")


class InquiryStatus(models.TextChoices):
    NEW = "new", _("Ny")
    IN_PROGRESS = "in_progress", _("Under hantering")
    QUOTED = "quoted", _("Offert skickad")
    CLOSED = "closed", _("Avslutad")


class Inquiry(TimeStampedModel):
    """A non-binding quote/contact request from the website."""

    reference = models.CharField(
        _("Referensnummer"),
        max_length=12,
        unique=True,
        default=_generate_reference,
        editable=False,
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=InquiryStatus.choices,
        default=InquiryStatus.NEW,
    )
    is_read = models.BooleanField(_("Läst"), default=False)

    # Customer type
    customer_type = models.CharField(
        _("Kundtyp"),
        max_length=10,
        choices=CustomerType.choices,
        default=CustomerType.COMPANY,
    )

    # Vad förfrågan gäller: värdet kommer ur en vitlista som byggs av
    # formuläret (tjänsterna + paketen + Annat) - lagras som text så
    # historiken överlever att en tjänst döps om eller tas bort.
    topic = models.CharField(_("Gäller"), max_length=100, blank=True)
    budget = models.CharField(
        _("Ungefärlig budget"),
        max_length=20,
        choices=InquiryBudget.choices,
        blank=True,
    )
    timeline = models.CharField(
        _("Önskad tidplan"),
        max_length=20,
        choices=InquiryTimeline.choices,
        blank=True,
    )

    # Contact details
    company_name = models.CharField(
        _("Företagsnamn / Föreningsnamn"),
        max_length=200,
        blank=True,
    )
    name = models.CharField(_("Namn"), max_length=200)
    email = models.EmailField(_("E-post"))
    phone = models.CharField(_("Telefon"), max_length=50, blank=True)
    street_address = models.CharField(_("Gatuadress"), max_length=255, blank=True)
    postal_code = models.CharField(_("Postnummer"), max_length=10, blank=True)
    city = models.CharField(_("Ort"), max_length=100, blank=True)

    # Description
    description = models.TextField(_("Beskrivning av ärende"))

    # --- Traffic attribution (analytics) -----------------------------------
    # A durable snapshot of where the visitor came from, captured at submit
    # time so it survives even if analytics rows are later purged. The link to
    # the live Session is kept too (SET_NULL) for deeper inspection in admin.
    analytics_session = models.ForeignKey(
        "analytics.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inquiries",
        verbose_name=_("Analyssession"),
    )
    traffic_source = models.CharField(
        _("Trafikkälla"),
        max_length=20,
        blank=True,
        help_text=_("Snapshot: var besökaren kom ifrån (Google, Facebook, …)."),
    )
    traffic_source_detail = models.CharField(
        _("Trafikkälla (detalj)"),
        max_length=100,
        blank=True,
    )
    traffic_referrer = models.URLField(
        _("Referrer"),
        max_length=1000,
        blank=True,
    )

    class Meta:
        verbose_name = _("Förfrågan")
        verbose_name_plural = _("Förfrågningar")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} - {self.name}"

    def get_absolute_url(self):
        return reverse("manage:inquiry_detail", kwargs={"pk": self.pk})

    @property
    def image_count(self):
        return self.images.count()

    @property
    def traffic_source_display(self):
        """Human-readable traffic source for staff/customer display."""
        from apps.analytics.models import TrafficSource

        if not self.traffic_source:
            return _("Okänd")
        label = dict(TrafficSource.choices).get(self.traffic_source, self.traffic_source)
        if self.traffic_source_detail:
            return f"{label} ({self.traffic_source_detail})"
        return str(label)


class InquiryImage(TimeStampedModel):
    """An image attached to an inquiry by the customer."""

    inquiry = models.ForeignKey(
        Inquiry,
        on_delete=models.CASCADE,
        related_name="images",
        verbose_name=_("Förfrågan"),
    )
    file = models.ImageField(
        _("Bild"),
        upload_to="inquiries/%Y/%m/",
    )
    original_filename = models.CharField(_("Filnamn"), max_length=255)
    file_size = models.PositiveIntegerField(_("Filstorlek"), default=0)

    class Meta:
        verbose_name = _("Bifogad bild")
        verbose_name_plural = _("Bifogade bilder")

    def __str__(self):
        return self.original_filename


class NewsletterSignup(models.Model):
    """E-postprenumerant från nyhetsbrevsblocket. Egen tabell - att blanda
    in dem bland förfrågningarna hade förorenat både statistik och inkorg."""

    email = models.EmailField(_("E-post"), unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    source_path = models.CharField(_("Sida"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("Nyhetsbrevsprenumerant")
        verbose_name_plural = _("Nyhetsbrevsprenumeranter")
        ordering = ["-created_at"]

    def __str__(self):
        return self.email
