"""
Länkvärdarna k.adx.se och klick.adx.se (README E.1) och, från S2-länkbyggaren,
reglerna för utskickens länkar (E.3, E.8).

Värdroutern (foundation, ändras inte av andra):

    LinkHostMiddleware          direkt efter SecurityMiddleware: på en länkvärd
                                svarar bara config.urls_links
    link_hosts()                UTSKICK_LINK_HOSTS som mängd, gemener
    link_host_kind(host)        "k" (sms), "klick" (mejl) eller "" (inte en länkvärd)
    host_of_scope(scope)        Host-huvudet ur ett ASGI-scope, utan port
    on_link_host(kind)          dekorator: vyn finns bara på den värden (annars 404)
    private(response)           Referrer-Policy no-referrer och ingen cache
                                (302:orna till målet, pixeln, .ics; E.1)
    site_urls()                 reverse() mot sajtens adresser inne i en
                                förfrågan på länkvärden (länk-byggaren)
    sms_link(code)              "k.adx.se/Ab12Cd": så står länken i sms:et
    email_url, unsubscribe_url, email_preferences_url, web_view_url,
    pixel_url, calendar_url, mailto_unsubscribe
                                S3: mejlens adresser på klick.adx.se (se avsnittet nedan)
    named_link_url, named_link_text, clean_named_slug, snippet_version,
    snippet_body, snippet_integrity, snippet_url, beacon_url, snippet_tag
                                S4: namngivna länkar och skriptet på egen sajt

På en länkvärd sätter mellanvaran request.urlconf = "config.urls_links",
request.is_link_host = True och request.link_host ("k" eller "klick"), och
varje svar får X-Robots-Tag: noindex, nofollow. Inget annat av sajten
svarar där: varken /manage/, /flamingo/, /kund/ eller MCP (asgi_app
svarar 404 på MCP och OAuth innan Django). Inga kakor sätts av vyerna där
(ingen {% csrf_token %} i templates/utskick/links/; POST:ar bär en signerad
formulärnonce, E.1).

Länkbyggarens del (S2, länk-byggaren enligt S2-HANDOFF.md) står under
rubriken "Länkarnas regler" längre ned: GLOBAL_HOSTS, clean_external,
host_status, request_host, add_link, link_problems, check_destinations,
build_destination och rollup. Retentionen för klicken och koderna (E.7)
sköts av retention.purge_s2 (utskick_daily).
"""

import ipaddress
import logging
import re
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import Http404
from django.http.request import split_domain_port
from django.urls import get_urlconf, set_urlconf
from django.utils.cache import add_never_cache_headers

logger = logging.getLogger(__name__)

#: Urlconf för länkvärdarna.
LINK_URLCONF = "config.urls_links"
KIND_SMS = "k"
KIND_EMAIL = "klick"


# ---------------------------------------------------------------------------
# Värdroutern
# ---------------------------------------------------------------------------


def link_hosts():
    """UTSKICK_LINK_HOSTS som mängd i gemener (lista eller kommasträng).
    Läses vid varje anrop, så att override_settings gäller i testerna."""
    hosts = getattr(settings, "UTSKICK_LINK_HOSTS", ()) or ()
    if isinstance(hosts, str):
        hosts = hosts.split(",")
    return {str(host).strip().lower().rstrip(".") for host in hosts if str(host).strip()}


def _base_host(name):
    base = getattr(settings, name, "") or ""
    return (urlsplit(base).hostname or "").lower()


def link_host_kind(host):
    """ "k" för sms-värden, "klick" för e-postvärden, "" när host inte är en
    länkvärd. host får ha port. Vilken som är vilken avgörs av
    UTSKICK_SMS_LINK_BASE och UTSKICK_EMAIL_LINK_BASE, annars av första
    ledet (k.adx.se, klick.localhost)."""
    domain, _port = split_domain_port(str(host or "").lower())
    domain = domain.rstrip(".")
    if not domain or domain not in link_hosts():
        return ""
    if domain == _base_host("UTSKICK_SMS_LINK_BASE"):
        return KIND_SMS
    if domain == _base_host("UTSKICK_EMAIL_LINK_BASE"):
        return KIND_EMAIL
    return KIND_SMS if domain.split(".", 1)[0] == KIND_SMS else KIND_EMAIL


def host_of_scope(scope):
    """Host-huvudet ur ett ASGI-scope (utan port, gemener), eller ""."""
    for name, value in scope.get("headers") or ():
        if name in (b"host", "host"):
            raw = value.decode("latin-1") if isinstance(value, bytes) else str(value)
            domain, _port = split_domain_port(raw.strip().lower())
            return domain
    return ""


class LinkHostMiddleware:
    """Direkt efter SecurityMiddleware (README C.3). På en länkvärd routas
    förfrågan genom config.urls_links, och bara där."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            host = request.get_host()
        except DisallowedHost:
            # Resten av kedjan svarar 400 som för vilken okänd värd som helst.
            return self.get_response(request)
        kind = link_host_kind(host)
        if not kind:
            return self.get_response(request)
        request.urlconf = LINK_URLCONF
        request.is_link_host = True
        request.link_host = kind
        response = self.get_response(request)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


def on_link_host(kind):
    """Vyn finns bara på länkvärden kind ("k" eller "klick"); 404 annars,
    också på adx.se (urls_links nås bara via mellanvaran, men vyn ska inte
    lita på det)."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if getattr(request, "link_host", "") != kind:
                raise Http404
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def private(response):
    """Omdirigeringen till målet, pixeln och .ics-filen (E.1): koden i
    adressen får inte följa med till målet som Referer, och svaret sparas
    ingenstans."""
    response["Referrer-Policy"] = "no-referrer"
    add_never_cache_headers(response)
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


@contextmanager
def site_urls():
    """reverse() mot sajtens adresser (ROOT_URLCONF) också inne i en
    förfrågan på en länkvärd, där request.urlconf är urls_links (till
    exempel landningssidans adress i klickets 302 och integritetstexten)."""
    previous = get_urlconf()
    set_urlconf(None)
    try:
        yield
    finally:
        set_urlconf(previous)


def sms_link(code, path=""):
    """Länken som den står i sms:et, utan schema: "k.adx.se/Ab12Cd",
    "k.adx.se/s/Ab12Cd" (path="s"). iOS och Android gör den klickbar."""
    base = getattr(settings, "UTSKICK_SMS_LINK_BASE", "") or "https://k.adx.se"
    parts = urlsplit(base)
    host = parts.netloc or parts.path
    prefix = f"{path.strip('/')}/" if path else ""
    return f"{host}/{prefix}{code}"


# ---------------------------------------------------------------------------
# S3 (foundation): adresserna i mejlen, på klick.adx.se (E.1, E.2, F.4)
#
#   email_link_base()                         UTSKICK_EMAIL_LINK_BASE utan / sist
#   email_url(utskick, recipient, link)       /m/<token>: klicket (recipient None = testmejl)
#   unsubscribe_url(account_id, value_hash)   /a/<token>: avregistreringen och List-Unsubscribe
#   email_preferences_url(account_id, value_hash)   /v/<token>: Mina utskick för e-post
#   web_view_url(utskick_id, recipient_id=None)     /w/<token>: Visa i webbläsaren
#   pixel_url(recipient_id)                   /o/<token>.gif: öppningen (bara tracking_ok, H.5)
#   calendar_url(utskick_id, block_id)        /c/<token>.ics: händelseblocket
#   mailto_unsubscribe(account_id, recipient_id)
#                                             mailto:s+u...@svar.utskick.adx.se?subject=avregistrera
#
# Absoluta adresser med schema (mejl har ingen bas). Inga rader i
# databasen: allt bärs av token (tokens.py). Vyerna står i link_views.py.
# ---------------------------------------------------------------------------


def email_link_base():
    """https://klick.adx.se (lokalt http://klick.localhost:8770)."""
    base = getattr(settings, "UTSKICK_EMAIL_LINK_BASE", "") or "https://klick.adx.se"
    return base.rstrip("/")


def email_url(utskick, recipient, link):
    """Klicklänken i mejlet för en mottagare och en TrackedLink (F.4).
    recipient None: testmejlet, där klicket leder rätt men inte räknas."""
    from . import tokens

    recipient_id = getattr(recipient, "pk", None) or 0
    link_id = getattr(link, "pk", link)
    return f"{email_link_base()}/m/{tokens.email_click_token(recipient_id, link_id)}"


def unsubscribe_url(account_id, value_hash):
    from . import tokens

    return f"{email_link_base()}/a/{tokens.unsubscribe_token(account_id, value_hash)}"


def email_preferences_url(account_id, value_hash):
    """Samma token som /utskick/val/<token>/ på adx.se (S1), här på klick."""
    from . import tokens

    token = tokens.preference_token(account_id, "email", value_hash)
    return f"{email_link_base()}/v/{token}"


def web_view_url(utskick_id, recipient_id=None):
    from . import tokens

    return f"{email_link_base()}/w/{tokens.web_view_token(utskick_id, recipient_id)}"


def pixel_url(recipient_id):
    from . import tokens

    return f"{email_link_base()}/o/{tokens.pixel_token(recipient_id)}.gif"


def calendar_url(utskick_id, block_id):
    from . import tokens

    return f"{email_link_base()}/c/{tokens.calendar_token(utskick_id, block_id)}.ics"


def mailto_unsubscribe(account_id, recipient_id):
    """Den andra adressen i List-Unsubscribe (D.6): ett mejl dit avregistrerar
    (inbound/email.py, token u)."""
    from . import tokens

    address = tokens.reply_address(tokens.MAILTO, account_id, recipient_id)
    return f"mailto:{address}?subject=avregistrera"


# ---------------------------------------------------------------------------
# S4 (foundation): namngivna länkar och skriptet på egen sajt (E.1, E.6)
#
#   NAMED_SLUG_RE, clean_named_slug(raw) -> str    slugens form, eller LinkRefused
#   named_link_url(link, public_slug=None)  "https://klick.adx.se/exempelror/vinter"
#   named_link_text(link, public_slug=None) "klick.adx.se/exempelror/vinter" (Kopiera, listan)
#   SNIPPET_SOURCE                          static/utskick/s.js (källan, länk-byggaren i S4)
#   snippet_version() -> "1a2b3c4d"         första 8 hex av källans sha256, "" utan fil
#   snippet_body(ver) -> bytes | None       filen för en version: den aktuella, eller en
#                                           sparad static/utskick/s.<ver>.js vars hash stämmer
#   snippet_integrity(ver=None) -> "sha384-..."   SRI för exakt de byte som serveras
#   snippet_url(ver=None)                   "https://klick.adx.se/s.1a2b3c4d.js"
#   beacon_url()                            "https://klick.adx.se/v"
#   snippet_tag(site)                       hela <script>-taggen för installationstexten
#
# Skriptets adress bär versionen, och taggen bär SRI-hashen: en ändring i
# s.js ger en ny adress och en ny hash, och en installerad tagg med den
# gamla versionen får 404 tills kunden klistrar in den nya. Spara därför den
# gamla filen som static/utskick/s.<gammal version>.js när s.js ändras (då
# serveras den vidare, snippet_body prövar hashen). Vyerna står i
# link_views.py (snippet, snippet_beacon och named).
# ---------------------------------------------------------------------------

#: En namngiven länks slug: små bokstäver, siffror och bindestreck, 1 till
#: 40 tecken, inget bindestreck först eller sist (klick.adx.se/<konto>/<slug>).
NAMED_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
NAMED_SLUG_TEXT = "Adressen får bara ha små bokstäver, siffror och bindestreck."

#: Skriptets källa under static/ (E.6).
SNIPPET_SOURCE = "utskick/s.js"
#: Filernas byte per process, nycklade på (sökväg, mtime, storlek).
_FILE_CACHE = {}


def clean_named_slug(raw):
    """Slugen för en namngiven länk, i gemener, eller LinkRefused med texten
    för kunden. Att den är ledig hos kontot prövar vyn (villkoret
    utskick_link_slug)."""
    slug = str(raw or "").strip().lower()
    if not NAMED_SLUG_RE.match(slug):
        raise LinkRefused(NAMED_SLUG_TEXT)
    return slug


def _public_slug(link, public_slug):
    if public_slug:
        return public_slug
    from .models import UtskickSettings

    return (
        UtskickSettings.objects.filter(account_id=link.account_id)
        .values_list("public_slug", flat=True)
        .first()
        or ""
    )


def named_link_url(link, public_slug=None):
    """Den namngivna länkens adress med schema (QR-koden, mejl, kvitton)."""
    return f"{email_link_base()}/{_public_slug(link, public_slug)}/{link.slug}"


def named_link_text(link, public_slug=None):
    """Som named_link_url, utan schema: så står den i listan och i Kopiera."""
    url = named_link_url(link, public_slug)
    return url.split("://", 1)[-1]


def _static_root():
    from pathlib import Path

    return Path(settings.BASE_DIR) / "static"


def _read(path):
    """Filens byte, cachade per process tills filen ändras (mtime, storlek),
    eller None när den saknas."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _FILE_CACHE:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if len(_FILE_CACHE) > 16:
            _FILE_CACHE.clear()
        _FILE_CACHE[key] = data
    return _FILE_CACHE[key]


def _version_of(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()[:8]


def snippet_version():
    """Versionen i skriptets adress: de första 8 hex av sha256 för
    static/utskick/s.js, eller "" när filen saknas."""
    data = _read(_static_root() / SNIPPET_SOURCE)
    return _version_of(data) if data is not None else ""


def snippet_body(ver):
    """Byte att servera för klick.adx.se/s.<ver>.js, eller None (404): den
    aktuella källan när versionen stämmer, annars en sparad
    static/utskick/s.<ver>.js vars innehåll har just den versionen."""
    ver = str(ver or "")
    if not re.fullmatch(r"[0-9a-f]{8}", ver):
        return None
    current = _read(_static_root() / SNIPPET_SOURCE)
    if current is not None and _version_of(current) == ver:
        return current
    archived = _read(_static_root() / "utskick" / f"s.{ver}.js")
    if archived is not None and _version_of(archived) == ver:
        return archived
    return None


def snippet_integrity(ver=None):
    """SRI-värdet (sha384, base64) för versionens byte, "" utan fil."""
    import base64
    import hashlib

    data = snippet_body(ver or snippet_version())
    if data is None:
        return ""
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def snippet_url(ver=None):
    """https://klick.adx.se/s.<version>.js"""
    return f"{email_link_base()}/s.{ver or snippet_version()}.js"


def beacon_url():
    """Besöksanropets adress: https://klick.adx.se/v (E.6)."""
    return f"{email_link_base()}/v"


def snippet_tag(site):
    """Taggen kunden klistrar in i sin <head> (E.6), för en SiteSnippet."""
    from django.utils.html import format_html

    return str(
        format_html(
            '<script src="{}" integrity="{}" crossorigin="anonymous" data-k="{}" async></script>',
            snippet_url(),
            snippet_integrity(),
            site.key,
        )
    )


# ---------------------------------------------------------------------------
# Länkarnas regler (S2, länk-byggaren; README E.3, E.7, E.8, F.5)
#
#   GLOBAL_HOSTS                          värdar utan granskning (värd, sökväg)
#   LinkRefused(text)                     ValueError med svensk text för vyn
#   HostPending(host, status)             värden väntar på ADX (eller är ny)
#   clean_external(account, url, *, allow_pending=False) -> str
#   host_status(account, host) -> "allowed" | "pending" | "refused" | "new"
#   own_site(account) -> str              kundens webbplats ur kundregistret, eller ""
#   request_host(account, host, user) -> AllowedHost
#   request_if_new(account, url, user)    en ny värd blir väntande (och byrån larmas)
#   add_link(utskick, *, key, campaign=None, destination="", label="", user=None,
#            request_new=True, preselect="")
#   link_problems(utskick) -> list[str]   Granska och förkontrollerna blockerar (I.6)
#   destination_ok(link) -> bool          klicket: får länken fortfarande gå dit?
#   check_destinations(account, urls) -> {url: True | False | None}
#   build_destination(link, recipient=None, click=None) -> str
#   bare_destination(link) -> str         HEAD och förhandsvisningen: utan ut och utm
#
# Förvälj svar (Giovanni 2026-10-10): en länk till en Flamingo-sida kan bära
# ?val=<fråga>.<alternativ> (apps/flamingo/answers.py), så att svaret redan
# är ikryssat i sidans formulär. Det står i TrackedLink.destination
# (".../lp/<slug>/?val=tjanst.reparation"); add_link och de namngivna
# länkarna godtar bara ett alternativ som finns på kampanjens sida nu
# (answers.preselect_choices, PRESELECT_TEXT annars), och bare_destination
# låter val följa med som ankaret. Sidan själv litar aldrig på det mer än
# som ett förval.
#   rollup(now, deadline=None) -> dict    ticken: TrackedLink.human_clicks och leads
#
# Externa adresser (E.8) måste vara absoluta http(s)-adresser med ett
# domännamn: ingen IP-adress, ingen annan port än 80 och 443, inget
# användarnamn, ingen förkortningstjänst, ingen omdirigering hos de fria
# värdarna (google.com/url, l.facebook.com) och inte länkvärdarna själva.
# Punktleden i sökvägen (/maps/../url) löses upp innan något prövas. gclid,
# gbraid och wbraid tas bort (de hör till Googles annonser). Värdar utan
# granskning: kundens webbplats i ADX kundregister (own_site: Customer.website,
# som bara byrån skriver, aldrig FlamingoAccount.website_url som kunden själv
# skriver), verifierade avsändardomäner (S3) och GLOBAL_HOSTS, alla med
# underdomäner; inte skriptets domäner (S4, se own_domains). Ett offentligt
# suffix (github.io, co.uk) är aldrig kundens eget. Annars en AllowedHost som
# byrån godkänner på kundkortet; Granska, förkontrollerna och klicket
# blockerar tills dess (destination_ok). Koderna bär aldrig adresser, så det
# finns ingen öppen omdirigering.
# ---------------------------------------------------------------------------


#: Värdar som får länkas utan byråns granskning, med underdomäner. Sökvägen
#: begränsar värden när den inte är tom (google.com bara för /maps).
GLOBAL_HOSTS = (
    ("google.com", "/maps"),
    ("maps.app.goo.gl", ""),
    ("g.page", ""),
    ("search.google.com", ""),
    ("facebook.com", ""),
    ("instagram.com", ""),
    ("linkedin.com", ""),
    ("youtube.com", ""),
    ("tiktok.com", ""),
    ("reco.se", ""),
)
#: Förkortningstjänster: målet syns inte och kan bytas efteråt (E.8).
SHORTENERS = frozenset(
    {
        "bit.ly",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "is.gd",
        "buff.ly",
        "rebrand.ly",
        "cutt.ly",
        "shorturl.at",
    }
)
#: Rena omdirigeringar hos de fria värdarna: målet syns inte (E.8).
REDIRECT_HOSTS = frozenset({"l.facebook.com", "lm.facebook.com", "l.instagram.com"})
#: Sökvägar som omdirigerar hos de fria värdarna (första ledet).
REDIRECT_PATHS = frozenset({"url", "redirect", "redir", "l.php", "link", "away"})
#: Offentliga suffix och värdar som många delar (ett urval ur Public Suffix
#: List plus webbhotell med kunderna i sökvägen). En kunds webbplats får
#: aldrig vara en av dem: då blev allt under dem "kundens eget".
SHARED_HOSTS = frozenset(
    {
        # Sverige (andranivådomäner i Public Suffix List).
        *(f"{letter}.se" for letter in "abcdefghiklmnoprstuwxyz"),
        "ac.se",
        "bd.se",
        "brand.se",
        "fh.se",
        "fhsk.se",
        "fhv.se",
        "komforb.se",
        "kommunalforbund.se",
        "komvux.se",
        "lanbib.se",
        "mil.se",
        "naturbruksgymn.se",
        "org.se",
        "parti.se",
        "pp.se",
        "press.se",
        "sshn.se",
        "tm.se",
        # Andra länders andranivå.
        "co.uk",
        "org.uk",
        "me.uk",
        "ltd.uk",
        "plc.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "co.jp",
        "co.za",
        "com.br",
        "com.cn",
        "com.tr",
        "com.mx",
        "co.in",
        "co.kr",
        "com.sg",
        "com.hk",
        "com.pl",
        "com.es",
        "co.il",
        # Webbhotell och plattformar där kunderna har underdomäner.
        "github.io",
        "gitlab.io",
        "herokuapp.com",
        "netlify.app",
        "vercel.app",
        "pages.dev",
        "workers.dev",
        "web.app",
        "firebaseapp.com",
        "appspot.com",
        "azurewebsites.net",
        "cloudfront.net",
        "amazonaws.com",
        "s3.amazonaws.com",
        "blogspot.com",
        "wordpress.com",
        "wixsite.com",
        "wixstudio.io",
        "squarespace.com",
        "webflow.io",
        "framer.app",
        "framer.website",
        "myshopify.com",
        "weebly.com",
        "jimdosite.com",
        "jimdofree.com",
        "simplesite.com",
        "carrd.co",
        "glitch.me",
        "onrender.com",
        "fly.dev",
        "ngrok.io",
        "ngrok-free.app",
        "notion.site",
        "business.site",
    }
)
#: Plattformar med kunderna i sökvägen (sites.google.com/view/x,
#: facebook.com/x): ingen värd under dem är en kunds egen.
PATH_TENANT_HOSTS = frozenset(
    {
        "google.com",
        "goo.gl",
        "g.page",
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "linktr.ee",
        "youtube.com",
        "tiktok.com",
        "reco.se",
        "bokadirekt.se",
    }
)
REDIRECT_TEXT = "Länken går till en omdirigering. Använd adressen till sidan den leder till."

#: Tas bort ur externa adresser (Googles klick-id:n, och adx: en inklistrad
#: adress med en mottagares adx skulle annars ge varje besök utan sparat klick,
#: till exempel ett testsms, till den mottagaren). ut tas inte bort: den gäller
#: bara på Flamingo-sidorna, och en annan sajt kan ha en egen parameter ut.
STRIP_PARAMS = frozenset({"gclid", "gbraid", "wbraid", "adx"})
URL_MAX = 500
KEY_MAX = 40
_KEY_RE = re.compile(r"^[a-z0-9åäö][a-z0-9åäö_-]{0,39}$")

PENDING_TEXT = "Väntar på ADX: länkar till nya webbplatser godkänns av ADX."
EMPTY_TEXT = "Skriv adressen till sidan."
ABSOLUTE_TEXT = "Skriv hela adressen, med https:// först."
TOO_LONG_TEXT = "Adressen är för lång."
USERINFO_TEXT = "Adressen får inte innehålla användarnamn eller lösenord."
IP_TEXT = "Skriv adressen med ett domännamn, inte en IP-adress."
PORT_TEXT = "Adressen får inte ha ett eget portnummer."
SHORTENER_TEXT = "Använd hela adressen, inte en förkortad länk."
LINK_HOST_TEXT = "Länken kan inte gå till en annan utskickslänk."
KEY_TEXT = "Länkens namn får bara ha små bokstäver, siffror och bindestreck."
CAMPAIGN_TEXT = "Sidan finns inte hos dig."
LOCKED_TEXT = "Utskicket går inte att ändra nu."
PRESELECT_TEXT = "Välj ett svar från sidan du valde, eller Inget förval."

STATUS_ALLOWED = "allowed"
STATUS_PENDING = "pending"
STATUS_REFUSED = "refused"
STATUS_NEW = "new"

#: Länkkollen (F.5): trådar, total tid, högst antal adresser, cache och gräns.
CHECK_WORKERS = 4
CHECK_BUDGET = 15.0
CHECK_MAX_URLS = 20
CHECK_CACHE_SECONDS = 600
CHECK_RUNS_PER_HOUR = 10
CHECK_MAX_BYTES = 16384
CHECK_TIME_LIMIT = 5

#: Uppräkningen av TrackedLink tittar så långt bakåt (ticken och dygnet).
ROLLUP_WINDOW = timedelta(days=2)
ROLLUP_BATCH = 500


class LinkRefused(ValueError):
    """Länken går inte att använda; str(exc) är texten för kunden."""


class HostPending(LinkRefused):
    """Värden väntar på ADX godkännande (E.8). status är "pending" (en rad
    finns) eller "new" (ingen rad än: request_host skapar den)."""

    def __init__(self, host, status=STATUS_PENDING):
        super().__init__(PENDING_TEXT)
        self.host = host
        self.status = status


def refused_text(host):
    return f"ADX har inte godkänt länkar till {host}."


def normalize_host(host):
    """Värden i gemener och IDNA (xn--), utan punkt sist. "" om den inte går
    att läsa."""
    text = str(host or "").strip().lower().rstrip(".")
    if not text:
        return ""
    try:
        return text.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _under(host, domain):
    return bool(domain) and (host == domain or host.endswith("." + domain))


def _is_ip(host):
    """IP-adresser och allt som ser ut som en (sista ledet bara siffror:
    127.1, 2130706433). Inga toppdomäner är siffror."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    return host.rsplit(".", 1)[-1].isdigit()


def _global(host, path):
    for domain, prefix in GLOBAL_HOSTS:
        if _under(host, domain) and (not prefix or _path_under(path, prefix)):
            return True
    return False


def _path_under(path, prefix):
    """ "/maps" och "/maps/..." ligger under "/maps", "/mapsx" gör det inte."""
    path = path or "/"
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _shortener(host):
    return any(_under(host, domain) for domain in SHORTENERS)


def _redirector(host, path):
    """En ren omdirigering hos en fri värd (l.facebook.com, google.com/url,
    youtube.com/redirect): målet syns inte, så den granskas aldrig fri."""
    if host in REDIRECT_HOSTS:
        return True
    if not any(_under(host, domain) for domain, _prefix in GLOBAL_HOSTS):
        return False
    first = (path or "/").lstrip("/").split("/", 1)[0].lower()
    return first in REDIRECT_PATHS


def resolve_dots(path):
    """Sökvägen med punktleden upplösta (RFC 3986 remove_dot_segments, också
    %2e): "/maps/../url" blir "/url", så att prefixen prövas på det
    webbläsaren faktiskt öppnar."""
    path = path or "/"
    text = re.sub(r"%2e", ".", path, flags=re.IGNORECASE)
    if "." not in text:
        return path
    segments = text.split("/")
    out = []
    for segment in segments[1:] if text.startswith("/") else segments:
        if segment == "..":
            if out:
                out.pop()
        elif segment != ".":
            out.append(segment)
    resolved = "/" + "/".join(out)
    if segments[-1] in (".", "..") and not resolved.endswith("/"):
        resolved += "/"
    return resolved


def is_shared_host(host):
    """Ett offentligt suffix (SHARED_HOSTS), en värd under en plattform med
    kunderna i sökvägen (PATH_TENANT_HOSTS), eller något som inte ens är ett
    domännamn med två led."""
    host = normalize_host(host).removeprefix("www.")
    if not host or "." not in host or _is_ip(host):
        return True
    if host in SHARED_HOSTS or any(_under(host, domain) for domain in PATH_TENANT_HOSTS):
        return True
    labels = host.split(".")
    # Två bokstäver sist och co/com/org före: co.uk, com.au (scan.registrable).
    return (
        len(labels) == 2
        and len(labels[1]) == 2
        and labels[0] in ("co", "com", "org", "net", "ac", "gov", "edu")
    )


def own_site(account):
    """Kundens webbplats som värd, utan www, eller "". Bara adressen i ADX
    kundregister (Customer.website), som byrån skriver: adressen kunden
    själv skrev i Flamingo (FlamingoAccount.website_url) sparas innan något
    läses och utan ägarkontroll, så den gör ingen värd fri. Aldrig ett
    offentligt suffix, en delad värd eller länkvärdarna."""
    customer = getattr(account, "customer", None)
    raw = str(getattr(customer, "website", "") or "").strip()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "https://" + raw
    try:
        host = normalize_host(urlsplit(raw).hostname)
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    if not host or is_shared_host(host) or host in link_hosts():
        return ""
    return host


def own_domains(account):
    """Kundens egna domäner, utan granskning: webbplatsen i kundregistret
    (own_site) och verifierade avsändardomäner (S3). www. räknas inte.

    Skriptets domäner (S4, SiteSnippet) räknas inte: kunden skriver domänen
    själv, och besöksanropet som sätter last_seen_at går att göra med vilken
    Origin som helst utanför en webbläsare, så en skriptdomän bevisar inget
    ägande. En skriptdomän som inte redan är kundens egen granskas av byrån
    som vilken värd som helst (S4-HANDOFF.md, avvikelser)."""
    domains = set()
    site = own_site(account)
    if site:
        domains.add(site)
    from django.apps import apps

    for label, filters in (("SenderDomain", {"status": "verified"}),):
        try:
            model = apps.get_model("utskick", label)
        except LookupError:
            continue
        rows = model.objects.filter(account=account, **filters).values_list("domain", flat=True)
        domains.update(normalize_host(d) for d in rows if d)
    domains.discard("")
    return {d for d in domains if not is_shared_host(d)}


def _reviewless(account, host, path):
    if _global(host, path):
        return True
    return any(_under(host, domain) for domain in own_domains(account))


def host_status(account, host):
    """ "allowed", "pending", "refused" eller "new" för en värd (normaliserad
    eller inte). google.com utan sökväg räknas inte som fri (bara /maps)."""
    from .models import AllowedHost

    host = normalize_host(host)
    if not host:
        return STATUS_NEW
    if _reviewless(account, host, ""):
        return STATUS_ALLOWED
    row = AllowedHost.objects.filter(account=account, host=host).only("status").first()
    if row is None:
        return STATUS_NEW
    if row.status == AllowedHost.Status.APPROVED:
        return STATUS_ALLOWED
    if row.status == AllowedHost.Status.REFUSED:
        return STATUS_REFUSED
    return STATUS_PENDING


def clean_external(account, url, *, allow_pending=False):
    """En extern adress som får användas i ett utskick, rensad (E.8).
    LinkRefused med texten för kunden när reglerna inte tillåter den, eller
    när byrån nekat värden. HostPending när värden väntar på byrån eller är
    ny, utom med allow_pending (add_link sparar länken ändå, och Granska
    blockerar tills värden är godkänd)."""
    from django.core.exceptions import ValidationError

    from apps.common.security import validate_url

    text = str(url or "").strip()
    if not text:
        raise LinkRefused(EMPTY_TEXT)
    if len(text) > URL_MAX:
        raise LinkRefused(TOO_LONG_TEXT)
    try:
        validate_url(text, max_length=URL_MAX)
    except ValidationError:
        raise LinkRefused(ABSOLUTE_TEXT) from None
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise LinkRefused(ABSOLUTE_TEXT)
    if "@" in parts.netloc:
        raise LinkRefused(USERINFO_TEXT)
    try:
        port = parts.port
    except ValueError:
        raise LinkRefused(PORT_TEXT) from None
    if port not in (None, 80, 443):
        raise LinkRefused(PORT_TEXT)
    raw_host = parts.hostname or ""
    if raw_host.startswith("[") or ":" in raw_host or _is_ip(raw_host):
        raise LinkRefused(IP_TEXT)
    host = normalize_host(raw_host)
    if not host or "." not in host:
        raise LinkRefused(ABSOLUTE_TEXT)
    if _is_ip(host):
        raise LinkRefused(IP_TEXT)
    if host in link_hosts():
        raise LinkRefused(LINK_HOST_TEXT)
    query = parts.query
    pairs = parse_qsl(query, keep_blank_values=True)
    if any(key.lower() in STRIP_PARAMS for key, _value in pairs):
        query = urlencode([(k, v) for k, v in pairs if k.lower() not in STRIP_PARAMS])
    default_port = 443 if scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = resolve_dots(parts.path or "/")
    cleaned = urlunsplit((scheme, netloc, path, query, parts.fragment))
    if len(cleaned) > URL_MAX:
        raise LinkRefused(TOO_LONG_TEXT)
    if _redirector(host, path):
        raise LinkRefused(REDIRECT_TEXT)
    if _reviewless(account, host, path):
        return cleaned
    if _shortener(host):
        raise LinkRefused(SHORTENER_TEXT)
    status = host_status(account, host)
    if status == STATUS_ALLOWED:
        return cleaned
    if status == STATUS_REFUSED:
        raise LinkRefused(refused_text(host))
    if allow_pending:
        return cleaned
    raise HostPending(host, status)


def request_host(account, host, user):
    """Värden blir en väntande AllowedHost (eller raden som redan finns), och
    byrån larmas en gång per kund och värd. Kunden mejlas aldrig."""
    from django.db import IntegrityError, transaction

    from . import alerts
    from .models import AllowedHost

    host = normalize_host(host)
    if not host:
        raise LinkRefused(ABSOLUTE_TEXT)
    requester = user if getattr(user, "is_authenticated", False) else None
    try:
        with transaction.atomic():
            row, created = AllowedHost.objects.get_or_create(
                account=account,
                host=host,
                defaults={"status": AllowedHost.Status.PENDING, "requested_by": requester},
            )
    except IntegrityError:
        row, created = AllowedHost.objects.get(account=account, host=host), False
    if created:
        logger.info("Utskick: länkvärd %s väntar på godkännande (konto %s)", row.pk, account.pk)
        customer = getattr(account, "customer", None)
        alerts.agency(
            "Utskick: en länk väntar på godkännande",
            [
                f"{getattr(customer, 'name', '') or f'Konto {account.pk}'} vill länka till "
                f"{host} i ett utskick.",
                "Godkänn eller neka länken på kundkortet (Kontakter och utskick) "
                "eller på /manage/utskick/. Utskicket kan inte skickas innan dess.",
            ],
            once=f"host:{account.pk}:{host}"[:80],
            window="day",
        )
    return row


def _clean_key(key):
    text = str(key or "").strip().lower()
    if not _KEY_RE.match(text):
        raise LinkRefused(KEY_TEXT)
    return text


def request_if_new(account, url, user):
    """En extern adress (redan rensad av clean_external) till en värd som
    varken är fri eller har en rad: värden blir väntande och byrån larmas
    (request_host). Returnerar AllowedHost-raden, eller None."""
    parts = urlsplit(str(url or ""))
    host = normalize_host(parts.hostname)
    if not host or _reviewless(account, host, parts.path):
        return None
    if host_status(account, host) != STATUS_NEW:
        return None
    return request_host(account, host, user)


def add_link(
    utskick,
    *,
    key,
    campaign=None,
    destination="",
    label="",
    user=None,
    request_new=True,
    preselect="",
):
    """Länken {länk:<key>} i utskicket: en Flamingo-sida (campaign, kontots
    egen) eller en extern adress (E.8). Finns nyckeln redan ersätts länken.
    En ny extern värd sparas som väntande (request_host) och Granska
    blockerar tills byrån godkänt den. Bara medan utskicket går att ändra.
    request_new=False: den som anropar begär värden själv efteråt
    (request_if_new), utanför sitt radlås, eftersom larmet är ett mejl.
    preselect ("tjanst.reparation") förväljer ett svar i sidans formulär
    (?val=); det ska finnas på kampanjens sida, annars PRESELECT_TEXT."""
    from apps.flamingo import answers as form_answers
    from apps.flamingo.exports import landing_page_url

    from .models import TrackedLink, Utskick

    if utskick.status not in Utskick.EDITABLE:
        raise LinkRefused(LOCKED_TEXT)
    key = _clean_key(key)
    account = utskick.account
    if campaign is not None:
        if campaign.account_id != utskick.account_id:
            raise LinkRefused(CAMPAIGN_TEXT)
        kind = TrackedLink.Kind.LP
        cleaned = landing_page_url(campaign)
        preselect = str(preselect or "").strip()
        if preselect:
            if preselect not in dict(form_answers.preselect_choices(campaign)):
                raise LinkRefused(PRESELECT_TEXT)
            cleaned = form_answers.with_preselect(cleaned, preselect)
    else:
        kind = TrackedLink.Kind.EXTERNAL
        cleaned = clean_external(account, destination, allow_pending=True)
        if request_new:
            request_if_new(account, cleaned, user or utskick.created_by)
    link, _created = TrackedLink.objects.update_or_create(
        utskick=utskick,
        key=key,
        defaults={
            "account": account,
            "kind": kind,
            "campaign": campaign,
            "destination": cleaned[:URL_MAX],
            "label": str(label or "")[:120],
        },
    )
    return link


def destination_ok(link):
    """Får klicket fortfarande gå till länkens mål (E.3, E.8)? En extern
    adress prövas igen med clean_external: en värd som byrån nekat eller som
    väntar, en omdirigering, eller en värd som inte längre är kundens egen
    ger False (klicket svarar "Länken har gått ut"). Flamingo-sidor är
    alltid kontots egna."""
    if link.kind != link.Kind.EXTERNAL:
        return True
    try:
        clean_external(link.account, link.destination)
    except LinkRefused:
        return False
    return True


def link_problems(utskick):
    """Det som stoppar utskicket i Granska och i förkontrollerna (I.6, D.3,
    E.8): värdar som väntar på ADX och värdar som ADX nekat, en text per sak."""
    from .models import TrackedLink

    problems = []
    pending = False
    for link in TrackedLink.objects.filter(utskick=utskick, kind=TrackedLink.Kind.EXTERNAL):
        try:
            clean_external(utskick.account, link.destination)
        except HostPending:
            pending = True
        except LinkRefused as exc:
            text = str(exc)
            if text not in problems:
                problems.append(text)
    if pending:
        problems.insert(0, PENDING_TEXT)
    return problems


def bare_destination(link):
    """Målet utan ut och utm (HEAD, E.3). En Flamingo-sida behåller ankaret
    som länken i mejlet hade (#boka, S3) och förvalet i formuläret
    (?val=tjanst.reparation, answers.preselect_of)."""
    if link.kind == link.Kind.LP and link.campaign_id and link.campaign is not None:
        from apps.flamingo import answers as form_answers
        from apps.flamingo.exports import landing_page_url

        with site_urls():
            url = landing_page_url(link.campaign)
        preselect = form_answers.preselect_of(link.destination)
        if preselect:
            url = form_answers.with_preselect(url, preselect)
        fragment = urlsplit(link.destination or "").fragment
        return f"{url}#{fragment}" if fragment else url
    return link.destination


def _with_params(url, params, replace):
    """url med params tillagda. replace=True byter ut samma nycklar;
    replace=False låter kundens egna värden stå kvar."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if replace:
        names = {name for name, _value in params}
        pairs = [(k, v) for k, v in pairs if k not in names]
        pairs.extend(params)
    else:
        present = {k for k, _value in pairs}
        pairs.extend((k, v) for k, v in params if k not in present)
    if not pairs:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def build_destination(link, recipient=None, click=None):
    """Adressen klicket skickas vidare till (E.3). Flamingo-sidor får ut
    (klickets token, bara med ett sparat klick) och utm; externa adresser
    behåller sin fråga och sitt fragment och får utm när add_utm är på
    (kundens egna utm-värden står kvar)."""
    from . import tokens

    channel = getattr(recipient, "channel", "") or getattr(click, "channel", "") or "sms"
    medium = "email" if channel == "email" else "sms"
    utm = []
    if link.utskick_id:
        utm = [
            ("utm_source", "flamingo"),
            ("utm_medium", medium),
            ("utm_campaign", f"utskick-{link.utskick_id}"),
        ]
    if link.kind == link.Kind.LP:
        params = list(utm)
        if click is not None and click.pk:
            params.insert(0, ("ut", tokens.ut_token(click.pk)))
        return _with_params(bare_destination(link), params, replace=True)
    # --- S4 (länk-byggaren): adx= till kundens egen sajt med skriptet (E.3, E.6).
    # Bara med ett sparat klick och bara när målets värd är en skriptdomän (eller
    # en underdomän) vars skript redan har setts (last_seen_at); annars som förut.
    url = link.destination if not link.add_utm else _with_params(link.destination, utm, False)
    if click is not None and click.pk and adx_wanted(link):
        url = _with_params(url, [("adx", tokens.adx_token(click.pk))], replace=True)
    # --- slut S4
    return url


def _check_one(url):
    from apps.tools import analyzer

    try:
        analyzer.fetch(url, max_bytes=CHECK_MAX_BYTES, time_limit=CHECK_TIME_LIMIT)
    except Exception:  # noqa: BLE001 - "svarar inte", aldrig felet
        return False
    return True


def check_destinations(account, urls):
    """Svarar adresserna (F.5, varning i Granska)? {url: True | False | None}.
    None när adressen inte kontrollerades (gränsen per konto och timme, eller
    tiden tog slut). Bara http(s), via apps.tools.analyzer.fetch (SSRF-skydd
    på varje hopp), fyra åt gången, högst CHECK_MAX_URLS, cache i tio
    minuter. Demokontot kontrollerar inget (påhittade adresser): True.
    Kroppen och felet visas aldrig."""
    import hashlib
    import time
    from concurrent.futures import ThreadPoolExecutor, wait

    from django.core.cache import cache

    from . import limits

    wanted = []
    for url in urls or ():
        text = str(url or "").strip()
        if text and text not in wanted and urlsplit(text).scheme in ("http", "https"):
            wanted.append(text)
    wanted = wanted[:CHECK_MAX_URLS]
    if not wanted:
        return {}
    if getattr(account, "is_demo", False):
        return dict.fromkeys(wanted, True)

    def cache_key(text):
        return "utskick-linkcheck:" + hashlib.sha256(text.encode()).hexdigest()

    results = {}
    todo = []
    for text in wanted:
        cached = cache.get(cache_key(text))
        if cached is None:
            todo.append(text)
        else:
            results[text] = bool(cached)
    if todo and limits.hit(
        "link_check", str(account.pk), limits.hour_window(), CHECK_RUNS_PER_HOUR
    ):
        todo_results = {}
    elif todo:
        todo_results = {}
        deadline = time.monotonic() + CHECK_BUDGET
        pool = ThreadPoolExecutor(max_workers=CHECK_WORKERS)
        try:
            futures = {pool.submit(_check_one, text): text for text in todo}
            done, _pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
            for future in done:
                text = futures[future]
                todo_results[text] = bool(future.result())
                cache.set(cache_key(text), int(todo_results[text]), CHECK_CACHE_SECONDS)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    else:
        todo_results = {}
    for text in wanted:
        if text not in results:
            results[text] = todo_results.get(text)
    return results


def rollup(now, deadline=None):
    """TrackedLink.human_clicks och leads ur Click och Lead (E.3) för länkar
    med klick eller förfrågningar de senaste ROLLUP_WINDOW. Siffrorna går
    aldrig nedåt: när retentionen tagit klicken (13 månader) står summorna
    kvar. deadline är time.monotonic() då ticken vill gå vidare."""
    import time

    from django.db.models import Count, Q
    from django.db.models.functions import Greatest

    from apps.flamingo.models import Lead

    from .models import Click, TrackedLink

    since = now - ROLLUP_WINDOW
    ids = set(
        Click.objects.filter(at__gte=since, link__isnull=False)
        .values_list("link_id", flat=True)
        .distinct()
    )
    # --- S4 (länk-byggaren): förfrågningarna via en namngiven länk har inget
    # utskick (Lead.utskick null), men attribution["channel"] är "named".
    tracked = Q(utskick__isnull=False) | Q(attribution__channel=Click.Channel.NAMED)
    # --- slut S4
    for value in (
        Lead.objects.filter(tracked, created_at__gte=since)
        .values_list("attribution__link", flat=True)
        .distinct()
    ):
        if isinstance(value, int):
            ids.add(value)
    ids = sorted(ids)
    updated = 0
    for start in range(0, len(ids), ROLLUP_BATCH):
        if deadline is not None and time.monotonic() > deadline:
            break
        batch = ids[start : start + ROLLUP_BATCH]
        clicks = dict(
            Click.objects.filter(link_id__in=batch, kind=Click.Kind.HUMAN)
            .values("link_id")
            .annotate(n=Count("pk"))
            .values_list("link_id", "n")
        )
        leads = {}
        for value in Lead.objects.filter(tracked, attribution__link__in=batch).values_list(
            "attribution__link", flat=True
        ):
            if isinstance(value, int):
                leads[value] = leads.get(value, 0) + 1
        for link_id in batch:
            updated += TrackedLink.objects.filter(pk=link_id).update(
                human_clicks=Greatest("human_clicks", clicks.get(link_id, 0)),
                leads=Greatest("leads", leads.get(link_id, 0)),
            )
    return {"links": updated} if updated else {}


# --- S4 (länk-byggaren): skriptdomänerna och lägena i Länkar (E.3, E.6, E.8) ---
#
#   seen_snippet_domains(account_id) -> set     domäner vars skript har setts
#   adx_wanted(link) -> bool                    får klicket adx=? (build_destination)
#   destination_states(account, links) -> {pk: (läge, text)}
#                                               läget för Länkar: "allowed", "pending"
#                                               eller "refused" (med texten), två frågor
#                                               för hela listan i stället för per länk
#
# Klicket prövar fortfarande med destination_ok (clean_external), som är
# det som gäller; destination_states är bara det listan visar.


def seen_snippet_domains(account_id):
    """Kontots skriptdomäner (SiteSnippet) där skriptet har rapporterat
    (last_seen_at satt): bara dit får länkar adx= (E.3)."""
    from .models import SiteSnippet

    rows = SiteSnippet.objects.filter(account_id=account_id, last_seen_at__isnull=False)
    return {normalize_host(d) for d in rows.values_list("domain", flat=True) if d}


def adx_wanted(link):
    """Går en extern länk till en skriptdomän (eller en underdomän) vars
    skript har setts? Då får klicket adx=<token> (E.3, E.6)."""
    if link.kind != link.Kind.EXTERNAL or not link.account_id:
        return False
    try:
        host = normalize_host(urlsplit(link.destination or "").hostname)
    except ValueError:
        return False
    if not host:
        return False
    return any(_under(host, domain) for domain in seen_snippet_domains(link.account_id))


def destination_states(account, tracked_links):
    """{länkens pk: (läge, text)} för de externa länkarna i listan: samma
    regler som clean_external (omdirigeringar, förkortare, fria värdar,
    kundens egna domäner och byråns beslut) med en fråga för de egna
    domänerna och en för AllowedHost. Flamingo-sidor är alltid "allowed"."""
    from .models import AllowedHost

    own = None
    decided = None
    out = {}
    for link in tracked_links:
        if link.kind != link.Kind.EXTERNAL:
            out[link.pk] = (STATUS_ALLOWED, "")
            continue
        try:
            parts = urlsplit(link.destination or "")
            host = normalize_host(parts.hostname)
        except ValueError:
            out[link.pk] = (STATUS_REFUSED, ABSOLUTE_TEXT)
            continue
        path = resolve_dots(parts.path or "/")
        if not host or host in link_hosts():
            out[link.pk] = (STATUS_REFUSED, ABSOLUTE_TEXT)
            continue
        if _redirector(host, path):
            out[link.pk] = (STATUS_REFUSED, REDIRECT_TEXT)
            continue
        if _global(host, path):
            out[link.pk] = (STATUS_ALLOWED, "")
            continue
        if own is None:
            own = own_domains(account)
            decided = dict(
                AllowedHost.objects.filter(account=account).values_list("host", "status")
            )
        if any(_under(host, domain) for domain in own):
            out[link.pk] = (STATUS_ALLOWED, "")
        elif _shortener(host):
            out[link.pk] = (STATUS_REFUSED, SHORTENER_TEXT)
        elif decided.get(host) == AllowedHost.Status.APPROVED:
            out[link.pk] = (STATUS_ALLOWED, "")
        elif decided.get(host) == AllowedHost.Status.REFUSED:
            out[link.pk] = (STATUS_REFUSED, refused_text(host))
        else:
            out[link.pk] = (STATUS_PENDING, PENDING_TEXT)
    return out


# --- slut S4 ---
