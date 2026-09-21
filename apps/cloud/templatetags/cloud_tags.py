from django import template

from apps.cloud.portal_views import visible_invoices

register = template.Library()


@register.simple_tag
def has_aws_invoices(customer):
    """Portalens meny visar Fakturor först när det finns något att hämta."""
    return bool(customer) and visible_invoices(customer).exists()
