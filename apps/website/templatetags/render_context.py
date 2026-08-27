"""
Template tags for rendering outward-facing content with site context variables.

Every customer-supplied string that appears on the public site MUST go through
one of these tags - never rendered raw. Ported from the ADX implementation and
adapted to the ADX single-site `SiteSettings` singleton (ADX is
multi-tenant and passes a `Tenant`).

Two variants:

* `render_with_context`       - plain-text fields (headlines, labels, menu
                                 labels, …). Strips ALL HTML, substitutes
                                 `{{ var }}` with raw values, returns a plain
                                 `str` so Django auto-escape encodes it.

* `render_with_context_rich`  - rich-text fields (article body, FAQ answers).
                                 Sanitizes HTML through nh3, substitutes
                                 `{{ var }}` with HTML-escaped values, returns
                                 a `SafeString`.

Only a closed allowlist of variable names is substituted; unknown names render
as empty. Substitution is a regex over `{{ name }}` - NOT Django template
rendering - so customer content can never execute template tags.
"""

import re
from datetime import datetime

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from apps.common.security import (
    sanitize_plain_text,
    sanitize_rich_html,
    sanitize_rich_html_basic,
)

register = template.Library()

# Pattern: {{ variable_name }} - only word characters inside.
VARIABLE_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def get_site_variables(settings) -> dict:
    """
    Build the variable context from SiteSettings.

    Returns a flat dict of `variable_name` → raw string value. Values are NOT
    escaped here; the renderer chooses escaping based on plain vs rich output.
    """
    if settings is None:
        return {"current_year": str(datetime.now().year)}

    variables = {
        # Site
        "site_name": settings.name or "",
        "company_name": settings.name or "",
        # Contact
        "phone": settings.phone or "",
        "email": settings.email or "",
        "street_address": settings.street_address or "",
        "postal_code": settings.postal_code or "",
        "city": settings.city or "",
        "org_number": settings.org_number or "",
        # Dynamic
        "current_year": str(datetime.now().year),
    }

    parts = [
        settings.street_address or "",
        f"{settings.postal_code or ''} {settings.city or ''}".strip(),
    ]
    variables["full_address"] = ", ".join(p for p in parts if p)

    return variables


def _substitute(text: str, variables: dict, *, html_escape: bool) -> str:
    """
    Replace `{{ var }}` patterns with values from `variables`.

    If `html_escape` is True, values are HTML-escaped before substitution
    (used when the surrounding text is HTML). Unknown variables render empty.
    """
    if not text:
        return ""

    def repl(match: re.Match) -> str:
        key = match.group(1)
        value = variables.get(key, "")
        value = "" if value is None else str(value)
        return escape(value) if html_escape else value

    return VARIABLE_PATTERN.sub(repl, text)


@register.filter(name="vars")
def vars_filter(value):
    """
    Template filter: substitute `{{ phone }}`-style tokens in plain text.

    Designed as a drop-in for `{{ obj.field }}` → `{{ obj.field|vars }}`.
    Reads SiteSettings.load() directly (no template context dependency) so it
    works in any template, including ones rendered without site context.

    Plain text only - strips ALL HTML before substitution.
    Use `vars_html` for rich HTML fields.
    """
    if not value:
        return ""

    cleaned = sanitize_plain_text(str(value), max_length=10_000)
    if not cleaned:
        return ""

    settings = _load_site_settings()
    return _substitute(cleaned, get_site_variables(settings), html_escape=False)


@register.filter(name="vars_html", is_safe=True)
def vars_html_filter(value):
    """
    Template filter for rich HTML: sanitises (rich allowlist) and substitutes.

    Replaces patterns like `{{ item.answer|safe }}` → `{{ item.answer|vars_html }}`.
    Returns a SafeString.
    """
    if not value:
        return mark_safe("")

    cleaned = sanitize_rich_html(str(value))
    if not cleaned:
        return mark_safe("")

    settings = _load_site_settings()
    out = _substitute(cleaned, get_site_variables(settings), html_escape=True)
    return mark_safe(out)


@register.filter(name="vars_url")
def vars_url_filter(value):
    """
    Template filter for href values: substitute + re-validate URL.

    Replaces `href="{{ obj.url }}"` → `href="{{ obj.url|vars_url }}"`. Falls
    back to "" if substitution produces a URL outside the allowlist.
    """
    if not value:
        return ""
    settings = _load_site_settings()
    substituted = _substitute(str(value), get_site_variables(settings), html_escape=False)

    from django.core.exceptions import ValidationError

    from apps.common.security import validate_url

    try:
        return validate_url(substituted)
    except ValidationError:
        return ""


def _load_site_settings():
    """Cheap loader so filters work without template-context plumbing."""
    try:
        from apps.website.models import SiteSettings

        return SiteSettings.load()
    except Exception:  # noqa: BLE001
        return None


@register.simple_tag(takes_context=True)
def render_with_context(context, text):
    """
    Plain-text rendering.

    * Strips ALL HTML from `text` (defense against attacker-stored markup).
    * Substitutes `{{ var }}` with raw site values.
    * Returns a regular `str` - Django auto-escape does the rest.

    Reads `site_settings` from the template context (set by the public views),
    so call sites can stay terse: `{% render_with_context block.title as t %}`.
    """
    if not text:
        return ""

    cleaned_text = sanitize_plain_text(text, max_length=10_000)
    if not cleaned_text:
        return ""

    settings = context.get("site_settings")
    variables = get_site_variables(settings)
    return _substitute(cleaned_text, variables, html_escape=False)


@register.simple_tag(takes_context=True)
def render_url_with_context(context, url):
    """
    URL rendering with variable substitution.

    Used for href targets where the customer may have written
    ``tel:{{ phone }}`` or ``mailto:{{ email }}``. The result is also
    re-validated against the URL allowlist so substituted values can't
    introduce a forbidden scheme.

    Returns a plain str (auto-escaped on output by Django).
    """
    if not url:
        return ""

    settings = context.get("site_settings")
    variables = get_site_variables(settings)
    substituted = _substitute(str(url), variables, html_escape=False)

    # Re-validate after substitution. If a variable expanded to something
    # that breaks the allowlist (extremely unlikely with our variables, but
    # still), fall back to "" rather than emit a dangerous href.
    from django.core.exceptions import ValidationError

    from apps.common.security import validate_url

    try:
        return validate_url(substituted)
    except ValidationError:
        return ""


@register.simple_tag(takes_context=True)
def render_with_context_rich(context, html):
    """
    Rich-text rendering.

    * Runs `html` through nh3 with the rich-text allowlist.
    * Substitutes `{{ var }}` with HTML-escaped site values.
    * Returns a `SafeString`.
    """
    if not html:
        return mark_safe("")

    cleaned = sanitize_rich_html(html)
    if not cleaned:
        return mark_safe("")

    settings = context.get("site_settings")
    variables = get_site_variables(settings)
    out = _substitute(cleaned, variables, html_escape=True)
    return mark_safe(out)


@register.simple_tag(takes_context=True)
def render_with_context_basic(context, html):
    """
    Basic rich-text rendering: bold, italic, links only.

    Same pipeline as render_with_context_rich but with the minimal allowlist
    (sanitize_rich_html_basic). Use for customer fields restricted to light
    formatting (Service/Category body, Audience intro, FAQ answers).
    """
    if not html:
        return mark_safe("")

    cleaned = sanitize_rich_html_basic(html)
    if not cleaned:
        return mark_safe("")

    settings = context.get("site_settings")
    variables = get_site_variables(settings)
    out = _substitute(cleaned, variables, html_escape=True)
    return mark_safe(out)


# Variable list for the (future) /manage/ editor dropdown. Mirror this in the
# Tiptap CONTEXT_VARIABLES when that lands.
AVAILABLE_VARIABLES = [
    {"key": "site_name", "label": "Företagsnamn", "example": "ADX"},
    {"key": "phone", "label": "Telefon", "example": "070-123 45 67"},
    {"key": "email", "label": "E-post", "example": "info@adx.se"},
    {"key": "street_address", "label": "Gatuadress", "example": "Exempelvägen 12"},
    {"key": "postal_code", "label": "Postnummer", "example": "123 45"},
    {"key": "city", "label": "Ort", "example": "Stockholm"},
    {
        "key": "full_address",
        "label": "Fullständig adress",
        "example": "Exempelvägen 12, 123 45 Stockholm",
    },
    {"key": "org_number", "label": "Org.nummer", "example": "559000-0000"},
    {"key": "current_year", "label": "Årtal", "example": "2026"},
]
