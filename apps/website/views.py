"""Public website views."""

from django.conf import settings as django_settings
from django.http import Http404
from django.shortcuts import render

from . import chrome
from .links import prime_pages
from .models import BlockPage, SiteSettings


def _get_site_context():
    """Build context shared by all public pages.

    Samma objekt som kontextprocessorn site_chrome lägger i kontexten
    (chrome.py): en gång per förfrågan, inte en gång per ställe.
    """
    header_menu, footer_menus = chrome.menus()
    return {
        "site_settings": SiteSettings.cached(),
        "header_menu": header_menu,
        "footer_menus": footer_menus,
        "analytics_enabled": django_settings.ANALYTICS_ENABLED,
    }


def homepage(request):
    """Render the homepage."""
    settings = SiteSettings.cached()
    page = settings.homepage if settings else None
    if page and page.is_flamingo:
        page = None  # en Flamingo-sida är aldrig sajtens startsida
    if not page:
        # Fallback: first published page
        page = (
            BlockPage.objects.filter(is_published=True, design=BlockPage.DESIGN_ADX)
            .order_by("order")
            .first()
        )
    if not page:
        raise Http404

    context = _get_site_context()
    context["page"] = page
    context["page_color"] = page.gradient_color
    blocks = page.blocks.filter(is_visible=True)
    context["blocks"] = blocks
    context["lcp_image_url"] = _get_hero_image_url(blocks)
    prime_pages(blocks)
    _add_ring(context, page)
    return render(request, "website/page.html", context)


def page_detail(request, slug):
    """Render a page by slug. Bara ADX-sidor - Flamingo-sidor bor under /flamingo/."""
    page = BlockPage.objects.filter(
        slug=slug, is_published=True, design=BlockPage.DESIGN_ADX
    ).first()
    if not page:
        raise Http404

    context = _get_site_context()
    context["page"] = page
    context["page_color"] = page.gradient_color
    blocks = page.blocks.filter(is_visible=True)
    context["blocks"] = blocks
    context["lcp_image_url"] = _get_hero_image_url(blocks)
    prime_pages(blocks)
    _add_ring(context, page)
    return render(request, "website/page.html", context)


def _add_ring(context, page):
    """Länkmotorns syskonlänkar, renderas sist på sidan (se related.py)."""
    from apps.website.related import ring_heading, ring_links

    context["auto_related"] = ring_links(page)
    context["auto_related_heading"] = ring_heading(page)


def _get_hero_image_url(blocks):
    """Extract the image URL from the first hero block for LCP preloading."""
    for block in blocks:
        if block.block_type == "hero":
            image_id = (block.data or {}).get("image_id")
            # Samma uppslag som {% media %} i hero-mallen: en fråga, inte två.
            media = chrome.media_file(image_id)
            if media is not None:
                return media.file.url
            break  # Only check the first hero
    return ""
