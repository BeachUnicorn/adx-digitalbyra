"""
STOPP och START i svaren till det delade svarsnumret (README G.1 punkt 6 till
8, H.6).

    normalise(text) -> list[str]     orden, versaler, utan skiljetecken och emoji
    classify(text) -> "stop" | "start" | ""
    looks_like_unsubscribe(text)     "Ser ut som en avregistrering" (bara på reklam)
    apply_stop(inbound, candidates, now)    spärren hos varje kandidat, trådarna
                                     och bekräftelsen i kö
    apply_start(inbound, stop, now)  en bekräftelselänk per konto som STOPP gällde
    stop_text(names) / start_text(name, link)   texterna i svaren

Ordlistan (G.1 punkt 6): STOPP eller STOP först stoppar oavsett längd; STOPPA,
AVSLUTA, AVREGISTRERA, AVANMÄL, SLUTA och UNSUBSCRIBE först stoppar med
högst åtta ord. Aldrig när andra ordet är INTE ("Stoppa inte min bokning").
START först med högst tre ord ber om en bekräftelselänk: START lyfter aldrig
något själv, eftersom avsändarnummer kan förfalskas.

En STOPP gäller varje kandidat (kunder som skickat från svarsnumret till
numret de senaste 30 dagarna, routing.candidates): spärr per kund (orsak
stop), samtycket avregistrerat med "Svar STOPP" i loggen, köade mottagare
hoppas över och tråden blir en STOPP-tråd vars förfrågan redan är klar.
Bekräftelsen ("Du får inga fler sms från Exempelrör. Svara START om du
ångrar dig.") köas till ticken (threads.send_due), en per kund: varje kund
betalar sin egen och ser bara sitt eget namn i sin tråd (en gemensam text
skulle visa en kund vilka andra ADX-kunder som skickar till personen).
Högst en per nummer, kund och dygn. Inget skickas härifrån.
"""

import unicodedata
from datetime import timedelta

from django.db.models import Q

from .. import codes, keys, links, suppression, threads
from ..access import Actor, settings_for
from ..models import (
    CHANNEL_SMS,
    REKLAM,
    Consent,
    Contact,
    InboundMessage,
    LinkCode,
    Recipient,
    Suppression,
    Thread,
)

STOP = "stop"
START = "start"

#: Första ordet som alltid stoppar, hur långt svaret än är.
STOP_ALWAYS = frozenset({"STOPP", "STOP"})
#: Första ordet som stoppar när svaret har högst STOP_MAX_WORDS ord.
STOP_SHORT = frozenset({"STOPPA", "AVSLUTA", "AVREGISTRERA", "AVANMÄL", "SLUTA", "UNSUBSCRIBE"})
STOP_MAX_WORDS = 8
START_WORD = "START"
START_MAX_WORDS = 3
#: Andra ordet som gör att inget stoppas ("Stoppa inte min bokning").
NEGATION = "INTE"

#: Fraser som gör att ett svar på reklam "ser ut som en avregistrering"
#: (G.1 punkt 8). Inte när nästa ord är INTE ("Sluta inte skicka").
UNSUBSCRIBE_PHRASES = (
    ("SLUTA",),
    ("TA", "BORT", "MIG"),
    ("VILL", "INTE", "HA"),
    ("INGA", "FLER"),
    ("AVBRYT",),
    ("NEJ", "TACK"),
)

#: START gäller kunderna i numrets senaste STOPP inom så här lång tid.
START_LOOKBACK = timedelta(days=396)
#: Högst ett svar på STOPP (och ett per kund på START) per nummer och dygn.
ANSWER_WINDOW = timedelta(hours=24)

#: Vem som avregistrerade, i samtyckesloggen.
STOP_ACTOR = Actor(label="Svar STOPP")


# ---------------------------------------------------------------------------
# Orden
# ---------------------------------------------------------------------------


def normalise(text):
    """Svarets ord: versaler, Å Ä Ö kvar, skiljetecken och emoji borta."""
    text = unicodedata.normalize("NFC", str(text or ""))
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        kept.append(ch if category[0] in ("L", "N") else " ")
    return "".join(kept).upper().split()


def classify(text):
    """ "stop", "start" eller "" (ett vanligt svar)."""
    words = normalise(text)
    if not words:
        return ""
    first = words[0]
    if len(words) > 1 and words[1] == NEGATION:
        return ""
    if first in STOP_ALWAYS:
        return STOP
    if first in STOP_SHORT and len(words) <= STOP_MAX_WORDS:
        return STOP
    if first == START_WORD and len(words) <= START_MAX_WORDS:
        return START
    return ""


def looks_like_unsubscribe(text):
    """Innehåller svaret en av fraserna (utan INTE efter)?"""
    words = normalise(text)
    for phrase in UNSUBSCRIBE_PHRASES:
        size = len(phrase)
        for i in range(len(words) - size + 1):
            if tuple(words[i : i + size]) != phrase:
                continue
            after = words[i + size] if i + size < len(words) else ""
            if after != NEGATION:
                return True
    return False


# ---------------------------------------------------------------------------
# Texterna
# ---------------------------------------------------------------------------


def _join(names):
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else "oss"
    return ", ".join(names[:-1]) + " och " + names[-1]


def stop_text(names):
    """Bekräftelsen på STOPP till en kund (apply_stop köar en per kund, så
    names har ett namn; flera namn skrivs "A och B")."""
    return f"Du får inga fler sms från {_join(names)}. Svara START om du ångrar dig."


def start_text(name, link):
    return f"Klicka för att få sms från {name} igen: {link}"


def display_name(account):
    return (settings_for(account).display_name or account.customer.name or "").strip()


# ---------------------------------------------------------------------------
# STOPP
# ---------------------------------------------------------------------------


def _contact(account, e164):
    return Contact.objects.filter(account=account, phone=e164).first()


def answer_recently_queued(e164, now, *, exclude_pk=None, account=None, keyword=START):
    """Har numret fått (eller väntar på) ett svar på STOPP det senaste dygnet?
    Med account: ett svar från den kunden på keyword (START, eller STOP för
    bekräftelsen på STOPP, som går en per kund)."""
    from ..models import ThreadMessage

    rows = ThreadMessage.objects.filter(
        direction=ThreadMessage.Direction.OUT,
        inbound__isnull=False,
        thread__address=e164,
        thread__channel=CHANNEL_SMS,
        at__gte=now - ANSWER_WINDOW,
    ).exclude(status=ThreadMessage.Status.FAILED)
    if exclude_pk:
        rows = rows.exclude(inbound_id=exclude_pk)
    statuses = {START: InboundMessage.Status.START, STOP: InboundMessage.Status.STOP}
    if account is not None:
        rows = rows.filter(thread__account=account, inbound__status=statuses[keyword])
    else:
        rows = rows.filter(inbound__status=InboundMessage.Status.STOP)
    return rows.exists()


def apply_stop(inbound, candidates, now):
    """STOPP hos varje kandidat (routing.Candidate, senaste först). Skriver
    spärrarna, samtyckena, trådarna och köar en bekräftelse. Anropas i
    hanterarens transaktion."""
    e164 = inbound.from_address
    accounts = []
    stop_threads = {}
    for candidate in candidates:
        account = candidate.account
        utskick = candidate.utskick
        detail = f"Svar STOPP på utskicket {utskick.name}" if utskick else "Svar STOPP"
        contact = threads.suppress_number(
            account,
            e164,
            reason=Suppression.Reason.STOP,
            source=Consent.Source.STOP,
            actor=STOP_ACTOR,
            source_detail=detail,
            utskick=utskick,
            now=now,
        )
        if candidate.recipient is not None:
            Recipient.objects.filter(pk=candidate.recipient.pk, stopped_at__isnull=True).update(
                stopped_at=now
            )
        thread = threads.thread_for_stop(
            account, e164, contact=contact, utskick=utskick, sms_message=candidate.message, now=now
        )
        threads.add_inbound(thread, inbound, raise_lead=False, now=now)
        if contact is not None:
            from .. import contacts

            contacts.touch(contact, STOP, now)
        accounts.append(account)
        stop_threads[account.pk] = thread
    inbound.status = InboundMessage.Status.STOP
    inbound.account = accounts[0] if accounts else None
    inbound.contact = _contact(accounts[0], e164) if accounts else None
    inbound.meta = {**(inbound.meta or {}), "keyword": STOP, "accounts": [a.pk for a in accounts]}
    confirmed = []
    for account in accounts:
        # En bekräftelse per kund, med bara den kundens namn och på dess
        # underlag: tråden syns i kundens Inkorg och sms-portal.
        if answer_recently_queued(e164, now, exclude_pk=inbound.pk, account=account, keyword=STOP):
            continue
        threads.queue_answer(
            stop_threads[account.pk], inbound, stop_text([display_name(account)]), now=now
        )
        confirmed.append(account.pk)
    if confirmed:
        inbound.meta["confirm"] = confirmed
    return accounts


# ---------------------------------------------------------------------------
# START
# ---------------------------------------------------------------------------


def latest_stop(e164, now, *, exclude_pk=None):
    """Numrets senaste STOPP inom START_LOOKBACK, eller None."""
    rows = InboundMessage.objects.filter(
        channel=CHANNEL_SMS,
        from_address=e164,
        status=InboundMessage.Status.STOP,
        received_at__gte=now - START_LOOKBACK,
    )
    if exclude_pk:
        rows = rows.exclude(pk=exclude_pk)
    return rows.order_by("-received_at", "-pk").first()


def apply_start(inbound, stop, now):
    """START efter en STOPP: en bekräftelselänk (/b/, 24 timmar, syfte start)
    per kund som STOPP gällde och som fortfarande spärrar numret. Lyfter
    ingenting själv. Returnerar kunderna som fick ett svar i kö."""
    from apps.flamingo.models import FlamingoAccount

    e164 = inbound.from_address
    value_hash = keys.value_hash(CHANNEL_SMS, e164)
    pks = [pk for pk in (stop.meta or {}).get("accounts", []) if isinstance(pk, int)]
    accounts = list(
        FlamingoAccount.objects.filter(pk__in=pks).select_related("customer").order_by("pk")
    )
    order = {pk: i for i, pk in enumerate(pks)}
    accounts.sort(key=lambda a: order.get(a.pk, 0))
    answered = []
    touched = []
    for account in accounts:
        if not suppression.is_suppressed(account, CHANNEL_SMS, value_hash=value_hash):
            continue
        contact = _contact(account, e164)
        thread = threads.thread_for_start(account, e164, contact=contact, now=now)
        threads.add_inbound(thread, inbound, raise_lead=False, now=now)
        touched.append(account.pk)
        if contact is not None:
            from .. import contacts

            contacts.touch(contact, START, now)
        if answer_recently_queued(e164, now, exclude_pk=inbound.pk, account=account):
            continue
        code = codes.create_confirm(
            account,
            value_hash=value_hash,
            purpose=LinkCode.Purpose.START,
            contact=contact,
            now=now,
        )
        body = start_text(display_name(account), links.sms_link(code.code, "b"))
        threads.queue_answer(thread, inbound, body, now=now)
        answered.append(account.pk)
    inbound.status = InboundMessage.Status.START
    inbound.account = accounts[0] if accounts else None
    inbound.contact = _contact(accounts[0], e164) if accounts else None
    inbound.meta = {
        **(inbound.meta or {}),
        "keyword": START,
        "stop": stop.pk,
        "accounts": touched,
        "answered": answered,
    }
    return answered


def is_suppressed_anywhere(e164, account_pks):
    """Spärrar någon av kunderna numret? (START utan spärr är ett vanligt svar.)"""
    value_hash = keys.value_hash(CHANNEL_SMS, e164)
    return Suppression.objects.filter(
        Q(account_id__in=account_pks), channel=CHANNEL_SMS, value_hash=value_hash
    ).exists()


def reklam_thread(thread):
    """Svarar tråden på ett reklamutskick (då flaggas avregistreringsfraser)?"""
    return isinstance(thread, Thread) and bool(
        thread.utskick_id and thread.utskick.purpose == REKLAM
    )
