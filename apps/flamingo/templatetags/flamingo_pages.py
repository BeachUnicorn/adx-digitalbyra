"""
Sidornas adresser i verktyget (app_views/page_preview.py).

    {% load flamingo_pages %}
    {% page_preview_url page %}             förhandsvisningen (en sida som inte är publicerad)

Sidans egen adress (/lp/<konto>/<sida>/, README Steg 2) kommer från
addresses.py i vyerna (page_address, page_href, page_full_url).
"""

from django import template

from ..app_views.page_preview import preview_url

register = template.Library()


@register.simple_tag
def page_preview_url(page):
    return preview_url(page)
