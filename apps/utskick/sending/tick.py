"""
Ticken (README D.1, D.2): en körning varje minut från cron
(management/commands/utskick_tick.py), med en tidsbudget.

    run(now, budget) -> dict        en tick; {"status": "locked" | "keys" | "idle" | "worked", ...}
    work_exists(now) -> bool        finns något att göra? En billig EXISTS per kö
    heartbeat(summary, now)         Switchboard.last_tick_at och sammanfattningen
    is_stale(switch, now) -> bool   har ticken stannat (fem minuter)?
    check_heartbeat(now) -> bool    monitor_check: larma byrån när ticken stannat med arbete kvar
    summary_line(summary, now)      raden i backups/utskick.log (bara antal)

Gången (D.2), S1:

    pg_try_advisory_lock(TICK_LOCK)      andra spärren bakom flock -n i cron
    keys.check_fingerprints()            fel nyckel: inget skrivs, inget skickas (larmar själv)
    work_exists(now)                     inget att göra: bara hjärtslaget
    fas 1  importer.recover(now)         importer som en förfrågan lämnat halvvägs
    fas 3  optin.send_due(now, 5 s)      bekräftelsemejlen (dubbel opt-in)
    fas 8  importer.import_chunk(15 s)   stora importer, bara med tid kvar
    hjärtslaget                          Switchboard.last_tick_at och sammanfattningen

Faserna 2 (inkommande), 4 till 7 (frysning, sms, e-post, flöden) och 9
(utskick som är klara) hör till S2, S3 och S5. Varje fas slutar när
budgeten är slut; budgetarna är tak, inga reservationer. boto3 läses in
först när ett mejl faktiskt ska skickas (email/transport.py).
"""

import logging
import time
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .. import importer, keys, limits, optin
from ..models import Switchboard

logger = logging.getLogger(__name__)

#: Ticken räknas som stannad när den inte hörts av på så här länge (C.3).
STALE_AFTER = timedelta(minutes=5)
#: Fas 3: bekräftelsemejlen får högst så här många sekunder.
DOI_SECONDS = 5
#: Fas 8: importerna får högst så här många sekunder, och körs bara när
#: minst IMPORT_MIN_SECONDS återstår.
IMPORT_SECONDS = importer.TICK_SECONDS
IMPORT_MIN_SECONDS = 2

STALE_TEXT = "Utskickens tick har inte gått på över fem minuter, och det finns arbete som väntar."


def work_exists(now=None):
    """Finns något för ticken? Bekräftelsemejl i kön (hos konton som får
    skicka, och bara när transporten kan leverera) eller importer i
    bakgrunden. En EXISTS per kö."""
    now = now or timezone.now()
    return optin.work_exists(now) or importer.work_exists(now)


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


def run(now=None, budget=None):
    """En tick. budget är sekunder (UTSKICK_TICK_SECONDS). Returnerar
    sammanfattningen; status säger varför den slutade."""
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
        if not work_exists(now):
            summary = {"status": "idle"}
            heartbeat(summary, now)
            return summary
        summary = {"status": "worked"}
        recovered = _phase(summary, "recover", lambda: importer.recover(now))
        if recovered:
            summary["recovered"] = recovered
        if time.monotonic() < deadline:
            phase_end = min(deadline, time.monotonic() + DOI_SECONDS)
            doi = _phase(summary, "doi", lambda: optin.send_due(now, deadline=phase_end))
            if doi is not None:
                summary["doi"] = doi
        left = deadline - time.monotonic()
        if left > IMPORT_MIN_SECONDS:
            seconds = min(IMPORT_SECONDS, left)
            imported = _phase(
                summary, "import", lambda: importer.import_chunk(now, seconds=seconds)
            )
            if imported is not None:
                summary["import"] = imported
        summary["seconds"] = round(time.monotonic() - started, 1)
        heartbeat(summary, timezone.now())
        return summary


def summary_line(summary, now=None):
    """En rad för backups/utskick.log: tid, status och antal, aldrig adresser."""
    now = timezone.localtime(now or timezone.now())
    parts = [f"{now:%Y-%m-%d %H:%M:%S}", "utskick_tick", str(summary.get("status", ""))]
    for key in ("doi", "import"):
        counts = summary.get(key)
        if counts:
            parts.append(key + " " + " ".join(f"{k}={v}" for k, v in counts.items()))
    if summary.get("recovered"):
        parts.append(f"recovered={summary['recovered']}")
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
