"""
Byråns sida av SMS-API:t i /manage/: kundkortets panel (#sms), översikten
/manage/sms/ med månadsunderlagen, stängningen, CSV-filen för
faktureringen och sms:en att stämma av mot 46elks.

Aktiveringen mejlar aldrig kunden (webapp/CLAUDE.md). Vill byrån berätta
det görs det manuellt.
"""

import csv
import re
import unicodedata
from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Max, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views.decorators.http import require_POST

from apps.projects.access import VIEW_AS_KEY, staff_required
from apps.projects.models import Customer

from . import elks, numbers, pricing, service
from .models import (
    MAX_MONTHLY_CAP_KR,
    MonthlyStatement,
    SmsAccount,
    SmsApiKey,
    SmsMessage,
    validate_sender,
)

#: Rimliga gränser för byråns fält, så att ett felskrivet tal syns direkt.
MAX_MARKUP_ORE = 1000
MAX_YEARLY_FEE_KR = 100_000


def _back(customer_id):
    return redirect(reverse("manage:customer_detail", args=[customer_id]) + "#sms")


def suggest_sender(name):
    """Ett förslag på avsändare ur kundens namn: bokstäver och siffror, ord
    med stor bokstav, högst elva tecken, börjar med en bokstav."""
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    words = [w for w in re.split(r"[^A-Za-z0-9]+", ascii_name) if w]
    words = [w for w in words if w.lower() not in ("ab", "hb", "kb", "aktiebolag")] or words
    text = ""
    for word in words:
        candidate = text + word[:1].upper() + word[1:]
        if len(candidate) > 11:
            break
        text = candidate
    if not text and words:
        text = words[0][:11]
    text = text.lstrip("0123456789")
    return text if len(text) >= 3 else ""


def card_context(customer):
    """Kundkortets SMS-panel (templatetags/sms_tags.sms_card)."""
    account = (
        SmsAccount.objects.filter(customer=customer)
        .select_related("enabled_by", "monthly_cap_changed_by")
        .first()
    )
    context = {
        "account": account,
        # Formulärets värden: kontots, eller standardvärdena och ett förslag
        # på avsändare innan något sparats.
        "form": account or SmsAccount(customer=customer, sender_name=suggest_sender(customer.name)),
        "configured": elks.is_configured(),
        "live": elks.is_live(),
    }
    if account is not None:
        context.update(
            {
                "usage": pricing.usage(account),
                "country_names": [(c, numbers.country_name(c)) for c in account.countries],
                "statements": list(account.statements.order_by("-period")[:3]),
                "active_keys": account.api_keys.filter(revoked_at__isnull=True).count(),
                "next_fee": pricing.next_fee_period(account),
                "to_check": pricing.needs_check_messages(account).count(),
            }
        )
    return context


def _int_field(post, name, low, high, label):
    raw = (post.get(name) or "").strip().replace(" ", "")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{label}: skriv ett heltal.") from None
    if value < low or value > high:
        raise ValueError(f"{label}: {low} till {high}.")
    return value


@staff_required
@require_POST
def customer_update(request, pk):
    """Aktivera, stäng av eller ändra kundens SMS. Gäller direkt; kunden
    mejlas inte."""
    customer = get_object_or_404(Customer, pk=pk)
    account = SmsAccount.objects.filter(customer=customer).first()
    is_new = account is None
    if is_new:
        account = SmsAccount(customer=customer)
    enable = "is_enabled" in request.POST
    try:
        sender = (request.POST.get("sender_name") or "").strip()
        if sender:
            validate_sender(sender)
        elif enable:
            raise ValueError("Avsändaren behövs innan SMS kan aktiveras.")
        markup = _int_field(request.POST, "markup_ore_per_part", 0, MAX_MARKUP_ORE, "Påslaget")
        fee = _int_field(request.POST, "yearly_fee_kr", 0, MAX_YEARLY_FEE_KR, "Årsavgiften")
        cap = _int_field(request.POST, "monthly_cap_kr", 0, MAX_MONTHLY_CAP_KR, "Taket")
        countries = numbers.parse_country_list(request.POST.get("allowed_countries", ""))
        if not countries:
            raise ValueError("Minst ett land behövs (SE för Sverige).")
        start_raw = (request.POST.get("service_year_start") or "").strip()
        try:
            service_year_start = date.fromisoformat(start_raw) if start_raw else None
        except ValueError:
            raise ValueError("Tjänsteåret: skriv datumet som ÅÅÅÅ-MM-DD.") from None
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return _back(pk)
    except ValueError as exc:
        messages.error(request, str(exc) if str(exc) else "Kontrollera fälten.")
        return _back(pk)

    account.sender_name = sender
    account.customer_manages_api = "customer_manages_api" in request.POST
    account.markup_ore_per_part = markup
    account.yearly_fee_kr = fee
    if is_new or cap != account.monthly_cap_kr:
        account.record_cap_change(request.user)
    account.monthly_cap_kr = cap
    account.allowed_countries = countries
    if service_year_start is not None:
        account.service_year_start = service_year_start
    was_enabled = account.is_enabled and not is_new
    if enable and not was_enabled:
        account.is_enabled = True
        account.enabled_at = timezone.now()
        account.enabled_by = request.user
        if account.service_year_start is None:
            account.service_year_start = pricing.local_today()
        account.save()
        messages.success(
            request,
            f"SMS är aktiverat för {customer.name} med avsändaren {sender}. "
            "Kunden har inte mejlats.",
        )
    elif not enable and was_enabled:
        account.is_enabled = False
        account.disabled_at = timezone.now()
        account.save()
        messages.success(
            request,
            f"SMS är avstängt för {customer.name}. API:t och nycklarna nekas från och med nu.",
        )
    else:
        account.save()
        messages.success(request, "SMS-inställningarna är sparade.")
    return _back(pk)


@staff_required
@require_POST
def view_as(request, pk):
    """Öppna kundens SMS-sidor med kundens ögon: samma sidor och knappar."""
    customer = get_object_or_404(Customer, pk=pk, is_active=True)
    request.session[VIEW_AS_KEY] = customer.pk
    return redirect("sms:dashboard")


def _periods_with_activity(limit=12):
    """Månader (nyast först) med sms eller stängda underlag."""
    periods = set(MonthlyStatement.objects.values_list("period", flat=True))
    first = SmsMessage.objects.order_by("created_at").values_list("created_at", flat=True).first()
    current = pricing.current_period()
    if first is not None:
        month = pricing.month_of(pricing.local_today(first))
        while month <= current:
            periods.add(month)
            month = pricing.next_month(month)
    for account in SmsAccount.objects.exclude(service_year_start=None):
        periods.add(pricing.month_of(account.service_year_start))
    return sorted((p for p in periods if p <= current), reverse=True)[:limit]


def _has_unclosed(period, accounts, closed):
    """Finns det något att stänga för månaden: ett konto utan stängt
    underlag men med sms eller årsavgift?"""
    done = set(closed.values_list("account_id", flat=True))
    for account in accounts:
        if account.pk in done:
            continue
        draft = pricing.build_statement(account, period)
        if draft.sms_count or draft.fee:
            return True
    return False


@staff_required
def overview(request):
    period = pricing.current_period()
    start, end = pricing.month_bounds(period)
    accounts = list(
        SmsAccount.objects.select_related("customer")
        .annotate(
            month_sms=Count(
                "messages",
                filter=Q(
                    messages__created_at__gte=start,
                    messages__created_at__lt=end,
                    messages__status__in=SmsMessage.ACCEPTED,
                )
                & ~Q(messages__error_code="provider_error"),
            ),
            month_cost=Sum(
                "messages__customer_price",
                filter=Q(messages__created_at__gte=start, messages__created_at__lt=end),
            ),
            month_provider=Sum(
                "messages__provider_cost",
                filter=Q(messages__created_at__gte=start, messages__created_at__lt=end),
            ),
            last_message=Max("messages__created_at"),
        )
        .order_by("-is_enabled", "customer__name")
    )
    for a in accounts:
        a.month_cost = int(a.month_cost or 0)
        a.month_provider = int(a.month_provider or 0)
        a.cap_pct = min(100, round(100 * a.month_cost / a.cap_units)) if a.cap_units else 100
    totals = {
        "enabled": sum(1 for a in accounts if a.is_enabled),
        "sms": sum(a.month_sms for a in accounts),
        "cost": sum(a.month_cost for a in accounts),
        "provider": sum(a.month_provider for a in accounts),
    }
    totals["margin"] = totals["cost"] - totals["provider"]

    periods = []
    all_accounts = list(SmsAccount.objects.all())
    for p in _periods_with_activity():
        closed = MonthlyStatement.objects.filter(period=p, closed_at__isnull=False)
        agg = closed.aggregate(n=Count("pk"), total=Sum("total"), fee=Sum("fee"))
        periods.append(
            {
                "period": p,
                "is_current": p == period,
                "closed_count": agg["n"],
                "closed_total": int(agg["total"] or 0),
                "closed_fee": int(agg["fee"] or 0),
                "open": p < period and _has_unclosed(p, all_accounts, closed),
            }
        )
    return render(
        request,
        "manage/sms/overview.html",
        {
            "active": "customers",
            "title": "SMS-API",
            "accounts": accounts,
            "totals": totals,
            "period": period,
            "periods": periods,
            "previous": pricing.previous_month(period),
            "configured": elks.is_configured(),
            "live": elks.is_live(),
            "to_check": list(
                pricing.needs_check_messages()
                .select_related("account__customer")
                .order_by("created_at")[:50]
            ),
        },
    )


@staff_required
@require_POST
def close_month(request):
    try:
        period = pricing.parse_period(request.POST.get("period", ""))
    except ValueError:
        messages.error(request, "Okänd månad.")
        return redirect("manage:sms_overview")
    if period >= pricing.current_period():
        messages.error(request, "Bara en månad som tagit slut kan stängas.")
        return redirect("manage:sms_overview")
    result = pricing.close_month(period, user=request.user)
    text = result.summary() + " Kunderna har inte mejlats."
    if result.waiting or result.failed:
        messages.warning(request, text)
    else:
        messages.success(request, text)
    return redirect("manage:sms_overview")


@staff_required
@require_POST
def resolve_check(request, pk):
    """Byrån har stämt av ett sms mot 46elks sms-historik: skickades det eller
    inte? Kunden mejlas inte."""
    message = get_object_or_404(SmsMessage, pk=pk)
    sent = request.POST.get("sent") == "1"
    if service.resolve_check(message, sent):
        if sent:
            text = f"Sms {message.pk} är avstämt som skickat."
        else:
            text = f"Sms {message.pk} är avstämt som inte skickat: det kostar inget."
        messages.success(request, text)
    else:
        messages.error(request, f"Sms {message.pk} väntar inte på avstämning.")
    return redirect(reverse("manage:sms_overview") + "#kontrollera")


def _csv_amount(units):
    return pricing.kr_text(units).replace("\xa0", "")


#: Tecken som får ett kalkylprogram att läsa en cell som en formel.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """En textcell i CSV-filen. Börjar den som en formel får den en apostrof
    först, så att Excel och Numbers visar texten i stället för att räkna."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


@staff_required
def statements_csv(request, year, month):
    """Underlaget för faktureringen: en rad per kund. Stängda underlag som de
    frystes; en månad som inte är stängd räknas fram och märks preliminär."""
    try:
        period = date(int(year), int(month), 1)
    except ValueError:
        raise Http404 from None
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="sms-underlag-{period:%Y-%m}.csv"'
    response.write("﻿")
    writer = csv.writer(response, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Kund",
            "Organisationsnummer",
            "Period",
            "Avsändare",
            "SMS",
            "Delar",
            "SMS-kostnad 46elks (kr)",
            "Påslag (kr)",
            "Årsavgift (kr)",
            "Summa exkl. moms (kr)",
            "Länder",
            "Stängt",
        ]
    )
    for account in SmsAccount.objects.select_related("customer").order_by("customer__name"):
        statement = MonthlyStatement.objects.filter(
            account=account, period=period, closed_at__isnull=False
        ).first()
        if statement is None:
            statement = pricing.build_statement(account, period)
        if not statement.sms_count and not statement.fee:
            continue
        countries = ", ".join(
            f"{line['country'] or '?'} {line['sms']}" for line in (statement.lines or [])
        )
        row = [
            account.customer.name,
            account.customer.org_number,
            f"{period:%Y-%m}",
            account.sender_name,
            statement.sms_count,
            statement.parts,
            _csv_amount(statement.provider_cost),
            _csv_amount(statement.markup),
            _csv_amount(statement.fee),
            _csv_amount(statement.total),
            countries,
            (
                timezone.localtime(statement.closed_at).strftime("%Y-%m-%d")
                if statement.closed_at
                else "preliminärt"
            ),
        ]
        writer.writerow([_cell(value) for value in row])
    return response


# ---------------------------------------------------------------- nycklarna åt kunden


def _keys_page(request, customer, account, new_key=None):
    response = render(
        request,
        "manage/sms/keys.html",
        {
            "title": f"SMS-nycklar: {customer.name}",
            "customer": customer,
            "account": account,
            "keys": list(account.api_keys.select_related("created_by", "revoked_by")),
            "new_key": new_key,
            "smsz_url": request.build_absolute_uri(reverse("smsz")),
        },
    )
    if new_key:
        # Sidan med klartexten får aldrig sparas i en cache.
        add_never_cache_headers(response)
    return response


def _sms_account_or_404(pk):
    customer = get_object_or_404(Customer, pk=pk)
    account = SmsAccount.objects.filter(customer=customer).first()
    if account is None:
        raise Http404
    return customer, account


@staff_required
def keys(request, pk):
    """Byrån sköter kundens nycklar (när kunden inte gör det själv, men sidan
    finns alltid för byrån). En ny nyckel visas en gång, direkt i svaret."""
    customer, account = _sms_account_or_404(pk)
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()[:80]
        if not name:
            messages.error(request, "Ge nyckeln ett namn, till exempel Webbshop eller Bokning.")
            return redirect("manage:sms_keys", pk=pk)
        if account.api_keys.filter(revoked_at__isnull=True).count() >= 20:
            messages.error(request, "Kontot har redan 20 aktiva nycklar. Återkalla någon först.")
            return redirect("manage:sms_keys", pk=pk)
        _key, raw = SmsApiKey.issue(account, name, request.user)
        messages.success(
            request,
            f"Nyckeln {name} är skapad åt {customer.name}. Kopiera den nu: den visas bara "
            "en gång. Kunden har inte mejlats.",
        )
        return _keys_page(request, customer, account, new_key=raw)
    return _keys_page(request, customer, account)


@staff_required
@require_POST
def key_revoke(request, pk, key_pk):
    customer, account = _sms_account_or_404(pk)
    key = get_object_or_404(SmsApiKey, pk=key_pk, account=account)
    if key.revoked_at is None:
        key.revoke(request.user)
        messages.success(request, f"Nyckeln {key.name} är återkallad. Anrop med den nekas nu.")
    return redirect("manage:sms_keys", pk=pk)
