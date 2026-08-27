"""Template tags for pricing schema.org structured data."""

import json

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def pricing_schema_json(services, audience, site_settings):
    """
    Output JSON-LD for Service schema with Offer/AggregateOffer per service.

    Services without a price get no Offer block (Google dislikes price=0).
    Only outputs for services that have pricing data. Wraps everything in an
    ItemList for the pricing page.
    """
    if not services:
        return ""

    provider = _build_provider(site_settings)
    vat_rate = getattr(site_settings, "vat_rate", 0)
    include_vat = getattr(audience, "prices_include_vat", True)
    items = []

    for i, service in enumerate(services, 1):
        item = {
            "@type": "Service",
            "name": service.name,
            "url": service.get_absolute_url(),
            "provider": provider,
        }
        if service.description:
            item["description"] = service.description
        if service.category:
            item["category"] = service.category.name

        # Offer - prices shown match the audience's VAT context.
        low = service.total_from(vat_rate, include_vat)
        high = service.total_to(vat_rate, include_vat)
        if low and high and high != low:
            item["offers"] = {
                "@type": "AggregateOffer",
                "priceCurrency": "SEK",
                "lowPrice": low,
                "highPrice": high,
            }
        elif low:
            item["offers"] = {
                "@type": "Offer",
                "priceCurrency": "SEK",
                "price": low,
            }

        items.append(
            {
                "@type": "ListItem",
                "position": i,
                "item": item,
            }
        )

    if not items:
        return ""

    data = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"Prislista - {audience.name}",
        "numberOfItems": len(items),
        "itemListElement": items,
    }

    return mark_safe(
        '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>"
    )


def _build_provider(site_settings):
    """Build a schema.org LocalBusiness from SiteSettings."""
    provider = {
        "@type": "LocalBusiness",
        "name": site_settings.name or "ADX",
    }
    if site_settings.phone:
        provider["telephone"] = site_settings.phone
    if site_settings.email:
        provider["email"] = site_settings.email
    if site_settings.street_address:
        provider["address"] = {
            "@type": "PostalAddress",
            "streetAddress": site_settings.street_address,
            "postalCode": site_settings.postal_code or "",
            "addressLocality": site_settings.city or "",
            "addressCountry": "SE",
        }
    return provider
