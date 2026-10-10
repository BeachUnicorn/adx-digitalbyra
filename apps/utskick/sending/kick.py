"""
Svaren direkt (README G.1 punkt 3, D13): knuffen efter webbanropet för
inkommande sms.

Svaren på STOPP och START och ägarens sms om svar köas av webbanropet
(inbound/elks.py) och skickades förut först av nästa tick, upp till en
minut senare. Efter commit startar webbanropet nu en kort bakgrundstråd som
kör samma funktion som tickens fas 3 (threads.send_due) direkt; ticken är
kvar som reserven. Ingenting kan gå två gånger: varje svar tas med
threads._claimed (pg_try_advisory_lock per meddelande) och ägarens sms med
select_for_update(skip_locked=True) på UtskickSettings, precis som i ticken.

    enabled() -> bool               settings.UTSKICK_KICK (av i testerna, config/test_runner.py)
    wanted(message) -> (bool, bool) (svar i kö, kan ge ett ägarsms) för ett inkommande sms
    after_inbound(message)          webbanropet: transaction.on_commit(start), bara när
                                    sms:et köade något hos en kund som inte är demot
    start(notice_at=None)           en daemon-tråd som kör run (eller ber den som redan
                                    går i processen att ta ett varv till, och väcker den
                                    om den väntar in ett ägarsms)
    run(notice_at=None, deadline=None) -> dict
                                    knuffen, synkront: threads.send_due nu, och igen när
                                    ägarsmset får gå (NOTICE_SETTLE efter svaret)

Ägarens sms tar bara med svar som är NOTICE_SETTLE (5 s) gamla (en
transaktion som inte är klar ska inte hamna mellan två sms), så knuffen
väntar in den tiden innan den kör en gång till, men bara när ett ägarsms
faktiskt väntar (notices_due) och inom tidsbudgeten KICK_SECONDS. En ny
knuff under väntan väcker tråden (_wake), så att ett STOPP som kommer då
besvaras direkt och inte först när ägarsmset får gå. Kommer en knuff när
budgeten är slut startar tråden en ny tråd med egen budget i stället för att
lämna den åt ticken. Demokontot knuffas aldrig. Tråden stänger sin
databasanslutning när den är klar. Inga personuppgifter i loggarna, bara
antal.

Ett ägarsms kan gå förlorat om processen stoppas mitt i knuffen (en deploy
startar om gunicorn, och daemon-tråden dör med den): reply_notice_at sparas
innan sms:et skickas, så ticken tar det inte efteråt. Ticken har samma lucka
mellan sparandet och sms:et; den har tagits med för att ett ägarsms aldrig
ska gå två gånger. Svaren på STOPP/START går inte förlorade: ett svar som
inte kom i väg är kvar i kön, och referensen x<inkommande> i apps/sms
stoppar ett andra sms.
"""

import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Knuffens tidsbudget i sekunder, väntan på ägarsmset inräknad.
KICK_SECONDS = 15
#: Svaren får högst så här lång tid per varv (som tickens CONFIRM_SECONDS).
ANSWER_SECONDS = 5
#: Marginal efter NOTICE_SETTLE innan ägarsmset prövas.
SETTLE_MARGIN = timedelta(milliseconds=300)

_lock = threading.Lock()
_state = {"running": False, "again": False, "notice_at": None}
#: Satt av start när en knuff redan går: väcker den om den väntar in ett ägarsms.
_wake = threading.Event()


def enabled():
    return bool(getattr(settings, "UTSKICK_KICK", True))


def wanted(message):
    """(svar, ägarsms) för ett sparat inkommande sms: finns ett köat svar på
    STOPP/START hos en kund som inte är demot, och är det ett vanligt svar
    som routats till en sådan kund (det kan ge ett ägarsms)?"""
    from .. import threads
    from ..models import InboundMessage

    if message is None or not message.pk:
        return False, False
    answers = (
        threads.answers_queued()
        .filter(inbound_id=message.pk, thread__account__is_demo=False)
        .exists()
    )
    notice = (
        message.status == InboundMessage.Status.ROUTED
        and message.account_id is not None
        and not getattr(message.account, "is_demo", True)
    )
    return answers, notice


def after_inbound(message):
    """Webbanropet, efter handle: knuffa svaren efter commit. Kastar aldrig
    (sms:et är redan sparat; ticken tar det annars)."""
    if not enabled():
        return
    try:
        answers, notice = wanted(message)
        if not (answers or notice):
            return
        notice_at = None
        if notice:
            from .. import threads

            created = message.created_at or timezone.now()
            notice_at = created + threads.NOTICE_SETTLE + SETTLE_MARGIN
        transaction.on_commit(lambda: start(notice_at=notice_at))
    except Exception:  # noqa: BLE001 - ticken tar det om en minut
        logger.exception("Utskick: knuffen efter inkommande %s gick inte att starta", message.pk)


def start(notice_at=None):
    """Kör run i en daemon-tråd. Går en knuff redan i processen tar den ett
    varv till i stället (en tråd i taget per process) och väcks om den
    väntar in ett ägarsms (_wake). Returnerar tråden, eller None."""
    with _lock:
        if notice_at is not None:
            current = _state["notice_at"]
            _state["notice_at"] = notice_at if current is None else max(current, notice_at)
        if _state["running"]:
            _state["again"] = True
            _wake.set()
            return None
        _state["running"] = True
        _state["again"] = False
    thread = threading.Thread(target=_loop, name="utskick-kick", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        with _lock:
            _state.update(running=False, again=False)
        logger.warning("Utskick: knuffen fick ingen tråd, ticken tar svaren")
        return None
    return thread


def _loop():
    deadline = time.monotonic() + KICK_SECONDS
    released = False
    restart = False
    try:
        while True:
            with _lock:
                _state["again"] = False
                notice_at, _state["notice_at"] = _state["notice_at"], None
                _wake.clear()
            try:
                run(notice_at=notice_at, deadline=deadline)
            except Exception:  # noqa: BLE001 - ticken tar det om en minut
                logger.exception("Utskick: knuffen misslyckades, ticken tar svaren")
            with _lock:
                more = _state["again"] or _state["notice_at"] is not None
                if more and time.monotonic() < deadline:
                    continue
                if more:
                    # Budgeten är slut men en knuff kom under det sista varvet:
                    # den får en ny tråd med egen budget (start nedan), med
                    # again och notice_at kvar.
                    _state["running"] = False
                    restart = True
                else:
                    _state.update(running=False, again=False, notice_at=None)
                released = True
                return
    finally:
        if not released:
            with _lock:
                _state.update(running=False, again=False)
        # Trådens egen anslutning stängs inte av Django.
        connection.close()
        if restart:
            start()


def _sleep(seconds):
    """Väntar seconds, eller tills start väcker knuffen. True när en ny
    knuff kom under väntan."""
    woke = _wake.wait(seconds)
    _wake.clear()
    return woke


def _notice_waits(at):
    """Väntar ett ägarsms som får gå vid at (ett svar som då är NOTICE_SETTLE
    gammalt, hos en kund vars 30 minuter har gått)?"""
    from .. import threads

    return threads.notices_due(at).exists()


def run(notice_at=None, deadline=None):
    """Knuffen, synkront (testerna anropar den direkt): samma fas 3 som
    ticken, med samma nycklar. Först svaren och de ägarsms som redan får gå;
    med notice_at väntar den sedan tills ägarsmset för svaret får gå (om ett
    väntar och tiden räcker) och kör en gång till. Bara antal i svaret."""
    from .. import keys, threads

    deadline = deadline if deadline is not None else time.monotonic() + KICK_SECONDS
    if not keys.check_fingerprints():
        # Fel nyckel i processen (H.7): inget skickas (check_fingerprints larmar).
        return {"status": "keys"}
    summary = {"status": "worked", "answers": 0, "notices": 0, "skipped": 0}

    def once():
        phase_end = min(deadline, time.monotonic() + ANSWER_SECONDS)
        counts = threads.send_due(timezone.now(), phase_end)
        for key in ("answers", "notices", "skipped"):
            summary[key] += counts.get(key, 0)

    once()
    if notice_at is not None and _notice_waits(notice_at):
        while True:
            wait = (notice_at - timezone.now()).total_seconds()
            if wait <= 0 or time.monotonic() + wait >= deadline - 1:
                break
            if not _sleep(wait):
                break
            # En ny knuff under väntan (ett STOPP till exempel): dess svar går
            # nu, inte först när ägarsmset får gå.
            once()
        if timezone.now() >= notice_at and time.monotonic() < deadline:
            once()
    logger.info(
        "Utskick: knuffen skickade %s svar och %s ägarsms",
        summary["answers"],
        summary["notices"],
    )
    return summary
