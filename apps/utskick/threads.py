"""
Svarstrådarna i Inkorgen (README B.2 "Why replies are Lead rows", G.1 punkt 5
till 9, G.2) och det de köar åt ticken.

Varje tråd har en förfrågan (Lead med source "reply") som är dess rad i
Inkorgen: badgen, listan och sidorna fungerar som för förfrågningarna.
Lead.status är också svarets läge: new är "Ny", contacted visas som "Klar"
(STOPP-trådar: "Avregistrerad automatiskt"). Ett nytt svar på en klar tråd
gör den ny igen; STOPP och START höjer aldrig badgen. Lead.message är de
första 200 tecknen av senaste svaret och Lead.activity_at när det kom
(Inkorgen sorteras på den).

Trådar och förfrågningar (anropas av inbound/, i hanterarens transaktion):

    thread_for(account, address, contact=, utskick=, sms_message=, now=)
                                    tråden ett svar hör till: den som bär sms:et,
                                    numrets öppna tråd (30 dagar) eller en ny
    thread_for_stop(...) / thread_for_start(...)   samma för STOPP och START
    add_inbound(thread, inbound, raise_lead=True, looks_like_stop=False, now=)
    queue_answer(thread, inbound, body, now=)   ett svar på STOPP/START i kö
    suppress_number(account, e164, reason=, source=, actor=, ...)   spärren

Inkorgen (app_views/inbox_reply.py):

    send_reply(thread, text, actor=, now=) -> Sent    svar med sms från svarsnumret
    unsubscribe(thread, actor=, now=)          "Avregistrera från sms"
    rows(thread) -> list[Row]                  meddelandena som mallen visar

Ticken (sending/tick.py, fas 3):

    work_exists(now) -> bool            köade svar på STOPP/START eller ägarsms att skicka
    send_due(now, deadline) -> dict     {"answers", "notices", "skipped"}; deadline är
                                        time.monotonic() när fasen ska sluta

Svaren på STOPP och START går från svarsnumret med källan system och
referensen x<inkommande>, betalas av kunden tråden hör till, prövas mot
taket innan apps/sms anropas och mot nödbromsen (checks.breaker_active),
men inte mot tidsfönstret eller sms_enabled (J S2 steg 6 provar STOPP innan
sms-utskicken slås på). Ägarens sms om svar (notify_on_reply) och om
förfrågningar via utskick (notify_sms) samlas: högst ett per kund och 30
minuter, via Flamingos vanliga ägarsms (flamingo.sms.notify_owner_text).
Kunden mejlas aldrig. Inga personuppgifter i loggarna, bara pk.
"""

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Value
from django.db.models.functions import Coalesce, Greatest
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import Lead

from . import keys, normalize
from . import suppression as suppressions
from .access import settings_for
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    ConsentLog,
    InboundMessage,
    Recipient,
    Thread,
    ThreadMessage,
    UtskickSettings,
)

logger = logging.getLogger(__name__)

#: En tråd är öppen så här länge efter senaste meddelandet (G.1 punkt 5).
OPEN_FOR = timedelta(days=30)
#: Lead.message: början av senaste svaret.
LEAD_MESSAGE_MAX = 200
#: Ägarens sms om svar: högst ett per kund så här ofta (G.1 punkt 9).
NOTICE_EVERY = timedelta(minutes=30)
#: Ägarens första sms tar med svar så här långt bakåt.
NOTICE_LOOKBACK = timedelta(hours=24)
#: Det som kommit in de sista sekunderna väntar till nästa ägarsms (en
#: transaktion som ännu inte är klar ska inte hamna mellan två sms).
NOTICE_SETTLE = timedelta(seconds=5)
NOTICE_BATCH = 50
#: Ett svar på STOPP/START som inte kunnat skickas på så här länge skickas inte.
ANSWER_MAX_AGE = timedelta(hours=6)
ANSWER_BATCH = 20
#: Samma svar två gånger i samma tråd inom så här kort tid är ett dubbelklick.
DOUBLE_SUBMIT = timedelta(seconds=60)

STOP_LEAD_LABEL = "Avregistrerad automatiskt"
DONE_LABEL = "Klar"

BREAKER_TEXT = "Sms kan inte skickas just nu. Försök igen om en stund."
SUPPRESSED_TEXT = "Personen har svarat STOPP."
EMPTY_TEXT = "Skriv ett svar."
TOO_LONG_TEXT = "Svaret blir för långt: högst 6 sms-delar."
NOT_SMS_TEXT = "Den här tråden kan inte besvaras med sms."
DUPLICATE_TEXT = "Svaret är redan skickat."
REPLY_ERRORS = {
    "rate_limited": BREAKER_TEXT,
    "monthly_cap_reached": "Månadens kostnadstak för sms är nått, så svaret skickades inte.",
    "sms_not_enabled": "Sms är inte aktiverat för dig. Be ADX slå på det.",
    "invalid_number": "Numret kan inte ta emot sms.",
    "country_not_allowed": "Du får inte skicka sms till numrets land. Be ADX om det behövs.",
    "message_too_long": TOO_LONG_TEXT,
    "provider_error": "Sms-leverantören tog inte emot svaret. Försök igen om en stund.",
    "sender_not_allowed": "Svarsnumret kan inte användas just nu. ADX har fått ett larm.",
}
CHECK_TEXTS = {
    "suppressed": SUPPRESSED_TEXT,
    "deleted": "Kontakten är borttagen.",
    "invalid_number": "Numret kan inte ta emot sms.",
}

#: Varför ett svar på STOPP/START inte skickades (InboundMessage.meta["answers"]).
ANSWER_TEXTS = {
    "cap": "Bekräftelsen skickades inte: kostnadstaket är nått.",
    "not_enabled": "Bekräftelsen skickades inte: sms är inte aktiverat för dig.",
    "demo": "Demokontot skickar aldrig.",
    "expired": "Bekräftelsen skickades inte: sms kunde inte skickas då.",
    "failed": "Bekräftelsen skickades inte.",
}


# ---------------------------------------------------------------------------
# Trådarna och förfrågningarna
# ---------------------------------------------------------------------------


def _lead_fields(contact, address, channel):
    """Namn och nummer (eller e-post) till trådens förfrågan, från kontakten."""
    name = ""
    if contact is not None:
        name = (contact.full_name or contact.company_name or "")[:120]
    if channel == CHANNEL_SMS:
        return {"name": name, "phone": normalize.display_phone(address)[:40]}
    return {"name": name, "email": address[:254]}


def _open_since(now):
    since = now - OPEN_FOR
    return Q(last_in_at__gte=since) | Q(last_out_at__gte=since) | Q(created_at__gte=since)


def open_thread(account, address, channel=CHANNEL_SMS, now=None):
    """Numrets öppna tråd hos kunden (ett meddelande de senaste 30 dagarna)."""
    now = now or timezone.now()
    return (
        Thread.objects.filter(account=account, channel=channel, address=address)
        .filter(_open_since(now))
        .select_related("lead")
        .order_by("-created_at", "-pk")
        .first()
    )


def _new_thread(account, address, *, contact, utskick, kind, channel=CHANNEL_SMS, now):
    """En ny tråd med sin förfrågan. En STOPP-tråd är klar från början."""
    status = Lead.STATUS_CONTACTED if kind == Thread.Kind.STOP else Lead.STATUS_NEW
    lead = Lead.objects.create(
        account=account,
        source=Lead.SOURCE_REPLY,
        status=status,
        contact=contact,
        created_at=now,
        activity_at=now,
        **_lead_fields(contact, address, channel),
    )
    return Thread.objects.create(
        account=account,
        contact=contact,
        channel=channel,
        kind=kind,
        lead=lead,
        utskick=utskick,
        address=address,
        created_at=now,
    )


def _adopt(thread, *, contact=None, utskick=None):
    """Kontakten och utskicket som svaret gäller, på en befintlig tråd."""
    changed = []
    if contact is not None and thread.contact_id is None:
        thread.contact = contact
        changed.append("contact")
        if thread.lead_id and thread.lead.contact_id is None:
            Lead.objects.filter(pk=thread.lead_id).update(contact=contact)
    if utskick is not None and thread.utskick_id != utskick.pk:
        thread.utskick = utskick
        changed.append("utskick")
    if changed:
        thread.save(update_fields=changed)
    return thread


def ensure_context(thread, sms_message):
    """Utskickets sms som svaret gäller står i tråden före svaret (som i
    mockupen: "Utskick · 8 okt 09:00"). En gång per sms och tråd."""
    if sms_message is None or sms_message.source not in ("utskick", "flow"):
        return None
    if ThreadMessage.objects.filter(thread=thread, sms_message=sms_message).exists():
        return None
    status = (
        ThreadMessage.Status.FAILED
        if sms_message.status in ("failed", "rejected", "blocked_cap")
        else ThreadMessage.Status.SENT
    )
    return ThreadMessage.objects.create(
        thread=thread,
        direction=ThreadMessage.Direction.OUT,
        body=sms_message.body or "",
        at=sms_message.sent_at or sms_message.created_at,
        sms_message=sms_message,
        status=status,
    )


def thread_for(account, address, *, contact=None, utskick=None, sms_message=None, now=None):
    """Tråden ett vanligt svar hör till (G.1 punkt 5): tråden som bär det
    senaste sms:et (ett svar från Inkorgen, en bekräftelse), annars numrets
    öppna tråd från de senaste 30 dagarna, annars en ny."""
    now = now or timezone.now()
    thread = None
    if sms_message is not None:
        carrier = (
            ThreadMessage.objects.filter(thread__account=account, sms_message=sms_message)
            .select_related("thread__lead")
            .order_by("-pk")
            .first()
        )
        if carrier is not None and carrier.thread.channel == CHANNEL_SMS:
            thread = carrier.thread
    if thread is None:
        thread = open_thread(account, address, now=now)
    if thread is None:
        thread = _new_thread(
            account, address, contact=contact, utskick=utskick, kind=Thread.Kind.REPLY, now=now
        )
    else:
        _adopt(thread, contact=contact, utskick=utskick)
    ensure_context(thread, sms_message)
    return thread


def thread_for_stop(account, address, *, contact=None, utskick=None, sms_message=None, now=None):
    """STOPP läggs i numrets öppna tråd (som då blir en STOPP-tråd), annars i
    en ny STOPP-tråd vars förfrågan redan är klar."""
    now = now or timezone.now()
    thread = thread_for(
        account, address, contact=contact, utskick=utskick, sms_message=sms_message, now=now
    )
    if thread.kind != Thread.Kind.STOP:
        Thread.objects.filter(pk=thread.pk).update(kind=Thread.Kind.STOP)
        thread.kind = Thread.Kind.STOP
        if not thread.messages.filter(direction=ThreadMessage.Direction.IN).exists():
            # En ny tråd (bara utskickets sms): STOPP är det första svaret,
            # och förfrågan är klar från början. Ett obesvarat svar före
            # STOPP lämnas som det är, så att kunden ser det.
            Lead.objects.filter(pk=thread.lead_id, status=Lead.STATUS_NEW).update(
                status=Lead.STATUS_CONTACTED
            )
    return thread


def thread_for_start(account, address, *, contact=None, now=None):
    """START läggs i numrets senaste tråd hos kunden (oftast STOPP-tråden),
    hur gammal den än är, annars i en ny STOPP-tråd."""
    now = now or timezone.now()
    thread = (
        Thread.objects.filter(account=account, channel=CHANNEL_SMS, address=address)
        .select_related("lead")
        .order_by("-created_at", "-pk")
        .first()
    )
    if thread is None:
        return _new_thread(
            account, address, contact=contact, utskick=None, kind=Thread.Kind.STOP, now=now
        )
    return _adopt(thread, contact=contact)


def add_inbound(thread, inbound, *, raise_lead=True, looks_like_stop=False, now=None):
    """Svaret i tråden. raise_lead: en klar förfrågan blir ny igen (vanliga
    svar; STOPP och START höjer aldrig badgen)."""
    now = now or timezone.now()
    body = (inbound.body or "")[: ThreadMessage.MAX_BODY]
    message = ThreadMessage.objects.create(
        thread=thread,
        direction=ThreadMessage.Direction.IN,
        body=body,
        at=inbound.received_at or now,
        inbound=inbound,
        status=ThreadMessage.Status.RECEIVED,
    )
    updates = {"last_in_at": now, "unread": True}
    if looks_like_stop:
        updates["looks_like_stop"] = True
    Thread.objects.filter(pk=thread.pk).update(**updates)
    for key, value in updates.items():
        setattr(thread, key, value)
    if thread.lead_id:
        lead_updates = {"message": " ".join(body.split())[:LEAD_MESSAGE_MAX], "activity_at": now}
        Lead.objects.filter(pk=thread.lead_id).update(**lead_updates)
        if raise_lead:
            Lead.objects.filter(pk=thread.lead_id, status=Lead.STATUS_CONTACTED).update(
                status=Lead.STATUS_NEW
            )
    return message


def queue_answer(thread, inbound, body, *, now=None):
    """Ett svar på STOPP eller START i kö till ticken (status sending utan sms)."""
    now = now or timezone.now()
    return ThreadMessage.objects.create(
        thread=thread,
        direction=ThreadMessage.Direction.OUT,
        body=body,
        at=now,
        inbound=inbound,
        status=ThreadMessage.Status.SENDING,
    )


# ---------------------------------------------------------------------------
# Spärren (STOPP, START-flödet och knappen "Avregistrera från sms")
# ---------------------------------------------------------------------------


def log_unsubscribe_without_contact(account, value_hash, *, source, actor, detail="", now):
    """En rad i samtyckesloggen när numret inte är en kontakt (tråd utan
    kontakt, avtalet saknas): vem och när, bundet till hashen."""
    ConsentLog.objects.create(
        account=account,
        contact=None,
        channel=CHANNEL_SMS,
        value_hash=value_hash,
        old_status="",
        new_status=Consent.Status.UNSUBSCRIBED,
        basis=Consent.Basis.NONE,
        source=source,
        source_detail=(detail or "")[:200],
        by_user=actor.user,
        by_label=(actor.label or "")[:120],
        by_staff=bool(actor.staff),
        at=now,
    )


def suppress_number(
    account, e164, *, reason, source, actor, source_detail="", utskick=None, now=None
):
    """Spärra numret hos kunden och avregistrera kontakten som har det (om
    någon); köade sms till numret hoppas över. Returnerar kontakten eller
    None. Fungerar också när utskick är avstängt för kunden (D.8)."""
    from .models import Suppression

    now = now or timezone.now()
    value_hash = keys.value_hash(CHANNEL_SMS, e164)
    existed = suppressions.is_suppressed(account, CHANNEL_SMS, value_hash=value_hash)
    with transaction.atomic():
        row, contact = suppressions.suppress(
            account,
            CHANNEL_SMS,
            e164,
            reason,
            source,
            source_detail=(source_detail or "")[:200],
            actor=actor,
            now=now,
        )
        if not existed and utskick is not None:
            Suppression.objects.filter(pk=row.pk, utskick__isnull=True).update(utskick=utskick)
        if contact is None and not existed:
            log_unsubscribe_without_contact(
                account, value_hash, source=source, actor=actor, detail=source_detail, now=now
            )
        Recipient.objects.filter(
            utskick__account=account,
            channel=CHANNEL_SMS,
            status=Recipient.Status.QUEUED,
            address=e164,
        ).update(status=Recipient.Status.SKIPPED, skip_reason=Recipient.SkipReason.SUPPRESSED)
    return contact


def is_suppressed(thread):
    if thread.channel != CHANNEL_SMS or not thread.address:
        return False
    return suppressions.is_suppressed(thread.account, CHANNEL_SMS, value=thread.address)


def unsubscribe(thread, *, actor, now=None):
    """Knappen "Avregistrera från sms" (G.1 punkt 8): spärr med orsak reply
    och en rad i samtyckesloggen med källan reply och den som tryckte."""
    from .models import Suppression

    now = now or timezone.now()
    contact = suppress_number(
        thread.account,
        thread.address,
        reason=Suppression.Reason.REPLY,
        source=Consent.Source.REPLY,
        actor=actor,
        source_detail="Avregistrerad från Inkorgen",
        utskick=thread.utskick,
        now=now,
    )
    logger.info(
        "Utskick: tråd %s avregistrerad från sms av användare %s",
        thread.pk,
        getattr(actor.user, "pk", None),
    )
    return contact


# ---------------------------------------------------------------------------
# Svar från Inkorgen (G.2)
# ---------------------------------------------------------------------------


@dataclass
class Sent:
    ok: bool
    error: str = ""
    message: ThreadMessage | None = None


def reply_number():
    from apps.sms import service

    return service.reply_number()


def display_name(account):
    return (settings_for(account).display_name or "").strip()


def suffix_for(account, text=""):
    """ " /Exempelrör" när företagets namn inte står i texten (H.5), annars ""."""
    name = display_name(account)
    if not name or name.casefold() in (text or "").casefold():
        return ""
    return f" /{name}"


def reply_body(account, text):
    return f"{text}{suffix_for(account, text)}"


def clean_text(text):
    """Svarstexten: radbrytningar som \\n, utan blanksteg runt."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def check_text(check):
    """Texten för en nekad kontroll (sending/checks.reply_checks)."""
    if getattr(check, "text", ""):
        return check.text
    if check.reason in CHECK_TEXTS:
        return CHECK_TEXTS[check.reason]
    if check.defer:
        return BREAKER_TEXT
    return "Sms:et kan inte skickas till den här personen."


def counter_text(text, ore=None):
    """Räknarens rad utan JavaScript, i samma form som räknaren i
    flamingo-app-utskick.js: "GSM-7 · 134 av 160 · 1 del · 0,39 kr"."""
    from apps.sms import encoding

    analysis = encoding.analyse(text)
    parts = max(analysis.parts, 1)
    label = "GSM-7" if analysis.encoding == encoding.GSM7 else "UCS-2"
    limit = encoding.max_length(analysis.encoding, parts)
    line = f"{label} · {analysis.units} av {limit} · {parts} {'del' if parts == 1 else 'delar'}"
    if ore:
        line += " · " + f"{ore * parts / 100:.2f}".replace(".", ",") + " kr"
    return line


def part_ore(account):
    """Ungefärligt pris per sms-del i öre för räknaren, eller None."""
    from apps.sms import pricing
    from apps.sms.pricing import UNITS_PER_KR

    from .sending import sms_wrapper

    sms_account = sms_wrapper.sms_account_for(account)
    if sms_account is None:
        return None
    part = pricing.recent_part_cost("SE")
    if not part:
        return None
    units = part + pricing.markup_for(sms_account, 1)
    return round(units * 100 / UNITS_PER_KR)


def _sync_late_report(sms):
    """En leveransrapport som kom mellan 46elks svar och kopplingen till
    trådens meddelande hittade inget meddelande (smsbridge): läget läses om
    ur databasen och förs över nu."""
    from apps.sms.models import SmsMessage

    from . import smsbridge

    if sms is None:
        return
    fresh = SmsMessage.objects.filter(pk=sms.pk).first()
    if fresh is not None and fresh.status in ("delivered", "failed"):
        smsbridge.sync_from_message(fresh)


def send_reply(thread, text, *, actor, now=None):
    """Svara i tråden med ett sms från svarsnumret, direkt (ett sms, som
    API:t). Kontrollerna som för ett utskick utom fönstret och kollisionen
    (checks.reply_checks), nödbromsen, och företagets namn sist när det inte
    står i texten. Demokontot skickar aldrig."""
    from apps.sms import encoding
    from apps.sms.service import MAX_PARTS

    from .sending import checks, sms_wrapper

    now = now or timezone.now()
    account = thread.account
    if account.is_demo:
        return Sent(False, sms_wrapper.DEMO_TEXT)
    if thread.channel != CHANNEL_SMS or not thread.address:
        return Sent(False, NOT_SMS_TEXT)
    text = clean_text(text)
    if not text:
        return Sent(False, EMPTY_TEXT)
    body = reply_body(account, text)
    if encoding.analyse(body).parts > MAX_PARTS:
        return Sent(False, TOO_LONG_TEXT)
    recent = (
        ThreadMessage.objects.filter(
            thread=thread,
            direction=ThreadMessage.Direction.OUT,
            body=body,
            inbound__isnull=True,
            at__gte=now - DOUBLE_SUBMIT,
        )
        .exclude(status=ThreadMessage.Status.FAILED)
        .first()
    )
    if recent is not None:
        return Sent(True, DUPLICATE_TEXT, recent)
    if checks.breaker_active(now):
        return Sent(False, BREAKER_TEXT)
    check = checks.reply_checks(account, thread.contact, thread.address, now)
    if not check.ok:
        return Sent(False, check_text(check))

    message = ThreadMessage.objects.create(
        thread=thread,
        direction=ThreadMessage.Direction.OUT,
        body=body,
        at=now,
        sent_by=actor.user,
        sent_as_staff=bool(actor.staff),
        status=ThreadMessage.Status.SENDING,
    )
    try:
        out = sms_wrapper.send(
            account,
            to=thread.address,
            body=body,
            sender=reply_number(),
            source="reply",
            reference=f"t{message.pk}",
        )
    except sms_wrapper.DemoRefused:
        message.delete()
        return Sent(False, sms_wrapper.DEMO_TEXT)
    except keys.KeyMismatch:
        message.delete()
        logger.error("Utskick: svar i tråd %s nekades, nycklarna stämmer inte", thread.pk)
        return Sent(False, BREAKER_TEXT)
    sms = out.message
    if out.ok or (out.unknown and sms is not None):
        message.sms_message = sms
        if sms is not None and sms.status in ("sent", "delivered"):
            message.status = ThreadMessage.Status.SENT
        message.save(update_fields=["sms_message", "status"])
        _sync_late_report(sms)
        Thread.objects.filter(pk=thread.pk).update(last_out_at=now)
        if thread.lead_id:
            Lead.objects.filter(pk=thread.lead_id, status=Lead.STATUS_NEW).update(
                status=Lead.STATUS_CONTACTED
            )
        logger.info(
            "Utskick: svar %s i tråd %s skickat av användare %s%s",
            message.pk,
            thread.pk,
            getattr(actor.user, "pk", None),
            " (ADX åt kunden)" if actor.staff else "",
        )
        return Sent(True, message=message)
    message.delete()
    return Sent(False, REPLY_ERRORS.get(out.error) or out.detail or BREAKER_TEXT)


# ---------------------------------------------------------------------------
# Det mallen visar
# ---------------------------------------------------------------------------


@dataclass
class Row:
    message: ThreadMessage
    direction: str
    body: str
    at: object
    label: str
    note: str = ""
    tone: str = ""


def _who(message):
    user = message.sent_by
    first = ""
    if user is not None:
        first = (user.first_name or user.get_username() or "").strip()
    if message.sent_as_staff:
        return f"Skickat av ADX ({first}) åt kunden" if first else "Skickat av ADX åt kunden"
    return f"Svar från {first}" if first else "Ditt svar"


def _utskick_context(thread, message):
    """Utskickets sms utan SmsMessage att peka på: demots simulerade sms
    (demo.py) och ett sms vars rad inte längre finns. Det står i tråden
    före tråden skapades (svaret skapar den) och har ingen avsändare."""
    return bool(
        thread.utskick_id
        and not message.sms_message_id
        and not message.sent_by_id
        and not message.sent_as_staff
        and message.at <= thread.created_at
    )


def rows(thread):
    """Trådens meddelanden i tidsordning, med etikett och läge."""
    out = []
    messages = thread.messages.select_related("sent_by", "inbound", "sms_message").order_by(
        "at", "pk"
    )
    for message in messages:
        note, tone = "", ""
        if message.direction == ThreadMessage.Direction.IN:
            keyword = (message.inbound.meta or {}).get("keyword") if message.inbound else ""
            label = {"stop": "Svarade STOPP", "start": "Svarade START"}.get(keyword, "Svar")
            out.append(Row(message, "in", message.body, message.at, label))
            continue
        if message.inbound_id:
            label = "Automatiskt svar från Flamingo"
            answers = (message.inbound.meta or {}).get("answers", {})
            reason = answers.get(str(thread.account_id), "")
            if message.status == ThreadMessage.Status.FAILED:
                note, tone = ANSWER_TEXTS.get(reason, ANSWER_TEXTS["failed"]), "warn"
            elif message.status == ThreadMessage.Status.SENDING:
                note = "Skickas"
        elif message.sms_message_id and message.sms_message.source in ("utskick", "flow"):
            label = "Utskick"
            if thread.utskick_id and message.sms_message.source == "utskick":
                label = f"Utskick: {thread.utskick.name}"
        elif _utskick_context(thread, message):
            label = f"Utskick: {thread.utskick.name}"
        else:
            label = _who(message)
        if not note:
            if message.status == ThreadMessage.Status.SENDING:
                note = "Skickas"
            elif message.status == ThreadMessage.Status.FAILED:
                note, tone = "Kom inte fram", "warn"
        out.append(Row(message, "out", message.body, message.at, label, note, tone))
    return out


def status_label(lead):
    """Förfrågans läge i Inkorgen: "Klar" för ett hanterat svar,
    "Avregistrerad automatiskt" för en STOPP-tråd."""
    if lead.source != Lead.SOURCE_REPLY or lead.status != Lead.STATUS_CONTACTED:
        return lead.get_status_display()
    thread = getattr(lead, "reply_thread", None)
    if thread is not None and thread.kind == Thread.Kind.STOP:
        return STOP_LEAD_LABEL
    return DONE_LABEL


def channel_label(thread):
    """Kanalen i Inkorgen: "Sms-svar", "E-postsvar" eller "STOPP" (C.2)."""
    if thread is None:
        return "Svar på utskick"
    if thread.kind == Thread.Kind.STOP:
        return "STOPP"
    return "E-postsvar" if thread.channel == CHANNEL_EMAIL else "Sms-svar"


# ---------------------------------------------------------------------------
# Ticken: svar på STOPP/START och ägarens sms
# ---------------------------------------------------------------------------


def answers_queued():
    return ThreadMessage.objects.filter(
        direction=ThreadMessage.Direction.OUT,
        status=ThreadMessage.Status.SENDING,
        sms_message__isnull=True,
        inbound__isnull=False,
    )


def notices_due(now):
    floor = now - NOTICE_LOOKBACK
    end = now - NOTICE_SETTLE
    rows = (
        UtskickSettings.objects.filter(account__is_demo=False)
        .filter(Q(reply_notice_at__isnull=True) | Q(reply_notice_at__lte=now - NOTICE_EVERY))
        .annotate(since=Greatest(Coalesce("reply_notice_at", Value(floor)), Value(floor)))
    )
    replies = ThreadMessage.objects.filter(
        thread__account=OuterRef("account"),
        direction=ThreadMessage.Direction.IN,
        inbound__status=InboundMessage.Status.ROUTED,
        inbound__created_at__gt=OuterRef("since"),
        inbound__created_at__lte=end,
    )
    leads = (
        Lead.objects.filter(
            account=OuterRef("account"),
            utskick__isnull=False,
            created_at__gt=OuterRef("since"),
            created_at__lte=end,
        )
        .exclude(source=Lead.SOURCE_REPLY)
        .exclude(status=Lead.STATUS_JUNK)
    )
    return rows.filter(
        (Q(notify_on_reply=True) & Exists(replies)) | (Q(account__notify_sms=True) & Exists(leads))
    )


def work_exists(now=None):
    """Köade svar på STOPP/START, eller ägarsms att skicka. Två EXISTS."""
    now = now or timezone.now()
    return answers_queued().exists() or notices_due(now).exists()


def _record_answer(message, reason):
    """Varför svaret slutade som det gjorde, på det inkommande meddelandet
    (meta["answers"][konto], inga texter)."""
    with transaction.atomic():
        inbound = InboundMessage.objects.select_for_update().get(pk=message.inbound_id)
        meta = dict(inbound.meta or {})
        answers = dict(meta.get("answers") or {})
        answers[str(message.thread.account_id)] = reason
        meta["answers"] = answers
        InboundMessage.objects.filter(pk=inbound.pk).update(meta=meta)


def _fail_answer(message, reason):
    ThreadMessage.objects.filter(pk=message.pk).update(status=ThreadMessage.Status.FAILED)
    _record_answer(message, reason)


def cap_allows(sms_account, to, body, now=None):
    """Taket före anropet (D.4): ryms svaret i det som är kvar av månaden?"""
    from apps.sms import encoding, numbers, pricing

    use = pricing.usage(sms_account, now)
    if use["cap_reached"]:
        return False
    try:
        country = numbers.parse(to).country
    except numbers.InvalidNumber:
        country = "SE"
    parts = max(encoding.analyse(body).parts, 1)
    per_part = pricing.recent_part_cost(country, now) or pricing.FALLBACK_PART_COST
    estimate = parts * per_part + pricing.markup_for(sms_account, parts)
    return use["remaining"] >= estimate


def _send_answers(now, deadline, counts):
    from .sending import checks, sms_wrapper

    queued = list(
        answers_queued()
        .select_related("thread__account__customer", "inbound")
        .order_by("at", "pk")[:ANSWER_BATCH]
    )
    breaker = None
    for message in queued:
        if time.monotonic() > deadline:
            break
        thread = message.thread
        account = thread.account
        if now - message.at > ANSWER_MAX_AGE:
            _fail_answer(message, "expired")
            counts["skipped"] += 1
            continue
        if account.is_demo:
            _fail_answer(message, "demo")
            counts["skipped"] += 1
            continue
        if breaker is None:
            breaker = checks.breaker_active(now)
        if breaker:
            break
        sms_account = sms_wrapper.sms_account_for(account)
        if sms_account is None or not sms_account.is_enabled:
            _fail_answer(message, "not_enabled")
            counts["skipped"] += 1
            continue
        if not cap_allows(sms_account, thread.address, message.body, now):
            _fail_answer(message, "cap")
            counts["skipped"] += 1
            continue
        try:
            out = sms_wrapper.send(
                account,
                to=thread.address,
                body=message.body,
                sender=reply_number(),
                source="system",
                reference=f"x{message.inbound_id}",
            )
        except sms_wrapper.DemoRefused:
            _fail_answer(message, "demo")
            counts["skipped"] += 1
            continue
        sms = out.message
        if out.ok or (out.unknown and sms is not None):
            status = (
                ThreadMessage.Status.SENT
                if sms is not None and sms.status in ("sent", "delivered")
                else ThreadMessage.Status.SENDING
            )
            ThreadMessage.objects.filter(pk=message.pk).update(sms_message=sms, status=status)
            _sync_late_report(sms)
            Thread.objects.filter(pk=thread.pk).update(last_out_at=timezone.now())
            _record_answer(message, "sent")
            counts["answers"] += 1
            continue
        if out.error == "rate_limited":
            # Minutgränsen eller 46elks 429: resten väntar till nästa tick.
            break
        reason = {"monthly_cap_reached": "cap", "sms_not_enabled": "not_enabled"}.get(
            out.error, "failed"
        )
        if sms is not None:
            ThreadMessage.objects.filter(pk=message.pk).update(sms_message=sms)
        _fail_answer(message, reason)
        counts["skipped"] += 1


def _single_reply_text(message, url):
    from apps.flamingo import sms as flamingo_sms

    lead = message.thread.lead
    return flamingo_sms.owner_reply_text(
        message.thread.account,
        1,
        0,
        url,
        name=lead.name if lead else "",
        phone=normalize.display_phone(message.thread.address),
        text=message.body,
    )


def _absolute(path):
    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else ""


def _send_notices(now, deadline, counts):
    from apps.flamingo import sms as flamingo_sms

    end = now - NOTICE_SETTLE
    floor = now - NOTICE_LOOKBACK
    for row in list(notices_due(now).select_related("account__customer")[:NOTICE_BATCH]):
        if time.monotonic() > deadline:
            break
        account = row.account
        with transaction.atomic():
            locked = (
                UtskickSettings.objects.select_for_update(skip_locked=True)
                .filter(pk=row.pk)
                .filter(
                    Q(reply_notice_at__isnull=True) | Q(reply_notice_at__lte=now - NOTICE_EVERY)
                )
                .first()
            )
            if locked is None:
                continue
            since = max(locked.reply_notice_at or floor, floor)
            replies = []
            if locked.notify_on_reply:
                replies = list(
                    ThreadMessage.objects.filter(
                        thread__account=account,
                        direction=ThreadMessage.Direction.IN,
                        inbound__status=InboundMessage.Status.ROUTED,
                        inbound__created_at__gt=since,
                        inbound__created_at__lte=end,
                    )
                    .select_related("thread__lead", "thread__account")
                    .order_by("at", "pk")
                )
            leads = []
            if account.notify_sms:
                leads = list(
                    Lead.objects.filter(
                        account=account,
                        utskick__isnull=False,
                        created_at__gt=since,
                        created_at__lte=end,
                    )
                    .exclude(source=Lead.SOURCE_REPLY)
                    .exclude(status=Lead.STATUS_JUNK)
                    .select_related("service")
                    .order_by("created_at", "pk")
                )
            UtskickSettings.objects.filter(pk=locked.pk).update(reply_notice_at=end)
        if not replies and not leads:
            continue
        inbox = _absolute(reverse("flamingo:app_inbox"))
        if len(replies) == 1 and not leads and replies[0].thread.lead_id:
            lead_url = _absolute(reverse("flamingo:app_lead", args=[replies[0].thread.lead_id]))
            text = _single_reply_text(replies[0], lead_url)
            lead = replies[0].thread.lead
        elif len(leads) == 1 and not replies:
            text = flamingo_sms.owner_text(leads[0])
            lead = leads[0]
        else:
            text = flamingo_sms.owner_reply_text(account, len(replies), len(leads), inbox)
            lead = None
        flamingo_sms.notify_owner_text(account, text, lead=lead)
        counts["notices"] += 1


def send_due(now=None, deadline=None):
    """Tickens fas 3: svaren på STOPP/START (nödbromsen och taket gäller,
    tidsfönstret inte) och ägarens samlade sms. Bara antal i svaret."""
    now = now or timezone.now()
    deadline = deadline if deadline is not None else time.monotonic() + 5
    counts = {"answers": 0, "notices": 0, "skipped": 0}
    _send_answers(now, deadline, counts)
    if time.monotonic() < deadline:
        _send_notices(now, deadline, counts)
    return counts
