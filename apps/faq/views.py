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
    context.update({"section": section, "faq_items": items})
    return render(request, "faq/section_detail.html", context)
