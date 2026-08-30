"""
Strukturerad data (schema.org JSON-LD) för den publika sajten.

Google läser detta för att förstå VAD sajten är (ett företag i Stockholm som
säljer webbtjänster), inte bara vad det står på den. Utan det gissar Google
utifrån texten; med det kvalificerar sidorna för brödsmulor i sökresultatet
och kopplingen till Business Profile blir entydig.

Samma mönster som apps/faq/templatetags/faq_tags.py: bygg dict, json.dumps,
mark_safe runt en <script type="application/ld+json">. All data kommer ur
SiteSettings och modellerna - ingenting hittas på här, och tomma fält
utelämnas i stället för att skickas som tomma strängar.
"""

import json

from django import template
from django.conf import settings as django_settings
from django.templatetags.static import static
from django.utils.safestring import mark_safe

register = template.Library()


def _base_url():
    return (django_settings.SITE_BASE_URL or "https://adx.se").rstrip("/")


def _script(data):
    payload = json.dumps(data, ensure_ascii=False)
    return mark_safe(f'<script type="application/ld+json">{payload}</script>')  # noqa: S308


def _clean(data):
    """Ta bort tomma värden rekursivt - schema med tomma strängar är sämre
    än schema utan fältet."""
    if isinstance(data, dict):
        return {k: _clean(v) for k, v in data.items() if v not in ("", None, [], {})}
    if isinstance(data, list):
        return [_clean(v) for v in data if v not in ("", None, [], {})]
    return data


@register.simple_tag
def organization_schema(site_settings):
    """Organisationen + lokal närvaro. Renderas på varje sida via base.html.

    ProfessionalService är LocalBusiness-undertypen för tjänsteföretag -
    det är den som kopplar sajten till "webbyrå i Stockholm" som entitet
    och som ska matcha Google Business Profile exakt (NAP-konsistens).
    """
    if site_settings is None:
        return ""
    base = _base_url()
    data = _clean(
        {
            "@context": "https://schema.org",
            "@type": ["Organization", "ProfessionalService"],
            "@id": f"{base}/#organization",
            "name": site_settings.name or "ADX",
            "url": f"{base}/",
            "logo": base + static("images/adx-logo.png"),
            "email": site_settings.email,
            "telephone": site_settings.phone,
            "address": _clean(
                {
                    "@type": "PostalAddress",
                    "streetAddress": site_settings.street_address,
                    "postalCode": site_settings.postal_code,
                    "addressLocality": site_settings.city,
                    "addressCountry": "SE",
                }
            ),
            "areaServed": {"@type": "Country", "name": "Sverige"},
        }
    )
    # En adress som bara innehåller @type är ingen adress.
    if list(data.get("address", {}).keys()) == ["@type"]:
        del data["address"]
    return _script(data)


@register.simple_tag
def website_schema(site_settings):
    base = _base_url()
    name = (site_settings.name if site_settings else "") or "ADX"
    return _script(
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "@id": f"{base}/#website",
            "url": f"{base}/",
            "name": name,
            "publisher": {"@id": f"{base}/#organization"},
        }
    )


@register.simple_tag
def area_breadcrumb_schema(area):
    """Brödsmulor för ortssidorna: Hem / Orter / [län] / [kommun] / [ort].

    Följer parent-kedjan i Area-trädet - samma hierarki som sidan visar
    visuellt. Ger brödsmulevisning i sökresultatet i stället för rå URL.
    """
    if area is None:
        return ""
    base = _base_url()
    chain = []
    node = area
    while node is not None:
        chain.append(node)
        node = node.parent
    chain.reverse()

    items = [
        {"@type": "ListItem", "position": 1, "name": "Hem", "item": f"{base}/"},
        {"@type": "ListItem", "position": 2, "name": "Orter", "item": f"{base}/webbyra/"},
    ]
    for offset, node in enumerate(chain):
        items.append(
            {
                "@type": "ListItem",
                "position": 3 + offset,
                "name": node.name,
                "item": f"{base}{node.get_absolute_url()}",
            }
        )
    return _script(
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": items,
        }
    )


@register.simple_tag
def service_schema(page, site_settings):
    """Service-schema på tjänstesidorna.

    En BlockPage vars slug matchar en aktiv Service ÄR tjänstens publika
    sida (så är innehållsmodellen byggd), och då beskrivs tjänsten som en
    Service levererad av organisationen.
    """
    if page is None:
        return ""
    from apps.services.models import Service

    service = Service.objects.filter(slug=page.slug, is_active=True).first()
    if service is None:
        return ""
    base = _base_url()
    return _script(
        _clean(
            {
                "@context": "https://schema.org",
                "@type": "Service",
                "name": service.name,
                "description": service.description,
                "url": f"{base}{page.get_absolute_url()}",
                "provider": {"@id": f"{base}/#organization"},
                "areaServed": {"@type": "Country", "name": "Sverige"},
            }
        )
    )
