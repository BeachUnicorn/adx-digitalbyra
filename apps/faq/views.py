"""Public FAQ views: list all sections + detail for one section."""

from django.shortcuts import get_object_or_404, render

from apps.website.views import _get_site_context

from .models import FAQSection


def section_list(request):
    sections = FAQSection.objects.filter(is_active=True)
    context = _get_site_context()
    context["sections"] = sections
    return render(request, "faq/section_list.html", context)


def section_detail(request, slug):
    section = get_object_or_404(FAQSection, slug=slug, is_active=True)
    items = section.items.filter(is_active=True)
    context = _get_site_context()
    context.update(
        {"section": section, "faq_items": items, "owner_links": _owner_links(section)}
    )
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
    faq_blocks = Block.objects.filter(
        block_type="faq", is_visible=True, page__is_published=True
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
