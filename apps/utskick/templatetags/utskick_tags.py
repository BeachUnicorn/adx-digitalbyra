"""
Mallhjälp för Kontakter och Utskick.

    {% load utskick_tags %}
    {{ andel|procent }}                 "27 %", "4,6 %"
    {{ "sms"|kanal }}                   "Sms" (email: "E-post")
    {{ kontakt.phone|maskerat_nummer }} "070-*** ** 67"
    {{ kontakt.phone|nummer }}          "070-123 45 67" (kontaktkortet och listan)
    {{ kontakt.email|maskerad_epost }}  "a***@e***.example"
    {% samtycke_chip kontakt "sms" as chip %}   {"label", "tone", "channel"}
    {% utskick_card customer as ut %}   kundkortets panel (manage_views.card_context)
    {{ field|beskriven:help }}          fältet med aria-describedby på hjälptexten och felet

Tal och kronor skrivs med flamingo_app ({{ n|tal }}, {{ n|kr }}). Mellanrummet
före % är ett hårt blanksteg, så att "27 %" aldrig bryts.
"""

from django import template

from .. import consent as consents
from .. import normalize
from ..models import CHANNEL_EMAIL, CHANNEL_SMS

register = template.Library()

_NBSP = " "


@register.filter
def procent(value):
    """Andel i procent: heltal från 10 och uppåt, en decimal under 10
    ("4,6 %"), inga onödiga decimaler ("3 %"). Tomt värde ger ""."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if abs(number) >= 10 or number == int(number):
        text = str(int(round(number)))
    else:
        text = f"{number:.1f}".replace(".", ",")
        if text.endswith(",0"):
            text = text[:-2]
    return f"{text}{_NBSP}%"


@register.filter
def beskriven(field, help_text=""):
    """Ett formulärfält (BoundField) med aria-describedby på hjälptexten
    (id <auto_id>_helptext) och felet (<auto_id>_error), som
    kontakter/_field.html skriver ut. aria-invalid sätter Django själv."""
    ids = []
    if field.auto_id and not field.is_hidden:
        if help_text:
            ids.append(f"{field.auto_id}_helptext")
        if field.errors:
            ids.append(f"{field.auto_id}_error")
    if not ids:
        return field.as_widget()
    return field.as_widget(attrs={"aria-describedby": " ".join(ids)})


@register.filter
def kanal(value):
    return {CHANNEL_SMS: "Sms", CHANNEL_EMAIL: "E-post"}.get(value, value)


@register.filter
def maskerat_nummer(value):
    return normalize.mask_phone(value)


@register.filter
def nummer(value):
    """Ett E.164-nummer som det skrivs: "070-123 45 67" för svenska nummer,
    "+45 81 23 45 67" för andra. Tomt eller otolkbart ger värdet som det är."""
    return normalize.display_phone(value)


@register.filter
def maskerad_epost(value):
    return normalize.mask_email(value)


@register.simple_tag
def samtycke_chip(kontakt, channel):
    """Kanalens etikett (consent.chip). Läser kontaktens samtycken ur
    prefetch_related("consents") när listan hämtat dem, annars med en fråga."""
    found = None
    for row in kontakt.consents.all():
        if row.channel == channel:
            found = row
            break
    return consents.chip(kontakt, channel, found)


@register.simple_tag
def utskick_card(customer):
    """Allt kundkortets utskickspanel behöver (manage/utskick/_customer_card.html)."""
    from ..manage_views import card_context

    return card_context(customer)
