"""
Skicka sms från kontaktkortet (README I.1, I.4, I.7, H.6, J S4): ett sms
från svarsnumret till kontaktens nummer, i kontaktens öppna sms-tråd eller
en ny tråd av slaget direct, så att svaret hamnar i samma tråd i Inkorgen.

    problem(contact, now=None) -> str         varför ett sms inte kan skickas nu
                                              ("Numret är avregistrerat från sms. ..."),
                                              "" när det går
    thread_for(contact, now=None) -> Thread   kontaktens öppna sms-tråd, annars en ny direct
    send(contact, text, *, actor, now=None) -> threads.Sent

Reglerna:

- Alla kontroller före sändningen (sending/checks.contact_sms_checks):
  demokontot, Switchboard och nödbromsen, kontot (utskick på, inte stoppat
  av byrån), spärren för numret (STOPP stoppar alltid), kundens sms-konto,
  tidsfönstret, kostnadstaket (och threads.cap_allows för just det här
  sms:et) och, när sms:et skickas, kollisionen på svarsnumret (en annan
  kund har skickat dit från numret de senaste 30 dagarna: svaret skulle
  vänta hos byrån; texten nämner aldrig den andra kunden). Inget
  veckotak och inget reklamsamtycke: ett enskilt sms från kortet är ingen
  reklam.
- Företagets namn: " /Exempelrör" läggs till när det inte står i texten
  (threads.reply_body, som Inkorgens svar, H.5).
- Byrån i kundvyn kryssar i "Jag skickar det här som ADX åt Exempelrör."
  (vyn prövar det) och ThreadMessage.sent_by och sent_as_staff sparas;
  loggen har användarens pk och "(ADX åt kunden)".
- Demokontot skickar aldrig (D12): send nekar före allt annat, och
  sms_wrapper nekar en gång till.
- Dubbeltryck: kontaktens rad låses (select_for_update) medan tråden och
  meddelandet skapas, och samma text i samma tråd inom
  threads.DOUBLE_SUBMIT skickas inte igen.
- Tråden: kontaktens öppna sms-tråd (threads.open_thread, ett meddelande de
  senaste 30 dagarna), annars en ny tråd av slaget direct vars förfrågan i
  Inkorgen börjar som "Klar" (contacted), så att kortets sms aldrig höjer
  badgen; ett svar gör den ny igen (threads.add_inbound). Går sms:et inte
  i väg tas en ny tråd bort igen. Svaret hittar tråden genom sms:et
  (inbound/routing och threads.thread_for, ThreadMessage.sms_message).
- Källan i apps/sms är "reply" (ett enskilt sms, ingen headroom), sändaren
  svarsnumret och referensen t<meddelande>, som threads.send_reply.

Kunden mejlas aldrig härifrån. Loggarna har bara pk.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.flamingo.models import Lead

from . import keys, threads
from .models import CHANNEL_SMS, Contact, Thread, ThreadMessage
from .sending import checks, sms_wrapper

logger = logging.getLogger(__name__)

EMPTY_TEXT = "Skriv ett sms."
TOO_LONG_TEXT = "Sms:et blir för långt: högst 6 sms-delar."
DUPLICATE_TEXT = "Sms:et är redan skickat."
DELETED_TEXT = "Kontakten är borttagen."
SENT_TEXT = "Sms:et är skickat. Svarar personen hamnar svaret i Inkorgen."
#: Felen från apps/sms med kortets ord (threads.REPLY_ERRORS säger "svaret").
SEND_ERRORS = {
    **threads.REPLY_ERRORS,
    "monthly_cap_reached": "Månadens kostnadstak för sms är nått, så sms:et skickades inte.",
    "provider_error": "Sms-leverantören tog inte emot sms:et. Försök igen om en stund.",
}


def problem(contact, now=None):
    """Texten som förklarar varför ett sms inte kan skickas till kontakten
    nu, eller "" när det går (för formuläret på kortet). Kollisionen på
    svarsnumret prövas bara när sms:et skickas (send), aldrig på en GET.
    Demokontot får formuläret med raden "Demokontot skickar aldrig." (som
    Inkorgens svar); send nekar det ändå."""
    check = checks.contact_sms_checks(contact.account, contact, now)
    if check.ok or check.reason == "demo":
        return ""
    return check.text or threads.check_text(check)


def _new_direct(contact, address, now, body=""):
    """En ny tråd av slaget direct med sin förfrågan, som börjar som "Klar".
    Förfrågan får ingen kontakt (tråden har den): ett sms som kunden själv
    skickade är ingen förfrågan på kontaktkortet ("1 förfrågan", tidslinjen).
    Borttagningen (H.4) och Inkorgens Kontaktkort går genom tråden."""
    lead = Lead.objects.create(
        account_id=contact.account_id,
        source=Lead.SOURCE_REPLY,
        status=Lead.STATUS_CONTACTED,
        message=" ".join(str(body or "").split())[: threads.LEAD_MESSAGE_MAX],
        created_at=now,
        activity_at=now,
        **threads._lead_fields(contact, address, CHANNEL_SMS),
    )
    return Thread.objects.create(
        account_id=contact.account_id,
        contact=contact,
        channel=CHANNEL_SMS,
        kind=Thread.Kind.DIRECT,
        lead=lead,
        address=address,
        unread=False,
        created_at=now,
    )


def _address(contact):
    return keys.clean_value(CHANNEL_SMS, contact.phone or "")


def thread_for(contact, now=None):
    """Tråden sms:et skrivs i: kontaktens öppna sms-tråd (threads.open_thread),
    annars en ny tråd av slaget direct."""
    now = now or timezone.now()
    address = _address(contact)
    thread = threads.open_thread(contact.account, address, now=now)
    if thread is not None:
        return threads._adopt(thread, contact=contact)
    return _new_direct(contact, address, now)


def _undo(message, thread, created):
    """Sms:et gick inte i väg: meddelandet bort, och en tråd som skapades för
    det (förfrågan tas bort, tråden följer med)."""
    ThreadMessage.objects.filter(pk=message.pk).delete()
    if created and thread.lead_id:
        Lead.objects.filter(pk=thread.lead_id).delete()


def send(contact, text, *, actor, now=None):
    """Skicka text till kontaktens nummer efter alla kontroller. Returnerar
    threads.Sent (ok, error, message); error är också texten för ett
    dubbeltryck (ok med DUPLICATE_TEXT)."""
    from apps.sms import encoding
    from apps.sms.service import MAX_PARTS

    now = now or timezone.now()
    account = contact.account
    if account is None or account.is_demo:
        return threads.Sent(False, sms_wrapper.DEMO_TEXT)
    text = threads.clean_text(text)
    if not text:
        return threads.Sent(False, EMPTY_TEXT)
    body = threads.reply_body(account, text)
    if encoding.analyse(body).parts > MAX_PARTS:
        return threads.Sent(False, TOO_LONG_TEXT)
    check = checks.contact_sms_checks(account, contact, now, sending=True)
    if not check.ok:
        return threads.Sent(False, check.text or threads.check_text(check))
    if not threads.cap_allows(sms_wrapper.sms_account_for(account), _address(contact), body, now):
        return threads.Sent(False, SEND_ERRORS["monthly_cap_reached"])

    with transaction.atomic():
        locked = (
            Contact.objects.select_for_update().filter(pk=contact.pk, account_id=account.pk).first()
        )
        if locked is None:
            return threads.Sent(False, DELETED_TEXT)
        address = _address(locked)
        if not address:
            return threads.Sent(False, checks.NO_PHONE_TEXT)
        thread = threads.open_thread(account, address, now=now)
        created = thread is None
        if created:
            thread = _new_direct(locked, address, now, body)
        else:
            threads._adopt(thread, contact=locked)
        recent = (
            ThreadMessage.objects.filter(
                thread=thread,
                direction=ThreadMessage.Direction.OUT,
                body=body,
                inbound__isnull=True,
                at__gte=now - threads.DOUBLE_SUBMIT,
            )
            .exclude(status=ThreadMessage.Status.FAILED)
            .first()
        )
        if recent is not None:
            return threads.Sent(True, DUPLICATE_TEXT, recent)
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
            to=address,
            body=body,
            sender=threads.reply_number(),
            source="reply",
            reference=f"t{message.pk}",
        )
    except sms_wrapper.DemoRefused:
        _undo(message, thread, created)
        return threads.Sent(False, sms_wrapper.DEMO_TEXT)
    except keys.KeyMismatch:
        _undo(message, thread, created)
        logger.error("Utskick: sms från kortet till kontakt %s nekades, nycklarna", contact.pk)
        return threads.Sent(False, threads.BREAKER_TEXT)
    sms = out.message
    if out.ok or (out.unknown and sms is not None):
        message.sms_message = sms
        if sms is not None and sms.status in ("sent", "delivered"):
            message.status = ThreadMessage.Status.SENT
        message.save(update_fields=["sms_message", "status"])
        threads._sync_late_report(sms)
        Thread.objects.filter(pk=thread.pk).update(last_out_at=now)
        if not created and thread.lead_id:
            # Som ett svar från Inkorgen: ett obesvarat svar är nu besvarat.
            Lead.objects.filter(pk=thread.lead_id, status=Lead.STATUS_NEW).update(
                status=Lead.STATUS_CONTACTED
            )
        logger.info(
            "Utskick: sms %s från kontaktkortet till kontakt %s i tråd %s, användare %s%s",
            message.pk,
            contact.pk,
            thread.pk,
            getattr(actor.user, "pk", None),
            " (ADX åt kunden)" if actor.staff else "",
        )
        return threads.Sent(True, message=message)
    _undo(message, thread, created)
    return threads.Sent(False, SEND_ERRORS.get(out.error) or out.detail or threads.BREAKER_TEXT)
