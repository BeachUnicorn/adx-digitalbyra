"""
Sidornas adresser i verktyget (app_views/page_preview.py).

    {% load flamingo_pages %}
    {% with links=campaigns|page_links %}   [{campaign, href, url}], en per
                                            kampanj som visar sidan
    {% page_preview_url page %}             förhandsvisningen (sidan utan kampanj)
"""

from django import template

from ..app_views.page_preview import page_links as _page_links
from ..app_views.page_preview import preview_url

register = template.Library()


@register.filter
def page_links(campaigns):
    """En adress per kampanj: href till /lp/<slug>/ och url att kopiera."""
    return _page_links(campaigns or [])


@register.simple_tag
def page_preview_url(page):
    return preview_url(page)
