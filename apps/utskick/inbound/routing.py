"""
Vem ett sms till det delade svarsnumret hör till (README G.1 punkt 5, D4).

    candidates(e164, now) -> list[Candidate]   kunder som skickat från svarsnumret
                                               till numret, senaste först
    process(inbound, now)                      routa ett nytt InboundMessage
                                               (anropas av elks.handle i dess transaktion)
    route_held(inbound, account, user, now)    byrån kopplar ett väntande sms till en kund
    ignore_held(inbound, user, now)            byrån lägger det åt sidan

Kandidaterna är kunderna vars SmsMessage(to=numret, sender=svarsnumret,
status skickat eller reserverat) finns de senaste 30 dagarna (det partiella
indexet sms_msg_reply_number), annars den senaste äldre. Sms:et från
kandidaten är "det senaste", och det avgör tråden: ett svar från Inkorgen
eller en bekräftelse (tråden som bär det), en mottagare i ett utskick
(kontaktens öppna tråd, annars en ny).

    STOPP              gäller varje kandidat (stop.apply_stop)
    START              gäller kunderna i numrets senaste STOPP (stop.apply_start)
    annan text         en kandidat de senaste 30 dagarna, eller ingen där och en
                       äldre: routas dit. Flera: ambiguous, väntar på byrån
                       (/manage/utskick/#inkommande) och syns för ingen kund.
                       Ingen: unroutable, samma lista. Byrån larmas högst en
                       gång i timmen.

Skydd mot slingor: en avsändare som inte är ett mobilnummer i E.164
(alfanumeriska avsändare, kortnummer) sparas som ignored och besvaras aldrig;
svarsnumret självt likaså. Fler än FLOOD_PER_HOUR vanliga svar i timmen från
ett nummer räknas bara (counted). Ett nummer i taget (rådgivande lås per
nummer i transaktionen), så att två sms som kommer samtidigt hamnar i samma
tråd. Texten töms på InboundMessage när den routats (den bor i
ThreadMessage); väntande sms behåller den tills byrån bestämt.
"""

import logging
import zlib
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection, transaction
from django.urls import reverse

from apps.flamingo.models import FlamingoAccount
from apps.sms import numbers
from apps.sms.models import SmsMessage

from .. import alerts, contacts, keys, limits, suppression, threads
from ..access import can_collect
from ..models import CHANNEL_SMS, Contact, InboundMessage, Recipient, ThreadMessage
from . import stop

logger = logging.getLogger(__name__)

#: Kunder som skickat de senaste så här många dagarna är kandidater (G.1).
RECENT = timedelta(days=30)
#: Högst så många vanliga svar per nummer och timme routas; resten räknas.
FLOOD_PER_HOUR = 20
#: pg_advisory_xact_lock för ett nummer i taget, bredvid limits.TICK_LOCK
#: (0x5554), ADX_MAIL_LOCK (0x5555) och CONTACT_LIMIT_LOCK (0x5556).
INBOUND_LOCK = 0x5557 << 32
#: Statusar som väntar på byrån.
HELD = (InboundMessage.Status.AMBIGUOUS, InboundMessage.Status.UNROUTABLE)
#: Sms som räknas som "skickat från svarsnumret" (46elks tog emot det, eller
#: läget är oklart).
SENT_LIKE = (*SmsMessage.ACCEPTED, SmsMessage.Status.RESERVED)


@dataclass
class Candidate:
    """En kund som numret kan svara: kontot, det senaste sms:et från
    svarsnumret till numret (eller None när byrån kopplar för hand),
    mottagaren och utskicket det sms:et hörde till."""

    account: FlamingoAccount
    message: SmsMessage | None = None
    recent: bool = True
    recipient: Recipient | None = None
    utskick: object = None


def reply_number():
    from apps.sms import service

    return service.reply_number()


def _lock_number(e164):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            [INBOUND_LOCK + (zlib.crc32(e164.encode()) & 0x7FFFFFFF)],
        )


def _flamingo_accounts(sms_account_ids):
    """{SmsAccount-id: FlamingoAccount} via kunden."""
    from apps.sms.models import SmsAccount

    customers = dict(
        SmsAccount.objects.filter(pk__in=sms_account_ids).values_list("pk", "customer_id")
    )
    accounts = {
        a.customer_id: a
        for a in FlamingoAccount.objects.filter(
            customer_id__in=set(customers.values())
        ).select_related("customer")
    }
    return {pk: accounts[cid] for pk, cid in customers.items() if cid in accounts}


def _describe(candidate):
    """Mottagaren och utskicket som kandidatens senaste sms hörde till."""
    message = candidate.message
    if message is None:
        return candidate
    recipient = (
        Recipient.objects.filter(sms_message=message, utskick__account=candidate.account)
        .select_related("utskick", "contact")
        .first()
    )
    if recipient is not None:
        candidate.recipient = recipient
        candidate.utskick = recipient.utskick
        return candidate
    carrier = (
        ThreadMessage.objects.filter(sms_message=message, thread__account=candidate.account)
        .select_related("thread__utskick")
        .first()
    )
    if carrier is not None:
        candidate.utskick = carrier.thread.utskick
    return candidate


def candidates(e164, now):
    """Kunderna som numret kan svara, senaste först (G.1 punkt 5): alla med
    ett sms från svarsnumret de senaste 30 dagarna, annars den senaste
    äldre. Tom lista: ingen kund har skickat dit."""
    rows = SmsMessage.objects.filter(to=e164, sender=reply_number(), status__in=SENT_LIKE).exclude(
        error_code="provider_error"
    )
    recent = list(rows.filter(created_at__gte=now - RECENT).order_by("-created_at", "-pk")[:200])
    latest = {}
    for message in recent:
        latest.setdefault(message.account_id, message)
    is_recent = True
    if not latest:
        older = rows.order_by("-created_at", "-pk").first()
        if older is None:
            return []
        latest = {older.account_id: older}
        is_recent = False
    accounts = _flamingo_accounts(list(latest))
    found = [
        _describe(Candidate(accounts[pk], message, recent=is_recent))
        for pk, message in latest.items()
        if pk in accounts
    ]
    return found


def candidate_for(account, e164, now):
    """Kontots kandidat för numret (byrån kopplar för hand): dess senaste sms
    från svarsnumret dit, om något."""
    from apps.sms.models import SmsAccount

    message = None
    sms_account = SmsAccount.objects.filter(customer_id=account.customer_id).first()
    if sms_account is not None:
        message = (
            SmsMessage.objects.filter(
                account=sms_account, to=e164, sender=reply_number(), status__in=SENT_LIKE
            )
            .exclude(error_code="provider_error")
            .order_by("-created_at", "-pk")
            .first()
        )
    return _describe(Candidate(account, message, recent=True))


# ---------------------------------------------------------------------------
# Routningen
# ---------------------------------------------------------------------------


def _new_contact(account, e164, now):
    """En kontakt för ett nummer som svarar men inte finns i registret, bara
    när kontot får ta in kontakter (H.1) och numret inte är spärrat (till
    exempel efter en GDPR-borttagning). Annars None: tråden får ingen kontakt."""
    if not can_collect(account):
        return None
    if suppression.is_suppressed(account, CHANNEL_SMS, value=e164):
        return None
    try:
        return contacts.create(
            account,
            {"phone": e164},
            source=Contact.Source.REPLY,
            source_detail="Svarade på ett sms",
            now=now,
        )
    except contacts.ContactError:
        logger.info("Utskick: svaret till konto %s fick ingen kontakt", account.pk)
        return None


def _contact_for(candidate, e164, now, create=True):
    recipient = candidate.recipient
    if recipient is not None and recipient.contact_id:
        return recipient.contact
    contact = Contact.objects.filter(account=candidate.account, phone=e164).first()
    if contact is not None or not create:
        return contact
    return _new_contact(candidate.account, e164, now)


def route_to(inbound, candidate, now, via, create_contact=True):
    """Ett vanligt svar till kandidatens Inkorg: tråden, förfrågan,
    mottagarens replied_at och "Ser ut som en avregistrering" (reklam).
    create_contact=False: ingen ny kontakt (byrån kopplade till en kund som
    inte har skickat till numret)."""
    e164 = inbound.from_address
    account = candidate.account
    contact = _contact_for(candidate, e164, now, create=create_contact)
    thread = threads.thread_for(
        account,
        e164,
        contact=contact,
        utskick=candidate.utskick,
        sms_message=candidate.message,
        now=now,
    )
    flagged = stop.reklam_thread(thread) and stop.looks_like_unsubscribe(inbound.body)
    threads.add_inbound(thread, inbound, raise_lead=True, looks_like_stop=flagged, now=now)
    if candidate.recipient is not None:
        Recipient.objects.filter(pk=candidate.recipient.pk, replied_at__isnull=True).update(
            replied_at=now
        )
    if contact is not None:
        contacts.touch(contact, "reply", now)
    inbound.status = InboundMessage.Status.ROUTED
    inbound.account = account
    inbound.contact = contact
    inbound.routed_via = via[:30]
    inbound.meta = {**(inbound.meta or {}), "thread": thread.pk}
    if flagged:
        inbound.meta["looks_like_stop"] = True
    return thread


def _hold(inbound, status, found, now):
    inbound.status = status
    inbound.meta = {**(inbound.meta or {}), "candidates": [c.account.pk for c in found]}
    transaction.on_commit(lambda: _alert_held(now))


def _alert_held(now):
    from ..models import InboundMessage as Model

    waiting = Model.objects.filter(status__in=HELD).count()
    alerts.agency(
        "Utskick: sms till svarsnumret väntar på byrån",
        [
            f"{waiting} sms till svarsnumret kunde inte kopplas till en kund.",
            "Koppla dem eller lägg dem åt sidan under Utskick, Inkommande sms:",
            reverse("manage:utskick_overview") + "#inkommande",
        ],
        once="inbound_held",
        now=now,
    )


def _alert_flood(now):
    alerts.agency(
        "Utskick: många sms från ett nummer",
        [
            f"Ett nummer har skickat fler än {FLOOD_PER_HOUR} sms till svarsnumret på en timme.",
            "De räknas men hamnar inte i någon Inkorg.",
        ],
        once="inbound_flood",
        now=now,
    )


def process(inbound, now):
    """Routa ett nytt inkommande sms. Skriver inbound (status, konto,
    kontakt, routed_via, meta) och sparar det. Körs i hanterarens
    transaktion; inga utgående anrop."""
    raw_from = inbound.from_address
    try:
        e164 = numbers.parse(raw_from).e164
    except numbers.InvalidNumber:
        e164 = ""
    shared = reply_number()
    if inbound.to_address != shared:
        return _ignore(inbound, "to")
    if not e164 or not raw_from.startswith("+"):
        return _ignore(inbound, "sender")
    if e164 == shared:
        return _ignore(inbound, "self")
    inbound.from_address = e164
    _lock_number(e164)

    keyword = stop.classify(inbound.body)
    if keyword == stop.STOP:
        found = candidates(e164, now)
        if found:
            stop.apply_stop(inbound, found, now)
            if found[0].message is not None:
                inbound.routed_via = f"sms:{found[0].message.pk}"
            return _done(inbound)
        inbound.meta = {**(inbound.meta or {}), "keyword": stop.STOP}
        _hold(inbound, InboundMessage.Status.UNROUTABLE, found, now)
        return _save(inbound)
    if keyword == stop.START:
        prior = stop.latest_stop(e164, now, exclude_pk=inbound.pk)
        if prior is not None and stop.is_suppressed_anywhere(
            e164, (prior.meta or {}).get("accounts", [])
        ):
            stop.apply_start(inbound, prior, now)
            inbound.routed_via = f"stopp:{prior.pk}"[:30]
            return _done(inbound)

    flood_key = keys.value_hash(CHANNEL_SMS, e164)
    if limits.hit("inbound_sms", flood_key, limits.hour_window(now), FLOOD_PER_HOUR):
        inbound.status = InboundMessage.Status.COUNTED
        inbound.body = ""
        transaction.on_commit(lambda: _alert_flood(now))
        return _save(inbound)

    found = candidates(e164, now)
    recent = [c for c in found if c.recent]
    if len(recent) > 1:
        _hold(inbound, InboundMessage.Status.AMBIGUOUS, found, now)
        return _save(inbound)
    if not found:
        _hold(inbound, InboundMessage.Status.UNROUTABLE, found, now)
        return _save(inbound)
    chosen = found[0]
    route_to(inbound, chosen, now, via=f"sms:{chosen.message.pk}")
    return _done(inbound)


def _ignore(inbound, reason):
    inbound.status = InboundMessage.Status.IGNORED
    inbound.body = ""
    inbound.meta = {**(inbound.meta or {}), "reason": reason}
    return _save(inbound)


def _done(inbound):
    """Routat: texten bor nu i ThreadMessage."""
    inbound.body = ""
    return _save(inbound)


def _save(inbound):
    inbound.save(
        update_fields=["status", "from_address", "body", "account", "contact", "routed_via", "meta"]
    )
    return inbound


# ---------------------------------------------------------------------------
# Byrån (manage_inbound.py)
# ---------------------------------------------------------------------------


class NotHeld(Exception):
    """Sms:et väntar inte längre på byrån (någon hann före)."""


def route_held(inbound_pk, account, *, user, now):
    """Byrån kopplar ett väntande sms till kontot: som ett vanligt svar, eller
    en STOPP hos just den kunden. Returnerar det uppdaterade sms:et."""
    with transaction.atomic():
        inbound = InboundMessage.objects.select_for_update().get(pk=inbound_pk)
        if inbound.status not in HELD:
            raise NotHeld
        _lock_number(inbound.from_address)
        candidate = candidate_for(account, inbound.from_address, now)
        via = f"byrå:{user.pk}"
        if (inbound.meta or {}).get("keyword") == stop.STOP:
            stop.apply_stop(inbound, [candidate], now)
            inbound.routed_via = via[:30]
        else:
            # Ingen kontakt skapas hos en kund som aldrig skickat till numret.
            route_to(inbound, candidate, now, via=via, create_contact=candidate.message is not None)
        inbound.meta = {**(inbound.meta or {}), "routed_by": user.pk}
        _done(inbound)
    logger.info(
        "Utskick: inkommande %s kopplat till konto %s av användare %s",
        inbound.pk,
        account.pk,
        user.pk,
    )
    return inbound


def ignore_held(inbound_pk, *, user, now):
    with transaction.atomic():
        inbound = InboundMessage.objects.select_for_update().get(pk=inbound_pk)
        if inbound.status not in HELD:
            raise NotHeld
        inbound.meta = {**(inbound.meta or {}), "ignored_by": user.pk}
        _ignore(inbound, "agency")
    logger.info("Utskick: inkommande %s lades åt sidan av användare %s", inbound.pk, user.pk)
    return inbound
