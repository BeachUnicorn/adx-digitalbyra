"""
Verktygets mallfilter: belopp och tal på svenska.

    {% load flamingo_app %}
    {{ lead.value_kr|kr }}          186 000 kr
    {{ numbers.leads|tal }}         1 204

Mellanrummet är ett hårt blanksteg, så "186 000 kr" aldrig bryts mitt i.
Ett tomt värde (None, "") blir ett tomt fält, aldrig "0 kr": en okänd
summa är inte noll.
"""

from django import template

register = template.Library()

_NBSP = " "


def _group(value):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    sign = "-" if number < 0 else ""
    return sign + f"{abs(number):,}".replace(",", _NBSP)


@register.filter
def tal(value):
    """Heltal med blanksteg som tusentalsavgränsare."""
    if value is None or value == "":
        return ""
    return _group(value)


@register.filter
def kr(value):
    """Hela kronor: '186 000 kr'. Tomt värde ger en tom sträng."""
    if value is None or value == "":
        return ""
    grouped = _group(value)
    return f"{grouped}{_NBSP}kr" if grouped else ""
