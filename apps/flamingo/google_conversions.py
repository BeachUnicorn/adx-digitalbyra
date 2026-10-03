"""
Konverteringarna till Google (README, Mätningen): förfrågningar, klick på
telefonnumret och vunna affärer som offline-konverteringar ("import från
klick") i kundens Google Ads-konto.

Vägen väljs med FLAMINGO_CONVERSIONS_UPLOAD (upload_path):

    datamanager  standard: Data Manager API, events:ingest
                 (upload_via_data_manager). Googles väg för
                 offline-konverteringar sedan uploadClickConversions slutade
                 ta emot nya användare 2026-06-15.
    googleads    Google Ads API, uploadClickConversions
                 (upload_via_google_ads). Bara för en inloggning som Google
                 redan släpper in; annars CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE.
    off          bara CSV-filen (exports.offline_conversions_csv).

    ensure_conversion_actions(account)  skapar de konverteringar som saknas i
                                        kundens konto (med Google Ads API) och
                                        sparar resursnamnen i
                                        account.google_conversion_actions
    upload_queued(account)              skickar raderna i kö den valda vägen
    check_sent(account)                 läser Googles besked om det som
                                        skickats med Data Manager API
    upload_status()                     läget för byråns sidor
    queue_context()                     det byråns kö visar om konverteringarna

Data Manager API, så som Google dokumenterar det
(developers.google.com/data-manager/api, läst 2026-10-03):

- POST https://datamanager.googleapis.com/v1/events:ingest med behörigheten
  https://www.googleapis.com/auth/datamanager. Inga andra headers än
  nyckeln: Google bortser från headers i ett ingest-anrop, och kontona står
  i destinations (devguides/concepts/destinations).
- Destinationen för ett förvaltarkonto som skickar till ett kundkonto:
  operatingAccount är kundens konto och loginAccount förvaltarkontot, båda
  med accountType GOOGLE_ADS och kontots id som siffror, och
  productDestinationId är konverteringsåtgärdens id (typ UPLOAD_CLICKS).
- Händelsen: transactionId (Googles dubblettskydd, orderId i Google Ads
  API), eventTimestamp (RFC 3339 med offset), eventSource (krävs, WEB),
  adIdentifiers.gclid, conversionValue och currency, och consent med
  adUserData CONSENT_GRANTED eller CONSENT_DENIED.
- Högst 2 000 händelser och 10 destinationer per anrop. Google tar emot
  allt eller inget (fast-fail): ett fel i en händelse stoppar hela anropet,
  med fältets sökväg i google.rpc.BadRequest (events.events[0]...).
- Svaret är requestId och fieldWarnings. Bearbetningen sker efteråt:
  requestStatus:retrieve (tidigast efter 30 minuter, upp till ett dygn)
  ger SUCCESS, FAILED, PARTIAL_SUCCESS eller PROCESSING per destination,
  med antal per orsak men inte vilken händelse det gällde.
- validateOnly prövar anropet utan att Google tar emot något.

Därför skickas en konvertering per anrop: både nejet direkt och
bearbetningens besked gäller då exakt en rad, och ingen rad behöver gissas
som skickad eller inte. Flamingos volymer är små (Googles gräns är 300
anrop i minuten per Cloud-projekt), och högst DM_MAX_PER_RUN skickas per
konto och körning.

API:t används bara när (upload_enabled) vägen inte är off, Google Ads API
är inkopplat, Google inte har stoppat vägen och, för Data Manager API,
inloggningen har behörigheten (google_ads.datamanager_scope_state). Per
konto (can_sync): inte demo, ett eget id och kontot under ADX
förvaltarkonto. En rad skickas bara när konverteringsåtgärden för dess sort
finns i kundens konto. ensure_conversion_actions skapar dem med Google Ads
API.

Ingen konvertering tappas och ingen räknas två gånger:

- En rad står i kö, och därmed i CSV-filen, tills Google tagit emot den.
  Ett nej eller ett fel lämnar den i kö med felet (byrån ser det i kön),
  och nästa försök väntar allt längre (backoff: 1, 2, 4, 8, 16 och sedan
  24 timmar). Efter ConversionUpload.MAX_ATTEMPTS skickas den inte av sig
  själv längre; "Försök ladda upp igen" på Google-sidan börjar om.
- En rad blir skickad först när Google tagit emot den (svaret har ett
  requestId). Googles besked om bearbetningen läses efteråt (check_sent),
  oavsett vilken väg som är vald nu: FAILED lägger raden tillbaka i kön,
  och den kan då laddas upp med filen.
- En rad går bara en väg. En nedladdad CSV-fil tar sina rader
  (ConversionUpload.downloaded_at) och API:t skickar dem aldrig; en rad som
  API:t skickat står inte i kö och kommer aldrig med i en fil. Raderna
  låses (SELECT ... FOR UPDATE SKIP LOCKED) medan de skickas, och
  nedladdningen hoppar över en låst rad.
- transactionId är detsamma för en förfrågan och sort vid varje försök
  (ConversionUpload.transaction_id), så Google räknar aldrig en konvertering
  två gånger. Tiden är densamma som i CSV-filen (exports.conversion_moment).
- Ett nej som gäller hela vägen (Data Manager API avslaget i
  Cloud-projektet, NOT_ALLOWLISTED) stoppar den vägen för alla konton
  (block_uploads) med en förklaring på Google-sidan. Saknas behörigheten
  (ACCESS_TOKEN_SCOPE_INSUFFICIENT) ber Google-sidan byrån koppla om med
  Google.

Vad som köas avgörs av Lead (models.py): allt med ett gclid
(Lead.can_send_to_google). Landningssidan frågar inte om samtycke (beslut
2026-10-03), så konverteringarna skickas utan fråga. Samtycket skickas med
(adUserData) bara när Lead.ad_consent faktiskt är "granted" eller "denied";
annars utelämnas det. Det hittas aldrig på. Värdet skickas bara för
affärer, i kronor (SEK).

gbraid och wbraid: Data Manager API tar emot dem i adIdentifiers, men
Flamingos konverteringar räknas en gång per klick (ONE_PER_CLICK, Googles
råd för förfrågningar), och en sådan åtgärd tar inte emot braid-id:n
(PROCESSING_ERROR_REASON_ONE_PER_CLICK_CONVERSION_ACTION_NOT_PERMITTED_WITH_BRAID).
Därför krävs gclid och bara gclid skickas, också när förfrågan har båda.

Demokonton, konton utan Google Ads-id eller som inte ligger under ADX
förvaltarkonto, och allt när API:t inte är inkopplat: inget händer. Byrån
har då CSV-filen kvar.
"""

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import exports, google_ads
from .google_ads import GoogleAdsError
from .models import ConversionUpload, FlamingoAccount, GoogleAdsConnection, Lead

logger = logging.getLogger(__name__)

PATH_DATAMANAGER = "datamanager"
PATH_GOOGLEADS = "googleads"
PATH_OFF = "off"
PATH_LABELS = {
    PATH_DATAMANAGER: "Data Manager API",
    PATH_GOOGLEADS: "Google Ads API (uploadClickConversions)",
    PATH_OFF: "Av, bara CSV-filen",
}

#: Konverteringens kategori hos Google, per sort.
CATEGORIES = {
    ConversionUpload.KIND_LEAD: "SUBMIT_LEAD_FORM",
    ConversionUpload.KIND_CALL: "PHONE_CALL_LEAD",
    ConversionUpload.KIND_DEAL: "CONVERTED_LEAD",
}
#: Konverteringar per anrop med uploadClickConversions (Google tar högst 2 000).
BATCH = 200
#: Google tar inte emot en konvertering för ett klick som är för nytt
#: (TOO_RECENT_EVENT, PROCESSING_ERROR_REASON_TOO_RECENT_CLICK). Förfrågan
#: kommer in minuter efter klicket, så raden väntar tills förfrågan är så här
#: gammal.
CLICK_SETTLE = timedelta(hours=6)
#: Väntan efter ett försök som inte gick fram: 1, 2, 4, 8, 16 och sedan 24
#: timmar.
BACKOFF_FIRST = timedelta(hours=1)
BACKOFF_MAX = timedelta(hours=24)

MSG_NO_GCLID = "Förfrågan saknar gclid, som Google behöver. Skickas inte."
MSG_WRONG_TYPE = (
    'Kundens konto har redan en konvertering som heter "{name}" men den är inte en import '
    "av klick. Byt namn på den i Google Ads, så skapar Flamingo sin egen."
)
MSG_ROW_FAILED = "Google tog inte emot konverteringen."
MSG_RECONNECT = "Koppla om med Google för att skicka konverteringar."
MSG_RECONNECT_ENV = (
    "Nyckeln i miljön (GOOGLE_ADS_REFRESH_TOKEN) har inte behörigheten för Data Manager API, "
    "eller har inte använts än. Skapa en ny nyckel med behörigheten, eller ta bort den ur "
    "miljön och koppla om med Google för att skicka konverteringar."
)
MSG_OFF = "FLAMINGO_CONVERSIONS_UPLOAD är off: konverteringarna går bara som CSV-filen."
MSG_UNKNOWN_PATH = (
    "FLAMINGO_CONVERSIONS_UPLOAD har ett okänt värde och räknas som off. Värdena är "
    "datamanager, googleads och off."
)
MSG_NOT_CONFIGURED = "Google Ads API är inte inkopplat, så konverteringarna går som CSV-filen."

# ---------------------------------------------------------------------------
# Google Ads API (uploadClickConversions)
# ---------------------------------------------------------------------------

#: Fel som betyder "försök igen senare": raden står kvar i kö utan att räkna
#: ett försök.
RETRY_CODES = frozenset(
    {
        "TOO_RECENT_EVENT",
        "TOO_RECENT_CONVERSION_ACTION",
        "TOO_RECENT_CALL",
        "CUSTOMER_NOT_ACCEPTED_CUSTOMER_DATA_TERMS",
        "NO_CONVERSION_ACTION_FOUND",
    }
)
#: Google har redan konverteringen (samma klick, konvertering och tid).
ALREADY_CODES = frozenset({"CLICK_CONVERSION_ALREADY_EXISTS"})
#: Googles koder för en rad, i klartext för byrån.
ROW_MESSAGES = {
    "TOO_RECENT_EVENT": "Klicket är för nytt för Google. Skickas igen vid nästa körning.",
    "TOO_RECENT_CONVERSION_ACTION": (
        "Konverteringen skapades nyss i kundens konto. Skickas igen vid nästa körning."
    ),
    "CUSTOMER_NOT_ACCEPTED_CUSTOMER_DATA_TERMS": (
        "Kundens konto har inte godkänt Googles villkor för kunddata (Mål, Konverteringar, "
        "Inställningar). Skickas igen när de är godkända."
    ),
    "NO_CONVERSION_ACTION_FOUND": (
        "Konverteringen finns inte längre i kundens konto. Den skapas igen vid nästa körning."
    ),
    "EXPIRED_EVENT": "Klicket är för gammalt: Google tar bara emot konverteringar inom 90 dagar.",
    "UNPARSEABLE_GCLID": "Google kunde inte läsa klick-id:t.",
    "CONVERSION_PRECEDES_EVENT": "Konverteringens tid ligger före klicket.",
    "EVENT_NOT_FOUND": "Google hittar inte klicket i kundens konto.",
    "INVALID_CUSTOMER_FOR_CLICK": "Klicket hör till ett annat Google Ads-konto.",
    "UNAUTHORIZED_CUSTOMER": "Klicket hör till ett annat Google Ads-konto.",
    "CONVERSION_TRACKING_NOT_ENABLED_AT_IMPRESSION_TIME": (
        "Konverteringsspårningen var inte påslagen i kontot när annonsen visades."
    ),
    "INVALID_CONVERSION_ACTION_TYPE": "Konverteringen i kundens konto är inte en import av klick.",
}
#: Google släpper inte in ADX i uploadClickConversions.
NOT_ALLOWLISTED = "CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE"
MSG_NOT_ALLOWLISTED = (
    "Google tar inte emot konverteringar från ADX med uploadClickConversions (inte på "
    "Googles lista sedan 2026-06-15). Uppladdningen är stoppad och raderna står kvar: "
    "exportera dem som CSV i kön, eller byt till Data Manager API "
    "(FLAMINGO_CONVERSIONS_UPLOAD=datamanager)."
)

# ---------------------------------------------------------------------------
# Data Manager API (events:ingest)
# ---------------------------------------------------------------------------

#: Kontotypen för Google Ads i destinationen (ProductAccount.accountType).
DM_ACCOUNT_TYPE = "GOOGLE_ADS"
#: Varifrån händelsen kommer (EventSource, krävs för offline-konverteringar):
#: förfrågan och klicket på numret sker på landningssidan i en webbläsare,
#: och affären följer av dem. Googles exempel för offline-konverteringar
#: använder WEB.
DM_EVENT_SOURCE = "WEB"
#: Lead.ad_consent som Data Manager API:s ConsentStatus. Bara ett svar som
#: besökaren faktiskt gett skickas; tomt utelämnas.
DM_CONSENT = {Lead.CONSENT_GRANTED: "CONSENT_GRANTED", Lead.CONSENT_DENIED: "CONSENT_DENIED"}
#: Konverteringar som skickas per konto och körning (ett anrop var).
DM_MAX_PER_RUN = 100
#: Rader som prövas med validateOnly (flamingo_google_sync --prova).
DM_VALIDATE_MAX = 20
#: Ett nej som gäller hela vägen, inte ett konto eller en rad: vägen stoppas
#: för alla konton (block_uploads) med texten.
DM_REFUSALS = {
    "NOT_ALLOWLISTED": (
        "Google tar inte emot konverteringar från ADX genom Data Manager API (inte på Googles "
        "lista för funktionen). Uppladdningen är stoppad och raderna står kvar: exportera dem "
        "som CSV i kön."
    ),
    "SERVICE_DISABLED": (
        "Data Manager API är inte påslaget i Google Cloud-projektet. Slå på det under API:er "
        "och tjänster i samma projekt som OAuth-klienten hör till, och tryck sedan Försök "
        "ladda upp igen. Raderna står kvar för CSV-filen."
    ),
}
DM_REFUSALS["PROJECT_DISABLED"] = DM_REFUSALS["SERVICE_DISABLED"]
#: Inloggningen saknar behörigheten: Google-sidan ber byrån koppla om.
DM_SCOPE_REASONS = frozenset({"ACCESS_TOKEN_SCOPE_INSUFFICIENT"})
#: Konverteringsåtgärden finns inte, eller är fel sort: glöm den, så letas den
#: upp eller skapas igen vid nästa körning.
DM_ACTION_REASONS = frozenset({"INVALID_CONVERSION_ACTION_ID", "INVALID_CONVERSION_ACTION_TYPE"})
#: Konverteringsåtgärden är för ny: sorten väntar till nästa körning.
DM_ACTION_WAIT = frozenset({"CONVERSION_ACTION_TOO_RECENTLY_CREATED"})
#: Fel som gäller kundens konto eller anropets form, inte en enskild rad:
#: kontots körning stoppas och felet syns på alla dess rader i kö.
DM_ACCOUNT_REASONS = frozenset(
    {
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "INVALID_DESTINATION",
        "DESTINATION_ACCOUNT_TYPE_MISMATCH",
        "OPERATING_ACCOUNT_LOGIN_ACCOUNT_MISMATCH",
        "TERMS_AND_CONDITIONS_NOT_SIGNED",
        "EVENT_SOURCE_AND_DESTINATION_MISMATCH",
        "INVALID_CURRENCY_CODE",
    }
)
#: Tillfälliga fel hos Google (Googles råd: försök bara igen för dem, med
#: backoff): raden försöks igen senare och kontots körning stoppas.
DM_TRANSIENT_STATUSES = frozenset(
    {"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "UNKNOWN", "ABORTED"}
)
DM_TRANSIENT_REASONS = frozenset({"INTERNAL_ERROR", "DEADLINE_EXCEEDED"})
#: Fel i händelsen som ett nytt försök inte ändrar: API:t ger upp om raden
#: direkt, och den står kvar för CSV-filen.
DM_PERMANENT_REASONS = frozenset(
    {"INVALID_AD_IDENTIFIER_FOR_ACCOUNT", "EVENT_TIME_INVALID", "NO_IDENTIFIERS_PROVIDED"}
)
#: Googles fel (ErrorReason) i klartext för byrån.
DM_MESSAGES = {
    "PERMISSION_DENIED": (
        "Inloggningen når inte kundens konto genom Data Manager API. Kontrollera att kontot "
        "ligger under ADX förvaltarkonto och att Google-kontot som kopplades har åtkomst till "
        "förvaltarkontot."
    ),
    "OPERATING_ACCOUNT_LOGIN_ACCOUNT_MISMATCH": (
        "Kundens konto ligger inte under ADX förvaltarkonto enligt Google."
    ),
    "NOT_FOUND": "Google hittar inte kundens konto eller konverteringen.",
    "INVALID_CONVERSION_ACTION_ID": (
        "Konverteringen finns inte längre i kundens konto. Den letas upp eller skapas igen vid "
        "nästa körning."
    ),
    "INVALID_CONVERSION_ACTION_TYPE": "Konverteringen i kundens konto är inte en import av klick.",
    "CONVERSION_ACTION_TOO_RECENTLY_CREATED": (
        "Konverteringen skapades nyss i kundens konto. Skickas igen senare."
    ),
    "TERMS_AND_CONDITIONS_NOT_SIGNED": (
        "Google säger att villkor inte är godkända. Kontrollera att kundens konto godkänt "
        "Googles villkor för kunddata (Mål, Konverteringar, Inställningar)."
    ),
    "EVENT_TIME_INVALID": "Konverteringens tid ligger utanför det Google tar emot.",
    "INVALID_AD_IDENTIFIER_FOR_ACCOUNT": "Klicket hör till ett annat Google Ads-konto.",
    "NO_IDENTIFIERS_PROVIDED": "Konverteringen saknar klick-id.",
    "INVALID_DESTINATION": "Google känner inte igen kundens konto som mottagare.",
    "DESTINATION_ACCOUNT_TYPE_MISMATCH": "Google känner inte igen kundens konto som mottagare.",
    "INTERNAL_ERROR": "Fel hos Google. Skickas igen senare.",
    "DEADLINE_EXCEEDED": "Google svarade inte i tid. Skickas igen senare.",
}

#: Googles besked om en sändning (requestStatus:retrieve) finns tidigast
#: efter en halvtimme, och bearbetningen kan ta upp till ett dygn
#: (data-manager/api/devguides/diagnostics).
DIAGNOSTICS_DELAY = timedelta(minutes=30)
#: Ger Google inget slutligt besked på så här lång tid står raden kvar som
#: skickad med en anteckning.
DIAGNOSTICS_GIVE_UP = timedelta(days=3)
#: Sändningar vars besked läses per konto och körning.
DIAGNOSTICS_PER_RUN = 50
#: Fel när beskedet läses som betyder att det aldrig kommer.
DIAGNOSTICS_GONE = frozenset({"INVALID_REQUEST_ID", "REQUEST_TOO_OLD"})
#: Bearbetningens fel (ProcessingErrorReason, utan prefix) som betyder att
#: Google redan har konverteringen.
DUPLICATE_REASONS = frozenset({"DUPLICATE_GCLID", "DUPLICATE_TRANSACTION_ID"})
#: Fel som ett nytt försök inte ändrar: raden står kvar för CSV-filen men
#: skickas inte igen av sig själv.
PERMANENT_REASONS = frozenset(
    {
        "EVENT_TOO_OLD",
        "INVALID_GCLID",
        "INVALID_GBRAID",
        "INVALID_WBRAID",
        "CONVERSION_PRECEDES_CLICK",
        "INVALID_CLICK",
        "INVALID_OPERATING_ACCOUNT_FOR_CLICK",
        "OPERATING_ACCOUNT_MISMATCH_FOR_AD_IDENTIFIER",
        "ONE_PER_CLICK_CONVERSION_ACTION_NOT_PERMITTED_WITH_BRAID",
    }
)
PROCESSING_MESSAGES = {
    "EVENT_TOO_OLD": "Konverteringen är för gammal för Google.",
    "DENIED_CONSENT": "Google tog inte emot den: samtycket är nej (besökarens eller kontots).",
    "NO_CONSENT": "Google tog inte emot den: kontot saknar samtycke för Googles tjänster.",
    "UNKNOWN_CONSENT": (
        "Google kunde inte avgöra samtycket. Kontrollera kundens inställning för samtycke i "
        "Google Ads."
    ),
    "INVALID_GCLID": "Google kunde inte läsa klick-id:t.",
    "CONVERSION_PRECEDES_CLICK": "Konverteringens tid ligger före klicket.",
    "TOO_RECENT_CLICK": "Klicket var för nytt för Google. Skickas igen.",
    "INVALID_CLICK": "Google kan inte koppla konverteringen till ett annonsklick.",
    "CLICK_NOT_FOUND": "Google hittar inte klicket i kundens konto.",
    "INVALID_OPERATING_ACCOUNT_FOR_CLICK": "Klicket hör till ett annat Google Ads-konto.",
    "OPERATING_ACCOUNT_MISMATCH_FOR_AD_IDENTIFIER": "Klicket hör till ett annat Google Ads-konto.",
    "ONE_PER_CLICK_CONVERSION_ACTION_NOT_PERMITTED_WITH_BRAID": (
        "Google tar inte emot gbraid eller wbraid för en konvertering som räknas en gång per klick."
    ),
    "INTERNAL_ERROR": "Fel hos Google. Skickas igen.",
}
MSG_DUPLICATE = "Fanns redan hos Google."
MSG_NO_VERDICT = "Google gav inget slutligt besked om sändningen."
MSG_NO_REQUEST_ID = "Google tog emot den men gav inget id för sändningen, så beskedet läses inte."

_DESTINATION_PATH = re.compile(r"(?:^|\.)destinations\[\d+\]")
_ACTION_PATH = re.compile(r"product_?destination_?id", re.IGNORECASE)
_ACTION_NAME = re.compile(r"customers/(\d+)/conversionActions/(\d+)")


# ---------------------------------------------------------------------------
# Vägen och kontot
# ---------------------------------------------------------------------------


def _raw_path():
    return str(getattr(settings, "FLAMINGO_CONVERSIONS_UPLOAD", "") or "").strip().lower()


def upload_path():
    """Vägen för konverteringarna (PATH_*): FLAMINGO_CONVERSIONS_UPLOAD, tomt
    är Data Manager API och ett okänt värde räknas som off."""
    value = _raw_path() or PATH_DATAMANAGER
    return value if value in PATH_LABELS else PATH_OFF


def customer_id(account):
    """Kontots id som tio siffror, eller ""."""
    value = google_ads.digits(account.google_ads_customer_id)
    return value if len(value) == 10 else ""


def can_sync(account):
    """Får Flamingo prata med kundens konto hos Google? API:t inkopplat,
    inget demokonto, ett eget id (inget annat Flamingo-konto har det) och
    kontot under ADX förvaltarkonto."""
    return (
        not account.is_demo
        and bool(customer_id(account))
        and account.google_linked
        and google_ads.is_configured()
        and not account.google_id_shared
    )


def _connection():
    return GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).first()


def upload_blocked(path=None):
    """Varför Google stoppade vägen (text), eller "". path är den valda
    vägen om inget annat anges; en spärr utan väg gäller uploadClickConversions."""
    path = path or upload_path()
    connection = _connection()
    if connection is None or connection.conversion_upload_blocked_at is None:
        return ""
    if (connection.conversion_upload_blocked_path or PATH_GOOGLEADS) != path:
        return ""
    return connection.conversion_upload_error or MSG_NOT_ALLOWLISTED


def upload_enabled():
    """Laddas konverteringarna upp med API:t nu? Vägen är inte off, Google
    Ads API är inkopplat, Google har inte stoppat vägen och, för Data Manager
    API, inloggningen har behörigheten. Annars går de som CSV."""
    path = upload_path()
    if path == PATH_OFF or not google_ads.is_configured() or upload_blocked(path):
        return False
    if path == PATH_DATAMANAGER:
        return google_ads.datamanager_scope_state() == google_ads.SCOPE_GRANTED
    return True


def verdicts_enabled():
    """Kan Googles besked om det som skickats med Data Manager API läsas?
    Oavsett vilken väg som är vald nu: en rad som skickats och sedan inte
    tagits emot ska tillbaka i kön även om vägen bytts."""
    return (
        google_ads.is_configured()
        and google_ads.datamanager_scope_state() == google_ads.SCOPE_GRANTED
        and not upload_blocked(PATH_DATAMANAGER)
    )


def block_uploads(now=None, message=MSG_NOT_ALLOWLISTED, path=PATH_GOOGLEADS):
    """Google släpper inte in ADX den vägen: stoppa den för alla konton tills
    byrån ber om ett nytt försök (unblock_uploads)."""
    now = now or timezone.now()
    GoogleAdsConnection.objects.get_or_create(pk=GoogleAdsConnection.SOLO_PK)
    GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        conversion_upload_blocked_at=now,
        conversion_upload_error=message[:300],
        conversion_upload_blocked_path=path,
    )
    logger.warning("Google: uppladdningen av konverteringar (%s) stoppad: %s", path, message)


def unblock_uploads():
    GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        conversion_upload_blocked_at=None,
        conversion_upload_error="",
        conversion_upload_blocked_path="",
    )


def retry_now():
    """ "Försök ladda upp igen": rader i kö som väntar efter ett nej, eller som
    API:t gett upp om, skickas vid nästa körning. Inte rader som varit med i
    en nedladdad fil: de går den vägen. Antal rader."""
    return (
        ConversionUpload.objects.filter(
            status=ConversionUpload.STATUS_QUEUED,
            lead__account__is_demo=False,
            downloaded_at__isnull=True,
        )
        .filter(Q(attempts__gt=0) | Q(next_attempt_at__isnull=False))
        .update(attempts=0, next_attempt_at=None)
    )


def upload_status():
    """Läget för Google-sidan och kön: vägen, om API:t laddar upp nu och
    annars varför, och om byrån ska koppla om med Google. Inga nycklar."""
    path = upload_path()
    configured = google_ads.is_configured()
    blocked = upload_blocked(path) if path != PATH_OFF else ""
    env_token = bool(str(getattr(settings, "GOOGLE_ADS_REFRESH_TOKEN", "") or "").strip())
    scope = google_ads.datamanager_scope_state() if path == PATH_DATAMANAGER else ""
    needs_reconnect = (
        path == PATH_DATAMANAGER
        and google_ads.has_refresh_token()
        and scope != google_ads.SCOPE_GRANTED
    )
    active = upload_enabled()
    reason = ""
    if not active:
        if path == PATH_OFF:
            reason = MSG_UNKNOWN_PATH if _raw_path() not in ("", PATH_OFF) else MSG_OFF
        elif not configured:
            reason = MSG_NOT_CONFIGURED
        elif blocked:
            reason = blocked
        elif needs_reconnect:
            reason = MSG_RECONNECT_ENV if env_token else MSG_RECONNECT
    real = ConversionUpload.objects.filter(lead__account__is_demo=False)
    stalled = real.filter(
        status=ConversionUpload.STATUS_QUEUED,
        attempts__gte=ConversionUpload.MAX_ATTEMPTS,
        downloaded_at__isnull=True,
    ).count()
    awaiting = (
        real.filter(status=ConversionUpload.STATUS_SENT, checked_at__isnull=True)
        .exclude(request_id="")
        .count()
    )
    return {
        "path": path,
        "path_label": PATH_LABELS[path],
        "active": active,
        "reason": reason,
        "blocked": blocked,
        "needs_reconnect": needs_reconnect,
        "env_token": env_token,
        "scope_state": scope,
        "stalled": stalled,
        "awaiting": awaiting,
        "max_attempts": ConversionUpload.MAX_ATTEMPTS,
    }


# ---------------------------------------------------------------------------
# Konverteringsåtgärderna (Google Ads API)
# ---------------------------------------------------------------------------


def _action(kind, name):
    return {
        "name": name,
        "type": "UPLOAD_CLICKS",
        "category": CATEGORIES[kind],
        "status": "ENABLED",
        "countingType": "ONE_PER_CLICK",
        "valueSettings": {
            "defaultValue": 0,
            "defaultCurrencyCode": exports.CONVERSION_CURRENCY,
            "alwaysUseDefaultValue": False,
        },
    }


def _existing_actions(customer, names):
    """{namn: (resursnamn, typ)} för konverteringarna i kontot med de
    namnen (borttagna räknas inte)."""
    listed = ", ".join(google_ads.gaql_string(name) for name in names)
    query = (
        "SELECT conversion_action.resource_name, conversion_action.name, "
        "conversion_action.type, conversion_action.status FROM conversion_action "
        f"WHERE conversion_action.name IN ({listed}) "
        "AND conversion_action.status != 'REMOVED'"
    )
    found = {}
    for row in google_ads.search(customer, query):
        action = row.get("conversionAction") or {}
        name = action.get("name")
        if name and action.get("resourceName") and name not in found:
            found[name] = (action["resourceName"], action.get("type") or "")
    return found


def ensure_conversion_actions(account):
    """Konverteringarna för förfrågan, samtal och affär i kundens konto, som
    {sort: resursnamn}. De som saknas letas upp med namn (byrån kan ha skapat
    affären för CSV-importen) och skapas annars, i ett anrop. Anropas igen
    utan att något händer hos Google när alla redan är sparade.

    Kastar GoogleAdsError (demokonto, inget id, fel från Google, eller en
    konvertering med samma namn som inte är en import av klick)."""
    google_ads.ensure_not_demo(account)
    customer = customer_id(account)
    if not customer:
        raise GoogleAdsError(google_ads.MSG_BAD_CUSTOMER_ID, status="INVALID_CUSTOMER_ID")
    names = exports.conversion_names()
    stored = {
        kind: value
        for kind, value in (account.google_conversion_actions or {}).items()
        if kind in names and value
    }
    missing = [kind for kind in names if kind not in stored]
    if not missing:
        return stored

    problem = None
    found = _existing_actions(customer, [names[kind] for kind in missing])
    to_create = []
    for kind in missing:
        hit = found.get(names[kind])
        if hit is None:
            to_create.append(kind)
        elif hit[1] and hit[1] != "UPLOAD_CLICKS":
            problem = problem or GoogleAdsError(
                MSG_WRONG_TYPE.format(name=names[kind]), status="CONVERSION_ACTION_TYPE"
            )
        else:
            stored[kind] = hit[0]
    if to_create:
        payload = google_ads.request(
            "POST",
            f"customers/{customer}/conversionActions:mutate",
            {
                "operations": [{"create": _action(kind, names[kind])} for kind in to_create],
                "partialFailure": False,
                "validateOnly": False,
            },
        )
        for kind, result in zip(to_create, payload.get("results") or [], strict=False):
            if isinstance(result, dict) and result.get("resourceName"):
                stored[kind] = result["resourceName"]
        logger.info("Google Ads: konverteringar skapade för konto %s: %s", account.pk, to_create)
    _save_actions(account, stored)
    if problem is not None:
        raise problem
    return stored


def _save_actions(account, stored):
    FlamingoAccount.objects.filter(pk=account.pk).update(google_conversion_actions=stored)
    account.google_conversion_actions = stored


def _forget_actions(account, kinds):
    """Glöm konverteringsåtgärderna för sorterna: de letas upp eller skapas
    igen vid nästa körning (ensure_conversion_actions)."""
    if not kinds:
        return
    stored = dict(account.google_conversion_actions or {})
    _save_actions(account, {k: v for k, v in stored.items() if k not in kinds})


def _actions_for(account, pending):
    """Konverteringsåtgärderna för sorterna i pending, skapade om de saknas.
    En som inte går att skapa (samma namn, annan typ) låter den sortens rader
    vänta medan de andra skickas; felet syns via kommandot."""
    actions = dict(account.google_conversion_actions or {})
    needed = set(pending.values_list("kind", flat=True).distinct())
    if any(not actions.get(kind) for kind in needed):
        try:
            actions = ensure_conversion_actions(account)
        except GoogleAdsError as error:
            if error.status != "CONVERSION_ACTION_TYPE":
                raise
            logger.warning("Google Ads: konto %s: %s", account.pk, error.message)
            actions = dict(account.google_conversion_actions or {})
    return actions


def _action_id(action, customer):
    """Konverteringsåtgärdens id (siffror) ur resursnamnet, bara om det
    gäller kundens konto; annars ""."""
    match = _ACTION_NAME.fullmatch(str(action or ""))
    if not match or match.group(1) != customer:
        return ""
    return match.group(2)


# ---------------------------------------------------------------------------
# Raderna i kö
# ---------------------------------------------------------------------------


def _empty_result():
    return {"sent": 0, "failed": 0, "waiting": 0}


def backoff(attempts):
    """Väntan efter attempts försök som inte gått fram: 1, 2, 4, 8, 16 och
    sedan 24 timmar."""
    hours = BACKOFF_FIRST * (2 ** max(int(attempts) - 1, 0))
    return min(hours, BACKOFF_MAX)


def _due(account, now):
    """Raderna i kö som API:t ska skicka nu: med gclid, förfrågan minst
    CLICK_SETTLE gammal, inte med i en nedladdad fil, och inte väntande
    efter ett nej eller uppgivna."""
    return (
        ConversionUpload.objects.filter(
            lead__account=account,
            status=ConversionUpload.STATUS_QUEUED,
            downloaded_at__isnull=True,
            lead__created_at__lte=now - CLICK_SETTLE,
            attempts__lt=ConversionUpload.MAX_ATTEMPTS,
        )
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .exclude(lead__gclid="")
    )


def _stop_ineligible(account):
    """Rader i kö som aldrig kan skickas (inget gclid) blir misslyckade med
    orsaken."""
    queued = ConversionUpload.objects.filter(
        lead__account=account, status=ConversionUpload.STATUS_QUEUED
    )
    return queued.filter(lead__gclid="").update(
        status=ConversionUpload.STATUS_FAILED, error=MSG_NO_GCLID
    )


def _retry_later(upload, message, now, permanent=False):
    """Försöket gick inte fram: raden står kvar i kö (och i CSV-filen) med
    felet, ett försök räknas och nästa väntar (backoff). permanent: ett nytt
    försök ändrar inget, så API:t ger upp direkt."""
    upload.status = ConversionUpload.STATUS_QUEUED
    upload.attempts = (
        ConversionUpload.MAX_ATTEMPTS
        if permanent
        else min(upload.attempts + 1, ConversionUpload.MAX_ATTEMPTS)
    )
    upload.next_attempt_at = None if upload.api_gave_up else now + backoff(upload.attempts)
    upload.error = str(message or MSG_ROW_FAILED)[:300]


def _note(uploads, message):
    """Ett fel som inte är radens (inloggningen, kvoten, kundens konto):
    felet syns på raderna men inget försök räknas. uploads är rader eller en
    queryset."""
    if isinstance(uploads, list | tuple | set):
        rows = ConversionUpload.objects.filter(pk__in=[u.pk for u in uploads])
    else:
        rows = ConversionUpload.objects.filter(pk__in=uploads.values("pk"))
    rows.filter(status=ConversionUpload.STATUS_QUEUED).update(error=str(message)[:300])


def upload_queued(account, now=None, validate_only=False):
    """Skicka kontots konverteringar i kö den valda vägen (upload_path).
    Returnerar {"sent", "failed", "waiting"} (antal rader; failed står kvar i
    kö med felet). validate_only (bara Data Manager API): Google prövar
    raderna men tar inte emot dem, och inget ändras här."""
    path = upload_path()
    if path == PATH_DATAMANAGER:
        return upload_via_data_manager(account, now=now, validate_only=validate_only)
    if path == PATH_GOOGLEADS and not validate_only:
        return upload_via_google_ads(account, now=now)
    return _empty_result()


def _refused(error, path):
    """Ett nej för hela vägen som GoogleAdsError med status UPLOAD_NOT_ALLOWED,
    efter att vägen stoppats (eller behörigheten markerats som saknad); None
    om felet inte är ett sådant nej."""
    names = set(error.code_names)
    if path == PATH_DATAMANAGER and names & DM_SCOPE_REASONS:
        google_ads.forget_datamanager_scope()
        message = MSG_RECONNECT
    elif path == PATH_DATAMANAGER and names & set(DM_REFUSALS):
        message = next(DM_REFUSALS[name] for name in DM_REFUSALS if name in names)
        block_uploads(message=message, path=path)
    elif path == PATH_GOOGLEADS and NOT_ALLOWLISTED in names:
        message = MSG_NOT_ALLOWLISTED
        block_uploads(message=message, path=path)
    else:
        return None
    return GoogleAdsError(
        message,
        status="UPLOAD_NOT_ALLOWED",
        codes=error.codes,
        request_id=error.request_id,
        http_status=error.http_status,
    )


def _send_locked(account, pending, send):
    """Skicka pending i omgångar om BATCH rader, låsta medan de skickas
    (SELECT ... FOR UPDATE SKIP LOCKED): två körningar samtidigt skickar
    aldrig samma rad, och exporten eller inkorgen ändrar inte en rad som är
    på väg. send(batch) gör anropen och sparar raderna; den returnerar ett
    GoogleAdsError som kastas när omgångens ändringar är sparade."""
    seen = set()
    while True:
        error = None
        with transaction.atomic():
            batch = list(
                pending.exclude(pk__in=seen)
                .select_related("lead")
                .select_for_update(skip_locked=True, of=("self",))
                .order_by("created_at", "pk")[:BATCH]
            )
            seen.update(upload.pk for upload in batch)
            if batch:
                error = send(batch)
        if error is not None:
            raise error
        if len(batch) < BATCH:
            return


# ---------------------------------------------------------------------------
# Data Manager API
# ---------------------------------------------------------------------------


def conversion_event(upload):
    """En konvertering som Data Manager API vill ha den (Event i JSON-form,
    data-manager/api/devguides/events/google-ads/offline/send-events).
    consent finns med bara när förfrågan har ett riktigt svar (DM_CONSENT),
    värdet och valutan bara för affärer. Bara gclid, aldrig gbraid eller
    wbraid (se modulens text)."""
    event = {
        "transactionId": upload.transaction_id,
        "eventTimestamp": google_ads.rfc3339(exports.conversion_moment(upload)),
        "eventSource": DM_EVENT_SOURCE,
        "adIdentifiers": {"gclid": upload.lead.gclid.strip()},
    }
    if upload.kind == ConversionUpload.KIND_DEAL and upload.value_kr is not None:
        event["conversionValue"] = float(upload.value_kr)
        event["currency"] = exports.CONVERSION_CURRENCY
    consent = DM_CONSENT.get(upload.lead.ad_consent)
    if consent:
        event["consent"] = {"adUserData": consent}
    return event


def destination(customer, action_id):
    """Kundens konto som mottagare, genom ADX förvaltarkonto
    (data-manager/api/devguides/concepts/destinations): operatingAccount är
    kundens konto, loginAccount förvaltarkontot och productDestinationId
    konverteringsåtgärdens id."""
    return {
        "operatingAccount": {"accountType": DM_ACCOUNT_TYPE, "accountId": customer},
        "loginAccount": {"accountType": DM_ACCOUNT_TYPE, "accountId": google_ads.mcc_id()},
        "productDestinationId": action_id,
    }


def ingest_body(upload, customer, action_id, validate_only=False):
    """Anropet till events:ingest för en rad: en destination och en händelse.
    Med en destination behövs varken reference eller destinationReferences."""
    return {
        "destinations": [destination(customer, action_id)],
        "events": [conversion_event(upload)],
        "validateOnly": bool(validate_only),
    }


def _dm_message(error):
    """Googles fel för ett anrop i klartext för byrån."""
    for name in error.code_names:
        if name in DM_MESSAGES:
            return DM_MESSAGES[name]
    return error.message


_EVENT, _ACTION, _ACCOUNT, _TRANSIENT = "event", "action", "account", "transient"


def _error_kind(error):
    """Vad ett nej till events:ingest gäller: händelsen (_EVENT),
    konverteringsåtgärden (_ACTION), kundens konto eller anropets form
    (_ACCOUNT), eller ett tillfälligt fel hos Google (_TRANSIENT)."""
    names = set(error.code_names)
    paths = [str(item.get("field_path") or "") for item in error.errors]
    if names & (DM_ACTION_REASONS | DM_ACTION_WAIT) or any(_ACTION_PATH.search(p) for p in paths):
        return _ACTION
    if (
        names & DM_ACCOUNT_REASONS
        or error.status == "PERMISSION_DENIED"
        or any(_DESTINATION_PATH.search(p) for p in paths)
    ):
        return _ACCOUNT
    status = error.http_status
    if (
        status is None
        or status == 404
        or status >= 500
        or error.status in DM_TRANSIENT_STATUSES
        or names & DM_TRANSIENT_REASONS
    ):
        return _TRANSIENT
    return _EVENT


_SAVED = ["status", "sent_at", "error", "attempts", "next_attempt_at", "request_id", "checked_at"]


def _warning(payload):
    """Svarets fieldWarnings (reason, description, field) som en kort text."""
    parts = []
    for item in payload.get("fieldWarnings") or []:
        if isinstance(item, dict):
            text = f"{item.get('reason') or ''}: {item.get('description') or ''}".strip(": ")
            if text:
                parts.append(text)
    return google_ads.scrub("; ".join(parts))[:300]


def _mark_sent(upload, now, request_id, event, warning=""):
    """Google tog emot anropet: raden är skickad och inte längre i kö (eller
    i CSV-filen). Utan requestId finns inget besked att läsa."""
    upload.status = ConversionUpload.STATUS_SENT
    upload.sent_at = now
    upload.error = ""
    upload.next_attempt_at = None
    upload.request_id = request_id
    upload.checked_at = None if request_id else now
    upload.response = {
        "api": "datamanager",
        "request_id": request_id,
        "transaction_id": event["transactionId"],
        "event_timestamp": event["eventTimestamp"],
    }
    if not request_id:
        upload.response["note"] = MSG_NO_REQUEST_ID
    if warning:
        upload.response["warning"] = warning


class _Run:
    """En körning för ett konto: resultatet, konverteringsåtgärderna, sorter
    som väntar till nästa körning och raderna som ska skickas."""

    def __init__(self, account, customer, actions, pending):
        self.account = account
        self.customer = customer
        self.actions = actions
        self.pending = pending
        self.paused = set()
        self.result = _empty_result()


def _ingest_failure(run, upload, error, now):
    """Google tog inte emot anropet med raden (låst av anroparen). Raden
    sparas; returnerar ett GoogleAdsError att kasta (kontots körning stoppas)
    eller None (nästa rad skickas)."""
    refused = _refused(error, PATH_DATAMANAGER)
    if refused is not None:
        # Hela vägen: raden är orörd och skickas när vägen är öppen igen.
        return refused
    if error.is_auth_error or error.is_quota_error:
        _note([upload], error.message)
        return error
    kind = _error_kind(error)
    message = _dm_message(error)
    if kind == _ACTION:
        # Raden är inte fel: den väntar utan att räkna ett försök.
        if set(error.code_names) & DM_ACTION_REASONS or any(
            _ACTION_PATH.search(str(item.get("field_path") or "")) for item in error.errors
        ):
            _forget_actions(run.account, {upload.kind})
            run.actions.pop(upload.kind, None)
        run.paused.add(upload.kind)
        _note([upload], message)
        run.result["waiting"] += 1
        return None
    permanent = kind == _EVENT and bool(set(error.code_names) & DM_PERMANENT_REASONS)
    _retry_later(upload, message, now, permanent=permanent)
    upload.save(update_fields=_SAVED)
    run.result["failed"] += 1
    logger.warning(
        "Data Manager: konto %s, rad %s står kvar i kö (%s, %s)",
        run.account.pk,
        upload.pk,
        kind,
        error.codes[:5],
    )
    if kind == _ACCOUNT:
        _note(run.pending.exclude(pk=upload.pk), message)
        return GoogleAdsError(
            message,
            status=error.status,
            codes=error.codes,
            request_id=error.request_id,
            http_status=error.http_status,
        )
    if kind == _TRANSIENT:
        return error
    return None


def _send_one(run, upload, action_id, now):
    """Skicka en rad (låst av anroparen) med events:ingest och spara den.
    Returnerar ett GoogleAdsError att kasta, eller None."""
    body = ingest_body(upload, run.customer, action_id)
    try:
        payload = google_ads.datamanager_request("POST", "events:ingest", body)
    except GoogleAdsError as error:
        return _ingest_failure(run, upload, error, now)
    request_id = google_ads.scrub(payload.get("requestId"))[:100]
    _mark_sent(upload, now, request_id, body["events"][0], _warning(payload))
    upload.save(update_fields=[*_SAVED, "response"])
    run.result["sent"] += 1
    return None


def upload_via_data_manager(account, now=None, validate_only=False):
    """Skicka kontots konverteringar i kö med Data Manager API (events:ingest),
    en per anrop och högst DM_MAX_PER_RUN. Returnerar {"sent", "failed",
    "waiting"} (antal rader).

    Varje rad låses (FOR UPDATE SKIP LOCKED) medan den skickas och sparas i
    samma transaktion. En rad blir skickad när Google tagit emot anropet;
    Googles besked om bearbetningen läses efteråt (check_sent). Ett nej för
    en händelse lämnar den raden i kö med felet och nästa rad skickas. Ett
    fel för kundens konto, ett tillfälligt fel hos Google, inloggningen eller
    kvoten stoppar kontots körning och kastas som GoogleAdsError; ett nej
    till hela vägen kastas med status UPLOAD_NOT_ALLOWED (raderna orörda).

    validate_only: validateOnly i anropen (_validate). Inget ändras här."""
    if upload_path() != PATH_DATAMANAGER or not can_sync(account) or not upload_enabled():
        return _empty_result()
    now = now or timezone.now()
    if validate_only:
        return _validate(account, now)
    stopped = _stop_ineligible(account)
    pending = _due(account, now)
    if not pending.exists():
        return {**_empty_result(), "failed": stopped}
    run = _Run(account, customer_id(account), _actions_for(account, pending), pending)
    run.result["failed"] += stopped
    seen = set()
    for _ in range(DM_MAX_PER_RUN):
        error = None
        with transaction.atomic():
            upload = (
                pending.exclude(pk__in=seen)
                .exclude(kind__in=run.paused)
                .select_related("lead")
                .select_for_update(skip_locked=True, of=("self",))
                .order_by("created_at", "pk")
                .first()
            )
            if upload is None:
                break
            seen.add(upload.pk)
            action_id = _action_id(run.actions.get(upload.kind), run.customer)
            if action_id:
                error = _send_one(run, upload, action_id, now)
            else:
                run.result["waiting"] += 1
        if error is not None:
            raise error
    return run.result


def _validate(account, now):
    """validateOnly för raderna som skulle skickas nu (högst DM_VALIDATE_MAX),
    en per anrop. Google prövar dem men tar inte emot något, och inget ändras
    här. Returnerar {"validated": antal godkända, "invalid": [(rad-id, text)]};
    ett nej för hela vägen, inloggningen eller kvoten kastas."""
    actions = dict(account.google_conversion_actions or {})
    customer = customer_id(account)
    result = {"validated": 0, "invalid": []}
    rows = _due(account, now).select_related("lead").order_by("created_at", "pk")
    for upload in rows[:DM_VALIDATE_MAX]:
        action_id = _action_id(actions.get(upload.kind), customer)
        if not action_id:
            continue
        body = ingest_body(upload, customer, action_id, validate_only=True)
        try:
            google_ads.datamanager_request("POST", "events:ingest", body)
        except GoogleAdsError as error:
            refused = _refused(error, PATH_DATAMANAGER)
            if refused is not None:
                raise refused from None
            if error.is_auth_error or error.is_quota_error:
                raise
            result["invalid"].append((upload.pk, _dm_message(error)))
            continue
        result["validated"] += 1
    return result


# ---------------------------------------------------------------------------
# Googles besked om det som skickats (Data Manager API)
# ---------------------------------------------------------------------------


def _processing_name(reason):
    text = str(reason or "")
    for prefix in ("PROCESSING_ERROR_REASON_", "PROCESSING_ERROR_"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _error_counts(item):
    """{orsak utan prefix: antal} ur errorInfo.errorCounts."""
    counts = {}
    for entry in (item.get("errorInfo") or {}).get("errorCounts") or []:
        if not isinstance(entry, dict):
            continue
        name = re.sub(r"[^A-Z0-9_]", "", _processing_name(entry.get("reason")))[:80]
        try:
            count = int(entry.get("recordCount") or 0)
        except (TypeError, ValueError):
            count = 0
        if name:
            counts[name] = counts.get(name, 0) + count
    return counts


def _reason_text(counts):
    real = [name for name in counts if name not in DUPLICATE_REASONS]
    if len(real) == 1:
        return PROCESSING_MESSAGES.get(real[0]) or f"Google tog inte emot den ({real[0]})."
    if real:
        return "Google tog inte emot den (" + ", ".join(sorted(real)) + ")."
    return MSG_ROW_FAILED


def _settle(upload, now, status, note=""):
    """Beskedet är läst och raden står kvar som skickad."""
    upload.checked_at = now
    upload.response = {**(upload.response or {}), "status": status}
    if note:
        upload.response["note"] = note
    upload.save(update_fields=["checked_at", "response"])


def _requeue(upload, now, request_id, message, permanent=False):
    """Google tog inte emot raden: den går tillbaka till kön (och därmed till
    CSV-filen), med felet och ett försök räknat."""
    _retry_later(upload, message, now, permanent=permanent)
    upload.sent_at = None
    upload.request_id = ""
    upload.checked_at = None
    upload.response = {**(upload.response or {}), "previous_request_id": request_id}
    upload.save(update_fields=[*_SAVED, "response"])


def _wait_or_give_up(members, now, status, result):
    for upload in members:
        if upload.sent_at and upload.sent_at <= now - DIAGNOSTICS_GIVE_UP:
            _settle(upload, now, status, MSG_NO_VERDICT)
            result["confirmed"] += 1
        else:
            result["pending"] += 1


def _apply_status(members, item, request_id, now, result):
    """Googles besked för sändningens destination. Ett anrop har en rad, så
    beskedet gäller den:

    - SUCCESS: raden står kvar som skickad.
    - FAILED (eller PARTIAL_SUCCESS) där allt som inte gick fram redan fanns
      hos Google (DUPLICATE_*): skickad, med en anteckning.
    - FAILED av ett annat skäl: tillbaka i kön med felet. Ett fel som ett
      nytt försök inte ändrar (PERMANENT_REASONS) gör att API:t ger upp om
      raden; den står kvar för CSV-filen.
    - PROCESSING: läses igen nästa körning."""
    status = str(item.get("requestStatus") or "")
    counts = _error_counts(item)
    duplicates = {name for name in counts if name in DUPLICATE_REASONS}
    real = set(counts) - duplicates
    if status == "SUCCESS" or (status in ("FAILED", "PARTIAL_SUCCESS") and duplicates and not real):
        note = MSG_DUPLICATE if duplicates else ""
        for upload in members:
            _settle(upload, now, status, note)
        result["confirmed"] += len(members)
    elif status in ("FAILED", "PARTIAL_SUCCESS"):
        message = _reason_text(counts)
        permanent = bool(real) and real <= PERMANENT_REASONS
        for upload in members:
            _requeue(upload, now, request_id, message, permanent=permanent)
        result["requeued"] += len(members)
        logger.warning(
            "Data Manager: sändningen %s gav %s för %s rader (%s)",
            request_id,
            status,
            len(members),
            sorted(counts)[:5],
        )
    else:
        _wait_or_give_up(members, now, status or "PROCESSING", result)


def awaiting_verdict(account, now=None):
    """Kontots rader som skickats med Data Manager API och vars besked kan
    läsas nu (minst DIAGNOSTICS_DELAY gamla)."""
    now = now or timezone.now()
    return (
        ConversionUpload.objects.filter(
            lead__account=account,
            status=ConversionUpload.STATUS_SENT,
            checked_at__isnull=True,
            sent_at__lte=now - DIAGNOSTICS_DELAY,
        )
        .exclude(request_id="")
        .order_by("sent_at", "pk")
    )


def check_sent(account, now=None):
    """Läs Googles besked om kontots sändningar med Data Manager API
    (GET requestStatus:retrieve?requestId=...), tidigast DIAGNOSTICS_DELAY
    efter sändningen och högst DIAGNOSTICS_PER_RUN per körning. Körs oavsett
    vilken väg som är vald nu (verdicts_enabled). Se _apply_status. Kan
    beskedet aldrig läsas (INVALID_REQUEST_ID, REQUEST_TOO_OLD) står raden
    kvar som skickad med en anteckning, och efter DIAGNOSTICS_GIVE_UP
    likaså.

    Returnerar {"confirmed", "requeued", "pending"}. Kastar GoogleAdsError
    för fel i inloggningen, kvoten och nej för hela vägen."""
    result = {"confirmed": 0, "requeued": 0, "pending": 0}
    if account.is_demo or not verdicts_enabled():
        return result
    now = now or timezone.now()
    rows = awaiting_verdict(account, now)
    request_ids = list(dict.fromkeys(rows.values_list("request_id", flat=True)))
    for request_id in request_ids[:DIAGNOSTICS_PER_RUN]:
        members = list(rows.filter(request_id=request_id))
        try:
            payload = google_ads.datamanager_request(
                "GET", "requestStatus:retrieve", params={"requestId": request_id}
            )
        except GoogleAdsError as error:
            refused = _refused(error, PATH_DATAMANAGER)
            if refused is not None:
                raise refused from None
            if error.is_auth_error or error.is_quota_error:
                raise
            if DIAGNOSTICS_GONE & set(error.code_names):
                for upload in members:
                    _settle(upload, now, "UNKNOWN", MSG_NO_VERDICT)
                result["confirmed"] += len(members)
            else:
                logger.warning("Data Manager: beskedet för %s lästes inte: %s", request_id, error)
                _wait_or_give_up(members, now, "UNKNOWN", result)
            continue
        statuses = [
            item
            for item in payload.get("requestStatusPerDestination") or []
            if isinstance(item, dict)
        ]
        if statuses:
            _apply_status(members, statuses[0], request_id, now, result)
        else:
            _wait_or_give_up(members, now, "PROCESSING", result)
    return result


# ---------------------------------------------------------------------------
# Google Ads API (uploadClickConversions)
# ---------------------------------------------------------------------------

#: Lead.ad_consent som Google Ads API:s Consent.adUserData. Bara ett svar som
#: besökaren faktiskt gett skickas; tomt utelämnas.
CONSENT_VALUES = {Lead.CONSENT_GRANTED: "GRANTED", Lead.CONSENT_DENIED: "DENIED"}


def click_conversion(upload, action):
    """En rad som Google Ads API vill ha den (ClickConversion i REST-form).
    consent finns med bara när förfrågan har ett riktigt svar
    (CONSENT_VALUES). orderId är samma id som Data Manager API får
    (transaction_id), så att ingen väg räknar en konvertering två gånger."""
    item = {
        "gclid": upload.lead.gclid,
        "conversionAction": action,
        "conversionDateTime": google_ads.google_datetime(exports.conversion_moment(upload)),
        "currencyCode": exports.CONVERSION_CURRENCY,
        "orderId": upload.transaction_id,
    }
    consent = CONSENT_VALUES.get(upload.lead.ad_consent)
    if consent:
        item["consent"] = {"adUserData": consent}
    if upload.kind == ConversionUpload.KIND_DEAL and upload.value_kr is not None:
        item["conversionValue"] = float(upload.value_kr)
    return item


def _row_message(error):
    name = str(error.get("code") or "").rsplit(".", 1)[-1]
    if name in ROW_MESSAGES:
        return ROW_MESSAGES[name]
    text = str(error.get("message") or "").strip()
    return (f"Google: {text}" if text else MSG_ROW_FAILED)[:300]


def upload_via_google_ads(account, now=None):
    """Skicka kontots konverteringar i kö med uploadClickConversions.
    Returnerar {"sent", "failed", "waiting"} (antal rader).

    Kastar GoogleAdsError för ett fel som gäller hela anropet; raderna står
    då kvar i kö med felet. Säger Google att ADX inte får ladda upp
    (NOT_ALLOWLISTED, för anropet eller en rad) stoppas vägen för alla
    konton (block_uploads) och felet kastas med status UPLOAD_NOT_ALLOWED;
    raderna står kvar i kö."""
    result = _empty_result()
    if upload_path() != PATH_GOOGLEADS or not can_sync(account) or not upload_enabled():
        return result
    now = now or timezone.now()
    result["failed"] += _stop_ineligible(account)
    pending = _due(account, now)
    if not pending.exists():
        return result
    actions = _actions_for(account, pending)
    customer = customer_id(account)

    def send(batch):
        try:
            with transaction.atomic():
                _send_batch(account, customer, batch, actions, now, result)
        except GoogleAdsError as error:
            refused = _refused(error, PATH_GOOGLEADS)
            if refused is not None:
                return refused
            # Hela anropet gick inte: raderna står kvar i kö, med felet noterat.
            _note(batch, error.message)
            return error
        return None

    _send_locked(account, pending, send)
    return result


def _send_batch(account, customer, batch, actions, now, result):
    """Ett anrop med raderna i batch (låsta av anroparen)."""
    sendable = [upload for upload in batch if actions.get(upload.kind)]
    for upload in batch:
        if upload not in sendable:
            result["waiting"] += 1
    if not sendable:
        return
    body = {
        "conversions": [click_conversion(u, actions[u.kind]) for u in sendable],
        "partialFailure": True,
        "validateOnly": False,
    }
    payload = google_ads.request("POST", f"customers/{customer}:uploadClickConversions", body)
    failure = google_ads.partial_failure_error(payload)
    if failure is not None and NOT_ALLOWLISTED in failure.code_names:
        # Gäller ADX, inte raden: inget sparas (transaktionen rullas
        # tillbaka) och raderna står kvar i kö.
        raise failure
    by_index = {}
    for error in failure.errors if failure is not None else []:
        if error.get("index") is not None:
            by_index.setdefault(error["index"], error)
    results = payload.get("results")
    results = results if isinstance(results, list) else []
    job = payload.get("jobId")
    lost_kinds = set()

    for index, upload in enumerate(sendable):
        error = by_index.get(index)
        returned = results[index] if index < len(results) else None
        if error is None and failure is not None and not returned:
            # Google sa att något gick fel men inte var: raden fick inget svar.
            error = {"code": "", "message": failure.message}
        response = {
            "api": "uploadClickConversions",
            "conversion_date_time": body["conversions"][index]["conversionDateTime"],
        }
        if job is not None:
            response["job_id"] = str(job)[:40]
        name = str((error or {}).get("code") or "").rsplit(".", 1)[-1]
        if error is None or name in ALREADY_CODES:
            if name:
                response["note"] = "Fanns redan hos Google."
            upload.status = ConversionUpload.STATUS_SENT
            upload.sent_at = now
            upload.error = ""
            upload.next_attempt_at = None
            result["sent"] += 1
        elif name in RETRY_CODES:
            upload.error = _row_message(error)
            if name == "NO_CONVERSION_ACTION_FOUND":
                lost_kinds.add(upload.kind)
            result["waiting"] += 1
        else:
            # Raden står kvar i kö (och i CSV-filen) med felet, och nästa
            # försök väntar.
            _retry_later(upload, _row_message(error), now)
            response["code"] = name[:80]
            result["failed"] += 1
        upload.response = response
        upload.save(
            update_fields=["status", "sent_at", "error", "attempts", "next_attempt_at", "response"]
        )

    if lost_kinds:
        # Konverteringen har tagits bort hos Google: glöm den, så skapas den
        # igen vid nästa körning (ensure_conversion_actions).
        _forget_actions(account, lost_kinds)
    if failure is not None:
        logger.warning(
            "Google Ads: uppladdningen för konto %s gav fel för %s rader (%s)",
            account.pk,
            len(by_index) or len(sendable),
            failure.codes[:5],
        )


# ---------------------------------------------------------------------------
# Byråns kö
# ---------------------------------------------------------------------------


def queue_context(include_demo=False):
    """Det byråns kö visar om konverteringarna: vägen och om API:t laddar
    upp dem (uploads_via_api) eller varför inte, namnen de har i kundernas
    konton och de senaste som inte kan skickas alls (utan demokontot, om
    byrån inte bett om det)."""
    names = exports.conversion_names()
    labels = dict(ConversionUpload.KIND_CHOICES)
    failed = ConversionUpload.objects.filter(status=ConversionUpload.STATUS_FAILED)
    if not include_demo:
        failed = failed.filter(lead__account__is_demo=False)
    status = upload_status()
    return {
        "google_api_on": google_ads.is_configured(),
        "uploads_via_api": status["active"],
        "upload_blocked": status["blocked"],
        "conversions_upload": status,
        "conversion_names": [(labels[kind], name) for kind, name in names.items()],
        "failed_uploads": failed.select_related("lead__account__customer").order_by(
            "-created_at", "-pk"
        )[:10],
    }
