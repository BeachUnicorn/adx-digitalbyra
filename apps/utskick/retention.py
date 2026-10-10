"""
Hur länge utskickens data sparas (README E.7), och dygnets städning
(management/commands/utskick_daily.py, 02.45).

    daily(now) -> dict              allt nedan, i ordning, med antal
    purge_events(now)               Event äldre än 25 månader
    purge_consent_logs(now)         samtyckesloggen för borttagna kontakter, efter 36 månader
    purge_exports(now)              ExportLog äldre än 25 månader
    flag_inactive(now)              kontakter utan grund och två år utan aktivitet
    refresh_stats(now)              S2: Utskick.stats för utskick klara de senaste 30
                                    dagarna (leveransrapporter, klick och svar kommer sent)
    purge_s2(now)                   S2: mottagare, klick, sms-koder och inkommande sms;
                                    stats räknas om först för utskicken som tappar
                                    sina mottagare
    (links.rollup)                  S2: länkarnas summor, också de dygn ticken vilat
    purge_s3(now)                   S3: kvittona för SES-händelserna efter 3 dagar och
                                    mejlens bilder som inget längre använder
    (S3 e-post)                     SES GetAccount och ADX hela hälsa (health.adx_wide),
                                    domänernas DNS (domains.check_due), DLQ:erna
                                    (queues.check_dlq), hinken för svar
                                    (inbound.email.sweep_bucket) och oklara mejl efter
                                    24 timmar (sending.email.stale_unknown)
    disk_check(now)                 larm till byrån under 15 % ledigt på disken
    table_sizes()                   utskickstabellernas storlek i byte (loggas)

Samtyckesloggen raderas bara här (models.ConsentLog.delete vägrar): raderna
för en kontakt som finns kvar sparas så länge kontakten finns, raderna för
en borttagen kontakt (contact är null) i 36 månader som pseudonymt bevis.
Spärrlistan sparas för alltid. Inaktiva kontakter tas aldrig bort av sig
själva: de flaggas, och kunden väljer "Rensa inaktiva kontakter".

Allt i omgångar (BATCH), så att en stor tabell inte låser länge på en liten
server. Inget här skickar något till kunden; disklarmet går till byrån.
"""

import logging
import shutil

from django.conf import settings
from django.db import connection
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from . import consent as consents
from . import importer, limits
from .models import Consent, ConsentLog, Contact, Event, ExportLog

logger = logging.getLogger(__name__)

EVENT_MONTHS = 25
EXPORT_MONTHS = 25
CONSENT_LOG_MONTHS = 36
INACTIVE_MONTHS = 24
#: Byrån larmas när mindre än så här mycket av disken är ledigt (E.7).
DISK_ALERT_FREE = 0.15
BATCH = 5000

#: Grund för att få reklam eller vänta på en bekräftelse: en sådan kontakt
#: är aldrig inaktiv.
BASIS = (consents.YES, consents.EXISTING, consents.COMPANY, consents.PENDING)


def months_ago(now, months):
    """Samma dag months månader tidigare (sista dagen i månaden om dagen
    saknas där), samma klockslag."""
    local = timezone.localtime(now)
    year = local.year + (local.month - 1 - months) // 12
    month = (local.month - 1 - months) % 12 + 1
    day = local.day
    while day > 28:
        try:
            return local.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return local.replace(year=year, month=month, day=day)


def _delete_in_batches(queryset):
    model = queryset.model
    total = 0
    while True:
        pks = list(queryset.values_list("pk", flat=True)[:BATCH])
        if not pks:
            return total
        deleted, _ = model.objects.filter(pk__in=pks).delete()
        total += deleted


def purge_events(now=None):
    now = now or timezone.now()
    return _delete_in_batches(Event.objects.filter(at__lt=months_ago(now, EVENT_MONTHS)))


def purge_consent_logs(now=None):
    """Bevisen för borttagna kontakter, efter 36 månader. Den enda vägen
    som tar bort rader ur samtyckesloggen."""
    now = now or timezone.now()
    old = ConsentLog.objects.filter(
        contact__isnull=True, at__lt=months_ago(now, CONSENT_LOG_MONTHS)
    )
    return _delete_in_batches(old)


def purge_exports(now=None):
    now = now or timezone.now()
    return _delete_in_batches(ExportLog.objects.filter(at__lt=months_ago(now, EXPORT_MONTHS)))


def flag_inactive(now=None):
    """Flagga kontakter utan grund (inget ja, ingen befintlig kund, inget
    företag, inget som väntar på bekräftelse) som inte hörts av på två år,
    och ta bort flaggan från dem som fått en grund. Kontakterna flaggas
    bara; Kontakter erbjuder "Rensa inaktiva kontakter" och kunden väljer."""
    now = now or timezone.now()
    cutoff = months_ago(now, INACTIVE_MONTHS)
    basis = Consent.objects.filter(contact=OuterRef("pk"), status__in=BASIS)
    quiet = Q(created_at__lt=cutoff) & (
        Q(last_activity_at__isnull=True) | Q(last_activity_at__lt=cutoff)
    )
    flagged = (
        Contact.objects.filter(inactive_flagged_at__isnull=True)
        .filter(quiet)
        .exclude(Exists(basis))
        .update(inactive_flagged_at=now)
    )
    cleared = (
        Contact.objects.filter(inactive_flagged_at__isnull=False)
        .filter(Exists(basis))
        .update(inactive_flagged_at=None)
    )
    return {"flagged": flagged, "cleared": cleared}


# ---------------------------------------------------------------------------
# S2 (sändningsmotorn): utskickens rader (E.7)
# ---------------------------------------------------------------------------

#: Mottagare, klick och klickkoder sparas så här länge efter att utskicket
#: blev klart; personkoderna tappar mottagaren då och tas bort efter 36
#: månader; bekräftelsekoderna efter sju dagar; skannrarna efter 14 dagar;
#: inkommande sms efter 90 dagar (texten töms redan när de routas).
RECIPIENT_MONTHS = 13
CLICK_MONTHS = 13
PERSON_CODE_MONTHS = 36
CONFIRM_CODE_DAYS = 7
SCANNER_DAYS = 14
INBOUND_DAYS = 90


#: Utskick.stats räknas om varje dygn så här länge efter att utskicket blev
#: klart (leveransrapporter, klick, svar och förfrågningar kommer efteråt).
STATS_REFRESH_DAYS = 30


def _store_stats(rows):
    """Utskick.stats ur mottagarna nu (sending.tick.final_stats, som behåller
    pausernas anteckningar). Antalet utskick."""
    from .models import Utskick
    from .sending.tick import final_stats

    done = 0
    for utskick in rows.select_related("account").order_by("pk").iterator(chunk_size=100):
        Utskick.objects.filter(pk=utskick.pk).update(stats=final_stats(utskick))
        done += 1
    return done


def refresh_stats(now=None):
    """Summorna för utskick som blev klara de senaste STATS_REFRESH_DAYS,
    så att rapporten och listan har de sena siffrorna också när mottagarna
    tagits bort (E.7). Antalet utskick."""
    from datetime import timedelta

    from .models import Recipient, Utskick

    now = now or timezone.now()
    rows = Utskick.objects.filter(
        status=Utskick.Status.SENT, finished_at__gte=now - timedelta(days=STATS_REFRESH_DAYS)
    ).filter(Exists(Recipient.objects.filter(utskick=OuterRef("pk"))))
    return _store_stats(rows)


def purge_s2(now=None):
    """Utskickens rader enligt E.7, i omgångar. Utskick.stats räknas om ur
    mottagarna precis innan de tas bort (och innan klicken tas bort), så
    att rapporten finns kvar med de sista siffrorna. {"stats",
    "recipients", "clicks", "codes", "inbound"}."""
    from datetime import timedelta

    from .models import Click, InboundMessage, LinkCode, Recipient, Utskick

    now = now or timezone.now()
    old = months_ago(now, RECIPIENT_MONTHS)
    counts = {}
    counts["stats"] = _store_stats(
        Utskick.objects.filter(finished_at__lt=old).filter(
            Exists(Recipient.objects.filter(utskick=OuterRef("pk")))
        )
    )
    counts["clicks"] = _delete_in_batches(
        Click.objects.filter(kind=Click.Kind.HUMAN, at__lt=months_ago(now, CLICK_MONTHS))
    ) + _delete_in_batches(
        Click.objects.filter(kind=Click.Kind.SCANNER, at__lt=now - timedelta(days=SCANNER_DAYS))
    )
    codes = _delete_in_batches(
        LinkCode.objects.filter(kind=LinkCode.Kind.LINK, link__utskick__finished_at__lt=old)
    )
    codes += _delete_in_batches(
        LinkCode.objects.filter(
            kind=LinkCode.Kind.CONFIRM, created_at__lt=now - timedelta(days=CONFIRM_CODE_DAYS)
        )
    )
    codes += _delete_in_batches(
        LinkCode.objects.filter(
            kind=LinkCode.Kind.PERSON,
            created_at__lt=months_ago(now, PERSON_CODE_MONTHS),
        )
    )
    LinkCode.objects.filter(
        kind=LinkCode.Kind.PERSON, recipient__isnull=False, created_at__lt=old
    ).update(recipient=None)
    counts["codes"] = codes
    counts["recipients"] = _delete_in_batches(
        Recipient.objects.filter(utskick__finished_at__lt=old)
    )
    counts["inbound"] = _delete_in_batches(
        InboundMessage.objects.filter(received_at__lt=now - timedelta(days=INBOUND_DAYS))
    )
    return {k: v for k, v in counts.items() if v}


def _rollup(now):
    """Länkarnas summor (links.rollup) en gång om dygnet också: ticken räknar
    bara upp när den har annat att göra, och fönstret på två dygn gör att
    dygnskörningen fångar varje klick."""
    from . import links

    return links.rollup(now)


def _month_end(now):
    from .sending import recover

    return recover.month_end_check(now)


# --- S3 (sändnings-byggaren) ------------------------------------------------


def purge_s3(now=None):
    """Kvittona för SES-händelserna och de inkommande mejlen (EventReceipt)
    efter KEEP_DAYS dagar, och mejlens bilder som inget utkast eller ingen
    mottagare längre använder (email.images.purge_unused, E.7)."""
    from datetime import timedelta

    from .email import images
    from .models import EventReceipt

    now = now or timezone.now()
    cutoff = now - timedelta(days=EventReceipt.KEEP_DAYS)
    summary = {"receipts": _delete_in_batches(EventReceipt.objects.filter(at__lt=cutoff))}
    summary["images"] = images.purge_unused(now)
    return summary


def s3_email_steps(now):
    """Dygnets e-postdelar (S3): (namn, funktion) i ordning."""
    import time

    from .email import domains
    from .inbound import email as inbound_email
    from .inbound import queues
    from .sending import email as email_loop
    from .sending import health

    return (
        ("s3", lambda: purge_s3(now)),
        ("ses", lambda: health.adx_wide(now)),
        ("domains", lambda: domains.check_due(now, time.monotonic() + 300)),
        ("dlq", lambda: queues.check_dlq(now)),
        ("inbound_sweep", lambda: inbound_email.sweep_bucket(now)),
        ("unknown_mail", lambda: email_loop.stale_unknown(now)),
    )


# --- slut S3 ---------------------------------------------------------------------


def disk_check(now=None, path=None):
    """Ledigt utrymme på disken där sajten ligger. Under 15 % larmas byrån
    högst en gång per dygn. Returnerar andelen ledigt (0 till 1)."""
    from . import alerts

    usage = shutil.disk_usage(path or settings.BASE_DIR)
    free = usage.free / usage.total if usage.total else 1.0
    if free < DISK_ALERT_FREE:
        alerts.agency(
            "Utskick: disken är nästan full",
            [
                f"{round(free * 100)} % av disken är ledigt (larmet går under "
                f"{round(DISK_ALERT_FREE * 100)} %).",
                "Utskicken sparar mottagare, klick och händelser; se tabellstorlekarna i "
                "backups/utskick-daily.log.",
            ],
            once="disk_low",
            window="day",
            now=now,
        )
    return free


def table_sizes():
    """Utskickstabellernas storlek med index, i byte, största först."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = current_schema() "
            "AND c.relname LIKE %s ORDER BY 2 DESC",
            ["utskick\\_%"],
        )
        return dict(cursor.fetchall())


# --- S4 (segment-byggaren): segmentens räkning för Listor (D.1) ---


def _recount_segments(now):
    """Räkna om segmenten (Listor visar Segment.cached_*); utskicken räknar
    alltid om själva. Högst två minuter, de äldsta räkningarna först."""
    from . import segments

    return segments.refresh_all(now, seconds=120)


# --- slut S4


def daily(now=None):
    """Dygnets städning (utskick_daily). Returnerar antal per del; ett fel
    i en del stoppar inte de andra (loggas)."""
    now = now or timezone.now()
    summary = {}
    steps = (
        ("counters", lambda: limits.purge(now - limits.KEEP)),
        ("imports", lambda: importer.cleanup(now)),
        ("events", lambda: purge_events(now)),
        ("consent_logs", lambda: purge_consent_logs(now)),
        ("exports", lambda: purge_exports(now)),
        ("inactive", lambda: flag_inactive(now)),
        ("stats", lambda: refresh_stats(now)),
        ("s2", lambda: purge_s2(now)),
        ("links", lambda: _rollup(now)),
        ("segments", lambda: _recount_segments(now)),  # S4 (segment-byggaren)
        ("reserved", lambda: _month_end(now)),
        *s3_email_steps(now),
        ("disk_free", lambda: round(disk_check(now), 3)),
    )
    for name, step in steps:
        try:
            summary[name] = step()
        except Exception:  # noqa: BLE001 - en trasig del får inte stoppa resten
            logger.exception("utskick_daily: %s misslyckades", name)
            summary[name] = "fel"
    try:
        summary["tables_mb"] = {
            name.removeprefix("utskick_"): round(size / (1024 * 1024), 1)
            for name, size in table_sizes().items()
        }
    except Exception:  # noqa: BLE001
        logger.exception("utskick_daily: tabellstorlekarna kunde inte läsas")
    return summary
