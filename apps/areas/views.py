"""
Public views for serviceområden.

Three pages:
- /vvs/                          overview, grouped by län
- /vvs/<slug>/                   one area (län, kommun or stadsdel)
- /vvs/<slug>/<service-slug>/    area x service combination page

`Http404` rather than a redirect for hidden areas: an area switched off (or
hidden by an inactive parent) should look like it doesn't exist yet, both to
visitors and to crawlers.
"""

import zlib

from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from apps.services.models import Service
from apps.website.views import _get_site_context

from .models import MAX_DEPTH, Area, AreaLevel, sort_areas


def _visible_or_404(slug):
    area = get_object_or_404(
        Area.objects.select_related("image", "parent", "parent__parent", "faq_section"),
        slug=slug,
    )
    if not area.is_visible:
        raise Http404("Området är inte publicerat.")
    return area


def _nearest(area, has_content):
    """
    Walk up from `area` to the first ancestor that satisfies `has_content`.

    Districts inherit their kommun's services, questions and neighbours unless
    they define their own. Without this a newly ticked district is an almost
    empty page - hero, trust strip and a CTA - which is exactly the thin
    doorway page these pages are supposed to avoid. Anything the district does
    define wins, so inheritance is a floor, not a ceiling.
    """
    node = area
    for _step in range(MAX_DEPTH):
        if has_content(node):
            return node
        if node.parent_id is None:
            return None
        node = node.parent
    return None


def _category_cards(area):
    """
    Kategorikorten på en ortssida, plus ett exempel på tjänst per kategori.

    Kategorier och inte tjänster: firman har 76 aktiva tjänster men 12
    kategorier, och 76 kort upprepade på 232 ortssidor vore en katalog -
    samma tunna innehåll som tjänst-och-ort-sidorna vi tog bort. Antalet
    kategorier är dessutom stabilt när tjänsterna växer.

    Exemplen väljs PSEUDOSLUMPAT MED ORTEN SOM FRÖ, inte slumpat per
    sidladdning. Två skäl: sidan ska se likadan ut varje gång någon
    besöker eller Google hämtar den, och olika orter får ändå olika
    exempel - vilket gör 232 sidor mindre lika varandra, inte mer.
    """
    from apps.services.models import ServiceCategory

    categories = list(
        ServiceCategory.objects.filter(is_active=True)
        .select_related("image")
        .prefetch_related("services")
        .order_by("order", "name")
    )
    seed = zlib.crc32(area.slug.encode("utf-8"))
    cards = []
    for index, category in enumerate(categories):
        services = [s for s in category.services.all() if s.is_active]
        if not services:
            continue
        example = services[(seed + index) % len(services)]
        cards.append({"category": category, "example": example})
    return cards


def _faq_items(area):
    """
    Area-specific questions first, then any shared section.

    Inherited questions are why `{{ ort }}` is worth using in the copy: the
    kommun's "Hur snabbt kan ni komma till {{ ort }}?" renders as "… till
    Råsunda?" on the district page, without a second copy of the text.
    """
    source = _nearest(area, lambda node: node.faq_items.exists())
    items = list(source.faq_items.all()) if source else []

    section_source = _nearest(area, lambda node: node.faq_section_id is not None)
    if section_source is not None:
        items += list(
            section_source.faq_section.items.filter(is_active=True).order_by("order", "id")
        )
    return items


#: Roughly how far the "we cover this" circle should reach, per level.
MAP_RADIUS = {
    AreaLevel.REGION: 40000,
    AreaLevel.MUNICIPALITY: 6000,
    AreaLevel.DISTRICT: 1800,
}


def _map_context(area):
    """Map is opt-in on two counts: a key must exist and the area must be placed."""
    key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
    if not key or not area.has_map:
        return {"show_map": False}
    return {
        "show_map": True,
        "google_maps_api_key": key,
        "map_radius": MAP_RADIUS.get(area.level, 6000),
    }


def _neighbours(area):
    """Explicit neighbours, else the siblings - other districts in the kommun."""
    own = [n for n in area.neighbours.all() if n.is_visible]
    if own or area.parent_id is None:
        return own
    return [
        sibling
        for sibling in sort_areas(area.parent.children.exclude(pk=area.pk))
        if sibling.is_visible
    ]


#: How many districts to show per municipality before the "+N fler" link.
#: The rest are still rendered - just hidden by CSS - so that search can reveal
#: them and, more importantly, so every district page gets an internal link
#: from this hub page. That link is what gets them indexed.
VISIBLE_DISTRICTS = 5


def area_list(request):
    """Stadsöversikten: alla aktiva städer, server-renderade (en länk som
    kräver JavaScript är värd mycket mindre)."""
    cities = Area.objects.filter(is_active=True).order_by("order", "name")
    context = _get_site_context()
    context.update({"cities": cities, "page_color": "#2f6f4f"})
    return render(request, "areas/area_list.html", context)


def area_detail(request, slug):
    area = _visible_or_404(slug)
    others = Area.objects.filter(is_active=True).exclude(pk=area.pk).order_by("order", "name")
    context = _get_site_context()
    context.update(
        {
            "area": area,
            "other_cities": others,
            "page_color": area.gradient_color or "#2f6f4f",
        }
    )
    return render(request, "areas/area_detail.html", context)


def other_services_in_area(area, exclude_service=None):
    """Helper kept for templates that need a plain service list."""
    qs = Service.objects.filter(is_active=True, area_links__area=area).distinct()
    if exclude_service is not None:
        qs = qs.exclude(pk=exclude_service.pk)
    return qs
