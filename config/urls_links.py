"""
Hela adresstabellen för länkvärdarna k.adx.se och klick.adx.se
(apps/utskick/README.md, E.1). apps.utskick.links.LinkHostMiddleware sätter
request.urlconf hit för de värdarna, så inget annat av sajten svarar där:
inte /manage/, /flamingo/, /kund/, /admin/ eller sajtens sidor. MCP och
OAuth nekas redan i asgi_app.

Namnrymd "links", re_path och inget snedstreck sist (koderna står så i sms:en).
Vilken värd en kod hör till avgör vyn (links.on_link_host): k för sms, klick
för mejl. E-postens adresser (/m/, /a/, /v/, /w/, /o/, /c/) kom med S3; deras
token bygger links.email_url och grannarna (tokens.py). S4 lägger till
skriptet på egen sajt (/s.<version>.js och besöksanropet /v) och de
namngivna länkarna (/<public_slug>/<slug>), sist: alla andra adresser med
ett snedstreck prövas först, och public_slug kan aldrig vara ett av
värdarnas egna första led (models.RESERVED_PUBLIC_SLUGS).

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
    # S4, klick.adx.se: skriptet på egen sajt och dess besöksanrop (E.6).
    re_path(r"^s\.(?P<ver>[0-9a-f]{8})\.js$", v.snippet, name="snippet"),
    re_path(r"^v$", v.snippet_beacon, name="snippet_beacon"),
    # S4, klick.adx.se: de namngivna länkarna, sist (E.1). public_slug får
    # understreck (access.validate_public_slug), slugen inte
    # (links.NAMED_SLUG_RE).
    re_path(r"^(?P<account>[a-z0-9_-]{1,40})/(?P<slug>[a-z0-9-]{1,40})$", v.named, name="named"),
    # S4 (integrationen): samma adress med versaler (en telefon gör första
    # bokstaven stor) får 301 till gemenerna, aldrig för värdarnas egna led.
    re_path(
        r"^(?P<account>[A-Za-z0-9_-]{1,40})/(?P<slug>[A-Za-z0-9-]{1,40})$",
        v.named_folded,
        name="named_folded",
    ),
]

urlpatterns = [path("", include((link_patterns, "links")))]

handler404 = "apps.utskick.link_views.not_found"
