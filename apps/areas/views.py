"""
Public views for städerna.

Two pages:
- /webbyra/            stadsöversikten
- /webbyra/<slug>/     en stad

`Http404` rather than a redirect for hidden areas: an area switched off (or
hidden by an inactive parent) should look like it doesn't exist yet, both to
visitors and to crawlers.

Adresserna låg på /digitalbyra/ fram till 2026-08-29. De två legacy-vyerna
längst ner 301:ar dit de nu ligger - sökordet ändras, inte länkarna.
"""

from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.website.views import _get_site_context

from .models import Area, AreaLevel, sort_areas, swedish_sort_key


def _visible_or_404(slug):
    area = get_object_or_404(
        Area.objects.select_related("image", "parent", "parent__parent", "faq_section"),
        slug=slug,
    )
    if not area.is_visible:
        raise Http404("Området är inte publicerat.")
    return area


def area_list(request):
    """
    Ortsöversikten, grupperad per län.

    Server-renderad (en länk som kräver JavaScript är värd mycket mindre) och
    grupperad, inte platt: med drygt hundra orter blir en enda alfabetisk lista
    obrukbar, och den blandar dessutom län, kommuner och stadsdelar som om de
    vore jämförbara. Länen är rubriker, kommunerna länkarna under dem.
    """
    visible = Area.objects.visible().select_related("parent", "parent__parent")
    by_parent = {}
    for area in visible:
        if area.level == AreaLevel.REGION:
            continue
        # Stadsdelar grupperas under sitt läns rubrik, inte under kommunen -
        # översikten ska vara en väg in, inte en avbild av hierarkin.
        region = area.parent if area.level == AreaLevel.MUNICIPALITY else area.parent.parent
        if region is not None:
            by_parent.setdefault(region.pk, []).append(area)

    regions = [
        {"region": region, "areas": sort_areas(by_parent.get(region.pk, []))}
        for region in sorted(
            (a for a in visible if a.level == AreaLevel.REGION),
            key=lambda a: swedish_sort_key(a.name),
        )
    ]
    context = _get_site_context()
    context.update({"regions": regions, "page_color": "#2f6f4f"})
    return render(request, "areas/area_list.html", context)


#: Hur många grannorter som listas på en ortssida. Sidan listade tidigare
#: ALLA andra områden - 108 länkar på varje sida. Det hjälper varken
#: besökaren, som inte letar efter en slumpvis kommun i ett annat län, eller
#: sökmotorn, som fördelar länkvärdet över allt utan att något pekas ut.
MAX_NEARBY = 12


def _nearby(area):
    """
    Områden som faktiskt hör ihop med det här: föräldern, syskonen i samma
    län och de egna stadsdelarna. Hierarkin finns redan i datan och är den
    enda relationen som betyder något för en besökare.
    """
    visible = Area.objects.visible().exclude(pk=area.pk).select_related("parent")
    # Samma förälder = syskon, och det gäller även rotnivån: ett län har
    # inget parent_id, men de andra länen är precis dess syskon. Utan det
    # fallet fick alla 21 länsidor noll grannlänkar.
    siblings = [a for a in visible if a.parent_id == area.parent_id]
    children = [a for a in visible if a.parent_id == area.pk]
    parents = [a for a in (area.parent, getattr(area.parent, "parent", None)) if a and a.is_visible]

    out, seen = [], {area.pk}
    for candidate in parents + sort_areas(children) + sort_areas(siblings):
        if candidate.pk not in seen:
            seen.add(candidate.pk)
            out.append(candidate)
    return out[:MAX_NEARBY]


#: Sidfotskolumnen vars länkar också visas som en egen sektion på ortssidan.
#: Ortssidorna ("webbyrå i X") och branschsidorna ("hemsida för X") är sajtens
#: två stora silor, och utan den här kopplingen länkar de inte till varandra
#: alls. Listan bor i menyn så att den finns på ETT ställe - den redigeras i
#: /manage/ som vilken meny som helst.
INDUSTRY_MENU_HEADING = "Branscher"


def _industry_links(context):
    return next(
        (m for m in context.get("footer_menus", []) if m.heading == INDUSTRY_MENU_HEADING),
        None,
    )


def area_detail(request, slug):
    area = _visible_or_404(slug)
    context = _get_site_context()
    context.update(
        {
            "area": area,
            "nearby": _nearby(area),
            "industry_menu": _industry_links(context),
            "page_color": area.gradient_color or "#2f6f4f",
        }
    )
    return render(request, "areas/area_detail.html", context)


def area_list_legacy(request):
    """/digitalbyra/ -> /webbyra/ (permanent)."""
    return redirect("areas:area_list", permanent=True)


def area_detail_legacy(request, slug):
    """/digitalbyra/<slug>/ -> /webbyra/<slug>/ (permanent).

    Redirectar utan att slå upp området: en okänd eller dold slug ska ge samma
    svar på båda adresserna, och det avgörs av den riktiga vyn.
    """
    return redirect("areas:area_detail", slug=slug, permanent=True)
