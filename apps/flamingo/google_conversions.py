"""
Konverteringarna till Google med Google Ads API (README steg 9-11):
förfrågningar, klick på telefonnumret och vunna affärer som
offline-konverteringar ("import från klick") i kundens Google Ads-konto.

    ensure_conversion_actions(account)  skapar de konverteringar som saknas i
                                        kundens konto och sparar resursnamnen
                                        i account.google_conversion_actions
    upload_queued(account)              skickar raderna i kö med
                                        uploadClickConversions och markerar
                                        varje rad som skickad eller misslyckad
                                        (bara när upload_enabled())
    queue_context()                     det byråns kö (/manage/flamingo/granska/)
                                        visar om konverteringarna

Vad som köas avgörs av Lead (models.py): allt med ett gclid
(Lead.can_send_to_google). Landningssidan frågar inte om samtycke (beslut
2026-10-03), så konverteringarna skickas utan fråga. Samtycket skickas med
(consent.adUserData) bara när Lead.ad_consent faktiskt är "granted" eller
"denied"; annars utelämnas det. Det hittas aldrig på. Tiden skickas i
svensk tid med offset (google_ads.google_datetime) och värdet bara för
affärer, i kronor (SEK).
Tiden är densamma som i CSV-filen (exports.conversion_moment), så Google
känner igen en konvertering som redan kommit in den andra vägen
(CLICK_CONVERSION_ALREADY_EXISTS räknas som skickad).

Konverteringarna räknas en gång per klick (ONE_PER_CLICK, Googles råd för
förfrågningar). Sådana tar Google inte emot med gbraid eller wbraid
(ONE_PER_CLICK_CONVERSION_ACTION_NOT_PERMITTED_WITH_BRAID), därför krävs gclid.

Fel per rad: "försök igen senare" (klicket eller konverteringen är för ny,
kundens datavillkor) lämnar raden i kö med felet noterat; allt annat gör
raden misslyckad med en kort svensk text. Ett fel för hela anropet (kvot,
inloggning, Google nere) lämnar alla rader i kö och kastas vidare.

Demokonton, konton utan Google Ads-id eller som inte ligger under ADX
förvaltarkonto, och allt när API:t inte är inkopplat: inget händer.
Byrån har då CSV-exporten (exports.offline_conversions_csv) kvar.

Uppladdningen med API:t är av från början (upload_enabled,
GOOGLE_ADS_UPLOAD_CONVERSIONS): Google tar inte emot nya användare av
uploadClickConversions sedan 2026-06-15, och den vägen ger då
CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE. Svarar Google så stoppas
uppladdningen för alla konton (GoogleAdsConnection.conversion_upload_blocked_at),
raderna står kvar i kö för CSV-filen, och byråns kö och Google-sida säger
det. Googles väg framåt är Data Manager API (ingestEvents), som inte är
byggd här. Konverteringsåtgärderna skapas ändå med API:t: CSV-importen
behöver dem.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import exports, google_ads
from .google_ads import GoogleAdsError
from .models import ConversionUpload, FlamingoAccount, GoogleAdsConnection, Lead

logger = logging.getLogger(__name__)

#: Konverteringens kategori hos Google, per sort.
CATEGORIES = {
    ConversionUpload.KIND_LEAD: "SUBMIT_LEAD_FORM",
    ConversionUpload.KIND_CALL: "PHONE_CALL_LEAD",
    ConversionUpload.KIND_DEAL: "CONVERTED_LEAD",
}
#: Konverteringar per anrop (Google tar högst 2 000).
BATCH = 200
#: Google tar inte emot en konvertering för ett klick som är yngre än sex
#: timmar (TOO_RECENT_EVENT). Förfrågan kommer in minuter efter klicket, så
#: raden väntar tills förfrågan är så här gammal.
CLICK_SETTLE = timedelta(hours=6)
#: Fel som betyder "försök igen senare": raden står kvar i kö.
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
MSG_NO_GCLID = "Förfrågan saknar gclid, som Google behöver. Skickas inte."
MSG_WRONG_TYPE = (
    'Kundens konto har redan en konvertering som heter "{name}" men den är inte en import '
    "av klick. Byt namn på den i Google Ads, så skapar Flamingo sin egen."
)
MSG_ROW_FAILED = "Google tog inte emot konverteringen."
#: Google släpper inte in ADX i uploadClickConversions.
NOT_ALLOWLISTED = "CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE"
MSG_NOT_ALLOWLISTED = (
    "Google tar inte emot konverteringar från ADX med uploadClickConversions (inte på "
    "Googles lista sedan 2026-06-15). Uppladdningen är stoppad och raderna står kvar: "
    "exportera dem som CSV i kön. Googles väg framåt är Data Manager API."
)


# ---------------------------------------------------------------------------
# Kontot
# ---------------------------------------------------------------------------


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


def upload_blocked():
    """Varför Google stoppade uppladdningen (text), eller ""."""
    connection = GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).first()
    if connection is None or connection.conversion_upload_blocked_at is None:
        return ""
    return connection.conversion_upload_error or MSG_NOT_ALLOWLISTED


def upload_enabled():
    """Laddas konverteringarna upp med API:t? Bara när API:t är inkopplat,
    byrån slagit på GOOGLE_ADS_UPLOAD_CONVERSIONS och Google inte stoppat
    uppladdningen. Annars går de som CSV."""
    return (
        bool(getattr(settings, "GOOGLE_ADS_UPLOAD_CONVERSIONS", False))
        and google_ads.is_configured()
        and not upload_blocked()
    )


def block_uploads(now=None, message=MSG_NOT_ALLOWLISTED):
    """Google släpper inte in ADX: stoppa uppladdningen för alla konton tills
    byrån ber om ett nytt försök (unblock_uploads)."""
    now = now or timezone.now()
    GoogleAdsConnection.objects.get_or_create(pk=GoogleAdsConnection.SOLO_PK)
    GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        conversion_upload_blocked_at=now, conversion_upload_error=message[:300]
    )
    logger.warning("Google Ads: uppladdningen av konverteringar stoppad: %s", message)


def unblock_uploads():
    GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).update(
        conversion_upload_blocked_at=None, conversion_upload_error=""
    )


def _not_allowlisted(names):
    return NOT_ALLOWLISTED in set(names)


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


# ---------------------------------------------------------------------------
# Uppladdningen
# ---------------------------------------------------------------------------


#: Lead.ad_consent som Googles Consent.adUserData. Bara ett svar som
#: besökaren faktiskt gett skickas; tomt utelämnas.
CONSENT_VALUES = {Lead.CONSENT_GRANTED: "GRANTED", Lead.CONSENT_DENIED: "DENIED"}


def click_conversion(upload, action):
    """En rad som Google vill ha den (ClickConversion i REST-form). consent
    finns med bara när förfrågan har ett riktigt svar (CONSENT_VALUES)."""
    item = {
        "gclid": upload.lead.gclid,
        "conversionAction": action,
        "conversionDateTime": google_ads.google_datetime(exports.conversion_moment(upload)),
        "currencyCode": exports.CONVERSION_CURRENCY,
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


def _stop_ineligible(account):
    """Rader i kö som aldrig kan skickas (inget gclid) blir misslyckade med
    orsaken."""
    queued = ConversionUpload.objects.filter(
        lead__account=account, status=ConversionUpload.STATUS_QUEUED
    )
    return queued.filter(lead__gclid="").update(
        status=ConversionUpload.STATUS_FAILED, error=MSG_NO_GCLID
    )


def upload_queued(account, now=None):
    """Skicka kontots konverteringar i kö till Google. Returnerar
    {"sent", "failed", "waiting"} (antal rader).

    Raderna låses medan de skickas (SELECT ... FOR UPDATE SKIP LOCKED), så
    två körningar samtidigt aldrig skickar samma rad, och exporten eller
    inkorgen inte ändrar en rad som är på väg. Kastar GoogleAdsError för ett
    fel som gäller hela anropet; raderna står då kvar i kö. Säger Google att
    ADX inte får ladda upp (NOT_ALLOWLISTED, för anropet eller en rad)
    stoppas uppladdningen för alla konton (block_uploads) och felet kastas
    med status UPLOAD_NOT_ALLOWED; raderna står kvar i kö."""
    result = {"sent": 0, "failed": 0, "waiting": 0}
    if not can_sync(account) or not upload_enabled():
        return result
    now = now or timezone.now()
    result["failed"] += _stop_ineligible(account)
    pending = ConversionUpload.objects.filter(
        lead__account=account,
        status=ConversionUpload.STATUS_QUEUED,
        lead__created_at__lte=now - CLICK_SETTLE,
    ).exclude(lead__gclid="")
    if not pending.exists():
        return result

    actions = dict(account.google_conversion_actions or {})
    needed = set(pending.values_list("kind", flat=True).distinct())
    if any(not actions.get(kind) for kind in needed):
        try:
            actions = ensure_conversion_actions(account)
        except GoogleAdsError as error:
            if error.status != "CONVERSION_ACTION_TYPE":
                raise
            # En av konverteringarna går inte att skapa: de andra skickas,
            # den sortens rader väntar (felet syns via kommandot).
            logger.warning("Google Ads: konto %s: %s", account.pk, error.message)
            actions = dict(account.google_conversion_actions or {})

    customer = customer_id(account)
    seen = set()
    while True:
        batch = []
        try:
            with transaction.atomic():
                batch = list(
                    pending.exclude(pk__in=seen)
                    .select_related("lead")
                    .select_for_update(skip_locked=True, of=("self",))
                    .order_by("created_at", "pk")[:BATCH]
                )
                seen.update(upload.pk for upload in batch)
                if batch:
                    _send_batch(account, customer, batch, actions, now, result)
        except GoogleAdsError as error:
            if _not_allowlisted(error.code_names):
                block_uploads()
                raise GoogleAdsError(
                    MSG_NOT_ALLOWLISTED,
                    status="UPLOAD_NOT_ALLOWED",
                    codes=error.codes,
                    request_id=error.request_id,
                    http_status=error.http_status,
                ) from None
            # Hela anropet gick inte: raderna står kvar i kö, med felet noterat.
            ConversionUpload.objects.filter(
                pk__in=[u.pk for u in batch], status=ConversionUpload.STATUS_QUEUED
            ).update(error=error.message[:300])
            raise
        if len(batch) < BATCH:
            break
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
    if failure is not None and _not_allowlisted(failure.code_names):
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
            result["sent"] += 1
        elif name in RETRY_CODES:
            upload.error = _row_message(error)
            if name == "NO_CONVERSION_ACTION_FOUND":
                lost_kinds.add(upload.kind)
            result["waiting"] += 1
        else:
            upload.status = ConversionUpload.STATUS_FAILED
            upload.error = _row_message(error)
            response["code"] = name[:80]
            result["failed"] += 1
        upload.response = response
        upload.save(update_fields=["status", "sent_at", "error", "response"])

    if lost_kinds:
        # Konverteringen har tagits bort hos Google: glöm den, så skapas den
        # igen vid nästa körning (ensure_conversion_actions).
        stored = {k: v for k, v in actions.items() if k not in lost_kinds}
        _save_actions(account, stored)
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
    """Det byråns kö visar om konverteringarna: om API:t laddar upp dem
    (uploads_via_api) eller varför Google stoppade det, namnen de har i
    kundernas konton och de senaste som misslyckades (utan
    demokontot, om byrån inte bett om det)."""
    names = exports.conversion_names()
    labels = dict(ConversionUpload.KIND_CHOICES)
    failed = ConversionUpload.objects.filter(status=ConversionUpload.STATUS_FAILED)
    if not include_demo:
        failed = failed.filter(lead__account__is_demo=False)
    return {
        "google_api_on": google_ads.is_configured(),
        "uploads_via_api": upload_enabled(),
        "upload_blocked": upload_blocked(),
        "conversion_names": [(labels[kind], name) for kind, name in names.items()],
        "failed_uploads": failed.select_related("lead__account__customer").order_by(
            "-created_at", "-pk"
        )[:10],
    }
