"""
Felrapporteringen till Sentry - och vad som ALDRIG får följa med dit.

adx.se har adresser som i sig är behörigheter: offertlänken
(/offert/<token>/ ger rätten att acceptera), AI-koden (/aiz/guide/?kod=),
den delade statusnyckeln (headern X-ADX-Key, samma på alla sajter vi
driftar), SMS-API:ts nycklar (adxsms_...) och leveransadresserna för sms
(/api/sms/46elks/dlr/<id>/<signatur>/). Sentry tar med adress, query-sträng,
headers och lokala variabler i varje händelse, så utan den här filen hamnar
de hos en tredje part.

Lokala variabler behålls (de gör en felrapport användbar, test_sentry.py),
men variabler med namnen i SECRET_NAMES maskas: där ligger också sms:ens
mottagare och texter (to, body, data). När en ram i händelsen kommer från
apps/sms, apps/utskick, landningssidorna eller inkorgen tas de lokala
variablerna bort i VARJE ram: där bär nästan varje variabel ett nummer, en
adress, en text eller en nyckel, under namn som inte går att lista, och
ramarna under dem (databasens params, formulärens data) bär samma sak.
Nummer och e-postadresser maskas dessutom var de än står.

Tre lager, eftersom Sentrys eget skydd bara täcker det första till hälften:

1. EventScrubber med våra headernamn. Den matchar på EXAKT namn i gemener
   med bindestreck - "x_adx_key" träffar inte, "X-ADX-Key" gör det.
2. scrub_event går igenom HELA händelsen och maskar på mönster: scrubbern
   tittar aldrig i url eller query_string, och en token kan lika gärna stå
   i ett loggmeddelande eller en lokal variabel.
3. Samma funktion på before_send OCH before_send_transaction. Spårningen
   (traces_sample_rate) skickar request-data för vart tionde anrop även när
   inget gått fel, och before_send körs inte på de händelserna.

Dessutom: inga personuppgifter (send_default_pii=False) och inga
formulärkroppar (max_request_body_size="never") - förfrågningar, ärenden
och offertaccepter innehåller namn, e-post och fritext från kunder.
"""

import json
import re
from pathlib import Path

FILTERED = "[Filtered]"

#: Headers som bär hemligheter, utöver Sentrys egen lista.
SECRET_HEADERS = ["X-ADX-Key", "X-ADX-Code", "developer-token", "X-goog-api-key"]

#: Formulärfält och variabelnamn som bär hemligheter, utöver Sentrys lista
#: (som redan har password, token, secret, session, csrf ...).
SECRET_NAMES = [
    "kod",
    "login_code",
    "adx_status_key",
    "status_key",
    # Google Ads (apps/flamingo/google_ads.py).
    "refresh_token",
    "access_token",
    "id_token",
    "client_secret",
    "refresh_token_encrypted",
    # Googles API-nycklar (PageSpeed, Chrome UX Report i apps/monitor).
    "api_key",
    "pagespeed_api_key",
    "crux_api_key",
    # SMS-API:t (apps/sms): mottagare, text och anropets kropp, och
    # leveransadressens signatur.
    "to",
    "body",
    "data",
    "signature",
    # Kontakter och utskick (apps/utskick, README C.3): adresser, namn,
    # sammanslagna fält, samtyckets bevis, sms- och mejltexter, råa
    # inkommande meddelanden och SQL-parametrarna (django.db bär dem i params).
    "address",
    "phone",
    "email",
    "first_name",
    "last_name",
    "merge",
    "text_shown",
    "evidence",
    "message",
    "raw",
    "params",
]

#: Moduler vars lokala variabler aldrig skickas (se ovan). Finns en enda ram
#: från någon av dem i händelsen töms ALLA ramars variabler: ramarna i
#: django.db bär frågans params och ramarna i django.forms formulärets data,
#: och de ligger under utskickens, sms:ens, landningssidans och inkorgens ramar.
_NO_LOCALS_MODULES = (
    "apps.sms.",
    "apps.utskick.",
    "apps.flamingo.public_views",
    "apps.flamingo.app_views.inbox",
)

_PATTERNS = [
    # Offertlänken: token i sökvägen.
    (re.compile(r"(/offert/)[A-Za-z0-9_-]{16,}"), r"\1" + FILTERED),
    # AI-koderna, var de än står.
    (re.compile(r"\bADX-[A-Z0-9]{4}-[A-Z0-9]{4}\b"), FILTERED),
    # Hemligheter i query-strängar: ?kod=..., &key=..., token=...
    (
        re.compile(r"(?i)((?:^|[?&\s\"'])(?:kod|key|token|secret|signature)=)[^&\s\"']+"),
        r"\1" + FILTERED,
    ),
    # Googles nycklar var de än står (apps/flamingo/google_ads.py): access
    # token, refresh token, OAuth-klientens hemlighet och urlkodade fält.
    (re.compile(r"ya29\.[\w.\-]+|1//[\w.\-]{10,}|GOCSPX-[\w\-]+"), FILTERED),
    # Googles API-nycklar (PAGESPEED_API_KEY, CRUX_API_KEY): AIza...
    (re.compile(r"AIza[\w\-]{20,}"), FILTERED),
    (
        re.compile(r"(?i)\b((?:refresh_token|access_token|client_secret|id_token)=)[^&\s\"']+"),
        r"\1" + FILTERED,
    ),
    # SMS-API:ts nycklar (apps/sms/models.SmsApiKey), var de än står.
    (re.compile(r"adxsms_[A-Za-z0-9_\-]{8,}"), FILTERED),
    # Leveransadressen för ett sms: signaturen i sökvägen.
    (re.compile(r"(/api/sms/46elks/dlr/\d+/)[0-9a-f]{32}"), r"\1" + FILTERED),
    # Utskick (apps/utskick, README C.3). Länkarna i sökvägen är behörigheter:
    # bekräftelsen och Mina utskick, 46elks inkommande, k.adx.se och klick.adx.se.
    (re.compile(r"(/utskick/(?:bekrafta|val)/)[A-Za-z0-9._-]{16,}"), r"\1" + FILTERED),
    (re.compile(r"(/api/utskick/46elks/inkommande/)[A-Za-z0-9_-]{24,}"), r"\1" + FILTERED),
    # Länkarna på k.adx.se och klick.adx.se, också utan schema som de står i
    # sms:en (k.adx.se/a8Kf2X, S2).
    (
        re.compile(r"((?:https?://)?\b(?:k|klick)\.adx\.se/)[^\s\"'<>]+"),
        r"\1" + FILTERED,
    ),
    # S3: mejlens adresser på klick.adx.se när bara sökvägen står (en logg,
    # request.path): /m/, /a/, /v/, /w/, /o/ och /c/ följt av en token med
    # punkt. Och den egna svarsadressens bekräftelselänk i verktyget.
    (re.compile(r"(/[mavwoc]/)[A-Za-z0-9_-]+\.[A-Za-z0-9._-]{6,}"), r"\1" + FILTERED),
    (re.compile(r"(/svarsadress/)[A-Za-z0-9._-]{16,}"), r"\1" + FILTERED),
    # Svarsadressen per mottagare (Reply-To) och utskickens API-nycklar.
    (re.compile(r"\bs\+[a-z0-9.]+@svar\.utskick\.adx\.se\b"), FILTERED),
    (re.compile(r"adxut_[A-Za-z0-9_\-]{8,}"), FILTERED),
    # Mottagarens token (ut=, adx=) och kontaktsökningarna (q= i Kontakter,
    # sok= bland utskickets mottagare) i query-strängar. Sentry sparar
    # query_string utan "?", därav ^.
    (re.compile(r"((?:^|[?&\s\"'])(?:ut|adx|q|sok)=)[^&\s\"']+"), r"\1" + FILTERED),
    # Kontakternas nummer och adresser, var de än står: E.164, svenska
    # mobilnummer som de skrivs och e-postadresser.
    (re.compile(r"\+\d{8,15}"), FILTERED),
    (re.compile(r"\b07\d[\d -]{6,10}\d\b"), FILTERED),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), FILTERED),
]

#: Anrop som inte ska spåras alls: maskinanrop var femte minut som annars
#: äter kvoten, statusanropet som bär den delade nyckeln, och 46elks
#: leveransrapporter (en per sms, med signaturen i adressen).
_UNTRACED_PREFIXES = (
    "/healthz",
    "/status/",
    "/static/",
    "/media/",
    "/favicon",
    "/api/sms/46elks/",
    # Utskick: 46elks inkommande och de publika sidorna (tokens i adressen).
    "/api/utskick/",
    "/utskick/",
    # Kontakternas och utskickens sidor i verktyget (sökningar och namn i
    # adresser och svar).
    "/flamingo/app/kontakter/",
    "/flamingo/app/utskick/",
)
#: Slutet på landningssidornas besöksanrop (/lp/<slug>/besok/, README C.2).
_UNTRACED_SUFFIXES = ("/besok/",)


def scrub_text(value):
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _walk(node):
    if isinstance(node, str):
        return scrub_text(node)
    if isinstance(node, dict):
        return {key: _walk(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_walk(value) for value in node]
    if isinstance(node, tuple):
        return tuple(_walk(value) for value in node)
    return node


def _frames(event):
    """Ramarna i händelsens undantag och trådar, hur de än är formade."""
    for key in ("exception", "threads"):
        container = event.get(key)
        values = container.get("values") if isinstance(container, dict) else None
        for item in values if isinstance(values, list) else []:
            stack = item.get("stacktrace") if isinstance(item, dict) else None
            frames = stack.get("frames") if isinstance(stack, dict) else None
            for frame in frames if isinstance(frames, list) else []:
                if isinstance(frame, dict):
                    yield frame


def _drop_locals(event):
    """Inga lokala variabler alls när en ram kommer från _NO_LOCALS_MODULES."""
    frames = list(_frames(event))
    if any(str(frame.get("module") or "").startswith(_NO_LOCALS_MODULES) for frame in frames):
        for frame in frames:
            if "vars" in frame:
                frame["vars"] = {}
    return event


def scrub_event(event, hint=None):
    """before_send och before_send_transaction. Får aldrig själv fälla en rapport."""
    try:
        return _drop_locals(_walk(event))
    except Exception:  # noqa: BLE001 - hellre ingen rapport än en omaskad
        return None


def _host(environ, scope):
    host = environ.get("HTTP_HOST") or ""
    if not host:
        for name, value in scope.get("headers") or ():
            if name in (b"host", "host"):
                host = value.decode("latin-1") if isinstance(value, bytes) else str(value)
                break
    return host.split(":", 1)[0].strip().lower()


def _link_hosts():
    """Utskickens länkvärdar (k.adx.se, klick.adx.se; S2). Tomt före S2."""
    try:
        from django.conf import settings

        hosts = getattr(settings, "UTSKICK_LINK_HOSTS", ()) or ()
    except Exception:  # noqa: BLE001 - spårningen får aldrig fälla ett anrop
        return set()
    if isinstance(hosts, str):
        hosts = hosts.split(",")
    return {str(host).strip().lower() for host in hosts if str(host).strip()}


def traces_sampler(sampling_context):
    environ = sampling_context.get("wsgi_environ") or {}
    scope = sampling_context.get("asgi_scope") or {}
    path = environ.get("PATH_INFO") or scope.get("path") or ""
    if path.startswith(_UNTRACED_PREFIXES) or path.endswith(_UNTRACED_SUFFIXES):
        return 0.0
    if _host(environ, scope) in _link_hosts():
        return 0.0
    return 0.1


def _release(base_dir):
    """Deployens revision ur release.json, så Sentry visar vilken deploy som införde felet."""
    try:
        rev = json.loads((Path(base_dir).parent / "release.json").read_text()).get("rev")
    except (OSError, ValueError, AttributeError):
        return None
    return f"adx@{rev}" if rev else None


def options(dsn, environment, base_dir):
    """Argumenten till sentry_sdk.init - en funktion, så testerna kör exakt samma."""
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

    return {
        "dsn": dsn,
        "environment": environment,
        "release": _release(base_dir),
        "send_default_pii": False,
        "max_request_body_size": "never",
        "traces_sampler": traces_sampler,
        "event_scrubber": EventScrubber(
            denylist=DEFAULT_DENYLIST + SECRET_HEADERS + SECRET_NAMES, recursive=True
        ),
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
    }


def init(dsn, environment, base_dir):
    import sentry_sdk

    sentry_sdk.init(**options(dsn, environment, base_dir))
