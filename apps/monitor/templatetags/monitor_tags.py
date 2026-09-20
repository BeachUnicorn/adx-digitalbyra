from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def sparkline(values, width=240, height=44):
    """En liten kurva som inline-SVG. Tomma serier ger en tom ruta, aldrig ett fel."""
    values = [v for v in (values or []) if isinstance(v, (int, float))]
    if len(values) < 2:
        return format_html(
            '<svg class="pt-spark" viewBox="0 0 {} {}" aria-hidden="true"></svg>', width, height
        )
    top, bottom = max(values), min(values)
    span = (top - bottom) or 1
    step = width / (len(values) - 1)
    points = " ".join(
        f"{i * step:.1f},{height - 4 - (v - bottom) / span * (height - 8):.1f}"
        for i, v in enumerate(values)
    )
    svg = (
        f'<svg class="pt-spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'aria-hidden="true"><polyline points="{points}" fill="none" stroke="currentColor" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )
    return mark_safe(svg)  # noqa: S308 - bara siffror i strängen


@register.filter
def minutes_text(minutes):
    minutes = int(minutes or 0)
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"


@register.filter
def uptime_text(seconds):
    seconds = int(seconds or 0)
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    return f"{days} d {hours} h" if days else f"{hours} h"


@register.filter
def get(mapping, key):
    try:
        return (mapping or {}).get(key)
    except AttributeError:
        return None
