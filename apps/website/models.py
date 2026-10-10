from django.core.validators import MaxValueValidator
from django.db import models
from django.utils.text import slugify

from apps.common.request_memo import memo

#: Startsidans id när ingen SiteSettings-rad finns (se _homepage_id).
_NO_SETTINGS = object()


def _homepage_id():
    """homepage_id på första SiteSettings-raden, eller _NO_SETTINGS.

    get_absolute_url anropas för varje menylänk, två gånger per länk i
    huvudet och mobilmenyn: 80-96 frågor per sida (Sentry
    ADX-DIGITALBYRA-8, F, G och J). Nu en gång per förfrågan
    (apps/common/request_memo.py).
    """

    def lookup():
        rows = list(SiteSettings.objects.order_by("pk").values_list("homepage_id", flat=True)[:1])
        return rows[0] if rows else _NO_SETTINGS

    return memo("website:homepage_id", lookup)


class MediaFile(models.Model):
    """Uploaded media files (images, documents, etc.)."""

    file = models.FileField(upload_to="media/")
    original_filename = models.CharField(max_length=255)
    alt_text = models.CharField(max_length=255, blank=True)

    # Focal point: the part of the image that must stay visible when a crop
    # (object-fit: cover / background-size: cover) cuts the image, e.g. on
    # mobile. Percentages from the top-left corner; 50/50 is plain centering.
    # Lives on the file, not on each usage: pick once, applies everywhere.
    focal_x = models.PositiveSmallIntegerField(default=50, validators=[MaxValueValidator(100)])
    focal_y = models.PositiveSmallIntegerField(default=50, validators=[MaxValueValidator(100)])
    mime_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Image optimization: original is preserved, optimized version served.
    original_file = models.FileField(
        upload_to="media/originals/",
        blank=True,
        help_text="Original file before optimization. Empty until first optimize.",
    )
    is_optimized = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_filename

    @property
    def focal_css(self):
        """The focal point as a CSS position value, e.g. "50% 30%".

        Templates expose it as the custom property --focal; the stylesheet
        decides which property consumes it (object-position for <img>,
        background-position for hero backgrounds).
        """
        return f"{self.focal_x}% {self.focal_y}%"

    @property
    def can_optimize(self):
        """True if this image would benefit from optimization."""
        if not self.mime_type or not self.mime_type.startswith("image/"):
            return False
        # Already optimized
        if self.is_optimized:
            return False
        # Check: too large, too wide, or not webp
        too_wide = (self.width or 0) > 1920
        too_heavy = (self.file_size or 0) > 200 * 1024  # >200KB
        not_webp = self.mime_type != "image/webp"
        return too_wide or too_heavy or not_webp

    @property
    def optimization_hint(self):
        """Human-friendly description of what optimization will do."""
        actions = []
        if (self.width or 0) > 1920:
            actions.append("Minskar upplösningen för snabbare laddning")
        if (self.file_size or 0) > 200 * 1024:
            actions.append("Komprimerar för mindre filstorlek")
        if self.mime_type and self.mime_type != "image/webp":
            actions.append("Konverterar till modernt webbformat")
        return ". ".join(actions) + "." if actions else ""


class BlockPage(models.Model):
    """A page container for the website."""

    #: Designen avgör allt som skiljer: vilken grundmall och stilmall sidan
    #: får, vilka blocktyper som går att lägga på den, adressen och vem som
    #: ser den. ADX-sidor är publika på /<slug>/. ADX Flamingo-sidor ligger
    #: under /flamingo/ (apps/flamingo): publicerade är de öppna och står i
    #: sitemapen och därmed i 404-förslagen (Giovanni 2026-10-04), men de
    #: syns aldrig i ADX:s menyer eller länkring.
    DESIGN_ADX = ""
    DESIGN_FLAMINGO = "flamingo"
    DESIGN_CHOICES = [(DESIGN_ADX, "ADX"), (DESIGN_FLAMINGO, "ADX Flamingo")]
    #: Flamingos startsida har den här sluggen och bor på /flamingo/.
    FLAMINGO_HOME_SLUG = "flamingo"

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    meta_title = models.CharField(max_length=255, blank=True)
    meta_description = models.TextField(blank=True)
    # ADX-designen: EN hexfärg per sida driver hela gradienten och
    # textfärgen (se apps/website/theme.py). Tom = sajtens standardfärg.
    gradient_color = models.CharField(max_length=7, blank=True)
    # Länkmotorns nyckel: sidor i samma kategori länkar automatiskt till
    # varandra i en ring (apps/website/related.py), så ingen sida blir
    # föräldralös. Tom = sidan står utanför motorn (t.ex. kontakt, om oss).
    CATEGORY_CHOICES = [
        ("bransch", "Bransch"),
        ("guide", "Guide"),
        ("case", "Case"),
    ]
    category = models.CharField(max_length=20, blank=True, default="", choices=CATEGORY_CHOICES)
    design = models.CharField(
        "Design", max_length=20, blank=True, default=DESIGN_ADX, choices=DESIGN_CHOICES
    )
    is_published = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "title"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        if self.is_flamingo:
            if self.slug == self.FLAMINGO_HOME_SLUG:
                return "/flamingo/"
            return f"/flamingo/{self.slug}/"
        # Startsidan svarar på "/".
        homepage_id = _homepage_id()
        if homepage_id is not _NO_SETTINGS and homepage_id == self.pk:
            return "/"
        return f"/{self.slug}/"

    @property
    def is_flamingo(self):
        return self.design == self.DESIGN_FLAMINGO


class BlockType(models.TextChoices):
    # ADX-designens komponentbibliotek (strict-design-guide.html). En typ =
    # en mall i templates/website/blocks/ + en post i block_schema.py -
    # wrappern härleder mallvägen ur typen (aldrig if/elif, antikatalogen).
    HERO = "hero", "Hero"
    CHIPS = "chips", "Nyckeltal (chips)"
    MARQUEE = "marquee", "Rullande band"
    SVC_LIST = "svc_list", "Tjänstelista"
    CASE = "case", "Case"
    STEPS = "steps", "Steg"
    QUOTES = "quotes", "Citat"
    WHY = "why", "Varför vi"
    BAR = "bar", "CTA-rad"
    SPLIT = "split", "Bild + text"
    FOLIO = "folio", "Portfolio-kort"
    PLANS = "plans", "Paket"
    COMPARE = "compare", "Jämförelsetabell"
    TEAM = "team", "Team"
    FAQ = "faq", "FAQ"
    PROSE = "prose", "SEO-text"
    RELATED = "related", "Relaterade länkar"
    CONTACT_CARDS = "contact_cards", "Kontaktkort"
    INQUIRY_FORM = "inquiry_form", "Förfrågningsformulär"
    NEWSLETTER = "newsletter", "Nyhetsbrev"
    SPACER = "spacer", "Mellanrum"
    # ADX Flamingos egna sektioner (adx-marketing/nara-mockup). Finns bara i
    # Flamingo-designen: mallen ligger i templates/website/blocks/flamingo/
    # och block_schema.py deklarerar dem FLAMINGO_ONLY.
    FL_HERO = "fl_hero", "Flamingo hero"
    FL_LOGOS = "fl_logos", "Flamingo integrationsrad"
    FL_LAYERS = "fl_layers", "Flamingo lager"
    FL_BAND = "fl_band", "Flamingo bandet"
    FL_ARCHETYPES = "fl_archetypes", "Flamingo sidtyper"


class Block(models.Model):
    """A content block on a page."""

    page = models.ForeignKey(BlockPage, on_delete=models.CASCADE, related_name="blocks")
    block_type = models.CharField(max_length=20, choices=BlockType.choices)
    data = models.JSONField(default=dict)
    order = models.PositiveIntegerField(default=0)
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.get_block_type_display()} - {self.page.title} (#{self.order})"


class Menu(models.Model):
    """Navigation menu. Header has one; the footer has several (one per column)."""

    LOCATION_CHOICES = [
        ("header", "Header"),
        ("footer", "Footer"),
    ]

    name = models.CharField(
        max_length=100,
        help_text="Internal name (admin only, not shown on the site).",
    )
    location = models.CharField(max_length=10, choices=LOCATION_CHOICES)
    heading = models.CharField(
        max_length=100,
        blank=True,
        help_text="Public column heading (footer). Leave blank for no heading.",
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Ordering among menus in the same location (footer columns).",
    )

    class Meta:
        ordering = ["location", "order", "id"]

    def __str__(self):
        return f"{self.name} ({self.get_location_display()})"


class MenuItem(models.Model):
    """A link (or plain label) in a navigation menu."""

    menu = models.ForeignKey(Menu, on_delete=models.CASCADE, related_name="items")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    label = models.CharField(max_length=255)
    page = models.ForeignKey(BlockPage, null=True, blank=True, on_delete=models.SET_NULL)
    url = models.CharField(max_length=500, blank=True)
    open_in_new_tab = models.BooleanField(default=False)
    is_button = models.BooleanField(
        default=False,
        help_text="Render as a call-to-action button (header menu only).",
    )
    order = models.PositiveIntegerField(default=0)
    is_visible = models.BooleanField(default=True)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.label

    def get_url(self):
        if self.page:
            return self.page.get_absolute_url()
        return self.url

    @property
    def is_alive(self):
        """False när posten pekar på en avpublicerad sida - menyerna döljer
        den vid rendering (länkregeln: skicka aldrig ut en död länk)."""
        if self.page_id:
            return bool(self.page and self.page.is_published and not self.page.is_flamingo)
        return True

    @property
    def is_link(self):
        """True when this item points somewhere; False for plain-text labels."""
        return bool(self.page_id or self.url)


class SiteSettings(models.Model):
    """Singleton model for site-wide design tokens and contact info."""

    # Contact info
    name = models.CharField(max_length=255, blank=True)
    # Sajtens standardfärg när en sida saknar egen (ADX-designen).
    default_gradient_color = models.CharField(max_length=7, blank=True, default="#f7fcff")
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    street_address = models.CharField(max_length=255, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=100, blank=True)
    org_number = models.CharField(max_length=20, blank=True)

    # Design tokens
    palette = models.JSONField(
        default=list,
        blank=True,
        help_text="List of 4 OKLCH color strings",
    )
    font_pairing = models.CharField(max_length=100, default="inter-inter")
    type_scale = models.CharField(
        max_length=10,
        choices=[
            ("compact", "Compact"),
            ("default", "Default"),
            ("spacious", "Spacious"),
        ],
        default="default",
    )
    border_radius = models.IntegerField(default=8, help_text="px")
    space_unit = models.IntegerField(default=8, help_text="px")

    # Navbar colors
    navbar_bg_color = models.CharField(max_length=100, blank=True)
    navbar_text_color = models.CharField(max_length=100, blank=True)

    # Footer colors
    footer_bg_color = models.CharField(max_length=100, blank=True)
    footer_text_color = models.CharField(max_length=100, blank=True)

    # Page background
    page_bg_color = models.CharField(max_length=100, blank=True)

    # Footer intro (rich text, edited with Tiptap in /manage/settings/).
    # May contain {{ variable }} tokens and basic formatting.
    footer_about = models.TextField(blank=True)

    # Branding
    logo = models.ForeignKey(
        MediaFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    favicon = models.ForeignKey(
        MediaFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # When True (and a logo is set) the logo image is shown; otherwise the
    # site name is rendered as text. Independent per location.
    show_logo_in_header = models.BooleanField(default=True)
    show_logo_in_footer = models.BooleanField(default=True)

    # Homepage
    homepage = models.ForeignKey(
        BlockPage, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    # Footer component page - renders its blocks just above the footer.
    footer_component_page = models.ForeignKey(
        BlockPage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # Google Analytics
    ga_enabled = models.BooleanField(default=False)
    ga_tracking_id = models.CharField(max_length=50, blank=True)

    # Custom CSS
    custom_css = models.TextField(blank=True)

    # Tone of voice handed to the AI assistant on every session. Kept as a
    # setting rather than a constant so the customer can adjust the writing
    # style without a deploy.
    ai_style_guide = models.TextField(
        "Skrivguide för AI",
        blank=True,
        help_text=(
            "Tonläge och skrivregler som AI-assistenten följer. "
            "T.ex. tilltal, längd, ord att undvika."
        ),
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

    def __str__(self):
        return self.name or "Site Settings"

    def save(self, *args, **kwargs):
        # Enforce singleton: always use pk=1
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def cached(cls):
        """load() en gång per förfrågan (apps/common/request_memo.py). Samma
        objekt delas av kontextprocessorn, vars-filtren och länkarna, så det
        är bara till för läsning: den som ska ändra inställningarna tar
        load()."""
        return memo("website:site_settings", cls.load)
