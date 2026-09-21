"""Övervakningen i /manage/: kundkortets panel och driftöversikten."""

import re

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.projects.access import staff_required
from apps.projects.models import Customer

from .models import (
    Incident,
    Kind,
    MonitoredDomain,
    MonitorSettings,
    settings_for,
    status_key_configured,
)
from .runner import active_domains, run_daily, run_quick


def _back(customer_id):
    return redirect(reverse("manage:customer_detail", args=[customer_id]) + "#overvakning")


def _clean_domain(raw):
    name = re.sub(r"^https?://", "", (raw or "").strip().lower()).split("/")[0].strip()
    name = re.sub(r"^www\.", "", name)
    return name if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", name) else ""


@staff_required
@require_POST
def monitor_update(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    monitor = settings_for(customer)
    for field in MonitorSettings.toggle_fields():
        setattr(monitor, field, field in request.POST)
    monitor.note = request.POST.get("note", "").strip()[:1000]
    monitor.sentry_project = request.POST.get("sentry_project", "").strip()[:80]
    monitor.save()
    messages.success(request, "Övervakningen är sparad.")
    return _back(pk)


@staff_required
@require_POST
def domain_add(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    name = _clean_domain(request.POST.get("name"))
    if not name:
        messages.error(request, "Skriv en domän, t.ex. nordanbygg.se.")
        return _back(pk)
    if MonitoredDomain.objects.filter(name=name).exists():
        messages.error(request, f"{name} övervakas redan.")
        return _back(pk)
    status_url = request.POST.get("status_url", "").strip()[:200]
    if request.POST.get("platform") and not status_url:
        status_url = f"https://{name}/status/adx/"
    domain = MonitoredDomain.objects.create(
        customer=customer,
        name=name,
        status_url=status_url,
        is_primary=not customer.domains.exists() or bool(request.POST.get("is_primary")),
    )
    if domain.is_primary:
        customer.domains.exclude(pk=domain.pk).update(is_primary=False)
    settings_for(customer)
    messages.success(request, f"{name} övervakas nu. Kör en kontroll för att få första mätningen.")
    return _back(pk)


@staff_required
@require_POST
def domain_update(request, pk):
    domain = get_object_or_404(MonitoredDomain.objects.select_related("customer"), pk=pk)
    action = request.POST.get("action", "")
    if action == "delete":
        domain.delete()
        messages.success(request, f"{domain.name} övervakas inte längre (historiken är borttagen).")
    elif action == "primary":
        domain.customer.domains.update(is_primary=False)
        domain.is_primary = True
        domain.save(update_fields=["is_primary"])
    elif action == "toggle":
        domain.is_active = not domain.is_active
        domain.save(update_fields=["is_active"])
    elif action == "status_url":
        domain.status_url = request.POST.get("status_url", "").strip()[:200]
        domain.save(update_fields=["status_url"])
        messages.success(request, "Statusendpointet är sparat.")
    return _back(domain.customer_id)


@staff_required
@require_POST
def monitor_run(request, pk):
    """Kör kontrollerna nu, synkront. Dygnskontrollen utan PageSpeed om inte 'full' begärs."""
    customer = get_object_or_404(Customer, pk=pk)
    mode = request.POST.get("mode", "quick")
    domains = list(customer.domains.filter(is_active=True))
    if not domains:
        messages.error(request, "Ingen aktiv domän att kontrollera.")
        return _back(pk)
    notes = []
    for domain in domains:
        try:
            if mode == "quick":
                run_quick(domain)
            else:
                notes += [
                    f"{domain.name}: {t}" for t in run_daily(domain, skip_slow=(mode != "full"))
                ]
        except Exception as exc:  # noqa: BLE001 - visa felet, krascha inte kundkortet
            notes.append(f"{domain.name}: kontrollen misslyckades ({type(exc).__name__}: {exc})")
    messages.success(
        request,
        f"{len(domains)} domän(er) kontrollerade."
        + (" Att titta på: " + "; ".join(notes) if notes else ""),
    )
    return _back(pk)


def _aws_rows():
    """Kundernas AWS-konton (apps/cloud): de med varningar eller fel först."""
    from apps.cloud.models import AwsAccount

    rows = []
    for account in AwsAccount.objects.filter(is_active=True).select_related("customer"):
        months = (account.snapshot.get("cost") or {}).get("months") or []
        full = [m for m in months if not m.get("partial")]
        rows.append(
            {
                "account": account,
                "last_month": full[-1] if full else None,
                "forecast": (account.snapshot.get("cost") or {}).get("forecast"),
                "problems": len(account.warnings) + bool(account.last_error),
            }
        )
    rows.sort(key=lambda r: (-r["problems"], r["account"].customer.name))
    return rows


@staff_required
def drift(request):
    """Alla kunders domäner på en skärm - problem först."""
    rows = []
    for domain in active_domains().prefetch_related("customer__monitor"):
        uptime = domain.latest(Kind.UPTIME)
        ssl = domain.latest(Kind.SSL)
        reg = domain.latest(Kind.DOMAIN)
        snapshot = domain.latest(Kind.SNAPSHOT)
        incident = domain.open_incident()
        problems = []
        if uptime and not uptime.ok:
            problems.append("nere")
        if ssl and ssl.data.get("days_left") is not None and ssl.data["days_left"] <= 14:
            problems.append(f"cert {ssl.data['days_left']} d")
        if reg and reg.data.get("days_left") is not None and reg.data["days_left"] <= 30:
            problems.append(f"domän {reg.data['days_left']} d")
        if snapshot and snapshot.ok:
            disk = snapshot.data.get("server", {}).get("disk", {}).get("used_pct")
            if isinstance(disk, (int, float)) and disk >= 85:
                problems.append(f"disk {disk:.0f} %")
        rows.append(
            {
                "domain": domain,
                "uptime": uptime,
                "ssl": ssl,
                "reg": reg,
                "snapshot": snapshot,
                "incident": incident,
                "problems": problems,
            }
        )
    rows.sort(key=lambda r: (not r["problems"], r["domain"].customer.name, r["domain"].name))
    return render(
        request,
        "projects/drift.html",
        {
            "active": "drift",
            "rows": rows,
            "open_incidents": Incident.objects.filter(ended_at__isnull=True).count(),
            "key_configured": status_key_configured(),
            "now": timezone.now(),
            "title": "Drift",
            "aws_accounts": _aws_rows(),
        },
    )
