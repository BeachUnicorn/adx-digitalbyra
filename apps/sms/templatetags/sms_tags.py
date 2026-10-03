from django import template

from apps.projects.access import is_agency_user, viewing_customer

from .. import numbers, pricing

register = template.Library()


@register.filter
def sms_kr(units, decimals=2):
    """Tiotusendels krona -> '1 234,56'."""
    return pricing.kr_text(units, int(decimals))


@register.filter
def sms_ore(units):
    """Tiotusendels krona -> öre, '52' eller '57,5'."""
    return pricing.per_unit_ore(units)


@register.filter
def sms_country(code):
    return numbers.country_name(code)


@register.simple_tag
def show_sms(request):
    """Ska portalens meny visa SMS? Kontakt hos en kund med SMS aktiverat,
    eller byrån i kundvyn på en sådan kund."""
    from ..models import SmsAccount

    user = getattr(request, "user", None)
    customer = getattr(request, "customer", None)
    if customer is None:
        if is_agency_user(user):
            customer = viewing_customer(request)
        elif user is not None and user.is_authenticated:
            customer = user.customers.filter(is_active=True).first()
    if customer is None:
        return False
    return SmsAccount.objects.filter(customer=customer, is_enabled=True).exists()


@register.simple_tag
def sms_card(customer):
    """Allt kundkortets SMS-panel behöver (manage/sms/_customer_panel.html)."""
    from ..manage_views import card_context

    return card_context(customer)
