"""
Sändningen, steg för steg (README: Så går en sändning till).

1. Nyckeln och aktiveringen prövas i api.py.
2. Fälten prövas: numret tolkas och får sitt land (numbers.py), landet ska
   vara tillåtet för kunden, avsändaren ska vara kundens godkända, och
   texten får högst MAX_PARTS delar (encoding.py). Ett stopp här blir en
   rad med status rejected och kostar inget.
3. Priset uppskattas: 46elks pris per del till landet från de senaste
   riktiga sms:en, annars 46elks provkörning (dryrun).
4. Under radlås på kundens SmsAccount (select_for_update): månadens summa
   plus uppskattningen prövas mot taket, och sms:et sparas som reserved
   med det uppskattade priset. Två anrop samtidigt kan alltså inte båda
   passera taket. Över taket blir raden blocked_cap.
   Under samma lås prövas minutgränsen per konto och för hela byrån
   (ratelimit.check_account_minute), exakt i databasen.
5. Transaktionen avslutas (låset släpps) och 46elks anropas.
6. Svaret stämmer av priset: 46elks verkliga cost och delar ersätter
   uppskattningen. Tar 46elks säkert inte emot sms:et släpps reservationen:
   status failed, felkod provider_error, pris 0, och byrån larmas.
7. Är svaret oklart (tidsgräns, bruten förbindelse, 5xx från 46elks) kan
   sms:et ha skickats. Då står det kvar som reserverat med uppskattat pris
   och sin reference, felkod provider_unknown, och byrån larmas. API:t svarar
   202 med status "unknown". Ett nytt anrop med samma reference ger samma
   sms tillbaka och skickar aldrig igen. Leveransrapporten från 46elks
   (apply_delivery_report) eller byrån (resolve_check) avgör sedan läget.

En reference från kunden gör anropet idempotent: samma reference ger samma
sms tillbaka, utan ny sändning. Prövningen görs igen under låset.

Här kastas inga fel vidare från 46elks: varje utfall blir ett Outcome. Inga
mejl till kunden, aldrig.
"""

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from . import alerts, elks, encoding, numbers, pricing, ratelimit
from .models import SmsAccount, SmsMessage

logger = logging.getLogger(__name__)

#: Längsta tillåtna sms, i delar (6 delar = 918 GSM-tecken eller 402 UCS-2).
MAX_PARTS = 6
#: Prövas med fullmatch: "$" i match släpper igenom ett radbrytningstecken sist.
REFERENCE_RE = re.compile(r"[A-Za-z0-9._:\-]{1,64}")
#: En leveransrapport för ett sms utan 46elks id är yngre än så här: 46elks
#: svar på sändningen är troligen på väg, och rapporten får 409 (46elks
#: försöker igen). Äldre: svaret kom aldrig, och rapportens id tas över.
DLR_ADOPT_AFTER = timedelta(minutes=2)

#: Felkoderna och deras HTTP-status. Koderna är stabila: kunderna bygger på dem.
HTTP_STATUS = {
    "invalid_request": 400,
    "invalid_key": 401,
    "sms_not_enabled": 403,
    "invalid_number": 400,
    "country_not_allowed": 400,
    "sender_not_allowed": 400,
    "message_too_long": 400,
    "monthly_cap_reached": 402,
    "not_found": 404,
    "method_not_allowed": 405,
    "reference_conflict": 409,
    "rate_limited": 429,
    "internal_error": 500,
    "provider_error": 502,
    # Inget fel i egentlig mening: sms:et finns, men läget är okänt.
    "provider_unknown": 202,
}

#: Kort förklaring per felkod (dokumentationen och portalen).
ERROR_TEXTS = {
    "invalid_request": "Anropet saknar ett fält eller har fel format.",
    "invalid_key": "Nyckeln saknas, är fel eller är återkallad.",
    "sms_not_enabled": "SMS är inte aktiverat för kontot.",
    "invalid_number": "Numret går inte att tolka, finns inte eller är inte ett mobilnummer.",
    "country_not_allowed": "Numret hör till ett land kontot inte får skicka till.",
    "sender_not_allowed": "Avsändaren är inte kontots godkända avsändare.",
    "message_too_long": f"Texten blir fler än {MAX_PARTS} sms-delar.",
    "monthly_cap_reached": "Månadens kostnadstak är nått.",
    "not_found": "Sms:et finns inte på kontot.",
    "method_not_allowed": "Adressen tar inte emot den metoden.",
    "reference_conflict": "Samma reference har redan använts för ett annat sms.",
    "rate_limited": "För många anrop. Vänta och försök igen.",
    "internal_error": "Något gick fel hos ADX. Försök igen.",
    "provider_error": "SMS-leverantören tog inte emot sms:et. Inget debiterades.",
    "provider_unknown": (
        "SMS-leverantören svarade inte säkert, så sms:et kan ha skickats. Skicka det inte"
        " igen med en ny reference: ADX stämmer av läget mot leverantören, och sms:et"
        " får status sent, delivered eller failed."
    ),
}


@dataclass
class Outcome:
    """Utfallet av ett anrop: ett sms (nytt eller befintligt), ett fel, eller
    en uppskattning (dryrun)."""

    message: SmsMessage | None = None
    error: str = ""
    detail: str = ""
    created: bool = False
    duplicate: bool = False
    estimate: dict = field(default_factory=dict)
    retry_after: int | None = None
    #: 46elks svar var oklart: sms:et finns men kan ha skickats eller inte.
    unknown: bool = False

    @property
    def ok(self):
        return not self.error

    @property
    def http_status(self):
        if self.error:
            return HTTP_STATUS.get(self.error, 400)
        if self.unknown:
            return 202
        return 201 if self.created else 200


def fail(code, detail="", message=None, retry_after=None):
    return Outcome(
        message=message,
        error=code,
        detail=detail or ERROR_TEXTS.get(code, ""),
        retry_after=retry_after,
    )


# ---------------------------------------------------------------- leveransrapporten


def _dlr_key(secret):
    return hashlib.sha256(f"adx-sms-dlr:{secret}".encode()).digest()


def dlr_signature(message_id, secret=None):
    """HMAC för sms:ets egen leveransadress. Bara den som känner SECRET_KEY
    kan räkna fram den, så en adress går inte att gissa för ett annat sms."""
    key = _dlr_key(settings.SECRET_KEY if secret is None else secret)
    mac = hmac.new(key, f"sms:{int(message_id)}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def dlr_signature_ok(message_id, signature):
    """Signaturen stämmer med SECRET_KEY eller någon av SECRET_KEY_FALLBACKS:
    ett byte av nyckeln gör inte adresserna för sms på väg ogiltiga."""
    given = str(signature or "")
    secrets = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    ok = False
    for secret in secrets:
        try:
            ok |= hmac.compare_digest(dlr_signature(message_id, secret), given)
        except (TypeError, ValueError):
            return False
    return ok


def callback_base():
    base = getattr(settings, "SMS_CALLBACK_BASE_URL", "") or getattr(settings, "SITE_BASE_URL", "")
    return (base or "").rstrip("/")


def dlr_url(message):
    """Adressen 46elks ska rapportera leveransen till, eller "" när sajten
    saknar en publik https-adress (lokalt): 46elks når inte localhost."""
    base = callback_base()
    if not base.startswith("https://"):
        return ""
    path = reverse("sms_api:dlr", args=[message.pk, dlr_signature(message.pk)])
    return f"{base}{path}"


#: Hur långt en status kommit. En rapport flyttar bara framåt, så en
#: upprepad eller sen rapport (46elks försöker igen i minst sex timmar)
#: aldrig flyttar tillbaka ett levererat sms.
_RANK = {"reserved": 0, "sent": 1, "delivered": 2, "failed": 2}


def _parse_elks_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _adopt(message, provider_id, now):
    """46elks svar på sändningen kom aldrig (oklart fel, eller processen dog),
    men en leveransrapport visar att sms:et skickades. Rapportens id tas
    över, och priset blir det uppskattade: rapporten har inget pris. Byrån
    stämmer av det mot 46elks (needs_check)."""
    message.provider_id = provider_id[:64]
    message.sent_at = message.sent_at or message.created_at or now
    message.provider_cost = message.estimated_cost
    message.customer_price = message.estimated_cost + message.markup
    message.needs_check = True
    if message.error_code == "provider_unknown":
        message.error_code = ""
        message.error = ""
    logger.warning("SMS: sms %s fick sitt 46elks-id ur leveransrapporten", message.pk)
    return [
        "provider_id",
        "sent_at",
        "provider_cost",
        "customer_price",
        "needs_check",
        "error_code",
        "error",
    ]


def apply_delivery_report(message_id, provider_id, status, delivered=None, now=None):
    """Uppdatera sms:et efter 46elks rapport. Returnerar (http-status, text).

    200: uppdaterat eller redan i det läget (en upprepning ändrar inget).
    404: inget sådant sms, eller id:t hör till ett annat sms.
    409: sms:et reserverades nyss och väntar på 46elks svar; 46elks försöker
         igen. Efter DLR_ADOPT_AFTER tas rapportens id över (_adopt).
    400: okänd status."""
    status = str(status or "").strip().lower()
    if status not in ("sent", "delivered", "failed"):
        return 400, "okänd status"
    provider_id = str(provider_id or "").strip()
    now = now or timezone.now()
    adopted = []
    with transaction.atomic():
        message = SmsMessage.objects.select_for_update().filter(pk=message_id).first()
        if message is None or not provider_id:
            return 404, "okänt sms"
        if not message.provider_id:
            waiting = message.status == SmsMessage.Status.RESERVED
            if waiting and now - message.created_at < DLR_ADOPT_AFTER:
                return 409, "väntar på 46elks svar"
            if not (waiting or message.error_code == "provider_unknown"):
                return 404, "okänt sms"
            adopted = _adopt(message, provider_id, now)
            message.status = SmsMessage.Status.SENT
        elif not hmac.compare_digest(message.provider_id, provider_id):
            return 404, "okänt sms"
        if message.status in SmsMessage.STOPPED:
            return 200, "ignorerad"
        if _RANK.get(status, 0) <= _RANK.get(message.status, 0):
            if adopted:
                message.save(update_fields=[*adopted, "status", "updated_at"])
                return 200, "uppdaterad"
            return 200, "oförändrad"
        message.status = status
        fields = [*adopted, "status", "updated_at"]
        if status == SmsMessage.Status.DELIVERED:
            message.delivered_at = _parse_elks_time(delivered) or now
            fields.append("delivered_at")
        if status == SmsMessage.Status.FAILED:
            # 46elks debiterar vid sändningen; priset ligger kvar (README).
            message.error_code = "delivery_failed"
            message.error = "Operatören kunde inte leverera sms:et."
            fields += ["error_code", "error"]
        message.save(update_fields=fields)
    return 200, "uppdaterad"


# ---------------------------------------------------------------- sändningen


def _clean_input(data):
    """Fälten ur anropet, eller ett Outcome med invalid_request."""
    if not isinstance(data, dict):
        return None, fail("invalid_request", "Skicka ett JSON-objekt.")
    to = data.get("to")
    body = data.get("message")
    sender = data.get("from")
    reference = data.get("reference")
    dryrun = data.get("dryrun", False)
    if not isinstance(to, str) or not to.strip():
        return None, fail("invalid_request", "Fältet to saknas.")
    if not isinstance(body, str) or not body.strip():
        return None, fail("invalid_request", "Fältet message saknas eller är tomt.")
    if sender is not None and not isinstance(sender, str):
        return None, fail("invalid_request", "Fältet from ska vara text.")
    if reference is not None and reference != "":
        if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
            return None, fail(
                "invalid_request",
                "Fältet reference ska vara 1-64 tecken: a-z, A-Z, 0-9 och . _ : -",
            )
    if not isinstance(dryrun, bool):
        return None, fail("invalid_request", "Fältet dryrun ska vara true eller false.")
    if len(body) > 2000:
        return None, fail("message_too_long")
    return {
        "to": to.strip(),
        "body": body,
        "sender": (sender or "").strip(),
        "reference": reference or "",
        "dryrun": dryrun,
    }, None


def _held_reference(account, reference):
    """Sms:et som håller kundens reference, eller None."""
    if not reference:
        return None
    return (
        SmsMessage.objects.filter(account=account, reference=reference)
        .exclude(status__in=SmsMessage.STOPPED)
        .exclude(error_code="provider_error")
        .order_by("-pk")
        .first()
    )


def _same_request(message, to, body):
    try:
        number = numbers.parse(to)
    except numbers.InvalidNumber:
        return False
    return message.to == number.e164 and message.body == body


def _replay(existing, to, body):
    """Samma reference igen: samma sms tillbaka, aldrig en ny sändning. Det
    gäller också ett sms med oklart läge (provider_unknown)."""
    if _same_request(existing, to, body):
        return Outcome(message=existing, duplicate=True)
    return fail("reference_conflict", message=existing)


def _record(account, api_key, *, status, to, body, sender, now, **extra):
    return SmsMessage.objects.create(
        account=account,
        api_key=api_key,
        status=status,
        to=str(to)[:32],
        body=body,
        sender=sender[:11],
        created_at=now,
        **extra,
    )


def _reject(account, api_key, code, detail, *, to, body, sender, now, dryrun, **extra):
    """Ett stopp före 46elks. Sparas (utom vid provkörning) och kostar inget."""
    message = None
    if not dryrun:
        message = _record(
            account,
            api_key,
            status=SmsMessage.Status.REJECTED,
            to=to,
            body=body,
            sender=sender,
            now=now,
            error_code=code,
            error=detail[:300],
            **extra,
        )
    return fail(code, detail, message=message)


def estimate_cost(country, parts, sender, to, body, now=None):
    """(46elks pris i tiotusendels krona, varifrån). Kastar ElksError om
    46elks behöver tillfrågas och inte svarar. Ett pris på 0 (46elks
    provkörning utan estimated_cost) blir FALLBACK_PART_COST per del: ett sms
    får aldrig se gratis ut för taket."""
    per_part = pricing.recent_part_cost(country, now)
    if per_part is not None and per_part > 0:
        return per_part * parts, "history"
    if not elks.is_configured():
        raise elks.ElksError("SMS-leverantören är inte inkopplad.")
    result = elks.estimate(sender, to, body)
    if result.cost <= 0:
        return pricing.FALLBACK_PART_COST * max(int(parts or 0), 1), "fallback"
    return result.cost, "dryrun"


def send(api_key, data, now=None):
    """Hela sändningen för ett API-anrop. Returnerar alltid ett Outcome."""
    now = now or timezone.now()
    account = api_key.account
    fields, error = _clean_input(data)
    if error:
        return error
    to_raw, body, reference, dryrun = (
        fields["to"],
        fields["body"],
        fields["reference"],
        fields["dryrun"],
    )
    sender = account.sender_name

    # Samma reference som ett tidigare sms: samma svar, ingen ny prövning.
    if reference and not dryrun:
        existing = _held_reference(account, reference)
        if existing is not None:
            return _replay(existing, to_raw, body)

    common = {
        "to": to_raw,
        "body": body,
        "sender": sender,
        "now": now,
        "dryrun": dryrun,
        "reference": reference,
    }
    if not sender:
        return _reject(account, api_key, "sender_not_allowed", "Kontot saknar avsändare.", **common)
    if fields["sender"] and fields["sender"] != sender:
        return _reject(
            account,
            api_key,
            "sender_not_allowed",
            f"Avsändaren ska vara {sender} (eller utelämnas).",
            **common,
        )
    try:
        number = numbers.parse(to_raw)
    except numbers.InvalidNumber as exc:
        return _reject(account, api_key, "invalid_number", str(exc), **common)
    common["to"] = number.e164
    analysis = encoding.analyse(body)
    extra = {
        "country": number.country,
        "parts": analysis.parts,
        "encoding": analysis.encoding,
    }
    if number.country not in account.countries:
        return _reject(
            account,
            api_key,
            "country_not_allowed",
            f"Kontot får inte skicka till {numbers.country_name(number.country)}"
            f" ({number.country}).",
            **common,
            **extra,
        )
    if analysis.parts > MAX_PARTS:
        return _reject(
            account,
            api_key,
            "message_too_long",
            f"Texten blir {analysis.parts} sms-delar; högst {MAX_PARTS} är tillåtet.",
            **common,
            **extra,
        )

    # Uppskattningen, före låset: ett anrop till 46elks ska aldrig hålla det.
    try:
        estimate, _source = estimate_cost(number.country, analysis.parts, sender, number.e164, body)
    except elks.ElksError as exc:
        return _provider_failure(account, api_key, str(exc), common, extra, dryrun)
    markup = pricing.markup_for(account, analysis.parts)

    if dryrun:
        return Outcome(
            estimate={
                "to": number.e164,
                "country": number.country,
                "from": sender,
                "parts": analysis.parts,
                "encoding": analysis.encoding,
                "price_sek": pricing.api_amount(estimate + markup),
            }
        )

    # Reservationen, under lås.
    blocked = None
    try:
        with transaction.atomic():
            locked = SmsAccount.objects.select_for_update().get(pk=account.pk)
            if not locked.is_enabled:
                return fail("sms_not_enabled")
            if reference:
                existing = _held_reference(locked, reference)
                if existing is not None:
                    return _replay(existing, to_raw, body)
            retry = ratelimit.check_account_minute(locked, now)
            if retry:
                return fail("rate_limited", retry_after=retry)
            spent = pricing.month_to_date_units(locked, now)
            price = estimate + markup
            # Taket 0 stoppar allt, också ett sms som skulle kosta 0.
            if locked.cap_units == 0 or spent + price > locked.cap_units:
                blocked = _record(
                    locked,
                    api_key,
                    status=SmsMessage.Status.BLOCKED_CAP,
                    to=number.e164,
                    body=body,
                    sender=sender,
                    now=now,
                    reference=reference,
                    error_code="monthly_cap_reached",
                    error=(
                        f"Taket {pricing.kr_text(locked.cap_units)} kr är nått: "
                        f"{pricing.kr_text(spent)} kr hittills i månaden."
                    ),
                    estimated_cost=estimate,
                    **extra,
                )
                cap_info = (locked, spent)
            else:
                message = _record(
                    locked,
                    api_key,
                    status=SmsMessage.Status.RESERVED,
                    to=number.e164,
                    body=body,
                    sender=sender,
                    now=now,
                    reference=reference,
                    estimated_cost=estimate,
                    markup=markup,
                    customer_price=price,
                    **extra,
                )
    except IntegrityError:
        # Två anrop med samma reference hann samtidigt förbi den första
        # prövningen; låset gör att den andra hittar den första här.
        existing = _held_reference(account, reference)
        if existing is not None:
            return _replay(existing, to_raw, body)
        raise

    if blocked is not None:
        locked, spent = cap_info
        alerts.cap_reached(
            locked,
            pricing.current_period(now),
            pricing.kr_text(spent),
            pricing.kr_text(locked.cap_units),
        )
        return fail("monthly_cap_reached", blocked.error, message=blocked)

    return _deliver(account, message, now)


def _provider_failure(account, api_key, detail, common, extra, dryrun):
    """46elks kunde inte tillfrågas före reservationen."""
    logger.warning("SMS: 46elks svarade inte på provkörningen (konto %s)", account.pk)
    if dryrun:
        return fail("provider_error")
    message = _record(
        account,
        api_key,
        status=SmsMessage.Status.FAILED,
        to=common["to"],
        body=common["body"],
        sender=common["sender"],
        now=common["now"],
        reference=common["reference"],
        error_code="provider_error",
        error=detail[:300],
        **extra,
    )
    alerts.provider_failed(account, message, detail)
    return fail("provider_error", message=message)


def _deliver(account, message, now):
    """Anropa 46elks för ett reserverat sms och stäm av priset."""
    try:
        result = elks.send(message.sender, message.to, message.body, whendelivered=dlr_url(message))
    except Exception as exc:  # noqa: BLE001 - tidsgräns, trasigt svar, vad som helst
        if isinstance(exc, elks.ElksError):
            detail = str(exc)
            ambiguous = exc.ambiguous
        else:
            # Ett fel utanför klienten: ingen vet om anropet gick iväg.
            logger.exception("SMS: oväntat fel mot 46elks (sms %s)", message.pk)
            detail = f"Oväntat fel: {type(exc).__name__}"
            ambiguous = True
        if ambiguous:
            return _hold_unknown(account, message, detail)
        message.status = SmsMessage.Status.FAILED
        message.error_code = "provider_error"
        message.error = detail[:300]
        message.markup = 0
        message.customer_price = 0
        message.save(update_fields=["status", "error_code", "error", "markup", "customer_price"])
        alerts.provider_failed(account, message, detail)
        return fail("provider_error", message=message)

    parts = result.parts or message.parts
    markup = pricing.markup_for(account, parts)
    message.provider_id = result.id
    message.provider_cost = result.cost
    message.parts = parts
    message.markup = markup
    message.customer_price = result.cost + markup
    message.status = SmsMessage.Status.SENT
    message.sent_at = timezone.now()
    message.test_mode = result.dryrun
    message.save(
        update_fields=[
            "provider_id",
            "provider_cost",
            "parts",
            "markup",
            "customer_price",
            "status",
            "sent_at",
            "test_mode",
        ]
    )
    return Outcome(message=message, created=True)


def _hold_unknown(account, message, detail):
    """46elks svar var oklart: sms:et kan ha skickats. Reservationen står kvar
    (uppskattat pris, reference hållen), så att ett nytt försök med samma
    reference aldrig skickar igen. Byrån larmas och stämmer av."""
    message.error_code = "provider_unknown"
    message.error = detail[:300]
    message.needs_check = True
    message.save(update_fields=["error_code", "error", "needs_check", "updated_at"])
    alerts.provider_unknown(account, message, detail)
    return Outcome(message=message, created=True, unknown=True)


def resolve_check(message, sent, now=None):
    """Byrån har stämt av sms:et mot 46elks (/manage/sms/). sent=True: det
    skickades. Ett reserverat sms blir sent med det uppskattade priset; ett
    som redan fått sitt id ur en leveransrapport behåller läget. sent=False:
    det skickades inte. Reservationen släpps som vid provider_error (pris 0,
    referencen fri). Returnerar False om sms:et inte väntade på avstämning."""
    with transaction.atomic():
        message = SmsMessage.objects.select_for_update().get(pk=message.pk)
        reserved = message.status == SmsMessage.Status.RESERVED
        if not (reserved or message.needs_check):
            return False
        fields = ["needs_check", "updated_at"]
        message.needs_check = False
        if sent and reserved:
            message.status = SmsMessage.Status.SENT
            message.sent_at = message.created_at or now or timezone.now()
            message.provider_cost = message.estimated_cost
            message.customer_price = message.estimated_cost + message.markup
            message.error_code = ""
            message.error = ""
            fields += [
                "status",
                "sent_at",
                "provider_cost",
                "customer_price",
                "error_code",
                "error",
            ]
        elif not sent:
            if not reserved:
                return False  # 46elks har redan rapporterat sms:et: det skickades.
            message.status = SmsMessage.Status.FAILED
            message.error_code = "provider_error"
            message.error = "Avstämt mot 46elks: sms:et skickades inte."
            message.markup = 0
            message.customer_price = 0
            fields += ["status", "error_code", "error", "markup", "customer_price"]
        message.save(update_fields=fields)
    return True
