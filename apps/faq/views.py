"""Public FAQ views: list all sections + detail for one section."""

from django.shortcuts import get_object_or_404, render

from apps.website.views import _get_site_context

from .visibility import public_sections


def section_list(request):
    # Bara ADX-sektioner: en sektion för ADX Flamingo hör till Flamingos
    # sidor, inte till /faq/ (visibility.py).
    sections = public_sections()
    context = _get_site_context()
    context["sections"] = sections
    return render(request, "faq/section_list.html", context)


def section_detail(request, slug):
    section = get_object_or_404(public_sections(), slug=slug)
    items = section.items.filter(is_active=True)
    context = _get_site_context()
    context.update({"section": section, "faq_items": items, "owner_links": _owner_links(section)})
    return render(request, "faq/section_detail.html", context)


def _owner_links(section):
    """
    Sidorna frågorna faktiskt hör till - länkmotorns FAQ-gren.

    En besökare som googlar sig rakt in på en FAQ-sida hade ingen väg
    vidare till sidan frågorna handlar om; sektionssidorna var återvänds-
    gränder med en enda inlänk (indexet). Ägarskapet finns redan i datan:
    faq-blocken bär sektionens id, och tjänster/områden pekar på sin
    sektion med FK. Motorn läser relationerna - ingen skriver länkar.
    """
    from apps.areas.models import Area
    from apps.services.models import Service
    from apps.website.models import Block

    links = []
    # Bara ADX-sidor: Flamingos sidor hör till /flamingo/ och länkas inte
    # från ADX:s FAQ.
    faq_blocks = Block.objects.filter(
        block_type="faq", is_visible=True, page__is_published=True, page__design=""
    ).select_related("page")
    for block in faq_blocks:
        # Seedvägen lagrar id:t som sträng, /manage/-formulär kan ge int.
        if str((block.data or {}).get("faq_section_id") or "") == str(section.pk):
            links.append((block.page.title, f"/{block.page.slug}/"))
    for service in Service.objects.filter(faq_section=section, is_active=True):
        links.append((service.name, service.get_absolute_url()))
    for area in Area.objects.filter(faq_section=section, is_active=True):
        links.append((area.name, area.get_absolute_url()))

    seen, unique = set(), []
    for title, href in links:
        if href not in seen:
            seen.add(href)
            unique.append({"title": title, "href": href})
    return unique
