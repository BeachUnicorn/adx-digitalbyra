"""
Dynamic XML sitemap for all public pages.

Generates entries for:
- Homepage and FAQ index
- Block pages (published)
- FAQ sections (active)
- Städer (visible areas)
"""

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from apps.areas.models import Area
from apps.faq.models import FAQSection
from apps.website.models import BlockPage


class StaticSitemap(Sitemap):
    """Homepage, pricing index, FAQ list."""

    priority = 1.0
    changefreq = "weekly"

    def items(self):
        return [
            "website:homepage",
            "faq:section_list",
        ]

    def location(self, item):
        return reverse(item)


class BlockPageSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return BlockPage.objects.filter(is_published=True)

    def location(self, obj):
        return obj.get_absolute_url()

    def lastmod(self, obj):
        return obj.updated_at


class FAQSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.5

    def items(self):
        return FAQSection.objects.filter(is_active=True)

    def location(self, obj):
        return obj.get_absolute_url()


class AreaSitemap(Sitemap):
    """Städerna. Hidden areas are excluded via the visible() filter."""

    changefreq = "monthly"
    priority = 0.7

    def items(self):
        return Area.objects.visible().order_by("level", "order", "name")

    def location(self, obj):
        return obj.get_absolute_url()

    def lastmod(self, obj):
        return obj.updated_at


sitemaps = {
    "static": StaticSitemap,
    "pages": BlockPageSitemap,
    "faq": FAQSitemap,
    "areas": AreaSitemap,
}
