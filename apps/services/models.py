"""
Tjänster (services) module.

Adapted from the ADX `services` app, single-site and VVS-focused. All
car-/booking-specific fields from the bdgroup prototype (is_bookable,
needs_mileage, popular_service, search_keywords, icon, CKEditor) are dropped.

Models:
- ServiceCategory : Tjänstekategori - a group of related services
- Service         : Tjänst - an individual service
- ServiceStep     : Aktivitet/steg som beskriver hur tjänsten utförs
- Audience        : Målgrupp - landing page per audience (Privatperson, BRF, …)

Rich-text fields (`body`, `intro`) are restricted to basic formatting
(bold/italic/links + variables) - see render_with_context_basic.
"""

from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel


def unique_slug(instance, base, fallback):
    """
    A slug for `instance` that no other row of its model uses.

    Names repeat in practice - two tjänster called "Felsökning" under different
    kategorier, an "Övrigt" that already exists - and the slug column is
    unique. Without this the save raises IntegrityError and the customer gets a
    500 page instead of a saved record, which is not a failure they can act on.
    Empty names (a title made only of punctuation slugifies to "") fall back to
    a stem so the row never lands with a blank slug.
    """
    model = type(instance)
    stem = base or fallback
    candidate = stem
    counter = 2
    while model.objects.filter(slug=candidate).exclude(pk=instance.pk).exists():
        candidate = f"{stem}-{counter}"
        counter += 1
    return candidate


class ServiceCategory(TimeStampedModel):
    """En grupp av relaterade tjänster (t.ex. 'Värme & värmepumpar')."""

    name = models.CharField(_("Namn"), max_length=200)
    slug = models.SlugField(_("Slug"), max_length=200, unique=True, blank=True)
    description = models.CharField(
        _("Kort beskrivning"),
        max_length=200,
        blank=True,
        help_text=_("Vanlig text. Används som meta-beskrivning."),
    )
    body = models.TextField(
        _("Brödtext"),
        blank=True,
        help_text=_("Rich text (fet, kursiv, länkar) för kategorisidan."),
    )
    image = models.ForeignKey(
        "website.MediaFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Bild"),
    )
    is_active = models.BooleanField(_("Aktiv"), default=True)
    order = models.PositiveIntegerField(_("Sortering"), default=0)

    class Meta:
        verbose_name = _("Tjänstekategori")
        verbose_name_plural = _("Tjänstekategorier")
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self, slugify(self.name), "kategori")
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        # Kategorier har ingen egen publik sida i ADX-designen - översikten
        # /tjanster/ är närmaste hem.
        return "/tjanster/"


class Service(TimeStampedModel):
    """En enskild tjänst som erbjuds."""

    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="services",
        verbose_name=_("Kategori"),
    )
    name = models.CharField(_("Namn"), max_length=200)
    # ADX-designen: sidans gradientfärg (tom = sajtens standard).
    gradient_color = models.CharField(max_length=7, blank=True)
    slug = models.SlugField(_("Slug"), max_length=200, unique=True, blank=True)
    description = models.CharField(
        _("Kort beskrivning"),
        max_length=200,
        blank=True,
        help_text=_("Vanlig text. Används som meta-beskrivning."),
    )
    body = models.TextField(
        _("Brödtext"),
        blank=True,
        help_text=_("Rich text (fet, kursiv, länkar) för tjänstesidan."),
    )
    image = models.ForeignKey(
        "website.MediaFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Bild"),
    )
    # ROT (Nivå 3): labour + material are the only price inputs. The total
    # price shown to customers is computed (labour + material). ROT is 30% of
    # the labour part only.
    is_rot_eligible = models.BooleanField(
        _("ROT-berättigad"),
        default=True,
        help_text=_("Markera om tjänsten ger rätt till ROT-avdrag (gäller arbetskostnad)."),
    )
    labor_price_from = models.PositiveIntegerField(
        _("Arbetskostnad från (kr)"), null=True, blank=True
    )
    labor_price_to = models.PositiveIntegerField(
        _("Arbetskostnad till (kr)"), null=True, blank=True
    )
    material_price_from = models.PositiveIntegerField(
        _("Materialkostnad från (kr)"), null=True, blank=True
    )
    material_price_to = models.PositiveIntegerField(
        _("Materialkostnad till (kr)"), null=True, blank=True
    )
    audiences = models.ManyToManyField(
        "Audience",
        blank=True,
        related_name="services",
        verbose_name=_("Målgrupper"),
        help_text=_("Vilka målgruppssidor denna tjänst ska listas på."),
    )
    is_active = models.BooleanField(_("Aktiv"), default=True)
    is_featured = models.BooleanField(
        _("Utvald"),
        default=False,
        help_text=_("Visa i utvalda/populära sektioner."),
    )
    is_emergency = models.BooleanField(
        _("Akut/jour"),
        default=False,
        help_text=_("Markera tjänster som erbjuds som akut utryckning/jour."),
    )
    faq_section = models.ForeignKey(
        "faq.FAQSection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="services",
        verbose_name=_("FAQ-sektion"),
        help_text=_("Valfri FAQ som visas på tjänstesidan."),
    )
    order = models.PositiveIntegerField(_("Sortering"), default=0)

    class Meta:
        verbose_name = _("Tjänst")
        verbose_name_plural = _("Tjänster")
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self, slugify(self.name), "tjanst")
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        # Tjänstesidan är en BlockPage med samma slug (seedad ihop med
        # tjänsten). Länken bor här så nav/sidfot/tjänstelistan delar den.
        return f"/{self.slug}/"

    # --- Pricing -----------------------------------------------------------
    # Stored labour/material values are NET (exkl moms). All display helpers
    # take an explicit vat_rate so VAT is applied (or not) at presentation
    # time. ROT is always computed on the labour cost INCLUDING VAT, matching
    # how Skatteverket calculates the deduction.

    @staticmethod
    def _apply_vat(amount, vat_rate, include_vat):
        if amount is None:
            return None
        if include_vat and vat_rate:
            return int(round(amount * (1 + vat_rate / 100)))
        return int(amount)

    @property
    def net_total_from(self):
        """Net (exkl moms) lower total: labour + material. None if neither."""
        vals = [p for p in (self.labor_price_from, self.material_price_from) if p]
        return sum(vals) if vals else None

    @property
    def net_total_to(self):
        """Net (exkl moms) upper total: labour + material. None if neither."""
        vals = [p for p in (self.labor_price_to, self.material_price_to) if p]
        return sum(vals) if vals else None

    @property
    def has_price(self) -> bool:
        return bool(self.net_total_from or self.net_total_to)

    def total_from(self, vat_rate=0, include_vat=False):
        """Total lower bound, with VAT applied when include_vat is True."""
        return self._apply_vat(self.net_total_from, vat_rate, include_vat)

    def total_to(self, vat_rate=0, include_vat=False):
        """Total upper bound, with VAT applied when include_vat is True."""
        return self._apply_vat(self.net_total_to, vat_rate, include_vat)

    @staticmethod
    def _range_display(low, high):
        if low and high and high != low:
            return f"{low:,} – {high:,} kr".replace(",", " ")
        if low:
            return f"Från {low:,} kr".replace(",", " ")
        if high:
            return f"Från {high:,} kr".replace(",", " ")
        return ""

    def price_display(self, vat_rate=0, include_vat=False) -> str:
        """Total price range string, VAT applied per the flags."""
        return self._range_display(
            self.total_from(vat_rate, include_vat),
            self.total_to(vat_rate, include_vat),
        )

    @property
    def price_display_incl_vat(self) -> str:
        """
        Convenience for audience-agnostic listings (service list, category
        page): total range INCL VAT, using the site's configured VAT rate.
        Loads SiteSettings once.
        """
        from apps.website.models import SiteSettings

        vat = SiteSettings.load().vat_rate
        return self.price_display(vat, include_vat=True)

    def rot_deduction(self, rot_percentage, vat_rate=0):
        """
        (from, to) ROT deduction, computed on labour cost INCL VAT.

        ROT is calculated on the VAT-inclusive labour cost regardless of how
        the surrounding prices are displayed, because that's how the actual
        deduction works. Returns (None, None) when not eligible.
        """
        if not self.is_rot_eligible or not rot_percentage:
            return (None, None)
        pct = rot_percentage / 100
        lf = self._apply_vat(self.labor_price_from, vat_rate, True)
        lt = self._apply_vat(self.labor_price_to, vat_rate, True)
        d_from = int(round(lf * pct)) if lf else None
        d_to = int(round(lt * pct)) if lt else None
        return (d_from, d_to)

    def price_after_rot(self, rot_percentage, vat_rate=0):
        """
        (from, to) gross total (incl VAT) minus the ROT deduction.

        Always VAT-inclusive - ROT only makes sense in the consumer (incl VAT)
        context. Returns (None, None) when not eligible.
        """
        if not self.is_rot_eligible or not rot_percentage:
            return (None, None)
        gross_from = self.total_from(vat_rate, include_vat=True)
        gross_to = self.total_to(vat_rate, include_vat=True)
        d_from, d_to = self.rot_deduction(rot_percentage, vat_rate)
        after_from = gross_from - d_from if gross_from and d_from else None
        after_to = gross_to - d_to if gross_to and d_to else None
        return (after_from, after_to)

    def price_after_rot_display(self, rot_percentage, vat_rate=0) -> str:
        after_from, after_to = self.price_after_rot(rot_percentage, vat_rate)
        return self._range_display(after_from, after_to)


class ServiceStep(TimeStampedModel):
    """Aktivitet/steg som beskriver hur tjänsten utförs."""

    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
        related_name="steps",
        verbose_name=_("Tjänst"),
    )
    title = models.CharField(_("Rubrik"), max_length=200)
    description = models.CharField(
        _("Beskrivning"),
        max_length=300,
        blank=True,
        help_text=_("Kort beskrivning av steget (vanlig text)."),
    )
    order = models.PositiveIntegerField(_("Sortering"), default=0)

    class Meta:
        verbose_name = _("Tjänststeg")
        verbose_name_plural = _("Tjänststeg")
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.service.name} - {self.title}"


class Audience(TimeStampedModel):
    """
    Målgrupp: Privatperson, BRF, Fastighetsägare, Företag.

    Egen modell (inte bara choices) så att varje målgrupp kan ha en egen
    landningssida med intro-text och SEO-fält.
    """

    name = models.CharField(_("Namn"), max_length=80, unique=True)
    slug = models.SlugField(max_length=90, unique=True, blank=True)
    intro = models.TextField(
        _("Intro för målgruppssida"),
        blank=True,
        help_text=_("Rich text (fet, kursiv, länkar)."),
    )
    image = models.ForeignKey(
        "website.MediaFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Bild"),
    )
    order = models.PositiveSmallIntegerField(_("Sortering"), default=0)
    is_active = models.BooleanField(_("Aktiv"), default=True)
    # ROT pricing applies per audience: private homeowners qualify, BRF and
    # companies generally do not. Drives whether after-ROT prices are shown.
    rot_applies = models.BooleanField(
        _("ROT-avdrag gäller"),
        default=False,
        help_text=_("Visa pris efter ROT-avdrag i prislistan för denna målgrupp."),
    )
    prices_include_vat = models.BooleanField(
        _("Visa priser inkl. moms"),
        default=True,
        help_text=_(
            "På: priser visas inkl. moms (privatpersoner/BRF). "
            "Av: priser visas exkl. moms (företag)."
        ),
    )
    price_note = models.CharField(
        _("Prisnotis"),
        max_length=200,
        blank=True,
        help_text=_("T.ex. 'Alla priser inkl. moms.' Visas under prislistan."),
    )
    meta_title = models.CharField(max_length=70, blank=True)
    meta_description = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ["order", "name"]
        verbose_name = _("Målgrupp")
        verbose_name_plural = _("Målgrupper")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self, slugify(self.name), "malgrupp")
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        # Målgrupper har ingen egen publik sida i ADX.
        return "/tjanster/"
