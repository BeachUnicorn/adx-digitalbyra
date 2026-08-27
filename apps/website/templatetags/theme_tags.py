"""Body-attribut för ADX-temat: färg + rätt textklass, beräknat server-side."""

from django import template
from django.utils.html import format_html

from apps.website.theme import resolve_page_color, text_is_dark

register = template.Library()


@register.simple_tag(takes_context=True)
def body_theme(context, page_color=None):
    """class + data-gradient för <body>. Sidans färg vinner över sajtens."""
    site = context.get("site_settings")
    color = resolve_page_color(page_color, getattr(site, "default_gradient_color", None))
    css_class = "text-dark" if text_is_dark(color) else "text-light"
    return format_html('class="{}" data-gradient="{}"', css_class, color)
