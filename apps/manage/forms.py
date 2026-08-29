"""Forms for the customer control panel (/manage/)."""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.areas.models import PARENT_LEVEL, Area, AreaLevel
from apps.common.security import (
    sanitize_plain_text,
    sanitize_rich_html,
    sanitize_rich_html_basic,
    validate_url,
)
from apps.services.models import Audience, Service, ServiceCategory
from apps.website.models import BlockPage, Menu, MenuItem, SiteSettings


class BlockPageForm(forms.ModelForm):
    """Create / edit a block page's metadata (not its blocks)."""

    class Meta:
        model = BlockPage
        fields = [
            "title",
            "slug",
            "meta_title",
            "meta_description",
            "is_published",
            "order",
        ]
        widgets = {
            "title": forms.TextInput(),
            "slug": forms.TextInput(attrs={"placeholder": _("genereras från titeln")}),
            "meta_title": forms.TextInput(),
            "meta_description": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "title": _("Titel"),
            "slug": _("Webbadress (slug)"),
            "meta_title": _("Metatitel (SEO)"),
            "meta_description": _("Metabeskrivning (SEO)"),
            "is_published": _("Publicerad"),
            "order": _("Ordning"),
        }
        help_texts = {
            "slug": _("Lämna tomt så skapas den automatiskt från titeln."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Slug is auto-generated from the title when left blank.
        self.fields["slug"].required = False

    def clean_title(self):
        return sanitize_plain_text(self.cleaned_data.get("title", ""), max_length=255)

    def clean_meta_title(self):
        return sanitize_plain_text(self.cleaned_data.get("meta_title", ""), max_length=255)

    def clean_meta_description(self):
        # Metafält är alltid ren text - de hamnar i <meta content="...">, där
        # taggar bara blir bokstavliga tecken i sökresultatet.
        return sanitize_plain_text(self.cleaned_data.get("meta_description", ""), max_length=300)

    def clean_slug(self):
        slug = (self.cleaned_data.get("slug") or "").strip()
        title = self.cleaned_data.get("title") or ""
        base = slugify(slug) if slug else slugify(title)
        if not base:
            raise forms.ValidationError(_("Ange en titel eller en webbadress."))

        # Guarantee uniqueness against other pages.
        candidate = base
        qs = BlockPage.objects.exclude(pk=self.instance.pk)
        i = 2
        while qs.filter(slug=candidate).exists():
            candidate = f"{base}-{i}"
            i += 1
        return candidate


class SiteSettingsForm(forms.ModelForm):
    """
    Edit the site's contact details + footer intro.

    Contact fields are the single source of truth for the {{ variable }}
    tokens used across blocks, menus and the footer. `footer_about` is a
    rich-text field edited with Tiptap (data-tiptap) and sanitized on save.
    """

    class Meta:
        model = SiteSettings
        fields = [
            "name",
            "phone",
            "email",
            "street_address",
            "postal_code",
            "city",
            "org_number",
            "logo",
            "favicon",
            "show_logo_in_header",
            "show_logo_in_footer",
            "footer_about",
            "ga_enabled",
            "ga_tracking_id",
            "footer_component_page",
        ]
        widgets = {
            "name": forms.TextInput(),
            "phone": forms.TextInput(),
            "email": forms.EmailInput(),
            "street_address": forms.TextInput(),
            "postal_code": forms.TextInput(),
            "city": forms.TextInput(),
            "org_number": forms.TextInput(),
            # Picked from the media library via the image-picker JS, which
            # drives these hidden inputs (MediaFile id) + a live thumbnail.
            "logo": forms.HiddenInput(),
            "favicon": forms.HiddenInput(),
            "ga_tracking_id": forms.TextInput(attrs={"placeholder": "G-XXXXXXXXXX"}),
            # data-tiptap triggers the rich-text editor; it stays a plain
            # textarea (and degrades gracefully) if the JS bundle is absent.
            "footer_about": forms.Textarea(
                attrs={
                    "data-tiptap": "1",
                    "rows": 4,
                    "placeholder": _("Kort presentation i sidfoten..."),
                }
            ),
        }
        labels = {
            "name": _("Företagsnamn"),
            "phone": _("Telefon"),
            "email": _("E-post"),
            "street_address": _("Gatuadress"),
            "postal_code": _("Postnummer"),
            "city": _("Ort"),
            "org_number": _("Org.nummer"),
            "logo": _("Logotyp"),
            "favicon": _("Favicon"),
            "show_logo_in_header": _("Visa logotyp i sidhuvud"),
            "show_logo_in_footer": _("Visa logotyp i sidfot"),
            "footer_about": _("Sidfotstext"),
            "ga_enabled": _("Aktivera Google Analytics"),
            "ga_tracking_id": _("GA Tracking ID"),
            "footer_component_page": _("Sidfots-blocksida"),
        }

    def clean_footer_about(self):
        # Server-side sanitization is the security boundary - never trust the
        # HTML the editor submits.
        return sanitize_rich_html(self.cleaned_data.get("footer_about", ""))


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

# Reused widget attrs for the basic (bold/italic/link + variables) editor.
_BASIC_TIPTAP = {"data-tiptap": "basic", "rows": 5}


class ServiceCategoryForm(forms.ModelForm):
    class Meta:
        model = ServiceCategory
        fields = ["name", "description", "body", "image", "is_active", "order"]
        widgets = {
            "body": forms.Textarea(attrs=_BASIC_TIPTAP),
            "image": forms.HiddenInput(),
        }

    def clean_body(self):
        return sanitize_rich_html_basic(self.cleaned_data.get("body", ""))


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = [
            "category",
            "name",
            "description",
            "body",
            "image",
            "audiences",
            "faq_section",
            "is_active",
            "is_featured",
            "order",
        ]
        widgets = {
            "body": forms.Textarea(attrs=_BASIC_TIPTAP),
            "image": forms.HiddenInput(),
            "audiences": forms.CheckboxSelectMultiple(),
        }

    def __init__(self, *args, **kwargs):
        """
        Förbocka alla aktiva målgrupper på en NY tjänst.

        En tjänst gäller i praktiken alltid alla målgrupper; att lämna
        rutorna tomma gör tjänsten osynlig på målgruppssidorna utan att
        något syns fel. Att ta bort en målgrupp ska vara ett medvetet val,
        inte något man råkar göra genom att inte fylla i.

        Bara på ett OBUNDET formulär för ett nytt objekt: vid spara ska en
        medvetet tömd markering respekteras, inte skrivas tillbaka. Samma
        förval gör AI:n i services_ops._apply_skapa.
        """
        super().__init__(*args, **kwargs)
        if self.instance.pk is None and not self.is_bound:
            self.initial.setdefault("audiences", list(Audience.objects.filter(is_active=True)))

    def clean_body(self):
        return sanitize_rich_html_basic(self.cleaned_data.get("body", ""))


class AudienceForm(forms.ModelForm):
    class Meta:
        model = Audience
        fields = [
            "name",
            "intro",
            "image",
            "order",
            "is_active",
            "meta_title",
            "meta_description",
        ]
        widgets = {
            "intro": forms.Textarea(attrs=_BASIC_TIPTAP),
            "image": forms.HiddenInput(),
        }

    def clean_intro(self):
        return sanitize_rich_html_basic(self.cleaned_data.get("intro", ""))


# ---------------------------------------------------------------------------
# Menus & navigation
# ---------------------------------------------------------------------------


class FooterColumnForm(forms.ModelForm):
    """Create / edit a single footer column (a Menu with location='footer')."""

    class Meta:
        model = Menu
        fields = ["name", "heading", "order"]
        widgets = {
            "name": forms.TextInput(),
            "heading": forms.TextInput(),
        }
        labels = {
            "name": _("Internt namn"),
            "heading": _("Rubrik (visas i sidfoten)"),
            "order": _("Ordning"),
        }
        help_texts = {
            "name": _("Endast för administration - visas inte på sajten."),
            "heading": _("Lämna tomt för en kolumn utan rubrik."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Order is auto-assigned (appended) when left blank on creation.
        self.fields["order"].required = False

    def clean_name(self):
        name = sanitize_plain_text(self.cleaned_data.get("name", ""), max_length=100)
        if not name:
            raise ValidationError(_("Ange ett internt namn."))
        return name

    def clean_heading(self):
        return sanitize_plain_text(self.cleaned_data.get("heading", ""), max_length=100)


class MenuItemForm(forms.ModelForm):
    """
    Add / edit a menu item (link or plain-text label) in any menu.

    An item points either at an internal page (`page`) or at a free URL
    (`url`). A page reference wins; when one is chosen the URL is cleared.
    Labels may contain {{ variable }} tokens (resolved at render time) so we
    keep plain-text sanitisation, which leaves those tokens intact.
    """

    class Meta:
        model = MenuItem
        fields = ["label", "page", "url", "open_in_new_tab", "is_button", "is_visible"]
        widgets = {
            "label": forms.TextInput(),
            "url": forms.TextInput(attrs={"placeholder": "/tjanster/, #faq, tel:..., https://..."}),
        }
        labels = {
            "label": _("Text"),
            "page": _("Länka till sida"),
            "url": _("Eller egen länk"),
            "open_in_new_tab": _("Öppna i ny flik"),
            "is_button": _("Visa som knapp (endast huvudmeny)"),
            "is_visible": _("Synlig"),
        }
        help_texts = {
            "page": _("Välj en sida, eller lämna tom och ange en egen länk nedan."),
            "url": _(
                "Relativ (/sida/), ankare (#id), tel:/mailto: eller fullständig URL. "
                "Lämna tom för en ren textetikett."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["page"].required = False
        self.fields["page"].queryset = BlockPage.objects.order_by("title")
        self.fields["url"].required = False

    def clean_label(self):
        label = sanitize_plain_text(self.cleaned_data.get("label", ""), max_length=255)
        if not label:
            raise ValidationError(_("Ange en text för menypunkten."))
        return label

    def clean_url(self):
        try:
            return validate_url(self.cleaned_data.get("url", ""))
        except ValidationError:
            raise ValidationError(
                _("Ogiltig länk. Använd /sida/, #id, tel:, mailto: eller https://...")
            ) from None

    def clean(self):
        cleaned = super().clean()
        # A page reference is authoritative; clear any free URL when set.
        if cleaned.get("page"):
            cleaned["url"] = ""
        return cleaned


# ---------------------------------------------------------------------------
# Serviceområden
# ---------------------------------------------------------------------------


class AreaForm(forms.ModelForm):
    """
    Create / edit one area (län, kommun or stadsdel).

    Children, FAQ rows and the service matrix are not ModelForm fields - they
    are rebuilt from the POST in area_views, because they are repeating rows
    rather than single values.
    """

    class Meta:
        model = Area
        fields = [
            "name",
            "slug",
            "level",
            "parent",
            "heading",
            "intro",
            "body",
            "image",
            "faq_section",
            "neighbours",
            "latitude",
            "longitude",
            "map_zoom",
            "is_active",
            "order",
            "meta_title",
            "meta_description",
        ]
        widgets = {
            "name": forms.TextInput(),
            "slug": forms.TextInput(attrs={"placeholder": _("genereras från namnet")}),
            "heading": forms.TextInput(attrs={"placeholder": _("Digitalbyrå i ...")}),
            "intro": forms.Textarea(attrs={"rows": 2}),
            "body": forms.Textarea(attrs=_BASIC_TIPTAP),
            "image": forms.HiddenInput(),
            "neighbours": forms.CheckboxSelectMultiple(),
            "meta_title": forms.TextInput(),
            "meta_description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slug"].required = False
        self.fields["parent"].required = False
        self.fields["parent"].queryset = Area.objects.exclude(level=AreaLevel.DISTRICT).order_by(
            "level", "name"
        )
        neighbours = Area.objects.exclude(level=AreaLevel.REGION).order_by("name")
        if self.instance.pk:
            self.fields["parent"].queryset = self.fields["parent"].queryset.exclude(
                pk=self.instance.pk
            )
            neighbours = neighbours.exclude(pk=self.instance.pk)
        self.fields["neighbours"].queryset = neighbours
        self.fields["neighbours"].required = False

    def clean_slug(self):
        slug = (self.cleaned_data.get("slug") or "").strip()
        return slugify(slug) if slug else ""

    def clean_intro(self):
        return sanitize_plain_text(self.cleaned_data.get("intro", ""), max_length=300)

    def clean_heading(self):
        return sanitize_plain_text(self.cleaned_data.get("heading", ""), max_length=200)

    def clean_body(self):
        return sanitize_rich_html_basic(self.cleaned_data.get("body", ""))

    def clean(self):
        cleaned = super().clean()
        level = cleaned.get("level")
        parent = cleaned.get("parent")
        expected = PARENT_LEVEL.get(level)
        if expected is None and parent is not None:
            self.add_error("parent", _("Ett län kan inte ha ett överordnat område."))
        elif expected is not None:
            if parent is None:
                self.add_error(
                    "parent",
                    _("Välj ett överordnat %(level)s.")
                    % {"level": AreaLevel(expected).label.lower()},
                )
            elif parent.level != expected:
                self.add_error(
                    "parent",
                    _("Överordnat område måste vara ett %(level)s.")
                    % {"level": AreaLevel(expected).label.lower()},
                )
        return cleaned
