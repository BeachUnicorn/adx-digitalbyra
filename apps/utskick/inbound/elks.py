"""
Inkommande sms från 46elks till det delade svarsnumret (README G.1).

    POST /api/utskick/46elks/inkommande/<token>/   (webhook_urls.py, utskick_api:elks_inbound)

    inbound(request, token)     webbanropet: token, IP-listan, handle, tom kropp
    handle(fields, now=None)    ett sms in (webbanropet och avstämningen):
                                InboundMessage plus routningen i en transaktion
    reconcile_due(now) -> bool  tickens fas 2: tio minuter sedan förra avstämningen
    reconcile(now, deadline) -> dict
                                46elks historik sedan förra avstämningen (minus
                                en marginal, högst 48 timmar): id som saknas här
                                går genom handle ({"checked", "inserted"})

Webbanropet (G.1 punkt 1 till 3):

- Token med compare_digest mot UTSKICK_ELKS_INBOUND_TOKEN (tom: inkommande
  är av, allt är 404). Klientens IP (common.net.client_ip) ska stå i
  SMS_DLR_ALLOWED_IPS. I produktion (DEBUG av) vägrar adressen när listan är
  tom: 404 och ett larm till byrån högst en gång per dygn. Lokalt räcker
  token när listan är tom. Fel token, IP eller metod: 404 med tom kropp.
- Fälten id, from, to, message, created och direction. to ska vara
  svarsnumret och from ett mobilnummer i E.164; annars sparas sms:et som
  ignored och besvaras aldrig (routing.process).
- Allt i en transaktion: InboundMessage på ("sms", id) (en dubblett ändrar
  inget och får 200), routningen, nyckelorden och trådarna. Svaret är 200 med
  tom kropp först efter commit; ett fel ger 500 med tom kropp. 46elks skickar
  varje svarstext tillbaka som ett sms, så kroppen är alltid tom. Inget
  anrop går ut i själva förfrågan: svar på STOPP/START och ägarens sms köas
  (threads.send_due), och efter commit skickar en kort bakgrundstråd dem
  direkt (sending/kick.py, av med UTSKICK_KICK=False och aldrig för demot).
  Ticken är reserven. Avstämningen (ticken) knuffar inte: fas 3 kommer
  strax efter i samma tick.

nginx loggar inte /api/utskick/ (C.5) och Sentry visar inte adressen (C.3):
token står i den.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from hmac import compare_digest

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from apps.common.net import client_ip
from apps.sms import elks as elks_client

from .. import alerts
from ..models import CHANNEL_SMS, InboundMessage, Switchboard, ThreadMessage
from . import routing

logger = logging.getLogger(__name__)

#: Avstämningen mot 46elks historik går så här ofta (G.1 punkt 4).
RECONCILE_EVERY = timedelta(minutes=10)
#: och läser bakåt till förra avstämningen minus marginalen, högst så långt.
#: Historiken har utgående sms också (de filtreras här), så en fast läsning
#: av 48 timmar räckte inte en dag med många utskick (2 000 rader är 20
#: sidor, ungefär en halvtimme vid byråns minutgräns).
RECONCILE_SINCE = timedelta(hours=48)
RECONCILE_MARGIN = timedelta(minutes=20)
#: Största kropp som tas emot (ett sms är högst några tusen tecken).
MAX_REQUEST_BYTES = 64 * 1024
#: 46elks id: bokstäver, siffror och några tecken.
ID_RE = re.compile(r"[A-Za-z0-9._:\-]{1,120}")


def _empty(status):
    """Alltid tom kropp: 46elks skickar svarets text som ett sms."""
    response = HttpResponse(b"", status=status, content_type="text/plain")
    response["Cache-Control"] = "no-store"
    return response


def _token_ok(token):
    """Token ur adressen mot inställningen. Som byte: compare_digest kastar
    TypeError för text med tecken utanför ASCII (ett 500 utan inloggning)."""
    expected = str(getattr(settings, "UTSKICK_ELKS_INBOUND_TOKEN", "") or "")
    given = str(token or "")
    return bool(expected) and compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _allowed_ips():
    return [ip.strip() for ip in getattr(settings, "SMS_DLR_ALLOWED_IPS", []) if ip.strip()]


def _ip_ok(request):
    allowed = _allowed_ips()
    if not allowed:
        if settings.DEBUG:
            return True
        logger.error("Utskick: inkommande sms nekas, SMS_DLR_ALLOWED_IPS är tom i produktion")
        alerts.agency(
            "Utskick: inkommande sms är stängda",
            [
                "46elks anropar adressen för inkommande sms, men SMS_DLR_ALLOWED_IPS är tom.",
                "Utan listan svarar adressen 404 i produktion. Lägg 46elks IP-adresser i .env "
                "(se .env.example) och starta om adx.",
            ],
            once="elks_inbound_ips",
            window="day",
        )
        return False
    return client_ip(request) in allowed


def _fields(request):
    """46elks skickar application/x-www-form-urlencoded; JSON tas också."""
    data = request.POST
    if not data and request.body:
        import json

        try:
            parsed = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {key: data.get(key, "") for key in data}


@csrf_exempt
@never_cache
def inbound(request, token):
    """46elks webbanrop för ett inkommande sms (G.1 punkt 1 till 3)."""
    if not _token_ok(token):
        return _empty(404)
    if request.method != "POST":
        return _empty(404)
    if not _ip_ok(request):
        return _empty(404)
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > MAX_REQUEST_BYTES:
        return _empty(413)
    fields = _fields(request)
    if not ID_RE.fullmatch(str(fields.get("id") or "")):
        return _empty(400)
    try:
        message, created = handle(fields)
    except Exception:
        logger.exception("Utskick: inkommande sms kunde inte hanteras")
        return _empty(500)
    if created:
        # Svaret på STOPP/START och ägarens sms går direkt efter commit, inte
        # först med nästa tick (sending/kick.py; ticken är reserven).
        from ..sending import kick

        kick.after_inbound(message)
    return _empty(200)


def _received_at(value, now):
    """När 46elks tog emot sms:et (fältet created, UTC), aldrig i framtiden."""
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        parsed = elks_client._parse_time(value)
    if parsed is None or parsed > now + timedelta(minutes=5):
        return now
    return parsed


def handle(fields, now=None):
    """Ett inkommande sms (webbanropet och avstämningen): sparas och routas i
    en transaktion. Returnerar (InboundMessage, ny). En dubblett av ett id som
    redan finns ändrar ingenting."""
    now = now or timezone.now()
    provider_id = str(fields.get("id") or "").strip()[:120]
    if not provider_id:
        raise ValueError("Inkommande sms utan id")
    direction = str(fields.get("direction") or "incoming").strip()
    body = str(fields.get("message") or "").replace("\x00", "")
    with transaction.atomic():
        existing = InboundMessage.objects.filter(
            channel=CHANNEL_SMS, provider_id=provider_id
        ).first()
        if existing is not None:
            return existing, False
        try:
            with transaction.atomic():
                message = InboundMessage.objects.create(
                    channel=CHANNEL_SMS,
                    provider_id=provider_id,
                    from_address=str(fields.get("from") or "").strip()[:254],
                    to_address=str(fields.get("to") or "").strip()[:254],
                    body=body[: ThreadMessage.MAX_BODY],
                    received_at=_received_at(fields.get("created"), now),
                    meta={"size": len(body)},
                    created_at=now,
                )
        except IntegrityError:
            # Samma id samtidigt (46elks omförsök mitt i ett anrop): det
            # första vann.
            return InboundMessage.objects.get(channel=CHANNEL_SMS, provider_id=provider_id), False
        if direction != "incoming":
            message.status = InboundMessage.Status.IGNORED
            message.body = ""
            message.meta = {**message.meta, "reason": "direction"}
            message.save(update_fields=["status", "body", "meta"])
            return message, True
        routing.process(message, now)
    logger.info("Utskick: inkommande %s hanterat (%s)", message.pk, message.status)
    return message, True


# ---------------------------------------------------------------------------
# Avstämningen (tickens fas 2)
# ---------------------------------------------------------------------------


def inbound_on():
    """Inkommande är påslaget (token satt) och 46elks är inkopplat."""
    return bool(getattr(settings, "UTSKICK_ELKS_INBOUND_TOKEN", "")) and (
        elks_client.is_configured()
    )


def reconcile_due(now=None):
    """Dags för avstämningen: inkommande är på och tio minuter har gått."""
    if not inbound_on():
        return False
    now = now or timezone.now()
    last = (
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK)
        .values_list("last_elks_reconcile_at", flat=True)
        .first()
    )
    return last is None or now - last >= RECONCILE_EVERY


def reconcile(now=None, deadline=None):
    """Läs 46elks historik (inkommande till svarsnumret, sedan förra
    avstämningen minus RECONCILE_MARGIN, högst 48 timmar) och hantera varje
    id som saknas här, med samma handle som webbanropet. En förlorad STOPP
    blir alltså fördröjd högst tio minuter. Läsningen håller tickens
    tidsgräns (deadline). Bara antal i svaret; ett fel hos 46elks loggas,
    tidpunkten för förra avstämningen står kvar och nästa tick försöker igen
    med samma period."""
    now = now or timezone.now()
    deadline = deadline if deadline is not None else time.monotonic() + 8
    switch = Switchboard.get_solo()
    previous = switch.last_elks_reconcile_at
    Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(last_elks_reconcile_at=now)
    counts = {"checked": 0, "inserted": 0}
    since = now - RECONCILE_SINCE
    if previous is not None and previous - RECONCILE_MARGIN > since:
        since = previous - RECONCILE_MARGIN
    try:
        rows = elks_client.list_messages(
            since, direction="incoming", to=routing.reply_number(), deadline=deadline
        )
    except elks_client.ElksError as exc:
        logger.warning("Utskick: avstämningen mot 46elks misslyckades: %s", exc)
        # Förra avstämningen står kvar: nästa tick läser samma period igen.
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK, last_elks_reconcile_at=now).update(
            last_elks_reconcile_at=previous
        )
        counts["failed"] = 1
        return counts
    if not getattr(rows, "complete", True):
        logger.warning("Utskick: avstämningen hann inte läsa hela 46elks historik")
        counts["partial"] = 1
    counts["checked"] = len(rows)
    ids = [row["id"] for row in rows if row.get("id")]
    known = set(
        InboundMessage.objects.filter(channel=CHANNEL_SMS, provider_id__in=ids).values_list(
            "provider_id", flat=True
        )
    )
    for row in reversed(rows):
        if row["id"] in known:
            continue
        if time.monotonic() > deadline:
            break
        try:
            _, created = handle(row, now=timezone.now())
        except Exception:
            logger.exception("Utskick: avstämningen kunde inte hantera ett inkommande sms")
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        if created:
            counts["inserted"] += 1
    if counts["inserted"]:
        logger.warning(
            "Utskick: avstämningen hittade %s inkommande sms som webbanropet missat",
            counts["inserted"],
        )
    return counts
