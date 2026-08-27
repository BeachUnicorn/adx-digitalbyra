"""Delade UI-komponenter för /manage/ - porterade från Atlas Holly (adx)."""

import json
import uuid

from django import template

register = template.Library()


@register.inclusion_tag("manage/includes/link_field.html")
def link_field(name, value=None, input_id=None):
    """Set link-fältet: dold JSON-input + läsbar pill + knappar, mot den
    delade modalen. `value` är en beskrivare eller legacy-sträng - strängar
    uppgraderas för visningen så gamla rader får en vettig pill direkt."""
    from apps.website.links import parse_href, resolve_link

    link = value
    if isinstance(value, str) and value.strip():
        link = parse_href(value)
    label = ""
    if isinstance(link, dict) and link.get("kind"):
        resolved = resolve_link(link)
        label = resolved.label or resolved.href
    return {
        "name": name,
        "value": json.dumps(link) if isinstance(link, dict) and link else "",
        "label": label,
        "input_id": input_id or f"lnkf_{uuid.uuid4().hex[:8]}",
    }
