"""
Flamingos sidor. Innehållet är vanliga blocksidor (BlockPage med
design="flamingo") som redigeras i /manage/ som alla andra sidor; vyerna här
lägger bara på Flamingos egen grundmall. De publicerade sidorna är öppna för
alla, indexeras och står i sitemapen (Giovanni 2026-10-04); opublicerade
sidor ser bara byrån. Bara verktyget kräver behörighet.

Verktyget (/flamingo/app/...) bor i app_views/, kundens landningssidor
(/lp/...) i public_views.py och byråns granskning i manage_review.py.
"""

from functools import wraps

from django.http import Http404
from django.shortcuts import render

from apps.website.models import BlockPage, SiteSettings

from .access import access_for


def flamingo_required(view):
    """Vyn kräver att grinden (middleware) gett förfrågan en FlamingoAccess.
    På Flamingos sidor får alla en (PUBLIC utan behörighet); i verktyget bara
    den med behörighet. Utkastförhandsvisningen kör utan middleware och
    räknas här."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if access_for(request) is None:
            raise Http404
        return view(request, *args, **kwargs)

    return wrapper


def flamingo_pages(access):
    """Flamingos sidor i ordning, för sidhuvudets meny. Byrån ser utkast."""
    pages = BlockPage.objects.filter(design=BlockPage.DESIGN_FLAMINGO)
    if not access.sees_drafts:
        pages = pages.filter(is_published=True)
    return pages.exclude(slug=BlockPage.FLAMINGO_HOME_SLUG).order_by("order", "title")


@flamingo_required
def flamingo_page(request, slug=None):
    """Startsidan (/flamingo/) eller en undersida (/flamingo/<slug>/)."""
    access = request.flamingo
    if slug == BlockPage.FLAMINGO_HOME_SLUG:
        raise Http404  # startsidan bor på /flamingo/, inte en gång till
    page = BlockPage.objects.filter(
        design=BlockPage.DESIGN_FLAMINGO, slug=slug or BlockPage.FLAMINGO_HOME_SLUG
    ).first()
    if page is None or not (page.is_published or access.sees_drafts):
        raise Http404
    blocks = page.blocks.filter(is_visible=True)
    return render(
        request,
        "flamingo/page.html",
        {
            "page": page,
            "blocks": blocks,
            "flamingo": access,
            "flamingo_nav": flamingo_pages(access),
            "site_settings": SiteSettings.load(),
        },
    )
