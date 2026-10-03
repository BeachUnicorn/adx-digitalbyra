"""
Google Ads API i panelens mallar (kundkortet och /manage/flamingo/).

    {% load flamingo_google %}
    {% flamingo_google_api as gapi %}
    {{ gapi.configured }} {{ gapi.missing|length }} {{ gapi.connection.google_email }}
    {% flamingo_invite_emails customer as invite_emails %}
    {{ account.google_billing_status|flamingo_billing }}

Bara för byråns mallar. Ingen nyckel finns i det som returneras.
"""

from django import template

from ..google_accounts import billing_label
from ..manage_google import api_state, invite_choices

register = template.Library()


@register.simple_tag
def flamingo_google_api():
    """{configured, missing, connection, env_token}: se manage_google.api_state."""
    return api_state()


@register.simple_tag
def flamingo_invite_emails(customer):
    """Kundens adresser en inbjudan från Google kan gå till: [(adress, etikett)]."""
    return invite_choices(customer)


@register.filter
def flamingo_billing(status):
    """Betalningens läge hos Google i klartext ("Klar", "Saknas" ...)."""
    return billing_label(status)
