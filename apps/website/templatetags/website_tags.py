"""
Template tags for the website app.

Provides block rendering, media URL resolution, and color resolution.
"""

from django import template

from apps.website.css_generator import resolve_color as _resolve_color
from apps.website.models import MediaFile, SiteSettings

register = template.Library()


@register.inclusion_tag("website/blocks/_wrapper.html", takes_context=True)
def render_block(context, block):
    """
    Render a content block using the appropriate template.

    Unpacks block.data into the template context alongside the block itself.
    """
    request = context.get("request")
    user = context.get("user") or getattr(request, "user", None)
    block_context = {
        "block": block,
        "block_type": block.block_type,
        "request": request,
        "user": user,
        # Variable substitution (render_with_context) reads site_settings
        # from the template context. Inclusion tags build their own context,
        # so we MUST forward it explicitly or every {{ phone }} renders empty.
        "site_settings": context.get("site_settings"),
    }
    # Unpack JSON data fields into context
    if block.data and isinstance(block.data, dict):
        block_context.update(block.data)
    return block_context


@register.inclusion_tag("website/partials/edit_pencil.html", takes_context=True)
def edit_pencil(context, url, label="Redigera"):
    """
    Render an edit pencil that links into /manage/ - only for logged-in users.

    Opens the target in a new tab (per the manage convention). Place inside a
    position:relative container; the pencil positions itself top-right.
    """
    user = context.get("user")
    if user is None:
        request = context.get("request")
        user = getattr(request, "user", None)
    return {
        "show": bool(user and user.is_authenticated and url),
        "url": url,
        "label": label,
    }


@register.simple_tag
def media_url(media_id):
    """
    Resolve a MediaFile ID to its URL.

    Usage: {% media_url media_id as img_url %}
    """
    if not media_id:
        return ""
    try:
        media = MediaFile.objects.get(pk=media_id)
        return media.file.url
    except (MediaFile.DoesNotExist, ValueError, TypeError):
        return ""


@register.simple_tag
def media(media_id):
    """
    Resolve a MediaFile ID to the full MediaFile object.

    Blocks store only `image_id`; this gives templates access to the file URL
    AND its metadata (alt_text, dimensions, filename) from the single source of
    truth. Use this so the UI never renders a bare ID.

    Usage:
        {% media block_image_id as img %}
        {% if img %}<img src="{{ img.file.url }}" alt="{{ img.alt_text }}"
             width="{{ img.width }}" height="{{ img.height }}">{% endif %}

    Returns None if the ID is empty or the file doesn't exist.
    """
    if not media_id:
        return None
    try:
        return MediaFile.objects.get(pk=media_id)
    except (MediaFile.DoesNotExist, ValueError, TypeError):
        return None


class ResolveColorNode(template.Node):
    """Template node for resolving color references."""

    def __init__(self, color_ref, var_name):
        self.color_ref = color_ref
        self.var_name = var_name

    def render(self, context):
        color_ref = self.color_ref.resolve(context)
        settings = context.get("site_settings")
        if not settings:
            settings = SiteSettings.load()
        context[self.var_name] = _resolve_color(color_ref, settings)
        return ""


@register.tag("resolve_color")
def do_resolve_color(parser, token):
    """
    Resolve a color reference to a CSS value using the site palette.

    Usage: {% resolve_color color_ref as var_name %}

    Supports:
      - "palette-1" through "palette-4"
      - "white", "black", "transparent"
      - Raw oklch() values
    """
    bits = token.split_contents()
    if len(bits) != 4 or bits[2] != "as":
        raise template.TemplateSyntaxError(
            f"'{bits[0]}' tag requires format: {{% resolve_color color_ref as var_name %}}"
        )
    color_ref = parser.compile_filter(bits[1])
    var_name = bits[3]
    return ResolveColorNode(color_ref, var_name)


@register.filter
def known_block_type(block_type):
    """True om typen finns i blockschemat - wrapperns registervakt."""
    from apps.manage.block_schema import BLOCK_EDIT_SCHEMA

    return block_type in BLOCK_EDIT_SCHEMA


@register.filter
def split_lines(value):
    """Textfält med en punkt per rad -> lista (paketens features)."""
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


@register.simple_tag
def faq_section(section_id):
    """Slå upp en aktiv FAQ-sektion till FAQ-blocket (eller None)."""
    from apps.faq.models import FAQSection

    if not section_id:
        return None
    try:
        return FAQSection.objects.get(pk=section_id, is_active=True)
    except (FAQSection.DoesNotExist, ValueError, TypeError):
        return None


@register.simple_tag
def botcheck_attr():
    """data-botcheck-attributet på <form> - site.js kopierar värdet till
    bc_proof vid första verkliga interaktionen (botskyddets JS-bevis)."""
    from django.utils.html import format_html

    from apps.common.botcheck import issue_token

    return format_html('data-botcheck="{}"', issue_token())


@register.inclusion_tag("website/partials/botcheck_fields.html")
def botcheck_fields():
    """Honeypot + signerad tidsstämpel + tomt JS-bevisfält."""
    from apps.common.botcheck import issue_token

    return {"token": issue_token()}


@register.inclusion_tag("inquiries/_form.html")
def inquiry_form():
    """Instansierar förfrågningsformuläret åt inquiry_form-blocket -
    blockdata bär bara sektionshuvudet, formuläret byggs här."""
    from apps.inquiries.forms import InquiryForm

    return {"form": InquiryForm()}


@register.simple_tag
def active_services():
    """Aktiva tjänster i ordning - för svc_list-blocket (render_block ger
    blocken en egen kontext, så processorernas nav_services når inte hit)."""
    from apps.services.models import Service

    return Service.objects.filter(is_active=True).order_by("order", "name")
