"""
Städer (areas) - the geographic pages.

A single self-referencing `Area` tree covers all three levels:

    Län (region) -> Kommun (municipality) -> Stadsdel/Ort (district)

Every level is a page in its own right and lives in one flat URL namespace
(`/digitalbyra/<slug>/`). Keeping the URL flat rather than nesting the
hierarchy is deliberate: `/digitalbyra/centrum-solna/` can never collide with
another branch of the tree. Hierarchy still exists in the data - it drives
breadcrumbs, the child list on a parent page, and visibility inheritance.

Visibility is inherited downwards: hiding a kommun hides all of its districts
without touching their own flag, so the customer can import the whole country
and switch areas on one at a time as photos and copy land. See
`AreaQuerySet.visible()` and `Area.is_visible`.

Which services a page shows is per-area *and* per-audience, via the
`AreaService` through model. That reuses the existing Audience objects
(Privatperson / BRF / Företag), so adding an audience later needs no change
here.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.common.models import TimeStampedModel

# How many levels up `visible()` has to look. The tree is region -> municipality
# -> district, so a district has at most two ancestors.
MAX_DEPTH = 3


class AreaLevel(models.TextChoices):
    REGION = "region", _("Län")
    MUNICIPALITY = "municipality", _("Kommun")
    DISTRICT = "district", _("Stadsdel/Ort")


#: Å, Ä and Ö sort after Z in Swedish, but the database collation puts them
#: next to A and O - which lands Österåker between Nynäshamn and Salem, and
#: Värmdö before Vaxholm. Mapping them onto the three ASCII characters that
#: follow "z" restores the order a Swedish reader expects. Done in Python so it
#: behaves identically on Postgres and SQLite, rather than depending on a
#: collation being installed.
_SWEDISH_ORDER = str.maketrans({"å": "{", "ä": "|", "ö": "}"})


def swedish_sort_key(value):
    """Sort key giving Swedish alphabetical order (... x, y, z, å, ä, ö)."""
    return (value or "").lower().translate(_SWEDISH_ORDER)


def sort_areas(areas):
    """Order a list of areas by their own `order` field, then Swedish name."""
    return sorted(areas, key=lambda a: (a.order, swedish_sort_key(a.name)))


#: Which parent level each level expects. None = must be a root.
PARENT_LEVEL = {
    AreaLevel.REGION: None,
    AreaLevel.MUNICIPALITY: AreaLevel.REGION,
    AreaLevel.DISTRICT: AreaLevel.MUNICIPALITY,
}


class AreaQuerySet(models.QuerySet):
    def visible(self):
        """
        Areas that are active *and* not hidden by an inactive ancestor.

        `exclude(parent__is_active=False)` leaves rows whose parent is NULL
        alone - the join doesn't match, so roots are never excluded. Two levels
        of exclusion covers the whole tree (see MAX_DEPTH).
        """
        return (
            self.filter(is_active=True)
            .exclude(parent__is_active=False)
            .exclude(parent__parent__is_active=False)
        )

    def regions(self):
        return self.filter(level=AreaLevel.REGION)

    def municipalities(self):
        return self.filter(level=AreaLevel.MUNICIPALITY)

    def districts(self):
        return self.filter(level=AreaLevel.DISTRICT)


class Area(TimeStampedModel):
    """Ett serviceområde: ett län, en kommun eller en stadsdel/ort."""

    name = models.CharField(_("Namn"), max_length=120)
    # ADX-designen: sidans gradientfärg (tom = sajtens standard).
    gradient_color = models.CharField(max_length=7, blank=True)
    slug = models.SlugField(_("Webbadress"), max_length=140, unique=True, blank=True)
    level = models.CharField(
        _("Nivå"),
        max_length=20,
        choices=AreaLevel.choices,
        default=AreaLevel.MUNICIPALITY,
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("Överordnat område"),
        help_text=_("Kommun för en stadsdel, län för en kommun."),
    )

    # --- Content ----------------------------------------------------------
    heading = models.CharField(
        _("Rubrik (H1)"),
        max_length=200,
        blank=True,
        help_text=_("Lämna tomt för 'Digitalbyrå i {namn}'."),
    )
    intro = models.CharField(
        _("Underrubrik"),
        max_length=300,
        blank=True,
        help_text=_("Kort text under rubriken. Vanlig text."),
    )
    body = models.TextField(
        _("Lokal text"),
        blank=True,
        help_text=_(
            "Rich text. Skriv något som bara stämmer för det här området - "
            "det är det som skiljer sidan från en mall."
        ),
    )
    image = models.ForeignKey(
        "website.MediaFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Bild"),
    )

    # --- Map ---------------------------------------------------------------
    # Filled by `manage.py geocode_areas`, editable by hand afterwards. An area
    # without coordinates simply renders no map rather than a wrong one.
    latitude = models.DecimalField(
        _("Latitud"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        _("Longitud"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    map_zoom = models.PositiveSmallIntegerField(
        _("Zoomnivå"),
        null=True,
        blank=True,
        help_text=_("Lämna tomt för standard: 8 för län, 11 för kommun, 13 för ort."),
    )

    # --- Relations --------------------------------------------------------
    neighbours = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="neighbour_of",
        verbose_name=_("Grannområden"),
        help_text=_("Länkas längst ner på sidan."),
    )
    services = models.ManyToManyField(
        "services.Service",
        through="AreaService",
        blank=True,
        related_name="areas",
        verbose_name=_("Tjänster"),
    )
    faq_section = models.ForeignKey(
        "faq.FAQSection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="areas",
        verbose_name=_("Delad FAQ-sektion"),
        help_text=_("Visas efter områdets egna frågor."),
    )

    # --- Publishing -------------------------------------------------------
    is_active = models.BooleanField(
        _("Aktiv"),
        default=True,
        help_text=_("Av döljer området och alla underområden."),
    )
    order = models.PositiveIntegerField(_("Sortering"), default=0)
    meta_title = models.CharField(_("Sidtitel (SEO)"), max_length=70, blank=True)
    meta_description = models.CharField(_("Beskrivning (SEO)"), max_length=200, blank=True)

    objects = AreaQuerySet.as_manager()

    class Meta:
        verbose_name = _("Serviceområde")
        verbose_name_plural = _("Serviceområden")
        ordering = ["level", "order", "name"]
        indexes = [
            models.Index(fields=["level", "is_active"]),
            models.Index(fields=["parent", "order"]),
        ]

    def __str__(self):
        return self.name

    # --- Validation & saving ---------------------------------------------

    def clean(self):
        expected = PARENT_LEVEL.get(self.level)
        if expected is None and self.parent_id:
            raise ValidationError({"parent": _("Ett län kan inte ha ett överordnat område.")})
        if expected is not None and self.parent_id:
            if self.parent.level != expected:
                raise ValidationError(
                    {
                        "parent": _("Överordnat område måste vara %(level)s.")
                        % {"level": AreaLevel(expected).label.lower()}
                    }
                )
        if self.parent_id and self.parent_id == self.pk:
            raise ValidationError({"parent": _("Ett område kan inte vara sitt eget.")})

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug(slugify(self.name))
        super().save(*args, **kwargs)

    def _unique_slug(self, base):
        """
        Slugs live in one flat namespace, so names that repeat across
        municipalities (Centrum, Björkhagen, ...) need disambiguating. Suffix
        with the parent's slug first - '/digitalbyra/centrum-solna/' reads
        better than '/digitalbyra/centrum-2/' - and fall back to a counter.
        """
        base = base or "omrade"
        candidates = [base]
        if self.parent_id:
            candidates.append(f"{base}-{self.parent.slug}")
        for candidate in candidates:
            if not Area.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                return candidate
        stem = candidates[-1]
        counter = 2
        while Area.objects.filter(slug=f"{stem}-{counter}").exclude(pk=self.pk).exists():
            counter += 1
        return f"{stem}-{counter}"

    # --- Display ----------------------------------------------------------

    def get_absolute_url(self):
        return reverse("areas:area_detail", kwargs={"slug": self.slug})

    @property
    def body_paragraphs(self):
        """Brödtexten som stycken (dubbla radbrytningar) för .prose-mallen."""
        return [p.strip() for p in (self.body or "").split("\n\n") if p.strip()]

    @property
    def display_heading(self):
        return self.heading or f"Digitalbyrå i {self.name}"

    @property
    def is_visible(self):
        """True when this area and every ancestor is active."""
        node = self
        for _step in range(MAX_DEPTH):
            if not node.is_active:
                return False
            if node.parent_id is None:
                return True
            node = node.parent
        return True

    @property
    def hidden_by_parent(self):
        """Active in itself, but an ancestor is switched off."""
        return self.is_active and not self.is_visible

    def ancestors(self):
        """Root-first list of ancestors, for breadcrumbs."""
        chain = []
        node = self.parent
        for _step in range(MAX_DEPTH):
            if node is None:
                break
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    #: Sensible map zoom per level when none is set on the area itself.
    DEFAULT_ZOOM = {
        AreaLevel.REGION: 8,
        AreaLevel.MUNICIPALITY: 11,
        AreaLevel.DISTRICT: 13,
    }

    @property
    def has_map(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def zoom(self):
        return self.map_zoom or self.DEFAULT_ZOOM.get(self.level, 11)

    @property
    def region(self):
        """The län this area belongs to (itself if it is one)."""
        if self.level == AreaLevel.REGION:
            return self
        for node in self.ancestors():
            if node.level == AreaLevel.REGION:
                return node
        return None

    @property
    def municipality(self):
        """The kommun this area belongs to (itself if it is one)."""
        if self.level == AreaLevel.MUNICIPALITY:
            return self
        for node in self.ancestors():
            if node.level == AreaLevel.MUNICIPALITY:
                return node
        return None


class AreaService(models.Model):
    """
    A service offered in an area, and to whom.

    `audiences` empty means "all audiences" - the common case, and the default
    so bulk-linking a service to 26 municipalities needs no extra clicks.
    """

    area = models.ForeignKey(
        Area,
        on_delete=models.CASCADE,
        related_name="area_services",
        verbose_name=_("Område"),
    )
    service = models.ForeignKey(
        "services.Service",
        on_delete=models.CASCADE,
        related_name="area_links",
        verbose_name=_("Tjänst"),
    )
    audiences = models.ManyToManyField(
        "services.Audience",
        blank=True,
        related_name="area_services",
        verbose_name=_("Målgrupper"),
        help_text=_("Tomt = visas för alla målgrupper."),
    )
    has_own_page = models.BooleanField(
        _("Egen sida"),
        default=False,
        help_text=_("Används inte - kombinationssidor per tjänst och stad finns inte i ADX."),
    )
    order = models.PositiveIntegerField(_("Sortering"), default=0)

    class Meta:
        verbose_name = _("Tjänst i område")
        verbose_name_plural = _("Tjänster i område")
        ordering = ["order", "service__order", "service__name"]
        constraints = [
            models.UniqueConstraint(fields=["area", "service"], name="unique_area_service")
        ]

    def __str__(self):
        return f"{self.service.name} i {self.area.name}"

    def matches_audience(self, audience):
        """Empty audience list means the service is shown to everyone."""
        if audience is None:
            return True
        linked = list(self.audiences.all())
        return not linked or audience in linked


class AreaFAQ(TimeStampedModel):
    """En ortsspecifik fråga. Renderas även som FAQPage-schema."""

    area = models.ForeignKey(
        Area,
        on_delete=models.CASCADE,
        related_name="faq_items",
        verbose_name=_("Område"),
    )
    question = models.CharField(_("Fråga"), max_length=300)
    answer = models.TextField(_("Svar"))
    order = models.PositiveIntegerField(_("Sortering"), default=0)

    class Meta:
        verbose_name = _("Vanlig fråga (område)")
        verbose_name_plural = _("Vanliga frågor (område)")
        ordering = ["order", "id"]

    def __str__(self):
        return self.question
