"""
Google Ads API (REST) för ADX Flamingo: den enda modulen som pratar HTTP med
Google Ads, Data Manager API (konverteringarna) och Googles inloggning
(OAuth).

Alla anrop görs som ADX: genom förvaltarkontot (MCC, headern
login-customer-id) och med den inloggning byrån gjort i /manage/
(GoogleAdsConnection, nyckeln krypterad i databasen), eller med
GOOGLE_ADS_REFRESH_TOKEN i miljön, som vinner. Kunden loggar aldrig in hos
Google genom oss.

Åtkomsten till API:t hör till Google Cloud-projektet som äger OAuth-klienten
(Test, Explorer, Basic, Standard). Utvecklartoken är avvecklad sedan
2026-09-09: Google bortser från headern. Den skickas bara om
GOOGLE_ADS_DEVELOPER_TOKEN är satt, och krävs inte.

Andra moduler bygger sina anrop på request(), search() och mutate() och
fångar GoogleAdsError. Felets message är skriven på svenska för byrån, säger
vad som ska göras och innehåller aldrig en nyckel.

Data Manager API (datamanager_request) har en egen värd och en egen
behörighet (DATAMANAGER_SCOPE) men samma inloggning, samma kortlivade nyckel
och samma skydd. Behörigheterna Google gav sparas när byrån kopplar och när
nyckeln förnyas (GoogleAdsConnection.granted_scopes), så att
datamanager_scope_state() vet om konverteringarna får skickas den vägen.

Samma inloggning ber också om läsbehörighet för Search Console
(WEBMASTERS_SCOPE) och Google Business Profile (BUSINESS_SCOPE), som
övervakningen använder (apps/monitor/google_api.py, med access_token() och
scope_state()). De anropen har sina egna värdar och går inte genom _http()
här; Flamingo påverkas inte av dem.

Säkerhet:

- Bara fasta adresser hos Google (ALLOWED_HOSTS), alltid https, en
  tidsgräns och ett tak för svarets storlek.
- Nycklarna (refresh token, access token, klienthemligheten, en eventuell
  utvecklartoken) loggas aldrig och står aldrig i ett felmeddelande: varje text från Google
  tvättas (_scrub) innan den sparas, loggas eller visas. De hålls inte heller
  i lokala variabler när ett fel kastas (Sentry tar med lokala variabler).
- Den kortlivade nyckeln (access token) cachas i Djangos cache under sin
  livstid minus en marginal, under ett namn som bara är en hash.

Demokonton (FlamingoAccount.is_demo) anropar aldrig Google: anropa
ensure_not_demo(account) först i varje flöde som gäller en kund.

Tester: patcha apps.flamingo.google_ads.urlopen. Allt HTTP går genom _http().
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import GoogleAdsConnection, normalize_scopes, token_fingerprint

logger = logging.getLogger(__name__)

API_HOST = "googleads.googleapis.com"
OAUTH_HOST = "oauth2.googleapis.com"
#: Data Manager API, konverteringarna (google_conversions.upload_via_data_manager).
#: developers.google.com/data-manager/api/reference/rest/v1/events/ingest
DATAMANAGER_HOST = "datamanager.googleapis.com"
DATAMANAGER_VERSION = "v1"
#: De enda värdarna modulen anropar. Inloggningssidan (AUTH_URL) anropas
#: aldrig härifrån: byråns webbläsare skickas dit.
ALLOWED_HOSTS = frozenset({API_HOST, OAUTH_HOST, DATAMANAGER_HOST})
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = f"https://{OAUTH_HOST}/token"
REVOKE_URL = f"https://{OAUTH_HOST}/revoke"
ADWORDS_SCOPE = "https://www.googleapis.com/auth/adwords"
#: Behörigheten för Data Manager API (Authorization scopes på events.ingest).
DATAMANAGER_SCOPE = "https://www.googleapis.com/auth/datamanager"
#: Search Console, bara läsning (övervakningen, apps/monitor/google_checks.py).
WEBMASTERS_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
#: Google Business Profile (övervakningen läser profil, statistik och omdömen).
BUSINESS_SCOPE = "https://www.googleapis.com/auth/business.manage"
SCOPE = f"{ADWORDS_SCOPE} {DATAMANAGER_SCOPE} {WEBMASTERS_SCOPE} {BUSINESS_SCOPE} openid email"

#: datamanager_scope_state(): har nyckeln som används behörigheten för Data
#: Manager API? Okänt när Google inte sagt det för just den nyckeln.
SCOPE_GRANTED = "granted"
SCOPE_MISSING = "missing"
SCOPE_UNKNOWN = "unknown"

#: Den senaste versionen 2026-10-03 (v25.2). Varje version har ett slutdatum
#: hos Google (Deprecation and sunset): byt innan dess, med
#: GOOGLE_ADS_API_VERSION eller här.
DEFAULT_VERSION = "v25"
TIMEOUT_SECONDS = 30
#: Tak för ett svar från Google Ads (en sida i en sökning är högst 10 000 rader).
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
#: Tak för svaren från inloggningen.
OAUTH_MAX_BYTES = 64 * 1024
#: Den kortlivade nyckeln cachas så här mycket kortare än Google säger.
TOKEN_MARGIN_SECONDS = 300
#: last_ok_at skrivs högst en gång på så här lång tid.
OK_TOUCH_SECONDS = 300
#: En sökning läser högst så här många sidor (skydd mot en evig loop).
MAX_PAGES = 200

STOCKHOLM = ZoneInfo("Europe/Stockholm")

_ACCESS_CACHE_PREFIX = "flamingo:google_ads:access:"
_OK_CACHE_KEY = "flamingo:google_ads:ok"
_FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}

#: Standardvärdet för login_customer_id: byråns förvaltarkonto ur
#: GOOGLE_ADS_LOGIN_CUSTOMER_ID. None skickar ingen header alls.
MCC = object()

#: Inställningarna som krävs, i den ordning missing_settings() listar dem.
#: Utvecklartoken är inte med: Google bortser från den sedan 2026-09-09.
REQUIRED_SETTINGS = (
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
)
#: Vad som saknas, i klartext för byråns sida.
SETTING_HELP = {
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "Förvaltarkontots id, tio siffror.",
    "GOOGLE_ADS_CLIENT_ID": (
        "OAuth-klientens id från Google Cloud-projektet där Google Ads API är påslaget."
    ),
    "GOOGLE_ADS_CLIENT_SECRET": "OAuth-klientens hemlighet från Google Cloud-projektet.",
    "GOOGLE_ADS_REFRESH_TOKEN": "Ingen inloggning hos Google: koppla ADX:s Google-konto.",
}

# ---------------------------------------------------------------------------
# Texterna för byrån
# ---------------------------------------------------------------------------

MSG_NOT_CONFIGURED = "Google Ads API är inte inkopplat: inställningar saknas i miljön."
MSG_NOT_CONNECTED = "ADX:s Google-konto är inte kopplat. Koppla det i panelen."
MSG_NO_CLIENT = (
    "OAuth-klienten saknas (GOOGLE_ADS_CLIENT_ID och GOOGLE_ADS_CLIENT_SECRET i miljön)."
)
MSG_DEMO = "Demokontot pratar aldrig med Google."
MSG_UNREACHABLE = "Google svarade inte. Försök igen senare."
MSG_TOO_LARGE = "Svaret från Google var större än väntat och lästes inte."
MSG_BAD_CUSTOMER_ID = "Google Ads-kontots id ska vara tio siffror."
MSG_RECONNECT = "Inloggningen hos Google gäller inte längre. Koppla ADX:s Google-konto igen."
MSG_INVALID_GRANT = (
    "Inloggningen hos Google gäller inte längre (återkallad, för gammal eller lösenordet "
    "bytt). Koppla ADX:s Google-konto igen."
)
MSG_CODE_REJECTED = (
    "Koden från Google gick inte att använda (för gammal eller redan använd). "
    "Koppla igen från början."
)
MSG_INVALID_CLIENT = (
    "Google känner inte igen OAuth-klienten. Kontrollera GOOGLE_ADS_CLIENT_ID och "
    "GOOGLE_ADS_CLIENT_SECRET."
)
MSG_NO_REFRESH_TOKEN = (
    "Google skickade ingen långlivad nyckel. Ta bort ADX ur Google-kontots appar med "
    "åtkomst och koppla igen."
)
MSG_SCOPE_MISSING = (
    "Behörigheten för Google Ads kryssades inte i. Koppla igen och låt rutan för "
    "Google Ads vara ikryssad."
)
MSG_QUOTA = "Google tar inte emot fler anrop just nu (kvoten är slut). Försök igen senare."
#: Åtkomsten hör till Google Cloud-projektet, inte till förvaltarkontot.
MSG_PROJECT_ACCESS = (
    "Google Cloud-projektet som OAuth-klienten hör till har bara Test-åtkomst och når inte "
    "riktiga konton. Ansök om Explorer på sidan Google Ads API Overview för projektet i "
    "Google Cloud Console (inte i API Center). Basic, som kräver varumärkesverifiering av "
    "projektet, behövs för att skapa konton åt kunder."
)
MSG_SERVICE_DISABLED = (
    "Google Ads API är inte påslaget i Google Cloud-projektet. Slå på det under API:er "
    "och tjänster i projektet som OAuth-klienten hör till."
)
MSG_DM_SERVICE_DISABLED = (
    "Data Manager API är inte påslaget i Google Cloud-projektet. Slå på det under API:er "
    "och tjänster i samma projekt som OAuth-klienten hör till."
)
MSG_DM_SCOPE_MISSING = (
    "Inloggningen saknar behörigheten för Data Manager API. Koppla om med Google för att "
    "skicka konverteringar."
)
MSG_DM_NOT_FOUND = "Data Manager API känner inte igen adressen."
#: Texterna för Data Manager API där Googles kod betyder något annat än för
#: Google Ads API.
DATAMANAGER_MESSAGES = {
    "SERVICE_DISABLED": MSG_DM_SERVICE_DISABLED,
    "PROJECT_DISABLED": MSG_DM_SERVICE_DISABLED,
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": MSG_DM_SCOPE_MISSING,
}

#: Googles felkoder (utan grupp) och vad byrån ska göra. Okända fel visas
#: med Googles egen text.
CODE_MESSAGES = {
    "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION": MSG_PROJECT_ACCESS,
    "DEVELOPER_TOKEN_NOT_APPROVED": MSG_PROJECT_ACCESS,
    "DEVELOPER_TOKEN_PROHIBITED": (
        "Google tar inte emot utvecklartoken med det här Google Cloud-projektet. Utvecklartoken "
        "behövs inte längre: ta bort GOOGLE_ADS_DEVELOPER_TOKEN ur miljön."
    ),
    "DEVELOPER_TOKEN_INVALID": (
        "Google tar inte emot utvecklartoken. Den behövs inte längre: ta bort "
        "GOOGLE_ADS_DEVELOPER_TOKEN ur miljön."
    ),
    "USER_PERMISSION_DENIED": (
        "Inloggningen når inte det här Google Ads-kontot. Kontrollera att kontot ligger "
        "under ADX förvaltarkonto, att GOOGLE_ADS_LOGIN_CUSTOMER_ID är förvaltarkontots id "
        "och att Google-kontot som kopplades har åtkomst till förvaltarkontot."
    ),
    "INVALID_LOGIN_CUSTOMER_ID_SERVING_CUSTOMER_ID_COMBINATION": (
        "Kontot ligger inte under ADX förvaltarkonto. Skicka en kopplingsinbjudan och låt "
        "kunden godkänna den först."
    ),
    "CUSTOMER_NOT_ENABLED": (
        "Google Ads-kontot är inte aktivt: det är inte färdigt, är avstängt eller stängt. "
        "Logga in i kontot hos Google och slutför eller återaktivera det."
    ),
    "ACCOUNT_NOT_SET_UP": (
        "Google Ads-kontot är inte färdigt. Logga in i kontot hos Google och slutför det."
    ),
    "ACTION_NOT_PERMITTED_FOR_SUSPENDED_ACCOUNT": (
        "Google har stängt av Google Ads-kontot. Logga in i kontot och se vad Google kräver."
    ),
    "CUSTOMER_NOT_FOUND": (
        "Google hittar inget Google Ads-konto med det id:t. Kontrollera kontots id "
        "(tio siffror) på kundkortet."
    ),
    "CLIENT_CUSTOMER_ID_INVALID": (
        "Google Ads-kontots id är inte giltigt. Skriv det som tio siffror."
    ),
    "NOT_ADS_USER": (
        "Google-kontot som kopplades har inget Google Ads-konto. Koppla med ett Google-konto "
        "som har åtkomst till ADX förvaltarkonto."
    ),
    "OAUTH_TOKEN_INVALID": MSG_RECONNECT,
    "OAUTH_TOKEN_EXPIRED": MSG_RECONNECT,
    "OAUTH_TOKEN_REVOKED": MSG_RECONNECT,
    "OAUTH_TOKEN_DISABLED": MSG_RECONNECT,
    "OAUTH_TOKEN_HEADER_INVALID": MSG_RECONNECT,
    "GOOGLE_ACCOUNT_COOKIE_INVALID": MSG_RECONNECT,
    "GOOGLE_ACCOUNT_DELETED": (
        "Google-kontot som kopplades finns inte längre. Koppla ett annat Google-konto."
    ),
    "TWO_STEP_VERIFICATION_NOT_ENROLLED": (
        "Google kräver tvåstegsverifiering för kontot som kopplades. Slå på den och koppla igen."
    ),
    "MISSING_TOS": (
        "Villkoren för Google Ads API är inte godkända. Godkänn dem i Google Cloud Console "
        "för projektet."
    ),
    "PROJECT_DISABLED": MSG_SERVICE_DISABLED,
    "SERVICE_DISABLED": MSG_SERVICE_DISABLED,
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": MSG_SCOPE_MISSING,
    "RESOURCE_EXHAUSTED": MSG_QUOTA,
    "RESOURCE_TEMPORARILY_EXHAUSTED": MSG_QUOTA,
    "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION": MSG_QUOTA,
    "EXCESSIVE_LONG_TERM_QUERY_RESOURCE_CONSUMPTION": MSG_QUOTA,
}
#: Koder som slutar så här: kontot eller kopplingen är inte aktiv.
MSG_NOT_ACTIVE = (
    "Kontot eller kopplingen är inte aktiv hos Google. Kontrollera kontot i Google Ads och "
    "att kopplingen till ADX förvaltarkonto är godkänd."
)

#: Fel som gäller ADX:s koppling (inte en enskild kund): sparas som
#: GoogleAdsConnection.last_error.
_CONNECTION_CODES = frozenset(
    {
        "DEVELOPER_TOKEN_NOT_APPROVED",
        "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION",
        "DEVELOPER_TOKEN_PROHIBITED",
        "DEVELOPER_TOKEN_INVALID",
        "NOT_ADS_USER",
        "OAUTH_TOKEN_INVALID",
        "OAUTH_TOKEN_EXPIRED",
        "OAUTH_TOKEN_REVOKED",
        "OAUTH_TOKEN_DISABLED",
        "OAUTH_TOKEN_HEADER_INVALID",
        "GOOGLE_ACCOUNT_COOKIE_INVALID",
        "GOOGLE_ACCOUNT_DELETED",
        "TWO_STEP_VERIFICATION_NOT_ENROLLED",
        "MISSING_TOS",
        "PROJECT_DISABLED",
        "SERVICE_DISABLED",
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        "invalid_grant",
        "invalid_client",
        "unauthorized_client",
    }
)

#: Nycklar som kan stå i en text från Google eller i ett undantag: access
#: token (ya29.), refresh token (1//), klienthemlighet (GOCSPX-), API-nyckel
#: (AIza), en Bearer-header och urlkodade nyckelfält.
_SECRET_PATTERNS = re.compile(
    r"ya29\.[\w.\-]+"
    r"|1//[\w.\-]{10,}"
    r"|GOCSPX-[\w\-]+"
    r"|AIza[\w\-]{20,}"
    r"|Bearer\s+\S+"
    r"|(?:refresh_token|access_token|client_secret|id_token|code)=[^&\s\"']+",
    re.I,
)
TEXT_MAX = 300


class GoogleAdsError(Exception):
    """Ett fel från Google (eller innan anropet). Allt här går att visa:

    message      svensk text för byrån, aldrig en nyckel (högst 300 tecken)
    status       Googles status ("PERMISSION_DENIED") eller vår egen
                 ("NOT_CONFIGURED", "DEMO", "UNAVAILABLE", "INVALID_CUSTOMER_ID")
    codes        ["authorizationError.USER_PERMISSION_DENIED", "oauth.invalid_grant", ...]
    errors       [{"code", "message", "index", "field_path"}] per fel; index är
                 operationens plats i anropet (mutate, uppladdning) eller None
    request_id   Googles id för anropet (för supporten), eller ""
    http_status  HTTP-status, eller None om inget svar kom
    """

    def __init__(
        self, message, *, status="", codes=None, errors=None, request_id="", http_status=None
    ):
        self.message = _scrub(message)[:TEXT_MAX] or "Okänt fel från Google."
        self.status = status or ""
        self.codes = list(codes or [])
        self.errors = list(errors or [])
        self.request_id = request_id or ""
        self.http_status = http_status
        super().__init__(self.message)

    def __str__(self):
        return self.message

    @property
    def code_names(self):
        """Koderna utan grupp: ["USER_PERMISSION_DENIED", ...]."""
        return [code.rsplit(".", 1)[-1] for code in self.codes]

    @property
    def is_auth_error(self):
        """Felet gäller ADX:s koppling (inloggningen, token, projektet), inte
        en enskild kund. Sparas på GoogleAdsConnection. Inte när
        konverteringarna stoppats för en väg (UPLOAD_NOT_ALLOWED): det
        stoppar bara den vägen, inte resten av synken."""
        if self.status == "UPLOAD_NOT_ALLOWED":
            return False
        if self.status == "UNAUTHENTICATED":
            return True
        return any(name in _CONNECTION_CODES for name in self.code_names)

    @property
    def is_quota_error(self):
        """Kvoten är slut: försök igen senare, inget är fel i anropet."""
        if self.status == "RESOURCE_EXHAUSTED" or self.http_status == 429:
            return True
        return any(CODE_MESSAGES.get(name) is MSG_QUOTA for name in self.code_names)


# ---------------------------------------------------------------------------
# Små hjälpare som andra moduler använder
# ---------------------------------------------------------------------------


def digits(customer_id):
    """'123-456-7890', 'customers/1234567890' eller 1234567890 blir '1234567890'."""
    return re.sub(r"\D", "", str(customer_id or ""))


def resource_id(resource_name):
    """Sista delen av ett resursnamn: 'customers/1/campaigns/22' blir '22'."""
    return str(resource_name or "").rstrip("/").rsplit("/", 1)[-1]


def to_micros(kronor):
    """Kronor som Googles mikros, som JSON-sträng (int64): 150 blir '150000000'."""
    return str(round(float(kronor) * 1_000_000))


def micros_to_kr(micros):
    """Googles mikros (sträng eller tal) som hela kronor."""
    try:
        return round(int(micros or 0) / 1_000_000)
    except (TypeError, ValueError):
        return 0


def gaql_string(value):
    """Ett värde som sträng i en GAQL-fråga, med citattecken och escape."""
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def google_datetime(moment):
    """En tidpunkt som Google vill ha den i uppladdningar, i svensk tid:
    '2026-10-03 14:05:00+02:00'."""
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, STOCKHOLM)
    local = moment.astimezone(STOCKHOLM)
    offset = local.strftime("%z")
    return local.strftime("%Y-%m-%d %H:%M:%S") + f"{offset[:3]}:{offset[3:]}"


def rfc3339(moment):
    """En tidpunkt i svensk tid som RFC 3339 med offset, på sekunden:
    '2026-10-03T14:05:00+02:00' (eventTimestamp i Data Manager API). Samma
    sekund som google_datetime och CSV-filen."""
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, STOCKHOLM)
    return moment.astimezone(STOCKHOLM).replace(microsecond=0).isoformat()


def ensure_not_demo(account):
    """Kastar GoogleAdsError för ett demokonto: det anropar aldrig Google."""
    if getattr(account, "is_demo", False):
        raise GoogleAdsError(MSG_DEMO, status="DEMO")


# ---------------------------------------------------------------------------
# Inställningarna
# ---------------------------------------------------------------------------


def _setting(name):
    return str(getattr(settings, name, "") or "").strip()


def api_version():
    version = _setting("GOOGLE_ADS_API_VERSION")
    return version if re.fullmatch(r"v\d{2,3}", version) else DEFAULT_VERSION


def mcc_id():
    """Byråns förvaltarkonto som siffror, eller ""."""
    return digits(_setting("GOOGLE_ADS_LOGIN_CUSTOMER_ID"))


def _stored_refresh_token():
    connection = GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).first()
    return connection.refresh_token() if connection else ""


def _current_refresh_token():
    """Miljöns nyckel vinner över den sparade. Får aldrig loggas."""
    return _setting("GOOGLE_ADS_REFRESH_TOKEN") or _stored_refresh_token()


def has_refresh_token():
    return bool(_current_refresh_token())


def oauth_configured():
    """OAuth-klienten finns: byrån kan logga in hos Google."""
    return bool(_setting("GOOGLE_ADS_CLIENT_ID") and _setting("GOOGLE_ADS_CLIENT_SECRET"))


def missing_settings():
    """Namnen på det som saknas (se SETTING_HELP), i REQUIRED_SETTINGS ordning.
    GOOGLE_ADS_REFRESH_TOKEN räknas som satt när byrån kopplat Google."""
    missing = []
    if len(mcc_id()) != 10:
        missing.append("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if not _setting("GOOGLE_ADS_CLIENT_ID"):
        missing.append("GOOGLE_ADS_CLIENT_ID")
    if not _setting("GOOGLE_ADS_CLIENT_SECRET"):
        missing.append("GOOGLE_ADS_CLIENT_SECRET")
    if not has_refresh_token():
        missing.append("GOOGLE_ADS_REFRESH_TOKEN")
    return missing


def is_configured():
    return not missing_settings()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _scrub(text, *secrets):
    """Texten utan nycklar och utan radbrytningar."""
    text = str(text or "")
    known = (
        _setting("GOOGLE_ADS_CLIENT_SECRET"),
        _setting("GOOGLE_ADS_DEVELOPER_TOKEN"),
        _setting("GOOGLE_ADS_REFRESH_TOKEN"),
        *secrets,
    )
    for secret in known:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    text = _SECRET_PATTERNS.sub("***", text)
    return " ".join(text.split())


def scrub(text):
    """Text från Google som en annan modul sparar: utan nycklar och utan
    radbrytningar."""
    return _scrub(text)


def _http(method, url, *, data=None, headers=None, max_bytes=MAX_RESPONSE_BYTES):
    """Ett anrop till Google. Returnerar (HTTP-status, svaret som dict).

    Ett svar med felstatus returneras (anroparen tolkar det); bara ett anrop
    som aldrig fick svar, eller ett för stort svar, kastar GoogleAdsError."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise GoogleAdsError("Adressen är inte en av Googles.", status="BAD_URL")
    request = Request(url, data=data, method=method, headers=dict(headers or {}))  # noqa: S310
    # Nycklarna finns nu bara i request (vars repr inte visar dem). Inga
    # lokala variabler med nycklar om något kastas nedan.
    data = headers = None
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            status = getattr(response, "status", None)
            raw = response.read(max_bytes + 1)
    except HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(max_bytes + 1)
        except Exception:  # noqa: BLE001 - ett trasigt felsvar är bara ett tomt svar
            raw = b""
        finally:
            exc.close()
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("Google Ads: %s %s nåddes inte (%s)", method, parts.path, type(exc).__name__)
        raise GoogleAdsError(MSG_UNREACHABLE, status="UNAVAILABLE") from None
    if not isinstance(status, int):
        status = 200
    if not isinstance(raw, bytes | bytearray):
        raw = b""
    if len(raw) > max_bytes:
        logger.warning("Google Ads: %s %s gav ett för stort svar", method, parts.path)
        raise GoogleAdsError(MSG_TOO_LARGE, status="TOO_LARGE", http_status=status)
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def _code_name(error_code):
    """{"authorizationError": "USER_PERMISSION_DENIED"} blir
    "authorizationError.USER_PERMISSION_DENIED"."""
    if isinstance(error_code, dict):
        for group, value in error_code.items():
            return f"{group}.{value}"
    return ""


def _location(location):
    """(index, "mutate_operations[3].campaign_operation.create") ur ett fels
    location."""
    elements = (location or {}).get("fieldPathElements") if isinstance(location, dict) else None
    index, path = None, []
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        name = str(element.get("fieldName") or "")
        at = element.get("index")
        if isinstance(at, int | str) and str(at).isdigit():
            if index is None:
                index = int(at)
            name += f"[{at}]"
        path.append(name)
    return index, ".".join(path)


def _message_for(code_names, status, http_status, fallback, overrides=None):
    for name in code_names:
        if overrides and name in overrides:
            return overrides[name]
        if name in CODE_MESSAGES:
            return CODE_MESSAGES[name]
        if name.endswith("NOT_ACTIVE"):
            return MSG_NOT_ACTIVE
    if status == "RESOURCE_EXHAUSTED" or http_status == 429:
        return MSG_QUOTA
    if status == "UNAUTHENTICATED" or http_status == 401:
        return MSG_RECONNECT
    if fallback:
        return f"Google: {fallback}"
    if http_status == 404:
        return (
            f"Google känner inte igen adressen. API-versionen ({api_version()}) kan vara fel: "
            "inte släppt än, eller avvecklad av Google. Se GOOGLE_ADS_API_VERSION."
        )
    if http_status and http_status >= 500:
        return f"Google svarade med ett fel (HTTP {http_status}). Försök igen senare."
    if http_status:
        return f"Google svarade med HTTP {http_status}."
    return "Okänt fel från Google."


def _field_violations(detail, secrets):
    """Felen per fält ur google.rpc.BadRequest (Data Manager API), som
    errors-poster. Fältets sökväg står kvar ("events.events[2].ad_identifiers.gclid");
    google_conversions läser ut vilken händelse den gäller."""
    found = []
    for violation in detail.get("fieldViolations") or []:
        if not isinstance(violation, dict):
            continue
        reason = re.sub(r"[^A-Z0-9_]", "", str(violation.get("reason") or "").upper())[:80]
        found.append(
            {
                "code": f"badRequest.{reason}" if reason else "",
                "message": _scrub(violation.get("description"), *secrets)[:TEXT_MAX],
                "index": None,
                "field_path": _scrub(violation.get("field"), *secrets)[:TEXT_MAX],
            }
        )
    return found


def error_from(error, http_status=None, request_id="", secrets=(), overrides=None):
    """GoogleAdsError ur Googles felobjekt {"code", "message", "status",
    "details"}: från ett HTTP-fel eller ur partialFailureError i ett lyckat
    svar (mutate med partial_failure, uploadClickConversions). Data Manager
    API:s fel (google.rpc.ErrorInfo, BadRequest med fältens fel,
    RequestInfo) läses också. overrides är texter per kod som går före
    CODE_MESSAGES."""
    error = error if isinstance(error, dict) else {}
    status = str(error.get("status") or "")
    codes, errors = [], []
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        kind = str(detail.get("@type") or "")
        if kind.endswith("google.rpc.BadRequest"):
            for item in _field_violations(detail, secrets):
                if item["code"]:
                    codes.append(item["code"])
                errors.append(item)
        elif kind.endswith("google.rpc.RequestInfo"):
            request_id = request_id or str(detail.get("requestId") or "")
        elif kind.endswith("GoogleAdsFailure"):
            request_id = request_id or str(detail.get("requestId") or "")
            for item in detail.get("errors") or []:
                if not isinstance(item, dict):
                    continue
                code = _code_name(item.get("errorCode"))
                index, field_path = _location(item.get("location"))
                if code:
                    codes.append(code)
                errors.append(
                    {
                        "code": code,
                        "message": _scrub(item.get("message"), *secrets)[:TEXT_MAX],
                        "index": index,
                        "field_path": field_path[:TEXT_MAX],
                    }
                )
        elif kind.endswith("google.rpc.ErrorInfo") and detail.get("reason"):
            codes.append(f"errorInfo.{detail['reason']}")
    names = [code.rsplit(".", 1)[-1] for code in codes]
    fallback = errors[0]["message"] if errors else _scrub(error.get("message"), *secrets)
    message = _message_for(names, status, http_status, fallback, overrides)
    if len(errors) > 1:
        message = f"{message} ({len(errors) - 1} fel till)"
    return GoogleAdsError(
        _scrub(message, *secrets),
        status=status,
        codes=codes,
        errors=errors,
        request_id=_scrub(request_id)[:100],
        http_status=http_status,
    )


def partial_failure_error(payload):
    """GoogleAdsError ur partialFailureError i ett lyckat svar, eller None."""
    error = (payload or {}).get("partialFailureError") if isinstance(payload, dict) else None
    if not error:
        return None
    return error_from(error)


def _oauth_error(payload, http_status, exchanging=False):
    """Ett fel från Googles inloggning ({"error": "invalid_grant",
    "error_description": ...})."""
    raw = payload.get("error")
    if isinstance(raw, dict):
        return error_from(raw, http_status=http_status)
    code = str(raw or "")
    if code == "invalid_grant":
        message = MSG_CODE_REJECTED if exchanging else MSG_INVALID_GRANT
    elif code in ("invalid_client", "unauthorized_client"):
        message = MSG_INVALID_CLIENT
    else:
        description = _scrub(payload.get("error_description") or code)
        message = f"Googles inloggning svarade med ett fel (HTTP {http_status}): {description}"
    status = "UNAUTHENTICATED" if code == "invalid_grant" and not exchanging else ""
    codes = [f"oauth.{_scrub(code)[:60]}"] if code else []
    return GoogleAdsError(message, status=status, codes=codes, http_status=http_status)


# ---------------------------------------------------------------------------
# Kopplingens läge
# ---------------------------------------------------------------------------


def _record_ok():
    """Ett lyckat anrop: last_ok_at (högst var femte minut) och inget fel."""
    if not cache.add(_OK_CACHE_KEY, 1, OK_TOUCH_SECONDS):
        return
    now = timezone.now()
    updated = GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        last_ok_at=now, last_error=""
    )
    if not updated:
        GoogleAdsConnection.objects.get_or_create(
            pk=GoogleAdsConnection.SOLO_PK, defaults={"last_ok_at": now}
        )


def _record_auth_failure(error):
    """Kopplingen fungerar inte: spara en kort text att visa i panelen."""
    cache.delete(_OK_CACHE_KEY)
    text = error.message[:TEXT_MAX]
    updated = GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        last_error=text
    )
    if not updated:
        GoogleAdsConnection.objects.get_or_create(
            pk=GoogleAdsConnection.SOLO_PK, defaults={"last_error": text}
        )


# ---------------------------------------------------------------------------
# Inloggningen (OAuth)
# ---------------------------------------------------------------------------


def _access_cache_key(token):
    """Cachenamnet för den kortlivade nyckeln: en hash av klienten och den
    långlivade nyckeln, aldrig nyckeln själv. En ny koppling får ett nytt namn."""
    material = f"{_setting('GOOGLE_ADS_CLIENT_ID')}:{token}".encode()
    return _ACCESS_CACHE_PREFIX + hashlib.sha256(material).hexdigest()[:40]


def _cache_access_token(secret, payload):
    """Cacha den kortlivade nyckeln ur Googles svar under den långlivades
    hash (secret). Returnerar den kortlivade."""
    token = str(payload.get("access_token") or "")
    try:
        expires = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires = 3600
    ttl = expires - TOKEN_MARGIN_SECONDS
    if token and ttl > 0:
        cache.set(_access_cache_key(secret), token, ttl)
    return token


def authorization_url(state, redirect_uri):
    """Adressen till Googles inloggning, dit byråns webbläsare skickas.

    access_type=offline och prompt=consent ger alltid en långlivad nyckel.
    state ska vara slumpad och prövas när Google skickar tillbaka koden."""
    client_id = _setting("GOOGLE_ADS_CLIENT_ID")
    if not client_id:
        raise GoogleAdsError(MSG_NO_CLIENT, status="NOT_CONFIGURED")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params, quote_via=quote)}"


def _email_from_id_token(id_token):
    """E-postadressen ur id_token, eller "".

    id_token kommer i samma svar direkt från Googles token-adress över TLS,
    så signaturen prövas inte här. Adressen visas bara i panelen ("kopplat
    som ...") och ger aldrig någon behörighet."""
    try:
        part = str(id_token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8"))
    except (IndexError, ValueError, TypeError, binascii.Error):
        return ""
    email = claims.get("email") if isinstance(claims, dict) else ""
    if not isinstance(email, str) or "@" not in email:
        return ""
    return email.strip()[:254]


def exchange_code(code, redirect_uri):
    """Byt koden från Googles inloggning mot (refresh token, e-post eller "",
    behörigheterna). Behörigheterna är svarets "scope" (de byrån lät vara
    ikryssade), sorterade; GoogleAdsConnection.set_refresh_token sparar dem.

    Kastar GoogleAdsError om Google säger nej, om ingen långlivad nyckel
    kom, eller om behörigheten för Google Ads inte kryssades i (då återkallas
    nyckeln direkt). Saknas bara behörigheten för Data Manager API sparas
    kopplingen ändå: konverteringarna går då som CSV tills byrån kopplar om."""
    if not oauth_configured():
        raise GoogleAdsError(MSG_NO_CLIENT, status="NOT_CONFIGURED")
    if not str(code or "").strip():
        raise GoogleAdsError(MSG_CODE_REJECTED, status="INVALID_CODE")
    status, payload = _http(
        "POST",
        TOKEN_URL,
        data=urlencode(
            {
                "grant_type": "authorization_code",
                "code": str(code).strip(),
                "client_id": _setting("GOOGLE_ADS_CLIENT_ID"),
                "client_secret": _setting("GOOGLE_ADS_CLIENT_SECRET"),
                "redirect_uri": redirect_uri,
            }
        ).encode(),
        headers=_FORM_HEADERS,
        max_bytes=OAUTH_MAX_BYTES,
    )
    if status != 200:
        error = _oauth_error(payload, status, exchanging=True)
        logger.warning("Google Ads: inloggningen gav HTTP %s %s", status, error.codes)
        raise error
    token = str(payload.pop("refresh_token", "") or "")
    granted = str(payload.get("scope") or "").split()
    if granted and ADWORDS_SCOPE not in granted:
        revoke(token or payload.get("access_token"))
        raise GoogleAdsError(MSG_SCOPE_MISSING, status="SCOPE_MISSING", codes=["oauth.scope"])
    if not token:
        raise GoogleAdsError(MSG_NO_REFRESH_TOKEN, status="NO_REFRESH_TOKEN")
    email = _email_from_id_token(payload.get("id_token"))
    scopes = normalize_scopes(payload.get("scope"))
    _cache_access_token(token, payload)
    payload = None
    return token, email, scopes


def revoke(token):
    """Återkalla en nyckel hos Google. True om Google tog emot det.

    Nyckeln skickas i kroppen, inte i adressen, så att den inte hamnar i
    någon logg. Kastar aldrig: kopplingen ska gå att glömma även om Google
    inte svarar."""
    token = str(token or "").strip()
    if not token:
        return False
    cache.delete(_access_cache_key(token))
    try:
        status, _ = _http(
            "POST",
            REVOKE_URL,
            data=urlencode({"token": token}).encode(),
            headers=_FORM_HEADERS,
            max_bytes=OAUTH_MAX_BYTES,
        )
    except GoogleAdsError:
        return False
    if status != 200:
        logger.warning("Google Ads: återkallelsen gav HTTP %s", status)
    return status == 200


def access_token(force_refresh=False):
    """En kortlivad nyckel för anropen, ur cachen eller hämtad med den
    långlivade. Loggas aldrig. Kastar GoogleAdsError."""
    if not oauth_configured():
        raise GoogleAdsError(MSG_NO_CLIENT, status="NOT_CONFIGURED")
    token = _current_refresh_token()
    if not token:
        raise GoogleAdsError(MSG_NOT_CONNECTED, status="NOT_CONFIGURED")
    key = _access_cache_key(token)
    if not force_refresh:
        cached = cache.get(key)
        if cached:
            return cached
    status, payload = _http(
        "POST",
        TOKEN_URL,
        data=urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": _setting("GOOGLE_ADS_CLIENT_ID"),
                "client_secret": _setting("GOOGLE_ADS_CLIENT_SECRET"),
            }
        ).encode(),
        headers=_FORM_HEADERS,
        max_bytes=OAUTH_MAX_BYTES,
    )
    if status != 200 or not payload.get("access_token"):
        payload.pop("access_token", None)
        cache.delete(key)
        error = _oauth_error(payload, status)
        logger.warning("Google Ads: nyckeln kunde inte förnyas: HTTP %s %s", status, error.codes)
        if error.is_auth_error or status in (400, 401):
            _record_auth_failure(error)
        token = None
        raise error
    _remember_scopes(token, payload.get("scope"))
    access = _cache_access_token(token, payload)
    payload = token = None
    return access


# ---------------------------------------------------------------------------
# Behörigheterna (scopes)
# ---------------------------------------------------------------------------


def _remember_scopes(token, scope):
    """Spara behörigheterna Google gav för nyckeln (svarets "scope" när den
    förnyas), med nyckelns avtryck. Inget sparas om Google inte skickade
    några. Nyckeln själv sparas eller loggas aldrig här."""
    scopes = normalize_scopes(scope)
    if not scopes:
        return
    fingerprint = token_fingerprint(token)
    GoogleAdsConnection.objects.get_or_create(pk=GoogleAdsConnection.SOLO_PK)
    GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).exclude(
        granted_scopes=scopes, scopes_for=fingerprint
    ).update(granted_scopes=scopes, scopes_for=fingerprint)


def granted_scopes():
    """Behörigheterna för nyckeln som används nu, som mängd, eller None när
    de inte är kända: ingen nyckel, nyckeln i miljön innan den förnyats här,
    eller en koppling som gjordes innan behörigheterna sparades."""
    token = _current_refresh_token()
    if not token:
        return None
    connection = GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).first()
    if connection is None or not connection.scopes_for:
        return None
    if not hmac.compare_digest(connection.scopes_for, token_fingerprint(token)):
        return None
    return set(connection.granted_scopes.split())


def scope_state(scope):
    """SCOPE_GRANTED när nyckeln har behörigheten scope, SCOPE_MISSING när
    Google sagt att den saknas, annars SCOPE_UNKNOWN."""
    scopes = granted_scopes()
    if scopes is None:
        return SCOPE_UNKNOWN
    return SCOPE_GRANTED if scope in scopes else SCOPE_MISSING


def datamanager_scope_state():
    """SCOPE_GRANTED när nyckeln har behörigheten för Data Manager API,
    SCOPE_MISSING när Google sagt att den saknas, annars SCOPE_UNKNOWN."""
    return scope_state(DATAMANAGER_SCOPE)


def forget_datamanager_scope():
    """Google sa att nyckeln saknar behörigheten för Data Manager API
    (ACCESS_TOKEN_SCOPE_INSUFFICIENT): spara det, så att Google-sidan ber
    byrån koppla om och inget mer skickas den vägen."""
    forget_scope(DATAMANAGER_SCOPE)


def forget_scope(scope):
    """Google sa att nyckeln saknar behörigheten scope: spara det, så att
    Google-sidan ber byrån koppla om."""
    token = _current_refresh_token()
    if not token:
        return
    scopes = granted_scopes() or set()
    scopes.discard(scope)
    GoogleAdsConnection.objects.get_or_create(pk=GoogleAdsConnection.SOLO_PK)
    GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        granted_scopes=normalize_scopes(" ".join(scopes)), scopes_for=token_fingerprint(token)
    )


# ---------------------------------------------------------------------------
# Google Ads
# ---------------------------------------------------------------------------


def _api_url(path):
    path = str(path or "").lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_\-/:.~]+", path) or ".." in path or "//" in path:
        raise GoogleAdsError("Ogiltig sökväg till Google Ads.", status="BAD_URL")
    return f"https://{API_HOST}/{api_version()}/{path}"


def _api_headers(token, login_customer_id):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Avvecklad 2026-09-09: Google bortser från den, och den skickas bara om
    # den är satt (Google aviserar att den nekas i en senare version).
    developer_token = _setting("GOOGLE_ADS_DEVELOPER_TOKEN")
    if developer_token:
        headers["developer-token"] = developer_token
    login = mcc_id() if login_customer_id is MCC else digits(login_customer_id)
    if login:
        headers["login-customer-id"] = login
    return headers


def _customer(customer_id):
    value = digits(customer_id)
    if len(value) != 10:
        raise GoogleAdsError(MSG_BAD_CUSTOMER_ID, status="INVALID_CUSTOMER_ID")
    return value


def request(method, path, body=None, login_customer_id=MCC):
    """Ett anrop till Google Ads: path efter versionen, till exempel
    "customers/1234567890/googleAds:mutate". body skickas som JSON.

    login_customer_id: MCC (standard) skickar byråns förvaltarkonto, None
    ingen header, annars det id som ges. Returnerar svaret som dict; kastar
    GoogleAdsError. Ett 401 (nyckeln gick ut i förtid) prövas en gång till
    med en ny nyckel."""
    url = _api_url(path)
    method = method.upper()
    data = None
    if body is not None or method != "GET":
        data = json.dumps(body if body is not None else {}).encode()
    for attempt in (1, 2):
        token = access_token(force_refresh=attempt == 2)
        status, payload = _http(
            method, url, data=data, headers=_api_headers(token, login_customer_id)
        )
        if status != 401:
            break
    if 200 <= status < 300:
        _record_ok()
        return payload
    error = error_from(payload.get("error"), http_status=status, secrets=(token,))
    token = None
    logger.warning(
        "Google Ads: %s %s gav HTTP %s %s (request %s)",
        method,
        urlsplit(url).path,
        status,
        error.codes,
        error.request_id or "-",
    )
    if error.is_auth_error:
        _record_auth_failure(error)
    raise error


def list_accessible_customers():
    """Kontona inloggningen når direkt, som siffror (förvaltarkontot bland dem)."""
    payload = request("GET", "customers:listAccessibleCustomers", login_customer_id=None)
    return [digits(name) for name in payload.get("resourceNames") or [] if digits(name)]


def search(customer_id, query, login_customer_id=MCC):
    """Raderna för en GAQL-fråga, sida för sida (en generator: anropen görs
    när raderna läses, och fel kastas där). Varje rad är ett dict i Googles
    form, till exempel {"campaign": {"resourceName": ..., "status": ...},
    "metrics": {"clicks": "12"}}."""
    path = f"customers/{_customer(customer_id)}/googleAds:search"
    page_token = ""
    for _ in range(MAX_PAGES):
        body = {"query": query}
        if page_token:
            body["pageToken"] = page_token
        payload = request("POST", path, body, login_customer_id)
        yield from payload.get("results") or []
        page_token = payload.get("nextPageToken") or ""
        if not page_token:
            return
    logger.warning("Google Ads: sökningen avbröts efter %s sidor", MAX_PAGES)


def mutate(
    customer_id, operations, validate_only=False, partial_failure=False, login_customer_id=MCC
):
    """googleAds:mutate: flera ändringar i ett anrop, allt eller inget (om
    inte partial_failure). Tillfälliga resursnamn med negativa id
    (customers/1/campaignBudgets/-1) knyter ihop det som skapas i samma anrop.

    Returnerar svaret: {"mutateOperationResponses": [{"campaignBudgetResult":
    {"resourceName": ...}}, ...]} och vid partial_failure eventuellt
    partialFailureError (se partial_failure_error())."""
    body = {
        "mutateOperations": list(operations),
        "partialFailure": bool(partial_failure),
        "validateOnly": bool(validate_only),
    }
    return request(
        "POST", f"customers/{_customer(customer_id)}/googleAds:mutate", body, login_customer_id
    )


# ---------------------------------------------------------------------------
# Data Manager API
# ---------------------------------------------------------------------------


def _datamanager_url(path, params=None):
    path = str(path or "").lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_\-/:.~]+", path) or ".." in path or "//" in path:
        raise GoogleAdsError("Ogiltig sökväg till Data Manager API.", status="BAD_URL")
    url = f"https://{DATAMANAGER_HOST}/{DATAMANAGER_VERSION}/{path}"
    if params:
        url += "?" + urlencode(params, quote_via=quote)
    return url


def _datamanager_headers(token):
    """Bara nyckeln. Ingen utvecklartoken och ingen login-customer-id: Data
    Manager bortser från headers när data skickas, och kontona står i
    anropets destinations (data-manager/api/devguides/concepts/destinations)."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def datamanager_request(method, path, body=None, params=None):
    """Ett anrop till Data Manager API: path efter versionen, till exempel
    "events:ingest" (POST med body) eller "requestStatus:retrieve" (GET med
    params). Samma skydd som request(): fast värd, https, tidsgräns, tak för
    svaret och tvättade fel, med samma inloggning och kortlivade nyckel.

    Returnerar svaret som dict; kastar GoogleAdsError. Ett 401 prövas en gång
    till med en ny nyckel. Ett fel i ADX:s inloggning sparas som för Google
    Ads; ett nej som bara gäller Data Manager (behörigheten, API:t avslaget i
    projektet) gör det inte, och lyckade anrop rör inte Google Ads-läget."""
    url = _datamanager_url(path, params)
    method = method.upper()
    data = json.dumps(body).encode() if body is not None else None
    for attempt in (1, 2):
        token = access_token(force_refresh=attempt == 2)
        status, payload = _http(method, url, data=data, headers=_datamanager_headers(token))
        if status != 401:
            break
    if 200 <= status < 300:
        token = None
        return payload
    error = error_from(
        payload.get("error"), http_status=status, secrets=(token,), overrides=DATAMANAGER_MESSAGES
    )
    token = None
    if status == 404 and not error.codes:
        error = GoogleAdsError(MSG_DM_NOT_FOUND, status=error.status, http_status=status)
    logger.warning(
        "Data Manager: %s %s gav HTTP %s %s (request %s)",
        method,
        urlsplit(url).path,
        status,
        error.codes,
        error.request_id or "-",
    )
    if status == 401 or error.status == "UNAUTHENTICATED":
        _record_auth_failure(error)
    raise error
