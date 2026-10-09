"""
Kundportalens SMS-sidor, /kund/sms/: översikten, nycklarna och taket,
dokumentationen och månadsunderlagen.

Vem ser vad:

    kontakt hos en kund med SMS aktiverat   sidorna för den kunden
    kontakt hos en kund utan SMS            404
    byrån i kundvyn                         exakt det kunden ser, med samma
                                            formulär och knappar; det byrån
                                            gör gäller på riktigt och sparas
                                            i byråns namn. Har kunden inte
                                            SMS visas en notis i stället.

Kundvyn är alltså inte skrivskyddad här (beslut 2026-10-03: "visa som
kunden" ska visa det kunden ser och kan göra). Allt hämtas via kundens
eget SmsAccount, så ett id från en annan kund ger 404.
"""

from datetime import datetime, time, timedelta
from functools import wraps
from itertools import groupby

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_POST

from apps.projects.access import customer_for, is_agency_user, viewing_customer

from . import hooks, numbers, pricing, ratelimit, service
from .models import (
    MAX_MONTHLY_CAP_KR,
    MonthlyStatement,
    SmsAccount,
    SmsApiKey,
    SmsMessage,
)

#: Sms per sida i listan.
PAGE_SIZE = 20
#: Dagar i stapeldiagrammet.
CHART_DAYS = 30

STATUS_FILTERS = [
    ("", "Alla"),
    ("delivered", "Levererade"),
    ("sent", "Skickade, ej bekräftade"),
    ("failed", "Misslyckade"),
    ("stopped", "Stoppade"),
]

#: Källfiltret (?kalla=), visas bara när kontot har sms från Flamingos
#: utskick. Utskick: utskick, flöden och testsändningar; Svar: svar från
#: Inkorgen och Flamingos bekräftelser (STOPP, START).
SOURCE_FILTERS = [
    ("", "Alla"),
    ("api", "API"),
    ("utskick", "Utskick"),
    ("svar", "Svar"),
]
SOURCE_GROUPS = {
    "api": (SmsMessage.Source.API,),
    "utskick": (SmsMessage.Source.UTSKICK, SmsMessage.Source.FLOW, SmsMessage.Source.TEST),
    "svar": (SmsMessage.Source.REPLY, SmsMessage.Source.SYSTEM),
}

MONTHS = [
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
]


def month_name(period):
    return MONTHS[period.month - 1]


def sms_portal(view):
    """Kunden (eller byrån i kundvyn) och kundens aktiverade SmsAccount."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/kund/logga-in/?next={request.path}")
        customer = customer_for(request.user)
        request.viewing_as = False
        if customer is None and is_agency_user(request.user):
            customer = viewing_customer(request)
            if customer is None:
                return redirect("manage:sms_overview")
            request.viewing_as = True
        if customer is None:
            return HttpResponseForbidden("Kontot är inte kopplat till någon kund.")
        request.customer = customer
        account = (
            SmsAccount.objects.filter(customer=customer, is_enabled=True)
            .select_related("monthly_cap_changed_by")
            .first()
        )
        if account is None:
            if request.viewing_as:
                return render(
                    request,
                    "sms/portal/not_enabled.html",
                    {"title": "SMS", "active": "sms", "customer": customer},
                )
            raise Http404
        request.sms_account = account
        return view(request, *args, **kwargs)

    return wrapped


def self_service(view):
    """Nycklar, tak och dokumentation finns bara för en kund som sköter dem
    själv (SmsAccount.customer_manages_api). Annars sköter ADX dem från
    kundkortet, och sidorna finns inte i portalen: samma 404 för kunden och
    för byrån i kundvyn, som ser det kunden ser."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.sms_account.customer_manages_api:
            raise Http404
        return view(request, *args, **kwargs)

    return wrapped


def _base(request, active_tab, title, **extra):
    account = request.sms_account
    return {
        "title": title,
        "active": "sms",
        "tab": active_tab,
        "customer": request.customer,
        "account": account,
        **extra,
    }


def _accepted(qs):
    return qs.filter(status__in=SmsMessage.ACCEPTED).exclude(error_code="provider_error")


def _chart(account, today):
    """Staplarna: sms per dag de senaste CHART_DAYS dagarna, med det som
    visas när man pekar på en stapel."""
    first = today - timedelta(days=CHART_DAYS - 1)
    since = datetime.combine(first, time(0, 0), tzinfo=pricing.STOCKHOLM)
    rows = (
        _accepted(SmsMessage.objects.filter(account=account, created_at__gte=since))
        .annotate(day=TruncDate("created_at", tzinfo=pricing.STOCKHOLM))
        .values("day")
        .annotate(
            sms=Count("pk"),
            parts=Sum("parts"),
            delivered=Count("pk", filter=Q(status=SmsMessage.Status.DELIVERED)),
            failed=Count("pk", filter=Q(status=SmsMessage.Status.FAILED)),
            cost=Sum("customer_price"),
        )
    )
    by_day = {row["day"]: row for row in rows}
    days = []
    for offset in range(CHART_DAYS):
        day = first + timedelta(days=offset)
        row = by_day.get(day, {})
        days.append(
            {
                "date": day,
                "sms": row.get("sms", 0),
                "parts": int(row.get("parts") or 0),
                "delivered": row.get("delivered", 0),
                "failed": row.get("failed", 0),
                "cost": int(row.get("cost") or 0),
            }
        )
    peak = max((d["sms"] for d in days), default=0)
    third = CHART_DAYS / 3
    for index, d in enumerate(days):
        # Heltal: ett decimaltal skrivs med komma i svensk mall och blir ogiltig CSS.
        d["height"] = round(100 * d["sms"] / peak) if peak else 0
        # Var rutan med siffrorna hänger: vid kanterna inåt, så att den
        # aldrig sticker ut ur diagrammet på en smal skärm.
        d["align"] = "start" if index < third else ("end" if index >= 2 * third else "mid")
    return {
        "days": days,
        "peak": peak,
        "total": sum(d["sms"] for d in days),
        "first": first,
        "middle": days[CHART_DAYS // 2]["date"],
    }


def _countries(account, period):
    rows = (
        _accepted(pricing.messages_in(account, period))
        .values("country")
        .annotate(
            sms=Count("pk"),
            parts=Sum("parts"),
            delivered=Count("pk", filter=Q(status=SmsMessage.Status.DELIVERED)),
            failed=Count("pk", filter=Q(status=SmsMessage.Status.FAILED)),
            cost=Sum("customer_price"),
        )
        .order_by("-sms", "country")
    )
    return [
        {
            **row,
            "parts": int(row["parts"] or 0),
            "cost": int(row["cost"] or 0),
            "name": numbers.country_name(row["country"]),
        }
        for row in rows
    ]


def _message_list(request, account):
    qs = SmsMessage.objects.filter(account=account).select_related("api_key")
    q = (request.GET.get("q") or "").strip()[:100]
    status = request.GET.get("status", "")
    source = request.GET.get("kalla", "")
    if q:
        digits = "".join(ch for ch in q if ch.isdigit())
        match = Q(body__icontains=q) | Q(reference__iexact=q)
        if len(digits) >= 4:
            match |= Q(to__contains=digits.lstrip("0"))
        qs = qs.filter(match)
    if status == "stopped":
        qs = qs.filter(status__in=SmsMessage.STOPPED)
    elif status in ("delivered", "sent", "failed"):
        qs = qs.filter(status=status)
    else:
        status = ""
    # Källfiltret finns bara för en kund som också skickar via Flamingo.
    has_sources = (
        SmsMessage.objects.filter(account=account).exclude(source=SmsMessage.Source.API).exists()
    )
    if has_sources and source in SOURCE_GROUPS:
        qs = qs.filter(source__in=SOURCE_GROUPS[source])
    else:
        source = ""
    page = Paginator(qs.order_by("-created_at", "-pk"), PAGE_SIZE).get_page(request.GET.get("sida"))
    # Sms som inte kom från API:t visar sin källa ("Utskick: Höstservice")
    # i stället för reference (hooks.labels).
    labels = hooks.labels(page.object_list)
    for message in page.object_list:
        message.source_label = labels.get(message.pk, "")
    return {
        "page": page,
        "q": q,
        "status": status,
        "source": source,
        "source_filters": SOURCE_FILTERS if has_sources else [],
        "filtered": bool(q or status or source),
    }


@sms_portal
def dashboard(request):
    account = request.sms_account
    today = pricing.local_today()
    period = pricing.month_of(today)
    usage = pricing.usage(account)
    listing = _message_list(request, account)
    query = request.GET.copy()
    query.pop("sida", None)
    return render(
        request,
        "sms/portal/dashboard.html",
        _base(
            request,
            "dashboard",
            "SMS",
            usage=usage,
            month=month_name(period),
            chart=_chart(account, today),
            countries=_countries(account, period),
            status_filters=STATUS_FILTERS,
            query=query.urlencode(),
            waiting=pricing.needs_check_messages(account)
            .filter(status=SmsMessage.Status.RESERVED)
            .count(),
            **listing,
        ),
    )


def _keys_page(request, new_key=None):
    account = request.sms_account
    response = render(
        request,
        "sms/portal/keys.html",
        _base(
            request,
            "keys",
            "SMS-nycklar och tak",
            keys=list(account.api_keys.select_related("created_by", "revoked_by")),
            new_key=new_key,
            usage=pricing.usage(account),
            month=month_name(pricing.current_period()),
            max_cap=MAX_MONTHLY_CAP_KR,
            country_names=[(c, numbers.country_name(c)) for c in account.countries],
        ),
    )
    if new_key:
        # Sidan med klartexten får aldrig sparas i en cache.
        add_never_cache_headers(response)
    return response


@sms_portal
@self_service
def keys(request):
    return _keys_page(request)


def _acting_note(request):
    return " (i kundvyn, i ditt namn)" if getattr(request, "viewing_as", False) else ""


@require_POST
@sms_portal
@self_service
def key_create(request):
    account = request.sms_account
    name = (request.POST.get("name") or "").strip()[:80]
    if not name:
        messages.error(request, "Ge nyckeln ett namn, till exempel Webbshop eller Bokning.")
        return redirect("sms:keys")
    if account.api_keys.filter(revoked_at__isnull=True).count() >= 20:
        messages.error(request, "Kontot har redan 20 aktiva nycklar. Återkalla någon först.")
        return redirect("sms:keys")
    _key, raw = SmsApiKey.issue(account, name, request.user)
    messages.success(
        request,
        f"Nyckeln {name} är skapad{_acting_note(request)}. Kopiera den nu: den visas bara en gång.",
    )
    # Klartexten visas direkt i svaret på POST:en och sparas ingenstans, inte
    # heller i sessionen (som ligger i databasen). En omladdning visar den inte.
    return _keys_page(request, new_key=raw)


@require_POST
@sms_portal
@self_service
def key_revoke(request, pk):
    key = get_object_or_404(SmsApiKey, pk=pk, account=request.sms_account)
    if key.revoked_at is None:
        key.revoke(request.user)
        messages.success(
            request,
            f"Nyckeln {key.name} är återkallad{_acting_note(request)}. Anrop med den nekas nu.",
        )
    return redirect("sms:keys")


@require_POST
@sms_portal
@self_service
def cap_update(request):
    account = request.sms_account
    raw = (request.POST.get("monthly_cap_kr") or "").strip().replace(" ", "")
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0 or value > MAX_MONTHLY_CAP_KR:
        messages.error(request, f"Skriv taket i hela kronor, 0 till {MAX_MONTHLY_CAP_KR}.")
        return redirect("sms:keys")
    if value != account.monthly_cap_kr:
        account.monthly_cap_kr = value
        account.record_cap_change(request.user)
        account.save(
            update_fields=[
                "monthly_cap_kr",
                "monthly_cap_changed_at",
                "monthly_cap_changed_by",
                "updated_at",
            ]
        )
    messages.success(
        request,
        f"Taket är {pricing.kr_text(account.cap_units, 0)} kr i månaden{_acting_note(request)}.",
    )
    return redirect(reverse("sms:keys") + "#tak")


def _api_base(request):
    base = (getattr(settings, "SITE_BASE_URL", "") or "").rstrip("/")
    if not base:
        base = f"{request.scheme}://{request.get_host()}"
    return f"{base}/api/sms/v1"


@sms_portal
@self_service
def docs(request):
    account = request.sms_account
    errors = [
        (code, service.HTTP_STATUS[code], service.ERROR_TEXTS[code])
        for code in (
            "invalid_request",
            "invalid_key",
            "sms_not_enabled",
            "invalid_number",
            "country_not_allowed",
            "sender_not_allowed",
            "message_too_long",
            "monthly_cap_reached",
            "not_found",
            "reference_conflict",
            "rate_limited",
            "provider_error",
            "internal_error",
            "provider_unknown",
        )
    ]
    return render(
        request,
        "sms/portal/docs.html",
        _base(
            request,
            "docs",
            "SMS-API: dokumentation",
            api_base=_api_base(request),
            errors=errors,
            limits=ratelimit.limits(),
            max_parts=service.MAX_PARTS,
            statuses=SmsMessage.Status.choices,
            country_names=[(c, numbers.country_name(c)) for c in account.countries],
            fee=account.yearly_fee_kr,
            cap=account.monthly_cap_kr,
            has_utskick=_has_utskick(account),
        ),
    )


def _has_utskick(account):
    """Har kunden Flamingos utskick (spärrlistan och det delade taket)?"""
    from .api import _utskick_account

    return _utskick_account(account) is not None


@sms_portal
def statements(request):
    account = request.sms_account
    period = pricing.current_period()
    current = pricing.build_statement(account, period)
    closed = list(account.statements.filter(closed_at__isnull=False).order_by("-period"))
    # Månader som tagit slut men inte stängts än visas som preliminära.
    closed_periods = {s.period for s in closed}
    oldest = account.messages.order_by("created_at").values_list("created_at", flat=True).first()
    first = pricing.month_of(
        pricing.local_today(oldest or account.enabled_at or account.created_at)
    )
    open_months = []
    month = pricing.previous_month(period)
    while month >= first and len(open_months) < 3:
        if month not in closed_periods:
            draft = pricing.build_statement(account, month)
            if draft.sms_count or draft.fee:
                open_months.append(draft)
        month = pricing.previous_month(month)
    rows = sorted([current, *open_months, *closed], key=lambda s: s.period, reverse=True)
    for row in rows:
        # Kunden ser sms-kostnaden som ett belopp: leverantörens pris och
        # påslaget redovisas inte var för sig (Giovanni 2026-10-04). Byrån
        # ser uppdelningen på /manage/sms/ och i CSV:n.
        row.sms_cost = (row.provider_cost or 0) + (row.markup or 0)
        row.country_lines = [
            {
                **line,
                "name": numbers.country_name(line["country"]),
                "cost": (line.get("provider_cost") or 0) + (line.get("markup") or 0),
            }
            for line in row.lines
        ]
    years = [
        {"year": year, "statements": list(items)}
        for year, items in groupby(rows, key=lambda s: s.period.year)
    ]
    return render(
        request,
        "sms/portal/statements.html",
        _base(
            request,
            "statements",
            "SMS-underlag",
            years=years,
            current_period=period,
            next_fee=pricing.next_fee_period(account),
            has_closed=MonthlyStatement.objects.filter(account=account).exists(),
        ),
    )
