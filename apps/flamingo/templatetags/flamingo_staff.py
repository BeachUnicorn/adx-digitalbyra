"""
Byråns siffror för ADX Flamingo i panelen (/manage/flamingo/).

    {% load flamingo_staff %}
    {% flamingo_queue_counts as counts %}
    {{ counts.to_review }} {{ counts.to_publish }} {{ counts.conversions }}
    {{ campaign|flamingo_staff_state }}

Bara för panelens mallar: siffrorna gäller alla kunder.
"""

from django import template

from ..manage_review import queue_counts, staff_state

register = template.Library()


@register.simple_tag
def flamingo_queue_counts():
    """{to_review, to_publish, conversions}: att granska, godkända som inte
    är publicerade, och konverteringar i kö."""
    return queue_counts()


@register.filter
def flamingo_staff_state(campaign):
    """Kampanjens läge sett från byrån ("Att granska", "Hos kunden" ...).
    Kampanjens egna statustexter är skrivna för kunden."""
    return staff_state(campaign, campaign.pending_review())[0]
