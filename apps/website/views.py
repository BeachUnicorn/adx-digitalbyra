"""Public website views."""

from django.conf import settings as django_settings
from django.http import Http404
from django.shortcuts import render

from .models import BlockPage, MediaFile, Menu, SiteSettings


def _get_site_context():
    """Build context shared by all public pages."""
    settings = SiteSettings.load()
    header_menu = Menu.objects.filter(location="header").prefetch_related("items__page").first()
    footer_menus = (
        Menu.objects.filter(location="footer")
        .order_by("order", "id")
        .prefetch_related("items__page")
    )
    return {
        "site_settings": settings,
        "header_menu": header_menu,
        "footer_menus": footer_menus,
        "analytics_enabled": django_settings.ANALYTICS_ENABLED,
    }


def homepage(request):
    """Render the homepage."""
    settings = SiteSettings.load()
    page = settings.homepage if settings else None
    if not page:
        # Fallback: first published page
        page = BlockPage.objects.filter(is_published=True).order_by("order").first()
    if not page:
        raise Http404

    context = _get_site_context()
    context["page"] = page
    context["page_color"] = page.gradient_color
    blocks = page.blocks.filter(is_visible=True)
    context["blocks"] = blocks
    context["lcp_image_url"] = _get_hero_image_url(blocks)
    return render(request, "website/page.html", context)


def page_detail(request, slug):
    """Render a page by slug."""
    page = BlockPage.objects.filter(slug=slug, is_published=True).first()
    if not page:
        raise Http404

    context = _get_site_context()
    context["page"] = page
    context["page_color"] = page.gradient_color
    blocks = page.blocks.filter(is_visible=True)
    context["blocks"] = blocks
    context["lcp_image_url"] = _get_hero_image_url(blocks)
    return render(request, "website/page.html", context)


def _get_hero_image_url(blocks):
    """Extract the image URL from the first hero block for LCP preloading."""
    for block in blocks:
        if block.block_type == "hero":
            image_id = (block.data or {}).get("image_id")
            if image_id:
                try:
                    media = MediaFile.objects.get(pk=image_id)
                    return media.file.url
                except MediaFile.DoesNotExist:
                    pass
            break  # Only check the first hero
    return ""
