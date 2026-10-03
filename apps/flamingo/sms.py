"""
Sms via 46elks (kundresan steg 10): ett till ägaren om en ny förfrågan och
ett autosvar till den som frågade.

Båda är val kunden själv slår på i sina inställningar (av från början), och
inget skickas förrän 46elks är inkopplat (ELKS_API_USERNAME,
ELKS_API_PASSWORD och ELKS_SENDER i miljön). Varje gång Flamingo skickar,
eller låter bli, blir det en rad i SmsLog med orsaken, så att inkorgen kan
visa vad som hände. Inget här kastar ett fel vidare: besökaren på
landningssidan ska aldrig märka att sms-leverantören krånglar.

Autosvaret skickas inte under tyst tid (21-07 svensk tid). Ägarens sms
skickas alltid: det är kunden själv som bett om det.

Skydd mot missbruk (landningssidan är publik, och autosvaret går till ett
nummer besökaren själv skriver, med kundens namn som avsändare):

- Besökarens text kommer aldrig rakt in i ett sms. Förnamnet i autosvaret
  ({namn}) används bara om det ser ut som ett förnamn (bokstäver och
  bindestreck, högst 20 tecken); annars tas platshållaren bort. Namnet i
  ägarens sms rensas från adresser och domäner och från allt utom
  bokstäver, siffror, mellanslag och vanliga skiljetecken, och kortas.
- Högst ett autosvar per mottagarnummer och konto på 24 timmar.
- Högst SMS_DAILY_MAX sms per konto och svenskt dygn, ägarens sms
  inräknade.

Ett sms som stoppas loggas med status "disabled" och orsaken i error.

Ett demokonto (FlamingoAccount.is_demo) skickar aldrig: varje sms loggas
som "disabled" med NOTE_DEMO, före alla andra prövningar och oavsett om
46elks är inkopplat.

Gränserna prövas och platsen reserveras i samma transaktion, med kontots
rad låst (select_for_update): raden sparas som "sending" innan 46elks
anropas och räknas från den stunden. Två förfrågningar samtidigt kan alltså
inte båda få ett autosvar, och dygnsgränsen kan inte passeras.

Inga mejl skickas härifrån, aldrig.
"""

import base64
import json
import logging
import re
import unicodedata
from datetime import datetime, time, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import FlamingoAccount, SmsLog

logger = logging.getLogger(__name__)

ELKS_URL = "https://api.46elks.com/a1/sms"
TIMEOUT_SECONDS = 10
STOCKHOLM = ZoneInfo("Europe/Stockholm")

#: Tyst tid för autosvaret, svensk tid: från QUIET_FROM till QUIET_UNTIL.
QUIET_FROM = time(21, 0)
QUIET_UNTIL = time(7, 0)

#: Ett sms längre än så här delas i många delar; autosvaret är kundens text.
BODY_MAX = 480
#: 46elks: en textavsändare är 3-11 tecken, a-z och siffror.
SENDER_MAX = 11
#: Sms per konto och svenskt dygn, ägarens sms och autosvaren tillsammans.
SMS_DAILY_MAX = 50
#: Ett autosvar per mottagarnummer och konto inom så här lång tid.
AUTOREPLY_WINDOW = timedelta(hours=24)
#: Besökarens namn i ägarens sms.
VISITOR_NAME_MAX = 40

NOTE_NOTIFY_OFF = "Sms om nya förfrågningar är avstängt i inställningarna."
NOTE_NO_NOTIFY_PHONE = "Inget mobilnummer för sms i inställningarna."
NOTE_AUTOREPLY_OFF = "Autosvaret är avstängt i inställningarna."
NOTE_NO_MOBILE = "Förfrågan har inget mobilnummer att svara till."
NOTE_QUIET = "Tyst tid 21-07: autosvaret skickas inte på natten."
NOTE_BAD_NUMBER = "Numret går inte att tolka som ett telefonnummer."
NOTE_ALREADY_REPLIED = "Numret har redan fått ett autosvar det senaste dygnet."
NOTE_DAILY_LIMIT = f"Dagens gräns på {SMS_DAILY_MAX} sms för kontot är nådd."
NOTE_DEMO = "Demokonto: inga sms skickas."


def is_configured():
    """46elks är inkopplat: alla tre inställningarna har ett värde."""
    return bool(
        getattr(settings, "ELKS_API_USERNAME", "")
        and getattr(settings, "ELKS_API_PASSWORD", "")
        and getattr(settings, "ELKS_SENDER", "")
    )


# ---------------------------------------------------------------------------
# Nummer
# ---------------------------------------------------------------------------


def normalize_phone(raw):
    """Ett telefonnummer i internationell form (+46701234567), eller None.

    '070-123 45 67', '+46 70 123 45 67' och '0046701234567' blir samma
    nummer. Svenska nummer utan landsnummer antas vara svenska."""
    text = re.sub(r"[^\d+]", "", raw or "")
    if text.startswith("00"):
        text = "+" + text[2:]
    if text.startswith("+"):
        digits = text[1:]
        if digits.isdigit() and 8 <= len(digits) <= 15:
            return "+" + digits
        return None
    if text.startswith("0") and text.isdigit() and 8 <= len(text) <= 11:
        return "+46" + text[1:]
    return None


def is_mobile(number):
    """Ett svenskt mobilnummer (070, 072, 073, 076, 079)."""
    return bool(re.fullmatch(r"\+467[02369]\d{7}", number or ""))


def tel_href(raw):
    """tel:-länken för ett nummer som det skrivits på sidan."""
    number = normalize_phone(raw)
    if number:
        return f"tel:{number}"
    digits = re.sub(r"[^\d+]", "", raw or "")
    return f"tel:{digits}" if digits else ""


def sender_for(account):
    """Autosvarets avsändare: företagets namn, så som 46elks tar emot det
    (högst elva tecken, a-z och siffror). Går namnet inte att korta till
    något läsbart används byråns avsändare."""
    from .models import company_slug

    words = []
    for part in company_slug(account.customer.name).split("-"):
        ascii_part = (
            unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode().capitalize()
        )
        if ascii_part:
            words.append(ascii_part)
    name = ""
    for word in words:
        if len(name + word) > SENDER_MAX:
            break
        name += word
    if not name and words:
        name = words[0][:SENDER_MAX]
    if len(name) >= 3:
        return name
    return settings.ELKS_SENDER


def in_quiet_hours(now=None):
    """Är klockan mellan 21 och 07 i Sverige?"""
    local = timezone.localtime(now or timezone.now(), STOCKHOLM).time()
    return local >= QUIET_FROM or local < QUIET_UNTIL


# ---------------------------------------------------------------------------
# Texterna
# ---------------------------------------------------------------------------


#: Ett förnamn som får stå i autosvaret: bara bokstäver och bindestreck.
_FIRST_NAME_RE = re.compile(r"^[A-Za-zÅÄÖåäöÉéÜüØøÆæ-]{1,20}$")
#: Adresser och domännamn i det besökaren skrivit ("https://...", "www.x",
#: "exempel.se/logga-in").
_URL_RE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.)\S*"
    r"|\b[\w-]+(?:\.[\w-]+)*\.[a-z][a-z0-9-]+\b\S*",
    re.IGNORECASE,
)
#: Allt utom bokstäver, siffror, mellanslag och vanliga skiljetecken.
_NOT_PLAIN_RE = re.compile(r"[^\w .,!?'()-]|_")


def first_name(name):
    """Förnamnet för {namn} i autosvaret, eller "" om det inte ser ut som
    ett förnamn (en adress, siffror, tecken). Besökaren skriver namnet själv
    och sms:et går med kundens namn som avsändare."""
    first = (name or "").strip().split(" ")[0] if name else ""
    return first if _FIRST_NAME_RE.match(first) else ""


def visitor_text(value, max_length=VISITOR_NAME_MAX):
    """Det besökaren skrev, säkert att lägga i ett sms: utan adresser och
    domäner, bara bokstäver, siffror, mellanslag och vanliga skiljetecken,
    och högst max_length tecken."""
    text = _URL_RE.sub(" ", str(value or ""))
    text = _NOT_PLAIN_RE.sub(" ", text)
    # En punkt följd av en bokstav eller siffra blir ett mellanslag, så att
    # inget som liknar en domän står kvar (inte heller med udda bokstäver).
    text = re.sub(r"\.(?=\w)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length].strip()


def owner_text(lead):
    """Ägarens sms: vem, nummer och tjänst, och länken till förfrågan.
    Kort med flit; resten står i inkorgen. Namnet och numret kommer från
    besökaren: namnet rensas (visitor_text), numret får bara ha siffror och
    + - ( ) och mellanslag."""
    phone = re.sub(r"[^\d+\-() ]", "", lead.phone or "").strip()[:VISITOR_NAME_MAX]
    parts = [visitor_text(lead.name), phone]
    who = ", ".join(p for p in parts if p) or "okänd"
    text = f"Ny förfrågan: {who}."
    if lead.service_name:
        text += f" {lead.service_name}."
    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    if base:
        text += f" Se mer: {base}{reverse('flamingo:app_lead', args=[lead.pk])}"
    return text[:BODY_MAX]


def autoreply_text(account, lead):
    """Kundens egen autosvarstext med {namn} utbytt mot förnamnet. Utan
    namn, eller med ett namn som inte ser ut som ett förnamn (first_name),
    tas platshållaren bort ("Hej {namn}!" blir "Hej!")."""
    text = account.autoreply_text or ""
    name = first_name(lead.name)
    if name:
        text = text.replace("{namn}", name)
    else:
        text = re.sub(r"\s*\{namn\}", "", text)
    return text.strip()[:BODY_MAX]


# ---------------------------------------------------------------------------
# Skicka
# ---------------------------------------------------------------------------


def _log(account, lead, kind, to, body, status, error="", provider_id=""):
    return SmsLog.objects.create(
        account=account,
        lead=lead,
        kind=kind,
        to=(to or "")[:20],
        body=body,
        status=status,
        error=(error or "")[:300],
        provider_id=(provider_id or "")[:64],
    )


def _post_to_elks(sender, to, body):
    """Ett anrop till 46elks. Returnerar leverantörens id; kastar vid fel
    (send() fångar det och loggar)."""
    credentials = f"{settings.ELKS_API_USERNAME}:{settings.ELKS_API_PASSWORD}"
    token = base64.b64encode(credentials.encode()).decode()
    data = urlencode({"from": sender, "to": to, "message": body}).encode()
    request = Request(  # noqa: S310 - fast https-adress, inget från användaren
        ELKS_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        raw = response.read(64 * 1024)
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("status") == "failed":
        raise RuntimeError("46elks svarade failed")
    return str(payload.get("id", "")) if isinstance(payload, dict) else ""


def _day_start(now=None):
    """Midnatt i Sverige för dagen som pågår."""
    local = timezone.localtime(now or timezone.now(), STOCKHOLM)
    return datetime.combine(local.date(), time(0, 0), tzinfo=STOCKHOLM)


#: Räknas mot gränserna: skickade och de som håller på att skickas.
COUNTED = (SmsLog.STATUS_SENT, SmsLog.STATUS_SENDING)


def sent_today(account, now=None):
    """Sms kontot skickat i dag (svensk tid), alla sorter."""
    return SmsLog.objects.filter(
        account=account, status__in=COUNTED, created_at__gte=_day_start(now)
    ).count()


def replied_recently(account, number, now=None):
    """Har numret fått ett autosvar från kontot de senaste 24 timmarna?"""
    since = (now or timezone.now()) - AUTOREPLY_WINDOW
    return SmsLog.objects.filter(
        account=account,
        kind=SmsLog.KIND_AUTOREPLY,
        to=number,
        status__in=COUNTED,
        created_at__gte=since,
    ).exists()


def _reserve(account, lead, kind, number, body, now, *, once_per_number):
    """Pröva gränserna och reservera platsen i en transaktion med kontot
    låst. Returnerar (rad, ok): en "sending"-rad att skicka, eller en
    "disabled"-rad med orsaken."""
    with transaction.atomic():
        FlamingoAccount.objects.select_for_update().filter(pk=account.pk).first()
        if once_per_number and replied_recently(account, number, now):
            row = _log(
                account, lead, kind, number, body, SmsLog.STATUS_DISABLED, NOTE_ALREADY_REPLIED
            )
            return row, False
        if sent_today(account, now) >= SMS_DAILY_MAX:
            logger.warning("Flamingo: dagens sms-gräns nådd för konto %s", account.pk)
            row = _log(account, lead, kind, number, body, SmsLog.STATUS_DISABLED, NOTE_DAILY_LIMIT)
            return row, False
        return _log(account, lead, kind, number, body, SmsLog.STATUS_SENDING), True


def _finish(row, status, error="", provider_id=""):
    row.status = status
    row.error = (error or "")[:300]
    row.provider_id = (provider_id or "")[:64]
    row.save(update_fields=["status", "error", "provider_id"])
    return row


def send(account, kind, to, body, lead=None, sender=None, now=None, once_per_number=False):
    """Skicka ett sms, eller logga varför det inte skickades. Returnerar
    SmsLog-raden. Kastar aldrig. once_per_number: högst ett per mottagare
    och AUTOREPLY_WINDOW (autosvaret)."""
    try:
        if getattr(account, "is_demo", False):
            return _log(account, lead, kind, to, body, SmsLog.STATUS_DISABLED, NOTE_DEMO)
        if not is_configured():
            return _log(account, lead, kind, to, body, SmsLog.STATUS_NOT_CONFIGURED)
        number = normalize_phone(to)
        if number is None:
            return _log(account, lead, kind, to, body, SmsLog.STATUS_FAILED, NOTE_BAD_NUMBER)
        row, ok = _reserve(account, lead, kind, number, body, now, once_per_number=once_per_number)
        if not ok:
            return row
        try:
            provider_id = _post_to_elks(sender or settings.ELKS_SENDER, number, body)
        except HTTPError as exc:
            return _finish(row, SmsLog.STATUS_FAILED, f"46elks: HTTP {exc.code}")
        except URLError as exc:
            return _finish(row, SmsLog.STATUS_FAILED, f"46elks: {exc.reason}")
        except Exception as exc:  # noqa: BLE001 - tidsgräns, trasigt svar, vad som helst
            logger.warning("Flamingo: sms via 46elks misslyckades: %s", type(exc).__name__)
            return _finish(row, SmsLog.STATUS_FAILED, f"46elks: {type(exc).__name__}: {exc}")
        return _finish(row, SmsLog.STATUS_SENT, provider_id=provider_id)
    except Exception:  # noqa: BLE001 - ett sms får aldrig fälla förfrågan
        logger.exception("Flamingo: sms kunde inte skickas eller loggas (konto %s)", account.pk)
        return None


def notify_new_lead(lead, now=None):
    """En ny förfrågan från landningssidan: sms till ägaren och autosvar
    till den som frågade, var för sig efter kundens inställningar.

    Returnerar SmsLog-raderna (ägaren först). Kastar aldrig."""
    rows = []
    try:
        account = lead.account
        rows.append(_notify_owner(account, lead, now))
        rows.append(_autoreply(account, lead, now))
    except Exception:  # noqa: BLE001 - besökaren ska aldrig se ett sms-fel
        logger.exception("Flamingo: sms för förfrågan %s misslyckades", lead.pk)
    return [row for row in rows if row is not None]


def _notify_owner(account, lead, now=None):
    body = owner_text(lead)
    kind = SmsLog.KIND_OWNER
    if getattr(account, "is_demo", False):
        status = SmsLog.STATUS_DISABLED
        return _log(account, lead, kind, account.notify_phone, body, status, NOTE_DEMO)
    if not account.notify_sms:
        status = SmsLog.STATUS_DISABLED
        return _log(account, lead, kind, account.notify_phone, body, status, NOTE_NOTIFY_OFF)
    if not account.notify_phone.strip():
        return _log(account, lead, kind, "", body, SmsLog.STATUS_DISABLED, NOTE_NO_NOTIFY_PHONE)
    return send(account, kind, account.notify_phone, body, lead=lead, now=now)


def _autoreply(account, lead, now=None):
    body = autoreply_text(account, lead)
    kind = SmsLog.KIND_AUTOREPLY
    if getattr(account, "is_demo", False):
        return _log(account, lead, kind, lead.phone, body, SmsLog.STATUS_DISABLED, NOTE_DEMO)
    if not account.autoreply_enabled:
        status = SmsLog.STATUS_DISABLED
        return _log(account, lead, kind, lead.phone, body, status, NOTE_AUTOREPLY_OFF)
    number = normalize_phone(lead.phone)
    if not is_mobile(number):
        return _log(account, lead, kind, lead.phone, body, SmsLog.STATUS_DISABLED, NOTE_NO_MOBILE)
    if in_quiet_hours(now):
        return _log(account, lead, kind, number, body, SmsLog.STATUS_DISABLED, NOTE_QUIET)
    return send(
        account,
        kind,
        number,
        body,
        lead=lead,
        sender=sender_for(account),
        now=now,
        once_per_number=True,
    )
