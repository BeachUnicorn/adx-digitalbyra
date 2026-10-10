"""
Ticken (README D.1, D.2): en körning varje minut från cron
(management/commands/utskick_tick.py), med en tidsbudget.

    run(now, budget, only=None) -> dict
                                    en tick; {"status": "locked" | "keys" | "idle" | "worked", ...}
                                    only: bara det utskicket (manage.py utskick_tick --only)
    work_exists(now) -> bool        finns något att göra? En billig EXISTS per kö
    heartbeat(summary, now)         Switchboard.last_tick_at och sammanfattningen
    is_stale(switch, now) -> bool   har ticken stannat (fem minuter)?
    check_heartbeat(now) -> bool    monitor_check: larma byrån när ticken stannat med arbete kvar
    summary_line(summary, now)      raden i backups/utskick.log (bara antal)
    finish(now, only=None) -> dict  fas 9: pågående utskick utan något kvar i kö blir sent

Gången (D.2):

    pg_try_advisory_lock(TICK_LOCK)      andra spärren bakom flock -n i cron
    keys.check_fingerprints()            fel nyckel: inget skrivs, inget skickas (larmar själv)
    work_exists(now)                     inget att göra: bara hjärtslaget
    fas 1  importer.recover(now)         importer som en förfrågan lämnat halvvägs
           recover.recover_stale(now)    mottagare som en död process lämnat i sending (D.5)
    fas 2  inbound.elks.reconcile        avstämningen mot 46elks var tionde minut (G.1)
    fas 3  optin.send_due(now, 5 s)      bekräftelsemejlen (dubbel opt-in)
           optin.send_due_sms, threads.send_due
                                         bekräftelse-sms, STOPP- och START-svar, ägarens sms
    fas 4  freeze.pause_unsendable, start_due, freeze_due (10 s)
                                         D.8, schemalagda som är dags, frysningen (D.3)
    fas 5  sms.send_due(deadline - 12 s) sms-slingan (D.4)
    fas 8  importer.import_chunk(15 s)   stora importer, bara med tid kvar
    fas 9  finish(now)                   klara utskick: sent, Utskick.stats, links.rollup,
                                         och avregistreringarnas spärr (D.9)
    hjärtslaget                          Switchboard.last_tick_at och sammanfattningen

S3 (sändnings-byggaren, markerade block): fas 2 läser SQS-köerna
(inbound.queues.poll när queues.poll_due) och fas 6 är e-postslingan
(sending.email.send_due, D.6). Fas 7 (flöden) hör till S5. Varje fas slutar när
budgeten är slut; budgetarna är tak, inga reservationer. En fas körs bara
när dess kö har något (en EXISTS), så en tick utan utskick gör som i S1.
boto3 läses in först när ett mejl faktiskt ska skickas (email/transport.py).
"""

import logging
import time
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .. import importer, keys, limits, optin
from ..models import CHANNEL_SMS, Recipient, Switchboard, Utskick

logger = logging.getLogger(__name__)

#: Ticken räknas som stannad när den inte hörts av på så här länge (C.3).
STALE_AFTER = timedelta(minutes=5)
#: Fas 3: bekräftelsemejlen får högst så här många sekunder (och lika
#: mycket var för bekräftelse-sms och svaren).
DOI_SECONDS = 5
CONFIRM_SECONDS = 5
#: Fas 2: avstämningen mot 46elks.
INBOUND_SECONDS = 8
#: Fas 4: frysningen.
FREEZE_SECONDS = 10
#: Fas 5 slutar så här långt före tickens slut (D.2): resten är importer
#: och fas 9.
SMS_RESERVE_SECONDS = 12
#: Fas 8: importerna får högst så här många sekunder, och körs bara när
#: minst IMPORT_MIN_SECONDS återstår.
IMPORT_SECONDS = importer.TICK_SECONDS
IMPORT_MIN_SECONDS = 2
#: Fas 9: så här många klara utskick per tick, och hur länge efter att ett
#: utskick blev klart länkarnas summor räknas om.
FINISH_BATCH = 100
ROLLUP_DAYS = timedelta(days=30)

STALE_TEXT = "Utskickens tick har inte gått på över fem minuter, och det finns arbete som väntar."


# ---------------------------------------------------------------------------
# Finns det något att göra?
# ---------------------------------------------------------------------------


def _open_queue():
    return Recipient.objects.filter(
        utskick=OuterRef("pk"), status__in=(Recipient.Status.QUEUED, Recipient.Status.SENDING)
    )


def _sendable():
    from .checks import sendable_q

    return sendable_q("account__")


def due_to_start(now):
    return Utskick.objects.filter(
        Q(status=Utskick.Status.SCHEDULED, scheduled_at__lte=now)
        | Q(status=Utskick.Status.FREEZING)
    ).filter(_sendable())


def sms_due(now):
    from .checks import sendable_q

    return Recipient.objects.filter(
        status=Recipient.Status.QUEUED,
        channel=CHANNEL_SMS,
        utskick__status=Utskick.Status.SENDING,
    ).filter(
        Q(not_before__isnull=True) | Q(not_before__lte=now),
        sendable_q("utskick__account__"),
    )


def finished(only=None):
    rows = Utskick.objects.filter(status=Utskick.Status.SENDING).exclude(Exists(_open_queue()))
    return rows.filter(pk=only) if only else rows


def clicks_to_roll_up(now):
    """Länkarnas summor (links.rollup) räknas medan klick kan komma: för
    pågående utskick och de som blev klara de senaste 30 dagarna."""
    return Utskick.objects.filter(
        Q(status=Utskick.Status.SENDING)
        | Q(status=Utskick.Status.SENT, finished_at__gte=now - ROLLUP_DAYS)
    ).exists()


def unsendable_active():
    return Utskick.objects.filter(status__in=Utskick.ACTIVE).exclude(_sendable())


def utskick_work_exists(now):
    """Utskickens köer (S2): schemalagda som är dags och frysningar, köade
    sms, klara utskick, övergivna mottagare och utskick hos konton som inte
    längre får skicka."""
    from . import recover

    return (
        due_to_start(now).exists()
        or sms_due(now).exists()
        or finished().exists()
        or recover.stale_exists(now)
        or unsendable_active().exists()
    )


def _inbound_due(now):
    from ..inbound import elks

    return elks.reconcile_due(now)


def _replies_due(now):
    from .. import threads

    return optin.sms_work_exists(now) or threads.work_exists(now)


# --- S3 (sändnings-byggaren): e-postslingan och SQS-köerna -----------------


def _email_due(now):
    """Köade mejl i pågående utskick (fas 6, D.6)."""
    from . import email as email_loop

    return email_loop.work_exists(now)


def _email_competes(now):
    """Väntar riktiga mejl (inte demot) medan e-posten är påslagen? Då får
    sms-slingan högst halva tiden, så att ett stort sms-utskick inte håller
    e-posten stilla (S3)."""
    from . import email as email_loop
    from . import state

    return state.email_live() and email_loop.email_due(now).exists()


def _queues_due(now):
    """Ska köerna läsas nu (fas 2, D.7)? Utan köer i inställningarna aldrig."""
    from ..inbound import queues

    return queues.poll_due(now)


# --- slut S3 -------------------------------------------------------------------


def work_exists(now=None):
    """Finns något för ticken? Bekräftelsemejl i kön (hos konton som får
    skicka, och bara när transporten kan leverera), importer i bakgrunden,
    utskickens köer, bekräftelse-sms och svar, och avstämningen mot 46elks;
    från S3 köade mejl och SQS-köerna. En EXISTS per kö."""
    now = now or timezone.now()
    return (
        optin.work_exists(now)
        or importer.work_exists(now)
        or utskick_work_exists(now)
        or _replies_due(now)
        or _inbound_due(now)
        or _email_due(now)
        or _queues_due(now)
    )


@contextmanager
def tick_lock():
    """Postgres rådgivande lås för ticken (sessionslås, släpps efteråt).
    Ger True när låset togs, False när en annan tick redan kör."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [limits.TICK_LOCK])
        (got,) = cursor.fetchone()
    try:
        yield bool(got)
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [limits.TICK_LOCK])


def heartbeat(summary, now=None):
    """Hjärtslaget på Switchboard: när ticken gick och vad den gjorde
    (bara antal). Rör inga andra fält, så att byråns brytare aldrig skrivs
    över av en tick som läste raden före en ändring."""
    now = now or timezone.now()
    Switchboard.get_solo()
    Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(
        last_tick_at=now, last_tick_summary=summary
    )


def is_stale(switch, now=None):
    now = now or timezone.now()
    return switch.last_tick_at is None or now - switch.last_tick_at > STALE_AFTER


def _phase(summary, name, func):
    """En fas. Ett fel loggas (och når Sentry) och stoppar inte nästa fas;
    sammanfattningen säger vilken fas som föll, utan detaljer."""
    try:
        return func()
    except Exception:
        logger.exception("utskick_tick: fasen %s misslyckades", name)
        summary.setdefault("failed", []).append(name)
        return None


def _keep(summary, key, value):
    if value:
        summary[key] = value


# ---------------------------------------------------------------------------
# Fas 9: klara utskick
# ---------------------------------------------------------------------------


def _basic_stats(utskick):
    """Summorna när reports.final_stats inte finns: mottagarna per läge och
    kostnaden (sms:ens kundpris)."""
    from django.db.models import Count, Sum

    from apps.sms.models import SmsMessage

    by_status = dict(
        Recipient.objects.filter(utskick=utskick)
        .values("status")
        .annotate(n=Count("pk"))
        .values_list("status", "n")
    )
    cost = (
        SmsMessage.objects.filter(utskick_recipients__utskick=utskick).aggregate(
            s=Sum("customer_price")
        )["s"]
        or 0
    )
    return {"recipients": by_status, "cost": int(cost)}


def final_stats(utskick):
    """Utskick.stats när utskicket är klart: reports.final_stats (rapportens
    siffror) plus det som redan står där (pausernas anteckningar)."""
    from .. import reports

    try:
        stats = reports.final_stats(utskick)
    except Exception:  # noqa: BLE001 - utan rapporten sparas grundsiffrorna
        logger.exception(
            "Utskick %s: rapportens slutsiffror gick inte, grundsiffror sparas", utskick.pk
        )
        stats = _basic_stats(utskick)
    merged = dict(utskick.stats or {})
    merged.update(stats or {})
    return merged


def finish(now=None, only=None):
    """Pågående utskick utan köade eller tagna mottagare blir sent, med
    Utskick.stats. {"sent": n}."""
    from . import state

    now = now or timezone.now()
    done = 0
    for utskick in finished(only).select_related("account").order_by("pk")[:FINISH_BATCH]:
        if state.transition(
            utskick,
            Utskick.Status.SENT,
            expected=(Utskick.Status.SENDING,),
            now=now,
            fields={"stats": final_stats(utskick)},
        ):
            done += 1
    return {"sent": done} if done else {}


# ---------------------------------------------------------------------------
# Ticken
# ---------------------------------------------------------------------------


def run(now=None, budget=None, only=None):
    """En tick. budget är sekunder (UTSKICK_TICK_SECONDS). only är ett
    utskicks pk (manuell körning): då gäller faserna 4, 5 och 9 bara det
    utskicket, och faserna 2, 3 och 8 hoppas över. Returnerar
    sammanfattningen; status säger varför den slutade."""
    from . import freeze, recover
    from . import sms as sms_loop

    started = time.monotonic()
    seconds = settings.UTSKICK_TICK_SECONDS if budget is None else budget
    deadline = started + max(1, int(seconds))
    now = now or timezone.now()
    with tick_lock() as got:
        if not got:
            return {"status": "locked"}
        if not keys.check_fingerprints():
            # Fel nyckel i processen (H.7): inga samtycken, spärrar eller mejl.
            # check_fingerprints har loggat och larmat byrån.
            return {"status": "keys"}
        if not (work_exists(now) or (only and Utskick.objects.filter(pk=only).exists())):
            summary = {"status": "idle"}
            heartbeat(summary, now)
            return summary
        summary = {"status": "worked"}

        # Fas 1: det som en förfrågan eller en död process lämnat.
        recovered = _phase(summary, "recover", lambda: importer.recover(now))
        if recovered:
            summary["recovered"] = recovered
        if recover.stale_exists(now):
            _keep(summary, "rescued", _phase(summary, "rescue", lambda: recover.recover_stale(now)))

        # Fas 2: avstämningen mot 46elks (G.1).
        if not only and _inbound_due(now):
            phase_end = min(deadline, time.monotonic() + INBOUND_SECONDS)
            from ..inbound import elks

            _keep(
                summary,
                "inbound",
                _phase(summary, "inbound", lambda: elks.reconcile(now, phase_end)),
            )

        # S3 (sändnings-byggaren): fas 2 läser SES-händelserna och de
        # inkommande mejlen ur SQS (D.7, G.3), inom samma budget.
        if not only and _queues_due(now):
            from ..inbound import queues

            phase_end = min(deadline, time.monotonic() + INBOUND_SECONDS)
            _keep(summary, "queues", _phase(summary, "queues", lambda: queues.poll(now, phase_end)))
        # --- slut S3

        # Fas 3: bekräftelser och svar.
        if not only and time.monotonic() < deadline:
            phase_end = min(deadline, time.monotonic() + DOI_SECONDS)
            doi = _phase(summary, "doi", lambda: optin.send_due(now, deadline=phase_end))
            if doi is not None:
                summary["doi"] = doi
        if not only and _replies_due(now):
            from .. import threads

            phase_end = min(deadline, time.monotonic() + CONFIRM_SECONDS)
            _keep(
                summary,
                "confirm_sms",
                _phase(summary, "confirm_sms", lambda: optin.send_due_sms(now, deadline=phase_end)),
            )
            phase_end = min(deadline, time.monotonic() + CONFIRM_SECONDS)
            _keep(
                summary,
                "answers",
                _phase(summary, "answers", lambda: threads.send_due(now, phase_end)),
            )

        # Fas 4: D.8, schemalagda som är dags och frysningen (D.3).
        if unsendable_active().exists():
            _keep(
                summary,
                "paused",
                _phase(summary, "pause", lambda: freeze.pause_unsendable(now, only)),
            )
        if due_to_start(now).exists():
            started_counts = _phase(summary, "start", lambda: freeze.start_due(now, only))
            _keep(summary, "start", {k: v for k, v in (started_counts or {}).items() if v})
            phase_end = min(deadline, time.monotonic() + FREEZE_SECONDS)
            _keep(
                summary,
                "freeze",
                _phase(summary, "freeze", lambda: freeze.freeze_due(now, phase_end, only)),
            )

        # Fas 5: sms-slingan (D.4).
        if sms_due(now).exists():
            phase_end = deadline - SMS_RESERVE_SECONDS
            # S3 (sändnings-byggaren): väntar mejl också delas tiden lika.
            if _email_competes(now):
                start = time.monotonic()
                phase_end = start + max(0.0, (phase_end - start) / 2)
            # --- slut S3
            _keep(
                summary,
                "sms",
                _phase(summary, "sms", lambda: sms_loop.send_due(now, phase_end, only)),
            )

        # S3 (sändnings-byggaren): fas 6, e-postslingan (D.6).
        if _email_due(now):
            from . import email as email_loop

            phase_end = deadline - SMS_RESERVE_SECONDS
            _keep(
                summary,
                "email",
                _phase(summary, "email", lambda: email_loop.send_due(now, phase_end, only)),
            )
        # --- slut S3

        # Fas 8: importer.
        if not only:
            left = deadline - time.monotonic()
            if left > IMPORT_MIN_SECONDS:
                seconds = min(IMPORT_SECONDS, left)
                imported = _phase(
                    summary, "import", lambda: importer.import_chunk(now, seconds=seconds)
                )
                if imported is not None:
                    summary["import"] = imported

        # Fas 9: avregistreringarna, klara utskick och länkarnas summor.
        if Utskick.objects.filter(status=Utskick.Status.SENDING).exists():
            _keep(
                summary,
                "stops",
                _phase(summary, "stops", lambda: sms_loop.stops_check(now, only)),
            )
            _keep(summary, "finish", _phase(summary, "finish", lambda: finish(now, only)))
        if not only and clicks_to_roll_up(now):
            from .. import links

            _keep(summary, "links", _phase(summary, "links", lambda: links.rollup(now, deadline)))
        summary["seconds"] = round(time.monotonic() - started, 1)
        heartbeat(summary, timezone.now())
        return summary


#: Sammanfattningens nycklar som har antal per del, i radens ordning.
SUMMARY_KEYS = (
    "rescued",
    "inbound",
    "doi",
    "confirm_sms",
    "answers",
    "start",
    "freeze",
    "sms",
    "queues",
    "email",
    "import",
    "stops",
    "finish",
    "links",
)


def summary_line(summary, now=None):
    """En rad för backups/utskick.log: tid, status och antal, aldrig adresser."""
    now = timezone.localtime(now or timezone.now())
    parts = [f"{now:%Y-%m-%d %H:%M:%S}", "utskick_tick", str(summary.get("status", ""))]
    for key in SUMMARY_KEYS:
        counts = summary.get(key)
        if counts and isinstance(counts, dict):
            parts.append(key + " " + " ".join(f"{k}={v}" for k, v in counts.items()))
    if summary.get("recovered"):
        parts.append(f"recovered={summary['recovered']}")
    if summary.get("paused"):
        parts.append(f"paused={summary['paused']}")
    if summary.get("failed"):
        parts.append("failed=" + ",".join(summary["failed"]))
    if "seconds" in summary:
        parts.append(f"{summary['seconds']} s")
    return " ".join(parts)


def check_heartbeat(now=None):
    """monitor_check (var femte minut): när ticken inte gått på fem minuter
    medan något väntar larmas byrån, högst en gång i timmen. True om ett
    larm skickades. Kunden mejlas aldrig."""
    from .. import alerts

    now = now or timezone.now()
    switch = Switchboard.objects.filter(pk=Switchboard.SOLO_PK).first()
    if switch is not None and not is_stale(switch, now):
        return False
    if not work_exists(now):
        return False
    last = (
        timezone.localtime(switch.last_tick_at).strftime("%Y-%m-%d %H:%M")
        if switch is not None and switch.last_tick_at
        else "aldrig"
    )
    logger.error("Utskick: ticken har stannat (senast %s) och arbete väntar", last)
    return alerts.agency(
        "Utskick: ticken går inte",
        [
            STALE_TEXT,
            f"Senaste tick: {last}.",
            "Kontrollera djangousers crontab (server/crontab.d/adx-utskick) och "
            "backups/utskick.log på servern.",
        ],
        once="tick_stale",
        now=now,
    )
