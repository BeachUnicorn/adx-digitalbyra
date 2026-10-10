"""
Översikten (kundresan steg 12) och kundväljaren.

Siffrorna räknas av databasen, aldrig av AI, och bara ur rader som finns.
Annonspengarna (och därmed kr per förfrågan och kr per affär), visningarna
och klicken kommer ur Googles rapporter (CampaignDayStats, som
google_reports.sync_stats fyller). Har kontot aldrig fått en rapport står
det så i rutan i stället för en siffra; en okänd kostnad visas aldrig som 0.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.db.models import Count, Max, Q, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..access import SESSION_KEY
from ..google_ads import STOCKHOLM
from ..models import CampaignDayStats, Lead
from ..rules import things_headline, three_things
from ..views import flamingo_required
from . import app_context, app_view
from .campaigns import is_approved, state_label

#: Perioden är de senaste 30 kalenderdagarna i svensk tid, i dag medräknad,
#: samma dagar för förfrågningarna och för Googles kostnad (period_start).
PERIOD_DAYS = 30


def period_start(now):
    """Början av perioden: midnatt i svensk tid för den första av de
    PERIOD_DAYS senaste dagarna (i dag medräknad)."""
    first_day = timezone.localdate(now, STOCKHOLM) - timedelta(days=PERIOD_DAYS - 1)
    return datetime.combine(first_day, time.min, tzinfo=STOCKHOLM)


def ad_stats(account, since):
    """Googles siffror för kontots kampanjer från och med since (dagarna i
    svensk tid): {"cost_kr", "clicks", "impressions", "read_at"}. None när
    kontot aldrig fått en rapport: då är kostnaden okänd, inte noll."""
    rows = CampaignDayStats.objects.filter(campaign__account=account)
    if not rows.exists():
        return None
    first_day = timezone.localdate(since, STOCKHOLM)
    totals = rows.filter(date__gte=first_day).aggregate(
        cost=Sum("cost_micros"), clicks=Sum("clicks"), impressions=Sum("impressions")
    )
    return {
        "cost_kr": round((totals["cost"] or 0) / 1_000_000),
        "clicks": totals["clicks"] or 0,
        "impressions": totals["impressions"] or 0,
        "read_at": rows.aggregate(at=Max("updated_at"))["at"],
    }


def ad_spend_kr(account, since):
    """Annonspengar sedan since, i hela kronor, ur Googles rapporter. None
    när kontot aldrig fått en rapport: en okänd kostnad visas aldrig som en
    siffra. Har rapporterna kommit men inget kostat i perioden är det 0."""
    stats = ad_stats(account, since)
    return None if stats is None else stats["cost_kr"]


@dataclass(frozen=True)
class Numbers:
    leads: int
    deals: int
    deals_without_value: int
    deal_value_kr: int
    spend_kr: int | None
    #: Av förfrågningarna i perioden: hur många som blivit affärer.
    cohort_deals: int
    #: Klick och visningar på annonserna enligt Google, None utan rapport.
    clicks: int | None = None
    impressions: int | None = None
    #: När Googles rapport senast lästes.
    stats_read_at: object = None
    #: Utskick (apps/utskick, README C.2 och D11): förfrågningar och affärer
    #: som kom via Google, det vill säga utan utskick, utan svar på utskick
    #: och (S4) utan en namngiven länk. Kronor per förfrågan och affär räknas bara på dem. None för
    #: ett konto där ingen räknat dem (samma tal som leads och deals).
    google_leads: int | None = None
    google_deals: int | None = None
    #: "Varav via utskick: 31 förfrågningar, 4 affärer" ("Varav via utskick
    #: och dina länkar" när named_leads, de via en namngiven länk, finns med).
    utskick_leads: int = 0
    utskick_deals: int = 0
    named_leads: int = 0

    @property
    def kr_per_lead(self):
        leads = self.leads if self.google_leads is None else self.google_leads
        if self.spend_kr is None or not leads:
            return None
        return round(self.spend_kr / leads)

    @property
    def kr_per_deal(self):
        deals = self.deals if self.google_deals is None else self.google_deals
        if self.spend_kr is None or not deals:
            return None
        return round(self.spend_kr / deals)

    @property
    def cohort_percent(self):
        return round(100 * self.cohort_deals / self.leads) if self.leads else None


def numbers_for(account, now):
    since = period_start(now)
    # Ett svar på ett utskick är en tråd i Inkorgen, ingen förfrågan (C.2).
    leads = account.leads.filter(created_at__gte=since, status__in=Lead.COUNTED_STATUSES).exclude(
        source=Lead.SOURCE_REPLY
    )
    won = account.leads.filter(status=Lead.STATUS_WON).filter(
        Q(won_at__gte=since) | Q(won_at__isnull=True, updated_at__gte=since)
    )
    # S4: en förfrågan via en namngiven länk (affischen, QR-koden; Lead.utskick
    # är null men spåret säger kanalen named) kom inte heller via Google.
    named = Q(attribution__contains={"channel": "named"})
    via_utskick = Q(utskick__isnull=False) | Q(source=Lead.SOURCE_REPLY) | named
    stats = ad_stats(account, since)
    ours = leads.aggregate(
        all=Count("pk", filter=Q(utskick__isnull=False) | named),
        named=Count("pk", filter=named),
    )
    return Numbers(
        google_leads=leads.exclude(via_utskick).count(),
        google_deals=won.exclude(via_utskick).count(),
        utskick_leads=ours["all"],
        named_leads=ours["named"],
        utskick_deals=won.filter(via_utskick).count(),
        leads=leads.count(),
        deals=won.count(),
        deals_without_value=won.filter(value_kr__isnull=True).count(),
        deal_value_kr=won.aggregate(total=Sum("value_kr"))["total"] or 0,
        spend_kr=None if stats is None else stats["cost_kr"],
        cohort_deals=leads.filter(status=Lead.STATUS_WON).count(),
        clicks=None if stats is None else stats["clicks"],
        impressions=None if stats is None else stats["impressions"],
        stats_read_at=None if stats is None else stats["read_at"],
    )


def band_path(leads, deals):
    """Bandet från förfrågningar till affärer som en SVG-väg (800 x 90).
    Vänsterkanten är förfrågningarna, högerkanten affärerna i samma skala."""
    top, bottom, mid = 4, 86, 45
    full = bottom - top
    right = max(4.0, full * deals / leads) if leads else 4.0
    r_top, r_bottom = mid - right / 2, mid + right / 2
    return (
        f"M0 {top} C 320 {top}, 480 {r_top:.1f}, 800 {r_top:.1f} "
        f"L 800 {r_bottom:.1f} C 480 {r_bottom:.1f}, 320 {bottom}, 0 {bottom} Z"
    )


def greeting(now, user, customer, is_contact):
    hour = timezone.localtime(now).hour
    if 5 <= hour < 10:
        hello = "God morgon"
    elif hour >= 18:
        hello = "God kväll"
    else:
        hello = "Hej"
    name = (user.first_name or "").strip() if is_contact else ""
    return f"{hello}, {name}" if name else f"{hello}, {customer.name}"


def headline(numbers, onboarding):
    if not onboarding.complete and not numbers.leads and not numbers.deals:
        return "Välkommen till ADX Flamingo."
    if numbers.deals:
        word = "affär" if numbers.deals == 1 else "affärer"
        return f"{numbers.deals} {word} på {PERIOD_DAYS} dagar."
    if numbers.leads:
        word = "förfrågan" if numbers.leads == 1 else "förfrågningar"
        return f"{numbers.leads} {word} på {PERIOD_DAYS} dagar."
    return f"Inga förfrågningar de senaste {PERIOD_DAYS} dagarna."


@app_view
def overview(request, account):
    now = timezone.now()
    context = app_context(request, "overview")
    onboarding = context["onboarding"]
    numbers = numbers_for(account, now)
    things = three_things(account, now)
    context.update(
        {
            "greeting": greeting(now, request.user, account.customer, context["is_contact"]),
            "headline": headline(numbers, onboarding),
            "show_steps": not onboarding.complete,
            "numbers": numbers,
            "period_days": PERIOD_DAYS,
            "things": things,
            "things_headline": things_headline(things),
            "band_path": band_path(numbers.leads, numbers.cohort_deals) if numbers.leads else "",
            "recent_leads": account.leads.exclude(status=Lead.STATUS_JUNK)
            .select_related("service", "campaign__service")
            .order_by("-created_at", "-id")[:5],
            "campaign_rows": [
                {"campaign": c, "state": state_label(c), "approved": is_approved(c)}
                for c in account.campaigns.select_related("service").order_by("-updated_at", "-id")[
                    :6
                ]
            ],
        }
    )
    return render(request, "flamingo/app/overview.html", context)


@flamingo_required
@require_POST
def choose_customer(request):
    """Kontakten i flera Flamingo-kunder väljer vilken verktyget visar.
    Valet prövas mot kontaktens egna Flamingo-kunder, aldrig rakt av."""
    access = request.flamingo
    try:
        pk = int(request.POST.get("customer", ""))
    except ValueError:
        raise Http404 from None
    if not any(c.pk == pk for c in access.customers):
        raise Http404
    request.session[SESSION_KEY] = pk
    # Alltid till översikten: sidan kontakten stod på hör till förra kunden.
    return redirect("flamingo:app")
