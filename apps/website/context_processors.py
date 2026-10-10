"""
Sajtens chrome i EN kontextprocessor: inställningar, menyer, tjänstelistan
och aktiv nav-markering. Vyerna slipper ladda samma saker var för sig, och
header/footer/mobilmeny kan aldrig visa olika sanningar (mönsterkatalogen §1).
Uppslagen görs en gång per förfrågan och delas med vyerna (chrome.py).
"""

from apps.website import chrome
from apps.website.models import SiteSettings


def site_chrome(request):
    settings = SiteSettings.cached()
    header_menu, footer_menus = chrome.menus()
    services = chrome.active_services()

    # Aktiv menymarkering: tjänstesidorna räknas till "Tjänster" (guidens
    # NAV_PARENT), övriga matchas på URL-prefix - längsta träff vinner.
    path = request.path
    service_urls = {s.get_absolute_url() for s in services}
    active_nav = None
    if path in service_urls:
        active_nav = "/tjanster/"
    elif header_menu:
        best = ""
        for item in header_menu.items.all():
            url = item.get_url()
            if not url:
                continue
            if (path == url or (url != "/" and path.startswith(url))) and len(url) > len(best):
                best = url
        if best or path == "/":
            active_nav = best or "/"

    return {
        "site_settings": settings,
        "header_menu": header_menu,
        "footer_menus": footer_menus,
        "nav_services": services,
        "active_nav": active_nav,
    }
