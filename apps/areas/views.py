"""
Public views for städerna.

Two pages:
- /digitalbyra/            stadsöversikten
- /digitalbyra/<slug>/     en stad

`Http404` rather than a redirect for hidden areas: an area switched off (or
hidden by an inactive parent) should look like it doesn't exist yet, both to
visitors and to crawlers.
"""

from django.http import Http404
from django.shortcuts import get_object_or_404, render

from apps.website.views import _get_site_context

from .models import Area


def _visible_or_404(slug):
    area = get_object_or_404(
        Area.objects.select_related("image", "parent", "parent__parent", "faq_section"),
        slug=slug,
    )
    if not area.is_visible:
        raise Http404("Området är inte publicerat.")
    return area


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
