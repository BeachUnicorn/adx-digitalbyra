"""
Hur länge utskickens data sparas (README E.7), och dygnets städning
(management/commands/utskick_daily.py, 02.45).

    daily(now) -> dict              allt nedan, i ordning, med antal
    purge_events(now)               Event äldre än 25 månader
    purge_consent_logs(now)         samtyckesloggen för borttagna kontakter, efter 36 månader
    purge_exports(now)              ExportLog äldre än 25 månader
    flag_inactive(now)              kontakter utan grund och två år utan aktivitet
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
