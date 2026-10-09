"""
Hela adresstabellen för länkvärdarna k.adx.se och klick.adx.se
(apps/utskick/README.md, E.1). apps.utskick.links.LinkHostMiddleware sätter
request.urlconf hit för de värdarna, så inget annat av sajten svarar där:
inte /manage/, /flamingo/, /kund/, /admin/ eller sajtens sidor. MCP och
OAuth nekas redan i asgi_app.

Namnrymd "links", re_path och inget snedstreck sist (koderna står så i sms:en).
Vilken värd en kod hör till avgör vyn (links.on_link_host): k för sms, klick
för mejl. E-postens adresser (/m/, /a/, /v/, /w/, /o/, /c/) kommer med S3 och
de namngivna länkarna och skriptet med S4.

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
]

urlpatterns = [path("", include((link_patterns, "links")))]

handler404 = "apps.utskick.link_views.not_found"
