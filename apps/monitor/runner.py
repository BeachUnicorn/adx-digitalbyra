"""
Körningen: vilka kontroller för vilka domäner, lagring, avbrott och larm.

Snabbkontrollen (cron var 5:e minut) pingar och hämtar statusendpointet.
Dygnskontrollen (cron en gång per dygn) gör det långsamma, och hämtar
Googles data (google_checks.py). Bara sådant kunden får se körs - en
avstängd panel kostar ingenting.

Googles larm (riktiga besökare, Search Console, Business Profile) har en
nyckel var. Samma nyckel larmar en gång och påminner sedan högst en gång i
veckan så länge problemet finns kvar (_once); nycklarna sparas i
Check.data["alerts"].
"""

import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.utils import timezone

from . import checks, google_checks
from .emails import alert_daily, alert_down, alert_up
from .google_api import GoogleApiError, crux_api_key
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


#: Ett Google-larm som finns kvar påminns om så här ofta.
REMIND_AFTER = timedelta(days=7)


class GoogleRun:
    """Det som är gemensamt för alla domäner i en körning och bara hämtas en
    gång: Search Console-egendomarna och Business Profile-platserna. Ett fel
    sparas och gäller då alla domäner i körningen."""

    def __init__(self):
        self._cache = {}

    def _get(self, name, fetch):
        if name not in self._cache:
            try:
                self._cache[name] = (fetch(), None)
            except GoogleApiError as error:
                self._cache[name] = (None, error)
        value, error = self._cache[name]
        if error:
            raise error
        return value

    def sites(self):
        return self._get("sites", google_checks.fetch_sites)

    def locations(self):
        return self._get("locations", google_checks.fetch_locations)


def site_origin(domain):
    """Sajtens origin som besökarna ser den: dit startsidan till slut ledde
    (www eller inte), annars https://domänen."""
    uptime = domain.latest(Kind.UPTIME)
    final = (uptime.data or {}).get("final_url", "") if uptime else ""
    parts = urlsplit(final) if final else None
    if parts and parts.scheme in ("http", "https") and parts.hostname:
        return f"{parts.scheme}://{parts.hostname}"
    return f"https://{domain.name}"


def _once(domain, kind, result, alerts):
    """Larmtexterna som ska skickas nu: nya nycklar, och gamla som inte
    påmints om på REMIND_AFTER. Sparar nycklarna i result["alerts"]."""
    previous = domain.latest(kind)
    sent = dict((previous.data or {}).get("alerts") or {}) if previous else {}
    if result.get("setup"):
        # Ingen data i dag: glöm inte vad som redan larmats.
        result["alerts"] = sent
        return []
    now = timezone.now()
    keep, out = {}, []
    for key, text in alerts:
        last = sent.get(key)
        try:
            recent = last and now - datetime.fromisoformat(last) < REMIND_AFTER
        except (TypeError, ValueError):
            recent = False
        if recent:
            keep[key] = last
            continue
        keep[key] = now.isoformat()
        out.append(text)
    result["alerts"] = keep
    result["alert_texts"] = [text for _key, text in alerts]
    return out


def inspections_today(domain):
    """URL-inspektioner gjorda i dag för domänen (kvoten är per dygn)."""
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return sum(
        int((data or {}).get("inspected") or 0)
        for data in Check.objects.filter(
            domain=domain, kind=Kind.SEARCH, checked_at__gte=start
        ).values_list("data", flat=True)
    )


def run_google(domain, enabled, google=None):
    """Googles data för domänen: riktiga besökare, Search Console och
    Business Profile, det som är påslaget. Returnerar larmrader."""
    google = google or GoogleRun()
    attention = []
    if Kind.CRUX in enabled and crux_api_key():
        result = google_checks.fetch_crux(site_origin(domain))
        attention += _once(domain, Kind.CRUX, result, google_checks.crux_alerts(result))
        _record(domain, Kind.CRUX, result)
    if Kind.SEARCH in enabled:
        try:
            budget = max(
                0,
                min(
                    google_checks.INSPECT_PER_RUN,
                    google_checks.MAX_INSPECTIONS_PER_DAY - inspections_today(domain),
                ),
            )
            result = google_checks.fetch_search(
                domain.name,
                site_origin(domain),
                sites=google.sites(),
                chosen=domain.search_property,
                inspect_budget=budget,
            )
        except GoogleApiError as error:
            result = google_checks._setup_failure(error)
        attention += _once(domain, Kind.SEARCH, result, google_checks.search_alerts(result))
        _record(domain, Kind.SEARCH, result)
    if Kind.GBP in enabled:
        try:
            locations = None if domain.gbp_location else google.locations()
            result = google_checks.fetch_gbp(
                domain.name,
                location=domain.gbp_location,
                account=domain.gbp_account,
                locations=locations,
            )
        except GoogleApiError as error:
            result = google_checks._setup_failure(error)
        attention += _once(domain, Kind.GBP, result, google_checks.gbp_alerts(result, domain.name))
        _record(domain, Kind.GBP, result)
    return attention


def run_daily(domain, *, skip_slow=False, google=None):
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
    attention += run_google(domain, enabled, google)
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
    google = GoogleRun()
    for domain in domains:
        try:
            if daily:
                attention += [
                    (domain, text) for text in run_daily(domain, skip_slow=skip_slow, google=google)
                ]
            else:
                run_quick(domain)
        except Exception:  # noqa: BLE001 - en trasig domän får inte stoppa de andra
            logger.exception("Övervakningen misslyckades för %s", domain.name)
    if daily:
        prune()
        if attention:
            alert_daily(attention)
    return len(domains), attention
