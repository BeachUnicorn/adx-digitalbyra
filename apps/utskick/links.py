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
#            request_new=True)
#   link_problems(utskick) -> list[str]   Granska och förkontrollerna blockerar (I.6)
#   destination_ok(link) -> bool          klicket: får länken fortfarande gå dit?
#   check_destinations(account, urls) -> {url: True | False | None}
#   build_destination(link, recipient=None, click=None) -> str
#   bare_destination(link) -> str         HEAD och förhandsvisningen: utan ut och utm
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
# skriver), verifierade avsändardomäner (S3), skriptets domäner (S4) och
# GLOBAL_HOSTS, alla med underdomäner. Ett offentligt suffix (github.io,
# co.uk) är aldrig kundens eget. Annars en AllowedHost som byrån godkänner på
# kundkortet; Granska, förkontrollerna och klicket blockerar tills dess
# (destination_ok). Koderna bär aldrig adresser, så det finns ingen öppen
# omdirigering.
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

#: Tas bort ur externa adresser (Googles klick-id:n).
STRIP_PARAMS = frozenset({"gclid", "gbraid", "wbraid"})
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
    (own_site), verifierade avsändardomäner (S3) och skriptets domäner (S4).
    www. räknas inte."""
    domains = set()
    site = own_site(account)
    if site:
        domains.add(site)
    from django.apps import apps

    for label, filters in (
        ("SenderDomain", {"status": "verified"}),
        ("SiteSnippet", {}),
    ):
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


def add_link(utskick, *, key, campaign=None, destination="", label="", user=None, request_new=True):
    """Länken {länk:<key>} i utskicket: en Flamingo-sida (campaign, kontots
    egen) eller en extern adress (E.8). Finns nyckeln redan ersätts länken.
    En ny extern värd sparas som väntande (request_host) och Granska
    blockerar tills byrån godkänt den. Bara medan utskicket går att ändra.
    request_new=False: den som anropar begär värden själv efteråt
    (request_if_new), utanför sitt radlås, eftersom larmet är ett mejl."""
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
    """Målet utan ut och utm (HEAD, E.3)."""
    if link.kind == link.Kind.LP and link.campaign_id and link.campaign is not None:
        from apps.flamingo.exports import landing_page_url

        with site_urls():
            return landing_page_url(link.campaign)
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
    if not link.add_utm:
        return link.destination
    return _with_params(link.destination, utm, replace=False)


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

    from django.db.models import Count
    from django.db.models.functions import Greatest

    from apps.flamingo.models import Lead

    from .models import Click, TrackedLink

    since = now - ROLLUP_WINDOW
    ids = set(
        Click.objects.filter(at__gte=since, link__isnull=False)
        .values_list("link_id", flat=True)
        .distinct()
    )
    for value in (
        Lead.objects.filter(created_at__gte=since, utskick__isnull=False)
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
        for value in Lead.objects.filter(
            utskick__isnull=False, attribution__link__in=batch
        ).values_list("attribution__link", flat=True):
            if isinstance(value, int):
                leads[value] = leads.get(value, 0) + 1
        for link_id in batch:
            updated += TrackedLink.objects.filter(pk=link_id).update(
                human_clicks=Greatest("human_clicks", clicks.get(link_id, 0)),
                leads=Greatest("leads", leads.get(link_id, 0)),
            )
    return {"links": updated} if updated else {}
