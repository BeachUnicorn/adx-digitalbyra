"""
Förfrågningarna (kundresan steg 9-11): skapa en från landningssidan eller
för hand, och byt status i inkorgen.

Allt som kommer utifrån saneras här, en gång, innan det sparas: namn och
nummer som ren text med maxlängd, meddelandet med radbrytningar,
klick-id:n bara om de ser ut som klick-id:n. Vyerna lämnar över rådata.

Inga mejl och inga sms skickas härifrån; landningssidan anropar
sms.notify_new_lead() efter create_lead().
"""

import logging
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from apps.common.security import sanitize_multiline_text, sanitize_plain_text

from .models import Lead, Service

logger = logging.getLogger(__name__)

NAME_MAX = 120
PHONE_MAX = 40
MESSAGE_MAX = 2000
ANSWER_LABEL_MAX = 120
ANSWER_MAX = 1000
ANSWERS_MAX = 12
TRACKING_MAX = 200
#: Ett affärsvärde över en miljard kronor är ett skrivfel.
VALUE_MAX_KR = 1_000_000_000

#: Googles klick-id:n. gbraid och wbraid (iOS) sparas i Lead.utm tills
#: modellen har egna fält för dem.
CLICK_ID_KEYS = ("gclid", "gbraid", "wbraid")
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
TRACKING_KEYS = CLICK_ID_KEYS + UTM_KEYS

_CLICK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,200}$")

#: Statusarna inkorgen erbjuder som knappar (Ny sätts bara när förfrågan kommer in).
INBOX_STATUSES = (
    Lead.STATUS_CONTACTED,
    Lead.STATUS_QUOTE,
    Lead.STATUS_WON,
    Lead.STATUS_LOST,
    Lead.STATUS_JUNK,
)


def _plain(value, max_length):
    return sanitize_plain_text(str(value or ""), max_length=max_length)


def clean_phone(value):
    """Numret som det skrevs, utan markup och tecken som inte hör hemma i ett
    nummer."""
    text = _plain(value, PHONE_MAX)
    return re.sub(r"[^\d+\-() ]", "", text).strip()


def clean_email(value):
    text = _plain(value, 254)
    if not text:
        return ""
    try:
        validate_email(text)
    except ValidationError:
        return ""
    return text


def tracking_from(*sources):
    """Klick-id och utm ur formulärets dolda fält, eller ur adressen.

    Det första värdet som finns vinner. Ett klick-id som inte ser ut som ett
    klick-id sparas inte alls (hellre inget än skräp i Googles import)."""
    found = {}
    for source in sources:
        if not source:
            continue
        for key in TRACKING_KEYS:
            if key in found:
                continue
            raw = source.get(key, "")
            if isinstance(raw, list | tuple):
                raw = raw[0] if raw else ""
            raw = str(raw or "").strip()
            if not raw:
                continue
            if key in CLICK_ID_KEYS:
                if _CLICK_ID_RE.match(raw):
                    found[key] = raw
            else:
                value = _plain(raw, TRACKING_MAX)
                if value:
                    found[key] = value
    return found


def _clean_answers(answers):
    cleaned = {}
    for label, value in list((answers or {}).items())[:ANSWERS_MAX]:
        label = _plain(label, ANSWER_LABEL_MAX)
        value = sanitize_multiline_text(str(value or ""), max_length=ANSWER_MAX)
        if label and value:
            cleaned[label] = value
    return cleaned


def create_lead(campaign, data, request=None, ip_hash=""):
    """En förfrågan från kampanjens landningssida (källa: formulär).

    data: name, phone, email, message, answers ({fråga: svar}) och de dolda
    spårningsfälten (gclid, gbraid, wbraid, utm_*). Saknas spårningen i data
    läses den ur adressen (request.GET), så att den följer med även om de
    dolda fälten skulle tappas. ip_hash kommer från limits.ip_hash (spärren
    på /lp/); landningssidan skapar förfrågan via limits.create_form_lead."""
    tracking = tracking_from(data, request.GET if request is not None else None)
    utm = {key: tracking[key] for key in UTM_KEYS if key in tracking}
    for key in ("gbraid", "wbraid"):
        if key in tracking:
            utm[key] = tracking[key]
    lead = Lead.objects.create(
        account=campaign.account,
        campaign=campaign,
        service=campaign.service,
        source=Lead.SOURCE_FORM,
        name=_plain(data.get("name"), NAME_MAX),
        phone=clean_phone(data.get("phone")),
        email=clean_email(data.get("email")),
        message=sanitize_multiline_text(str(data.get("message") or ""), max_length=MESSAGE_MAX),
        answers=_clean_answers(data.get("answers")),
        gclid=tracking.get("gclid", ""),
        utm=utm,
        keyword=tracking.get("utm_term", "")[:TRACKING_MAX],
        ip_hash=(ip_hash or "")[:64],
    )
    logger.info("Flamingo: ny förfrågan %s på kampanj %s", lead.pk, campaign.pk)
    return lead


def create_manual_lead(account, data, user=None):
    """En förfrågan kunden lägger in själv (ett samtal, ett mejl, någon i
    butiken). Tjänsten måste vara kontots egen; annars lämnas den tom.
    Inga sms: kunden vet redan om den."""
    service = None
    service_id = str(data.get("service") or "").strip()
    if service_id.isdigit():
        service = Service.objects.filter(pk=int(service_id), account=account).first()
    return Lead.objects.create(
        account=account,
        service=service,
        source=Lead.SOURCE_MANUAL,
        name=_plain(data.get("name"), NAME_MAX),
        phone=clean_phone(data.get("phone")),
        message=sanitize_multiline_text(str(data.get("message") or ""), max_length=MESSAGE_MAX),
    )


def parse_value_kr(raw):
    """'186 000', '186000 kr' och '186 000,00' blir 186000. Tomt blir None.
    Kastar ValueError med en text för kunden om det inte är ett belopp."""
    text = str(raw or "").strip().lower()
    text = re.sub(r"\s|kr|:-", "", text)
    if not text:
        return None
    text = re.sub(r"[,.]\d{1,2}$", "", text)  # ören stryks: hela kronor
    text = text.replace(".", "").replace(",", "")
    if not text.isdigit():
        raise ValueError("Skriv beloppet i hela kronor, till exempel 186 000.")
    value = int(text)
    if value > VALUE_MAX_KR:
        raise ValueError("Beloppet är för stort. Skriv det i hela kronor.")
    return value


def set_status(lead, status, value_kr=None, user=None):
    """Byt status från inkorgen. Vunnen kräver ett belopp i hela kronor
    (det är det som gör mätningen hel). Lead.set_status() köar, ändrar eller
    tar bort konverteringen till Google.

    Kastar ValueError med en text för kunden vid fel."""
    valid = dict(Lead.STATUS_CHOICES)
    if status not in valid:
        raise ValueError("Välj en status.")
    value = None
    if status == Lead.STATUS_WON:
        if isinstance(value_kr, str) or value_kr is None:
            value = parse_value_kr(value_kr)
        else:
            value = int(value_kr)
        if value is None:
            raise ValueError("Skriv affärens värde i kronor för att markera den som vunnen.")
        if value < 1:
            raise ValueError("Affärens värde måste vara minst 1 kr.")
        if value > VALUE_MAX_KR:
            raise ValueError("Beloppet är för stort. Skriv det i hela kronor.")
    lead.set_status(status, value_kr=value)
    logger.info(
        "Flamingo: förfrågan %s satt till %s av användare %s",
        lead.pk,
        status,
        getattr(user, "pk", None),
    )
    return lead
