"""Övervakningen i /manage/: kundkortets panel, driftöversikten och
Google-sidan per domän (domain_google)."""

import re

from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.common import providers
from apps.projects.access import staff_required
from apps.projects.models import Customer

from . import google_present
from .google_api import GoogleApiError
from .google_checks import fetch_locations, fetch_sites
from .models import (
    GOOGLE_KINDS,
    Incident,
    Kind,
    MonitoredDomain,
    MonitorSettings,
    settings_for,
    status_key_configured,
)
from .runner import GoogleRun, active_domains, run_daily, run_google, run_quick


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
    # Anteckningen står överst på kundens statussida.
    warning = providers.warning(monitor.note)
    if warning:
        messages.warning(request, f"Anteckningen: {warning}")
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
        google = sum(
            len((check.data or {}).get("alert_texts") or [])
            for check in (domain.latest(kind) for kind in GOOGLE_KINDS)
            if check
        )
        if google:
            problems.append(f"Google {google}")
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


# ---------------------------------------------------------------------------
# Google per domän: allt PageSpeed, riktiga besökare, Search Console och
# Business Profile har sagt, och valet av egendom och plats.
# ---------------------------------------------------------------------------

#: Listorna att välja egendom och plats ur hämtas på knapptryck och sparas en timme.
CHOICES_CACHE = "monitor:google:choices"
CHOICES_SECONDS = 3600


def _google_back(domain):
    return redirect(reverse("manage:monitor_domain_google", args=[domain.pk]))


def _fetch_choices():
    """Egendomarna och platserna ADX:s Google-konto når, med eventuella fel."""
    choices = {"sites": [], "locations": [], "errors": []}
    for key, fetch in (("sites", fetch_sites), ("locations", fetch_locations)):
        try:
            choices[key] = fetch()
        except GoogleApiError as error:
            choices["errors"].append(error.message)
    cache.set(CHOICES_CACHE, choices, CHOICES_SECONDS)
    return choices


def _google_post(request, domain):
    action = request.POST.get("action", "")
    if action == "run":
        enabled = settings_for(domain.customer).enabled_kinds()
        try:
            notes = run_google(domain, enabled, GoogleRun())
        except Exception as exc:  # noqa: BLE001 - visa felet, krascha inte sidan
            messages.error(request, f"Hämtningen misslyckades ({type(exc).__name__}).")
            return
        text = "Googles data är hämtad."
        if notes:
            text += " Att titta på: " + "; ".join(notes)
        messages.success(request, text)
    elif action == "choices":
        choices = _fetch_choices()
        if choices["errors"]:
            messages.warning(request, " ".join(dict.fromkeys(choices["errors"])))
        else:
            messages.success(
                request,
                f"{len(choices['sites'])} egendomar och {len(choices['locations'])} platser "
                "hämtade.",
            )
    elif action == "property":
        value = request.POST.get("search_property", "").strip()[:300]
        if value and not re.fullmatch(r"sc-domain:[a-z0-9.-]+|https?://[^\s]+/", value):
            messages.error(
                request, "Egendomen skrivs sc-domain:exempel.se eller https://exempel.se/."
            )
            return
        domain.search_property = value
        domain.save(update_fields=["search_property"])
        messages.success(
            request,
            f"Search Console-egendomen är {value}." if value else "Egendomen hittas automatiskt.",
        )
    elif action == "location":
        value = request.POST.get("gbp", "").strip()
        account, _sep, location = value.partition("|")
        if value and not (
            re.fullmatch(r"accounts/\d+", account) and re.fullmatch(r"locations/\d+", location)
        ):
            messages.error(request, "Välj en plats i listan.")
            return
        title = ""
        for loc in (cache.get(CHOICES_CACHE) or {}).get("locations") or []:
            if loc.get("name") == location:
                title = loc.get("title", "")
        domain.gbp_account = account if value else ""
        domain.gbp_location = location if value else ""
        domain.gbp_title = title[:200] if value else ""
        domain.save(update_fields=["gbp_account", "gbp_location", "gbp_title"])
        messages.success(
            request,
            f"Profilen är kopplad: {title or location}."
            if value
            else "Profilen matchas automatiskt på webbadress.",
        )
    else:
        messages.error(request, "Okänd åtgärd. Inget ändrades.")


def _google_email():
    """Google-kontot som övervakningen läser som (det som kopplats på Google-sidan)."""
    from apps.flamingo.models import GoogleAdsConnection

    connection = GoogleAdsConnection.objects.filter(pk=GoogleAdsConnection.SOLO_PK).first()
    return (connection.google_email if connection else "") or "ADX:s Google-konto"


@staff_required
@require_http_methods(["GET", "POST"])
def domain_google(request, pk):
    domain = get_object_or_404(MonitoredDomain.objects.select_related("customer"), pk=pk)
    if request.method == "POST":
        _google_post(request, domain)
        return _google_back(domain)
    monitor = settings_for(domain.customer)
    performance = domain.latest(Kind.PERFORMANCE)
    crux = domain.latest(Kind.CRUX)
    search = domain.latest(Kind.SEARCH)
    gbp = domain.latest(Kind.GBP)
    choices = cache.get(CHOICES_CACHE) or {}
    from apps.flamingo.manage_google import monitor_scope_states

    return render(
        request,
        "projects/monitor_google.html",
        {
            "active": "drift",
            "title": f"Google: {domain.name}",
            "domain": domain,
            "monitor": monitor,
            "performance": performance,
            "psi_categories": google_present.psi_categories(
                performance.data if performance else None
            ),
            "psi_lab": google_present.psi_lab(performance.data if performance else None),
            "psi_field": google_present.psi_field(performance.data if performance else None),
            "crux": crux,
            "crux_trends": google_present.crux_trends(crux.data if crux else None),
            "search": search,
            "search_view": google_present.search_view(search.data if search else None),
            "gbp": gbp,
            "gbp_view": google_present.gbp_view(gbp.data if gbp else None),
            "sites": choices.get("sites") or [],
            "locations": choices.get("locations") or [],
            "scopes": monitor_scope_states(),
            "no_traffic": google_present.NO_TRAFFIC,
            "google_email": _google_email(),
        },
    )
