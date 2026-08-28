"""
Tjänster (services) module.

ADX:s tjänstekatalog (webbutveckling, automation, hosting, …). Priser bor i
paketblocken på blocksidorna, aldrig på tjänstemodellen - allt utanför
paketen är offert.

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
