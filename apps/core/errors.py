"""
Felsidorna. En 404 på adx.se ska se ut som adx.se, inte som en naken
"Not Found" från ramverket.

404 väljer utseende efter VAR besökaren är:

- sondering (wp-login.php, .env ...): en rad text, ingen databas. Det är
  botar, och de ska inte kosta fem frågor och en "menade du"-sökning.
- /aiz/: text, eftersom läsaren är en AI som väntar sig markdown.
- JSON-klienter (tavlans fetch, /status/): JSON.
- /manage/ för byrån: panelens skinn, med vägen tillbaka till tavlan.
- /kund/: portalens skinn.
- /offert/: sajtens sida, men med text om att LÄNKEN är fel. Kunden som
  klippt av en offertlänk i mejlet ska inte få höra att "sidan har flyttat".
- allt annat: sajtens sida med förslag på vad besökaren kan ha menat.

Faller renderingen (databasen nere mitt i en 404) visas den fristående
sidan i stället. 500-sidan är ALLTID fristående: ingen kontext, ingen
databas, inga kontextprocessorer - den visas ju när något av det har gått
sönder.
"""

import difflib
import logging
import re

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.template import loader

logger = logging.getLogger(__name__)

#: Sonderingar efter andra system. Ingen människa skriver de här adresserna.
_PROBE_RE = re.compile(
    r"(\.(php\d?|aspx?|jsp|cgi|env|ini|sql|bak|old|zip|tar|gz|yml|yaml|git)(/|$))"
    r"|(^/(wp-|wordpress|\.git|\.env|\.aws|\.ssh|\.vscode|cgi-bin|phpmyadmin|vendor/|xmlrpc))",
    re.IGNORECASE,
)

_CANDIDATES_CACHE_KEY = "errors:404:candidates"
_CANDIDATES_CACHE_SECONDS = 600
MAX_SUGGESTIONS = 3

#: Sidans färg (gradienten härleds ur den, se apps/website/theme.py). Djup
#: indigo: ingen annan sida har den, så man ser direkt att man hamnat fel.
PAGE_COLOR = "#34389a"


#: Vad förslaget är, per sitemap. Visas på raden: en sida och dess FAQ
#: heter ofta likadant, och då ska man se vilken som är vilken.
_KINDS = {"pages": "Sida", "faq": "Vanliga frågor", "areas": "Stad"}


def _candidates():
    """
    (slug, titel, url, sort) för allt publikt - samma källa som sitemapen,
    så förslagen kan aldrig peka på en avpublicerad sida.
    """
    cached = cache.get(_CANDIDATES_CACHE_KEY)
    if cached is not None:
        return cached
    from apps.core.sitemap import sitemaps

    rows = []
    for name, sitemap_class in sitemaps.items():
        if name == "static":
            continue
        sitemap = sitemap_class()
        for obj in sitemap.items():
            url = sitemap.location(obj)
            slug = url.strip("/").rsplit("/", 1)[-1]
            title = getattr(obj, "title", None) or getattr(obj, "name", None) or str(obj)
            if slug:
                rows.append((slug.lower(), title, url, _KINDS.get(name, "Sida")))
    cache.set(_CANDIDATES_CACHE_KEY, rows, _CANDIDATES_CACHE_SECONDS)
    return rows


def suggestions_for(path):
    """
    Upp till tre sidor som liknar det besökaren skrev, bäst först. Sista
    segmentet i adressen jämförs mot sajtens sluggar på tre sätt: hela
    sluggen (stavfel som "webutveckling"), prefix (avklippta adresser som
    "webb") och ord för ord ("webbyra-goteborg" hittar både tjänsten och
    orten, fast ingen sida heter så).
    """
    wanted = path.strip("/").rsplit("/", 1)[-1].lower()
    wanted = re.sub(r"\.html?$", "", wanted)
    if len(wanted) < 3:
        return []
    by_slug = {slug: (title, url, kind) for slug, title, url, kind in _candidates()}
    words = [w for w in re.split(r"[-_]+", wanted) if len(w) >= 4]

    scores = {}
    for slug in by_slug:
        score = difflib.SequenceMatcher(None, wanted, slug).ratio()
        if slug.startswith(wanted) or wanted.startswith(slug):
            score = max(score, 0.8)
        for word in words:
            for part in slug.split("-"):
                if len(part) >= 4:
                    ratio = difflib.SequenceMatcher(None, word, part).ratio()
                    if ratio >= 0.84:
                        # Ett ordträff väger lite mindre än en träff på hela sluggen.
                        score = max(score, ratio * 0.9)
        if score >= 0.72:
            # Sidan före sin egen FAQ när båda träffar lika bra.
            scores[slug] = score * (0.95 if by_slug[slug][2] == _KINDS["faq"] else 1)

    best = sorted(scores, key=lambda s: (-scores[s], len(s)))[:MAX_SUGGESTIONS]
    return [dict(zip(("title", "url", "kind"), by_slug[s], strict=True)) for s in best]


def _wants_json(request):
    accept = request.headers.get("Accept", "")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return "application/json" in accept and "text/html" not in accept


def _standalone(code, status):
    """Den fristående sidan: ren mall utan kontext, fungerar utan databas."""
    body = loader.get_template(f"{code}.html").render()
    return HttpResponse(body, status=status)


def _plain_404():
    return HttpResponse("404: sidan finns inte.\n", status=404, content_type="text/plain")


def not_found(request, exception=None, path=None):
    path = path or request.path

    if _PROBE_RE.search(path):
        return _plain_404()
    if path.startswith("/aiz/"):
        return HttpResponse(
            "# 404\n\nAdressen finns inte. Börja på /aiz/ - där står hur guiderna hämtas.\n",
            status=404,
            content_type="text/markdown; charset=utf-8",
        )
    if path.startswith(("/status/", "/mcp", "/analytics/")) or _wants_json(request):
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)

    try:
        return _render_404(request, path)
    except Exception:
        # En 404 får aldrig bli en 500 för att menyn inte gick att ladda.
        logger.exception("404-sidan gick inte att rendera, visar den fristående.")
        return _standalone("404", 404)


def _render_404(request, path):
    from apps.projects.access import customer_for, is_agency_user, viewing_customer

    user = getattr(request, "user", None)
    is_agency = bool(user and is_agency_user(user))

    if path.startswith("/manage/") and is_agency:
        context = {"path": path, "title": "Sidan finns inte"}
        body = loader.render_to_string("errors/404_manage.html", context, request)
        return HttpResponse(body, status=404)

    if path.startswith("/kund/"):
        # Portalens meny läser request.customer, som annars sätts av
        # customer_required - och den vyn kördes ju aldrig.
        if not hasattr(request, "customer") and user and user.is_authenticated:
            viewing = viewing_customer(request)
            request.customer = viewing or customer_for(user)
            request.viewing_as = viewing is not None
        context = {"path": path, "title": "Sidan finns inte"}
        body = loader.render_to_string("errors/404_portal.html", context, request)
        return HttpResponse(body, status=404)

    is_offer = path.startswith("/offert/")
    context = {
        "path": path,
        "is_offer": is_offer,
        "suggestions": [] if is_offer else suggestions_for(path),
        "page_color": PAGE_COLOR,
    }
    body = loader.render_to_string("errors/404_site.html", context, request)
    return HttpResponse(body, status=404)


def server_error(request):
    try:
        return _standalone("500", 500)
    except Exception:
        return HttpResponse(
            "Något gick fel hos oss. Försök igen om en stund.",
            status=500,
            content_type="text/plain; charset=utf-8",
        )


def preview(request, code):
    """Bara under DEBUG (config/urls.py): titta på felsidorna utan att stänga av DEBUG."""
    if code == "404":
        return not_found(request, path=request.GET.get("path") or "/finns-inte/")
    if code == "500":
        return server_error(request)
    if code in {"400", "403", "403_csrf"}:
        return _standalone(code, int(code[:3]))
    return not_found(request)


def email_preview(request, name):
    """Bara under DEBUG (config/urls.py): kundmejlens HTML med påhittade uppgifter."""
    from apps.projects.emails import preview as render_email

    result = render_email(name)
    if result is None:
        return not_found(request)
    return HttpResponse(result[1])
