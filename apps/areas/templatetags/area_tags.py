"""
Area-aware variable substitution.

The site already substitutes `{{ phone }}`-style tokens from SiteSettings (see
website/templatetags/render_context.py). These filters add the geographic
tokens on top, so one piece of copy can be reused across every area:

    "Vi hjälper dig i hela {{ ort }}."  ->  "Vi hjälper dig i hela Solna."

Tokens: `{{ ort }}` (this area), `{{ kommun }}`, `{{ lan }}`.

Area tokens are replaced first, then the value is handed to the existing site
filters, which do the sanitising. Nothing here trusts the input.
"""

import json

from django import template
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe

from apps.website.templatetags.render_context import vars_filter, vars_html_filter

register = template.Library()


def _area_tokens(area):
    if area is None:
        return {}
    municipality = area.municipality
    region = area.region
    return {
        "ort": area.name,
        "kommun": municipality.name if municipality else area.name,
        "lan": region.name if region else "",
    }


def _substitute_area(value, area):
    text = str(value or "")
    if not text:
        return ""
    for token, replacement in _area_tokens(area).items():
        for spelling in (f"{{{{ {token} }}}}", f"{{{{{token}}}}}"):
            text = text.replace(spelling, replacement)
    return text


@register.filter(name="area_vars")
def area_vars(value, area):
    """Plain text: substitute area tokens, then site tokens."""
    return vars_filter(_substitute_area(value, area))


@register.filter(name="area_vars_html")
def area_vars_html(value, area):
    """Rich text: substitute area tokens, then site tokens. Returns SafeString."""
    substituted = _substitute_area(value, area)
    if not substituted:
        return mark_safe("")
    return vars_html_filter(substituted)


@register.inclusion_tag("areas/_footer_areas.html")
def footer_areas():
    """
    Every published municipality, for the footer band.

    A tag rather than context, because the footer renders on pages served by
    several apps and not all of them build the shared area context. One indexed
    query returning ~26 rows is cheap enough to run per page; if that ever
    shows up in profiling it is a candidate for a short cache, not for
    denormalisation.

    Districts are deliberately left out. The point of the band is that a
    visitor recognises their municipality, and 200+ links in site-wide
    boilerplate is link stuffing that crowds out the parts of the footer that
    actually convert.
    """
    from apps.areas.models import Area, sort_areas

    municipalities = sort_areas(Area.objects.visible().municipalities())
    return {
        "footer_municipalities": municipalities,
        "footer_area_total": Area.objects.visible().count(),
    }


@register.simple_tag(takes_context=True)
def area_directory_schema(context, groups, site_settings):
    """
    Structured data for the /vvs/ directory: an ItemList of every area plus a
    `Plumber` whose `areaServed` names all of them.

    The ItemList is what lets a search engine treat the page as a directory
    rather than prose. `areaServed` is the piece that answers "who works in
    Täby?" for an assistant reading the page - without it the coverage is only
    implied by the link text.
    """
    request = context.get("request")
    if not groups or request is None:
        return ""

    items = []
    served = []
    position = 0
    for group in groups:
        for row in group["municipalities"]:
            municipality = row["area"]
            position += 1
            items.append(
                {
                    "@type": "ListItem",
                    "position": position,
                    "name": municipality.name,
                    "url": request.build_absolute_uri(municipality.get_absolute_url()),
                }
            )
            served.append({"@type": "AdministrativeArea", "name": municipality.name})
            for district in row["districts"]:
                position += 1
                items.append(
                    {
                        "@type": "ListItem",
                        "position": position,
                        "name": district.name,
                        "url": request.build_absolute_uri(district.get_absolute_url()),
                    }
                )

    name = getattr(site_settings, "name", "") or "VVS"
    business = {
        "@context": "https://schema.org",
        "@type": "Plumber",
        "name": name,
        "url": request.build_absolute_uri("/"),
        "areaServed": served,
    }
    phone = getattr(site_settings, "phone", "")
    if phone:
        business["telephone"] = phone

    directory = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"Serviceområden - {name}",
        "numberOfItems": len(items),
        "itemListElement": items,
    }

    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "name": "Hem",
                "item": request.build_absolute_uri("/"),
            },
            {
                "@type": "ListItem",
                "position": 2,
                "name": "Serviceområden",
                "item": request.build_absolute_uri(),
            },
        ],
    }

    return mark_safe(
        "".join(
            '<script type="application/ld+json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
            for payload in (business, directory, breadcrumbs)
        )
    )


@register.simple_tag
def area_schema_json(area, site_settings, services=None):
    """
    Emit `Plumber` structured data with `areaServed` for one area.

    This is the piece that lets Google show the business as a local result for
    the area rather than treating the page as generic copy. Kept separate from
    the FAQ schema tag so a page can carry both.
    """
    if area is None:
        return ""

    name = getattr(site_settings, "name", "") or "VVS"
    data = {
        "@context": "https://schema.org",
        "@type": "Plumber",
        "name": name,
        "description": strip_tags(area.intro or "") or f"VVS-tjänster i {area.name}.",
        "areaServed": {"@type": "AdministrativeArea", "name": area.name},
    }

    phone = getattr(site_settings, "phone", "")
    if phone:
        data["telephone"] = phone
    email = getattr(site_settings, "email", "")
    if email:
        data["email"] = email

    street = getattr(site_settings, "street_address", "")
    postal = getattr(site_settings, "postal_code", "")
    city = getattr(site_settings, "city", "")
    if street or postal or city:
        address = {"@type": "PostalAddress", "addressCountry": "SE"}
        if street:
            address["streetAddress"] = street
        if postal:
            address["postalCode"] = postal
        if city:
            address["addressLocality"] = city
        data["address"] = address

    names = [s for s in (services or []) if s]
    if names:
        data["hasOfferCatalog"] = {
            "@type": "OfferCatalog",
            "name": f"VVS-tjänster i {area.name}",
            "itemListElement": [
                {
                    "@type": "Offer",
                    "itemOffered": {"@type": "Service", "name": f"{n} i {area.name}"},
                }
                for n in names[:20]
            ],
        }

    return mark_safe(
        '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>"
    )
