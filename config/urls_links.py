"""
Hela adresstabellen för länkvärdarna k.adx.se och klick.adx.se
(apps/utskick/README.md, E.1). apps.utskick.links.LinkHostMiddleware sätter
request.urlconf hit för de värdarna, så inget annat av sajten svarar där:
inte /manage/, /flamingo/, /kund/, /admin/ eller sajtens sidor. MCP och
OAuth nekas redan i asgi_app.

Namnrymd "links", re_path och inget snedstreck sist (koderna står så i sms:en).
Vilken värd en kod hör till avgör vyn (links.on_link_host): k för sms, klick
för mejl. E-postens adresser (/m/, /a/, /v/, /w/, /o/, /c/) kom med S3; deras
token bygger links.email_url och grannarna (tokens.py). De namngivna
länkarna och skriptet kommer med S4.

    reverse("links:click", urlconf="config.urls_links", args=["Ab12Cd"]) -> "/Ab12Cd"
"""

from django.urls import include, path, re_path

from apps.utskick import link_views as v

CODE = r"(?P<code>[A-Za-z0-9]{6})"

link_patterns = [
    re_path(r"^$", v.home, name="home"),
    re_path(r"^robots\.txt$", v.robots, name="robots"),
    # S2, k.adx.se (sms)
    re_path(rf"^{CODE}$", v.click, name="click"),
    re_path(rf"^s/{CODE}$", v.sms_unsubscribe, name="sms_unsubscribe"),
    re_path(rf"^p/{CODE}$", v.sms_preferences, name="sms_preferences"),
    re_path(rf"^b/{CODE}$", v.confirm, name="confirm"),
    # S3, klick.adx.se (mejl). Formerna står i tokens.py.
    re_path(r"^m/(?P<token>[A-Za-z0-9.]{12,40})$", v.email_click, name="email_click"),
    re_path(
        r"^a/(?P<token>[A-Za-z0-9._-]{40,120})$", v.email_unsubscribe, name="email_unsubscribe"
    ),
    re_path(
        r"^v/(?P<token>[A-Za-z0-9._-]{40,120})$", v.email_preferences, name="email_preferences"
    ),
    re_path(r"^w/(?P<token>[A-Za-z0-9.]{16,40})$", v.web_view, name="web_view"),
    re_path(r"^o/(?P<token>[A-Za-z0-9.]{10,30})\.gif$", v.open_pixel, name="open_pixel"),
    re_path(r"^c/(?P<token>[A-Za-z0-9._]{20,60})\.ics$", v.calendar, name="calendar"),
]

urlpatterns = [path("", include((link_patterns, "links")))]

handler404 = "apps.utskick.link_views.not_found"
