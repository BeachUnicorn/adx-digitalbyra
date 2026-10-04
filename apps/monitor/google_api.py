"""
Övervakningens anrop till Google: Chrome UX Report (API-nyckel), Search
Console och Google Business Profile (ADX:s Google-inloggning).

Inloggningen är densamma som ADX Flamingo använder (apps/flamingo/google_ads.py,
GoogleAdsConnection): access_token() ger den kortlivade nyckeln och
scope_state() säger om Google gett behörigheten. Allt annat bor här, så att
Flamingos anrop och felhantering inte påverkas.

Skydd, som i google_ads.py:

- Bara fasta värdar hos Google (ALLOWED_HOSTS), alltid https, en tidsgräns
  och ett tak för svarets storlek.
- Nycklar (access token, API-nyckel) loggas aldrig och står aldrig i ett
  fel: texter från Google tvättas med google_ads.scrub. API-nyckeln skickas
  i headern X-goog-api-key, inte i adressen.
- Ett fel blir GoogleApiError med en svensk text för byrån och ett `kind`
  som säger vad byrån ska göra (KIND_*). Den visas aldrig för kunden.

Tester: patcha apps.monitor.google_api.urlopen och google_ads.access_token.
Inga tester anropar Google.
"""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings

from apps.flamingo import google_ads
from apps.flamingo.google_ads import GoogleAdsError

logger = logging.getLogger(__name__)

CRUX_HOST = "chromeuxreport.googleapis.com"
WEBMASTERS_HOST = "www.googleapis.com"
SEARCHCONSOLE_HOST = "searchconsole.googleapis.com"
ACCOUNTS_HOST = "mybusinessaccountmanagement.googleapis.com"
BUSINESS_INFO_HOST = "mybusinessbusinessinformation.googleapis.com"
PERFORMANCE_HOST = "businessprofileperformance.googleapis.com"
REVIEWS_HOST = "mybusiness.googleapis.com"
ALLOWED_HOSTS = frozenset(
    {
        CRUX_HOST,
        WEBMASTERS_HOST,
        SEARCHCONSOLE_HOST,
        ACCOUNTS_HOST,
        BUSINESS_INFO_HOST,
        PERFORMANCE_HOST,
        REVIEWS_HOST,
    }
)
#: På www.googleapis.com får bara Search Console (webmasters/v3) anropas.
_PATH_PREFIX = {WEBMASTERS_HOST: "/webmasters/v3/"}

TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Vad byrån ska göra åt ett fel.
KIND_NOT_CONNECTED = "not_connected"  # ingen Google-inloggning
KIND_SCOPE = "scope"  # inloggningen saknar behörigheten: koppla om
KIND_NOT_ENABLED = "not_enabled"  # API:t är inte påslaget i Cloud-projektet
KIND_QUOTA_ZERO = "quota_zero"  # kvoten är 0: åtkomsten är inte godkänd (GBP)
KIND_QUOTA = "quota"  # kvoten är slut för stunden
KIND_PERMISSION = "permission"  # inloggningen når inte resursen
KIND_NOT_FOUND = "not_found"  # finns inte (CrUX: för lite trafik)
KIND_AUTH = "auth"  # inloggningen gäller inte längre
KIND_NO_KEY = "no_key"  # ingen API-nyckel för Chrome UX Report
KIND_UNAVAILABLE = "unavailable"  # Google svarade inte
KIND_OTHER = "other"

#: Fel som gäller inställningen (kopplingen, projektet), inte en enskild sajt.
SETUP_KINDS = frozenset(
    {KIND_NOT_CONNECTED, KIND_SCOPE, KIND_NOT_ENABLED, KIND_QUOTA_ZERO, KIND_AUTH, KIND_NO_KEY}
)

GBP_FORM_URL = "https://support.google.com/business/contact/api_default"

MSG_NOT_CONNECTED = "ADX:s Google-konto är inte kopplat. Koppla det på /manage/flamingo/google/."
MSG_SCOPE = {
    "search": "Inloggningen saknar behörigheten för Search Console. Koppla om för att läsa "
    "Search Console på /manage/flamingo/google/.",
    "gbp": "Inloggningen saknar behörigheten för Business Profile. Koppla om för att läsa "
    "Business Profile på /manage/flamingo/google/.",
}
MSG_NOT_ENABLED = {
    "crux": "Chrome UX Report API är inte påslaget i Google Cloud-projektet som "
    "API-nyckeln hör till. Slå på det under API:er och tjänster.",
    "search": "Google Search Console API är inte påslaget i Google Cloud-projektet. Slå på "
    "det under API:er och tjänster i projektet som OAuth-klienten hör till.",
    "gbp": "Business Profile-API:erna är inte påslagna i Google Cloud-projektet. Slå på My "
    "Business Account Management API, My Business Business Information API, Business "
    "Profile Performance API och Google My Business API. De syns först när Google godkänt "
    f"ansökan om API-åtkomst ({GBP_FORM_URL}).",
}
MSG_QUOTA_ZERO = (
    "Google har inte godkänt ADX för Business Profile-API:erna än (kvoten är 0). Ansök om "
    f"åtkomst i Googles formulär {GBP_FORM_URL} (Application for Basic API Access) med "
    "projektets nummer. När kvoten i Cloud Console visar 300 per minut är det godkänt."
)
MSG_QUOTA = "Google tar inte emot fler anrop just nu (kvoten är slut). Försöker igen i morgon."
MSG_PERMISSION = {
    "search": "Ingen åtkomst i Search Console.",
    "gbp": "Inloggningen når inte profilen i Business Profile. Lägg till ADX:s Google-konto "
    "som ansvarig för profilen.",
    "crux": "Google nekade anropet till Chrome UX Report. Kontrollera API-nyckelns "
    "begränsningar i Cloud Console.",
}
MSG_AUTH = "Inloggningen hos Google gäller inte längre. Koppla ADX:s Google-konto igen."
MSG_NO_KEY = (
    "Ingen API-nyckel för Chrome UX Report: sätt CRUX_API_KEY (eller PAGESPEED_API_KEY) i env."
)
MSG_UNAVAILABLE = "Google svarade inte. Försöker igen i morgon."
MSG_TOO_LARGE = "Svaret från Google var större än väntat och lästes inte."


class GoogleApiError(Exception):
    """Ett fel från Google eller före anropet. message är svensk och går att
    visa för byrån; kind säger vad som ska göras (KIND_*)."""

    def __init__(self, message, kind=KIND_OTHER, http_status=None):
        self.message = google_ads.scrub(message)[:300] or "Okänt fel från Google."
        self.kind = kind
        self.http_status = http_status
        super().__init__(self.message)

    def __str__(self):
        return self.message

    @property
    def is_setup(self):
        return self.kind in SETUP_KINDS


def crux_api_key():
    """CRUX_API_KEY, annars PAGESPEED_API_KEY (samma Cloud-projekt räcker)."""
    return str(
        getattr(settings, "CRUX_API_KEY", "") or getattr(settings, "PAGESPEED_API_KEY", "") or ""
    ).strip()


def _http(method, url, *, body=None, headers=None):
    """(HTTP-status, svaret som dict). Kastar bara när inget svar kom."""
    parts = urlsplit(url)
    prefix = _PATH_PREFIX.get(parts.hostname, "/")
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or not parts.path.startswith(prefix)
    ):
        raise GoogleApiError("Adressen är inte en av Googles.", KIND_OTHER)
    data = json.dumps(body).encode() if body is not None else None
    request = Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    headers = None
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            status = getattr(response, "status", 200)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        except Exception:  # noqa: BLE001
            raw = b""
        finally:
            exc.close()
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("Google: %s %s nåddes inte (%s)", method, parts.path, type(exc).__name__)
        raise GoogleApiError(MSG_UNAVAILABLE, KIND_UNAVAILABLE) from None
    if not isinstance(status, int):
        status = 200
    if not isinstance(raw, bytes | bytearray):
        raw = b""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GoogleApiError(MSG_TOO_LARGE, KIND_OTHER, status)
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def _reasons(error):
    """Googles orsaker ur ett felobjekt: ErrorInfo.reason och errors[].reason."""
    reasons, quota_zero = set(), False
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if detail.get("reason"):
            reasons.add(str(detail["reason"]))
        metadata = detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {}
        if str(metadata.get("quota_limit_value", "")) == "0":
            quota_zero = True
    for item in error.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.add(str(item["reason"]))
    return reasons, quota_zero


def error_for(api, http_status, payload):
    """GoogleApiError ur ett felsvar. api är "crux", "search" eller "gbp"."""
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    status = str(error.get("status") or "")
    message = google_ads.scrub(error.get("message") or "")
    reasons, quota_zero = _reasons(error)
    if reasons & {"SERVICE_DISABLED", "accessNotConfigured", "API_DISABLED"} or (
        "has not been used in project" in message or "is disabled" in message
    ):
        return GoogleApiError(MSG_NOT_ENABLED[api], KIND_NOT_ENABLED, http_status)
    if reasons & {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"} or (
        "insufficient authentication scopes" in message.lower()
    ):
        if api == "crux":
            return GoogleApiError(MSG_PERMISSION["crux"], KIND_PERMISSION, http_status)
        return GoogleApiError(MSG_SCOPE[api], KIND_SCOPE, http_status)
    if status == "RESOURCE_EXHAUSTED" or http_status == 429 or reasons & {"RATE_LIMIT_EXCEEDED"}:
        if quota_zero or (api == "gbp" and "limit '0'" in message):
            return GoogleApiError(MSG_QUOTA_ZERO, KIND_QUOTA_ZERO, http_status)
        return GoogleApiError(MSG_QUOTA, KIND_QUOTA, http_status)
    if http_status == 401 or status == "UNAUTHENTICATED":
        return GoogleApiError(MSG_AUTH, KIND_AUTH, http_status)
    if http_status == 404 or status == "NOT_FOUND":
        return GoogleApiError(message or "Finns inte hos Google.", KIND_NOT_FOUND, http_status)
    if http_status == 403 or status == "PERMISSION_DENIED":
        if api == "gbp" and quota_zero:
            return GoogleApiError(MSG_QUOTA_ZERO, KIND_QUOTA_ZERO, http_status)
        return GoogleApiError(MSG_PERMISSION[api], KIND_PERMISSION, http_status)
    if http_status and http_status >= 500:
        return GoogleApiError(
            f"Google svarade med ett fel (HTTP {http_status}). Försöker igen i morgon.",
            KIND_UNAVAILABLE,
            http_status,
        )
    return GoogleApiError(
        f"Google: {message}" if message else f"Google svarade med HTTP {http_status}.",
        KIND_OTHER,
        http_status,
    )


def crux_call(body):
    """POST till Chrome UX Report med API-nyckeln (i en header, inte i adressen)."""
    key = crux_api_key()
    if not key:
        raise GoogleApiError(MSG_NO_KEY, KIND_NO_KEY)
    url = f"https://{CRUX_HOST}/v1/records:queryHistoryRecord"
    status, payload = _http("POST", url, body=body, headers={"X-goog-api-key": key})
    key = None
    if 200 <= status < 300:
        return payload
    raise error_for("crux", status, payload)


def oauth_call(api, method, host, path, *, params=None, body=None, scope=None):
    """Ett anrop med ADX:s Google-inloggning. api är "search" eller "gbp"
    (styr felens texter), scope behörigheten anropet kräver. Ett 401 prövas
    en gång till med en ny nyckel. Returnerar svaret; kastar GoogleApiError."""
    if scope and google_ads.scope_state(scope) == google_ads.SCOPE_MISSING:
        raise GoogleApiError(MSG_SCOPE[api], KIND_SCOPE)
    url = f"https://{host}{path}"
    if params:
        url += "?" + urlencode(params, doseq=True)
    for attempt in (1, 2):
        try:
            token = google_ads.access_token(force_refresh=attempt == 2)
        except GoogleAdsError as exc:
            if exc.status == "NOT_CONFIGURED":
                raise GoogleApiError(MSG_NOT_CONNECTED, KIND_NOT_CONNECTED) from None
            if exc.is_auth_error:
                raise GoogleApiError(MSG_AUTH, KIND_AUTH) from None
            raise GoogleApiError(exc.message, KIND_UNAVAILABLE) from None
        status, payload = _http(
            method, url, body=body, headers={"Authorization": f"Bearer {token}"}
        )
        token = None
        if status != 401:
            break
    if 200 <= status < 300:
        return payload
    error = error_for(api, status, payload)
    logger.warning(
        "Google %s: %s %s gav HTTP %s (%s)", api, method, urlsplit(url).path, status, error.kind
    )
    if error.kind == KIND_SCOPE and scope:
        google_ads.forget_scope(scope)
    raise error
