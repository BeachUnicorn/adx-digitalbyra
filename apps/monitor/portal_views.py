"""
Övervakningen i kundportalen: statussidan och månadsrapporterna.

Allt här är läsning. Vad som visas styrs av MonitorSettings; en avstängd
panel finns inte i HTML:en. Kunden ser bara sina egna domäner.
"""

import calendar
from datetime import date, datetime, time, timedelta

from django.db.models import Avg
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from apps.projects.access import customer_required
from apps.projects.models import CustomerLogEntry

from .models import Check, Kind, settings_for
from .status_areas import areas_for, daily_uptime


def _series(domain, since, until=None):
    qs = Check.objects.filter(domain=domain, kind=Kind.UPTIME, checked_at__gte=since)
    if until:
        qs = qs.filter(checked_at__lt=until)
    return list(qs.order_by("checked_at").values_list("checked_at", "ok", "ms"))


def _uptime_pct(rows):
    if not rows:
        return None
    return round(100 * sum(1 for _t, ok, _ms in rows if ok) / len(rows), 2)


def _avg_ms(rows):
    values = [ms for _t, ok, ms in rows if ok and ms is not None]
    return int(sum(values) / len(values)) if values else None


def _bundle(domain, monitor):
    """Allt statussidan visar för EN domän, läst en gång."""
    now = timezone.now()
    day, month = (
        _series(domain, now - timedelta(hours=24)),
        _series(domain, now - timedelta(days=30)),
    )
    latest = {kind: domain.latest(kind) for kind in Kind.values}
    incidents = list(domain.incidents.filter(started_at__gte=now - timedelta(days=30))[:10])
    snapshot = latest[Kind.SNAPSHOT]
    events = []
    if monitor.show_events:
        for inc in incidents:
            events.append(
                (
                    inc.started_at,
                    "avbrott",
                    f"Sajten svarade inte i {inc.duration_minutes} min"
                    + ("" if inc.ended_at else " (pågår)"),
                )
            )
        for entry in domain.customer.log_entries.filter(
            date__gte=(now - timedelta(days=30)).date()
        )[:20]:
            events.append(
                (timezone.make_aware(datetime.combine(entry.date, time(12))), "logg", entry.text)
            )
        if snapshot and snapshot.ok and snapshot.data.get("deploy", {}).get("at"):
            try:
                at = datetime.fromisoformat(snapshot.data["deploy"]["at"].replace("Z", "+00:00"))
                events.append(
                    (
                        at,
                        "deploy",
                        f"Sajten uppdaterades av ADX ({snapshot.data['deploy'].get('rev', '')})",
                    )
                )
            except ValueError:
                pass
        events.sort(key=lambda e: e[0], reverse=True)
    return {
        "domain": domain,
        "now": latest[Kind.UPTIME],
        "uptime_30d": _uptime_pct(month),
        "uptime_24h": _uptime_pct(day),
        "ms_now": latest[Kind.UPTIME].ms if latest[Kind.UPTIME] else None,
        "ms_avg_24h": _avg_ms(day),
        "ms_avg_30d": _avg_ms(month),
        "spark_24h": [ms or 0 for _t, _ok, ms in day][-96:],
        "spark_30d": _daily_avg(month),
        "incidents": incidents,
        "open_incident": domain.open_incident(),
        "ssl": latest[Kind.SSL],
        "reg": latest[Kind.DOMAIN],
        "email": latest[Kind.EMAIL],
        "performance": latest[Kind.PERFORMANCE],
        "security": latest[Kind.SECURITY],
        "snapshot": snapshot if snapshot and snapshot.ok else None,
        "snapshot_failed": snapshot if snapshot and not snapshot.ok else None,
        "errors": latest[Kind.ERRORS],
        "events": events[:30],
        "days_up": _days_since_incident(domain),
        "days": daily_uptime(month),
    }


def _daily_avg(rows):
    buckets = {}
    for t, ok, ms in rows:
        if ok and ms is not None:
            buckets.setdefault(timezone.localtime(t).date(), []).append(ms)
    return [int(sum(v) / len(v)) for _d, v in sorted(buckets.items())]


def _days_since_incident(domain):
    last = domain.incidents.exclude(ended_at__isnull=True).order_by("-ended_at").first()
    first_check = domain.checks.filter(kind=Kind.UPTIME).order_by("checked_at").first()
    if domain.open_incident() or first_check is None:
        return None
    start = last.ended_at if last else first_check.checked_at
    return (timezone.now() - start).days


@customer_required
def status(request):
    monitor = settings_for(request.customer)
    domains = list(request.customer.domains.filter(is_active=True))
    bundles = []
    for index, domain in enumerate(domains):
        bundle = _bundle(domain, monitor)
        bundle["areas"], bundle["pending"] = areas_for(bundle, monitor)
        bundle["attention"] = sum(1 for a in bundle["areas"] if a["status"] != "ok")
        # Missade kontroller som inte blev ett avbrott (ett avbrott kräver två i rad).
        bundle["blips"] = any(d["state"] in ("warn", "bad") for d in bundle["days"])
        # Sidokolumnen ritas bara om den har något att visa - annars 340 punkter tomt.
        bundle["note"] = monitor.note if index == 0 else ""
        bundle["has_side"] = bool(
            bundle["note"]
            or (monitor.show_response and len(bundle["spark_30d"]) > 1)
            or (monitor.show_events and bundle["events"])
        )
        bundles.append(bundle)
    return render(
        request,
        "portal/status.html",
        {
            "customer": request.customer,
            "monitor": monitor,
            "bundles": bundles,
            "title": "Övervakning",
            "active": "status",
        },
    )


def _months_with_data(customer, limit=12):
    months, cursor = [], timezone.localdate().replace(day=1)
    for _ in range(limit):
        start = cursor
        end = (start + timedelta(days=32)).replace(day=1)
        has_checks = Check.objects.filter(
            domain__customer=customer, checked_at__date__gte=start, checked_at__date__lt=end
        ).exists()
        has_log = customer.log_entries.filter(date__gte=start, date__lt=end).exists()
        if has_checks or has_log:
            months.append(start)
        cursor = (start - timedelta(days=1)).replace(day=1)
    return months


@customer_required
def reports(request):
    return render(
        request,
        "portal/reports.html",
        {
            "customer": request.customer,
            "months": _months_with_data(request.customer),
            "title": "Rapporter",
            "active": "reports",
        },
    )


@customer_required
def report(request, year, month):
    try:
        start = date(int(year), int(month), 1)
    except ValueError as exc:
        raise Http404 from exc
    end = (start + timedelta(days=32)).replace(day=1)
    if start > timezone.localdate():
        raise Http404
    monitor = settings_for(request.customer)
    tz_start = timezone.make_aware(datetime.combine(start, time.min))
    tz_end = timezone.make_aware(datetime.combine(end, time.min))
    sections = []
    for domain in request.customer.domains.all():
        rows = _series(domain, tz_start, tz_end)
        incidents = list(domain.incidents.filter(started_at__gte=tz_start, started_at__lt=tz_end))
        in_month = Check.objects.filter(
            domain=domain, checked_at__gte=tz_start, checked_at__lt=tz_end
        )
        latest_in = {
            kind: in_month.filter(kind=kind).order_by("-checked_at").first() for kind in Kind.values
        }
        errors = latest_in[Kind.ERRORS]
        error_total = None
        if errors and errors.ok:
            error_total = sum(
                n
                for d, n in zip(
                    errors.data.get("days", []), errors.data.get("series", []), strict=False
                )
                if start.isoformat() <= d < end.isoformat()
            )
        sections.append(
            {
                "domain": domain,
                "checks": len(rows),
                "uptime": _uptime_pct(rows),
                "avg_ms": _avg_ms(rows),
                "incidents": incidents,
                "downtime_minutes": sum(i.duration_minutes for i in incidents),
                "ssl": latest_in[Kind.SSL],
                "performance": latest_in[Kind.PERFORMANCE],
                "security": latest_in[Kind.SECURITY],
                "snapshot": latest_in[Kind.SNAPSHOT]
                if latest_in[Kind.SNAPSHOT] and latest_in[Kind.SNAPSHOT].ok
                else None,
                "error_total": error_total,
            }
        )
    log_entries = list(
        request.customer.log_entries.filter(date__gte=start, date__lt=end).order_by("date")
    )
    return render(
        request,
        "portal/report.html",
        {
            "customer": request.customer,
            "monitor": monitor,
            "start": start,
            "sections": sections,
            "log_entries": log_entries,
            "days_in_month": calendar.monthrange(start.year, start.month)[1],
            "title": f"Rapport {start:%B %Y}",
            "active": "reports",
        },
    )


__all__ = ["status", "reports", "report", "Avg", "CustomerLogEntry"]
