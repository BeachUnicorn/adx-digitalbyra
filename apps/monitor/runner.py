"""
Körningen: vilka kontroller för vilka domäner, lagring, avbrott och larm.

Snabbkontrollen (cron var 5:e minut) pingar och hämtar statusendpointet.
Dygnskontrollen (cron en gång per dygn) gör det långsamma. Bara sådant
kunden får se körs - en avstängd panel kostar ingenting.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from . import checks
from .emails import alert_daily, alert_down, alert_up
from .models import DAILY_KINDS, QUICK_KINDS, Check, Incident, Kind, MonitoredDomain, settings_for

logger = logging.getLogger(__name__)

#: Snabbkontrollens historik hålls 90 dagar; dygnskontrollernas 400.
RETENTION = {kind: timedelta(days=90) for kind in QUICK_KINDS}
RETENTION.update({kind: timedelta(days=400) for kind in DAILY_KINDS})


def active_domains():
    return MonitoredDomain.objects.filter(is_active=True, customer__is_active=True).select_related(
        "customer"
    )


def _record(domain, kind, result):
    return Check.objects.create(
        domain=domain,
        kind=kind,
        ok=bool(result.get("ok")),
        ms=result.get("ms") if isinstance(result.get("ms"), int) else None,
        data=result,
    )


def _handle_incident(domain, check):
    """
    Avbrott öppnas vid ANDRA misslyckandet i rad (ett enstaka fel är oftast
    nätet mellan oss), larmar en gång, och stängs vid nästa lyckade kontroll.
    """
    incident = domain.open_incident()
    if check.ok:
        if incident:
            incident.ended_at = check.checked_at
            incident.save(update_fields=["ended_at"])
            if incident.alerted_at:
                alert_up(domain, incident.duration_minutes)
        return
    previous = (
        Check.objects.filter(domain=domain, kind=Kind.UPTIME, checked_at__lt=check.checked_at)
        .order_by("-checked_at")
        .first()
    )
    if incident is None and previous is not None and not previous.ok:
        incident = Incident.objects.create(
            domain=domain, started_at=previous.checked_at, error=check.data.get("error", "")[:300]
        )
    if incident and not incident.alerted_at:
        if alert_down(domain, incident.error or check.data.get("error", "")):
            incident.alerted_at = timezone.now()
            incident.save(update_fields=["alerted_at"])


def run_quick(domain):
    enabled = settings_for(domain.customer).enabled_kinds()
    if Kind.UPTIME in enabled:
        check = _record(domain, Kind.UPTIME, checks.check_uptime(domain.name))
        _handle_incident(domain, check)
    if Kind.SNAPSHOT in enabled and domain.status_url:
        _record(domain, Kind.SNAPSHOT, checks.fetch_status_endpoint(domain.status_url))


def run_daily(domain, *, skip_slow=False):
    """Returnerar rader som byrån bör titta på (för dygnslarmet)."""
    monitor = settings_for(domain.customer)
    enabled = monitor.enabled_kinds()
    attention = []
    if Kind.SSL in enabled:
        result = checks.check_ssl(domain.name)
        _record(domain, Kind.SSL, result)
        if result.get("days_left") is not None and result["days_left"] <= 14:
            attention.append(f"certifikatet går ut om {result['days_left']} dagar")
        elif not result.get("ok"):
            attention.append(f"certifikatet kunde inte kontrolleras: {result.get('error', '')}")
    if Kind.DOMAIN in enabled:
        result = checks.check_domain(domain.name)
        _record(domain, Kind.DOMAIN, result)
        if result.get("days_left") is not None and result["days_left"] <= 30:
            attention.append(
                f"domänen går ut om {result['days_left']} dagar ({result.get('registrar', '')})"
            )
    if Kind.EMAIL in enabled:
        _record(domain, Kind.EMAIL, checks.check_email(domain.name))
    if Kind.SECURITY in enabled:
        _record(domain, Kind.SECURITY, checks.check_security(domain.name))
    if Kind.PERFORMANCE in enabled and not skip_slow:
        _record(domain, Kind.PERFORMANCE, checks.check_performance(domain.name))
    if Kind.ERRORS in enabled and monitor.sentry_project:
        result = checks.fetch_sentry_errors(monitor.sentry_project)
        _record(domain, Kind.ERRORS, result)
        if result.get("ok") and result.get("total_7d", 0) > 0:
            attention.append(f"{result['total_7d']} fel i Sentry senaste 7 dagarna")
    snapshot = domain.latest(Kind.SNAPSHOT)
    if snapshot and snapshot.ok:
        disk = snapshot.data.get("server", {}).get("disk", {}).get("used_pct")
        if isinstance(disk, (int, float)) and disk >= 85:
            attention.append(f"disken är {disk:.0f} % full")
        backup = snapshot.data.get("backup", {})
        if backup.get("age_hours") is not None and backup["age_hours"] > 36:
            attention.append(f"senaste backup är {backup['age_hours']:.0f} timmar gammal")
    return attention


def prune():
    for kind, keep in RETENTION.items():
        Check.objects.filter(kind=kind, checked_at__lt=timezone.now() - keep).delete()


def run_all(*, daily=False, skip_slow=False, domains=None):
    """Körs av cron. Returnerar (antal domäner, larmrader)."""
    domains = list(domains if domains is not None else active_domains())
    attention = []
    for domain in domains:
        try:
            if daily:
                attention += [(domain, text) for text in run_daily(domain, skip_slow=skip_slow)]
            else:
                run_quick(domain)
        except Exception:  # noqa: BLE001 - en trasig domän får inte stoppa de andra
            logger.exception("Övervakningen misslyckades för %s", domain.name)
    if daily:
        prune()
        if attention:
            alert_daily(attention)
    return len(domains), attention
