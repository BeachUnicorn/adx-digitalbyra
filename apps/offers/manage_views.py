"""
/manage/offerter/ - offertbyggaren.

Designbeslut:

- Radredigering och kunduppgifter AUTOSPARAS via fetch (inga sparaknappar) -
  byggaren ska kännas som mockupen, inte som ett adminformulär. Strukturella
  ändringar (lägg till rad, ta bort, skicka) är vanliga POST + redirect.
- Ordningen sätts med dra-och-släpp: klienten skickar radernas id i ny
  ordning, servern numrerar om HELA listan. Att skriva om allt är billigare
  och robustare än att försöka vara smart med enskilda swappar.
- Allt är @login_required precis som resten av /manage/. Publika ytan bor i
  public_views och nås bara via offertens token.
"""

import json
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.website.models import SiteSettings

from .emails import send_quote_to_customer
from .models import PricePeriod, Product, Quote, QuoteLine, QuoteStatus


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "offers"}
    ctx.update(extra)
    return ctx


def _json_body(request):
    try:
        return json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _clean_price(value):
    """Heltal kronor, aldrig negativt. Tål '12 500' och '12500'."""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return min(int(digits), 99_999_999) if digits else 0


def _clean_period(value):
    return value if value in PricePeriod.values else PricePeriod.ONE_TIME


def _locked(quote):
    """En accepterad offert är en affärshandling - innehållet är fryst."""
    return quote.status == QuoteStatus.ACCEPTED


# ------------------------------------------------------------------ offerter


@login_required
def offer_list(request):
    status_filter = request.GET.get("status", "")
    quotes = Quote.objects.prefetch_related("lines")
    if status_filter in QuoteStatus.values:
        quotes = quotes.filter(status=status_filter)
    counts = {
        row["status"]: row["n"]
        for row in Quote.objects.values("status").annotate(n=Count("id"))
    }
    status_chips = [
        (value, label, counts.get(value, 0)) for value, label in QuoteStatus.choices
    ]
    return render(
        request,
        "manage/offers/list.html",
        _ctx(
            quotes=quotes,
            status_chips=status_chips,
            status_filter=status_filter,
        ),
    )


@login_required
@require_POST
def offer_create(request):
    name = request.POST.get("customer_name", "").strip()
    if not name:
        messages.error(request, "Skriv kundens namn.")
        return redirect("manage:offer_list")
    quote = Quote.objects.create(
        customer_name=name,
        project_title=request.POST.get("project_title", "").strip(),
        valid_until=date.today() + timedelta(days=30),
        created_by=request.user,
    )
    return redirect("manage:offer_edit", pk=quote.pk)


@login_required
def offer_edit(request, pk):
    quote = get_object_or_404(Quote.objects.prefetch_related("lines"), pk=pk)
    return render(
        request,
        "manage/offers/edit.html",
        _ctx(
            quote=quote,
            products=Product.objects.filter(is_active=True),
            periods=PricePeriod,
            totals=quote.totals(),
            statuses=QuoteStatus,
        ),
    )


@login_required
@require_POST
def offer_update(request, pk):
    """Autospar för kunduppgifterna. Tar emot bara de fält som skickas."""
    quote = get_object_or_404(Quote, pk=pk)
    if _locked(quote):
        return JsonResponse({"ok": False, "error": "Accepterad offert är låst."}, status=400)
    data = _json_body(request)
    editable = ("customer_name", "customer_email", "project_title", "intro")
    fields = []
    for field in editable:
        if field in data:
            # Trunkera mot fältets faktiska max_length - en platt gräns
            # över modellens tak blir ett DataError i Postgres.
            max_length = Quote._meta.get_field(field).max_length or 5000
            setattr(quote, field, str(data[field]).strip()[:max_length])
            fields.append(field)
    if "valid_until" in data:
        raw = str(data["valid_until"]).strip()
        try:
            quote.valid_until = date.fromisoformat(raw) if raw else None
            fields.append("valid_until")
        except ValueError:
            pass
    if not fields:
        return JsonResponse({"ok": False}, status=400)
    quote.save(update_fields=fields + ["updated_at"])
    return JsonResponse({"ok": True})


@login_required
@require_POST
def offer_status(request, pk):
    """Manuell statusändring - t.ex. accepterad per telefon, eller förlorad."""
    from django.utils import timezone

    quote = get_object_or_404(Quote, pk=pk)
    status = request.POST.get("status", "")
    if status not in QuoteStatus.values:
        return redirect("manage:offer_edit", pk=pk)
    quote.status = status
    if status == QuoteStatus.ACCEPTED and not quote.accepted_at:
        quote.accepted_at = timezone.now()
    if status == QuoteStatus.DECLINED and not quote.declined_at:
        quote.declined_at = timezone.now()
    quote.save()
    messages.success(request, f"Status: {quote.get_status_display()}.")
    return redirect("manage:offer_edit", pk=pk)


@login_required
@require_POST
def offer_send(request, pk):
    quote = get_object_or_404(Quote.objects.prefetch_related("lines"), pk=pk)
    if not quote.customer_email:
        messages.error(request, "Fyll i kundens e-post först.")
        return redirect("manage:offer_edit", pk=pk)
    if not quote.lines.exists():
        messages.error(request, "Offerten har inga rader än.")
        return redirect("manage:offer_edit", pk=pk)
    if quote.status == QuoteStatus.ACCEPTED:
        messages.error(request, "Offerten är redan accepterad.")
        return redirect("manage:offer_edit", pk=pk)

    from django.utils import timezone

    if send_quote_to_customer(quote):
        # Villkorade UPDATE:ar i stället för att spara det stallästa
        # objektet: mejlsändningen tar sekunder, och hinner kunden trycka
        # Acceptera under tiden får omsändningen inte regrediera statusen.
        now = timezone.now()
        Quote.objects.filter(
            pk=quote.pk, status__in=(QuoteStatus.DRAFT, QuoteStatus.DECLINED)
        ).update(status=QuoteStatus.SENT)
        Quote.objects.filter(pk=quote.pk, sent_at__isnull=True).update(sent_at=now)
        Quote.objects.filter(pk=quote.pk).update(updated_at=now)
        messages.success(request, f"Offerten är mejlad till {quote.customer_email}.")
    else:
        messages.error(request, "Mejlet kunde inte skickas - kontrollera e-postinställningarna.")
    return redirect("manage:offer_edit", pk=pk)


@login_required
@require_POST
def offer_delete(request, pk):
    quote = get_object_or_404(Quote, pk=pk)
    if quote.status == QuoteStatus.ACCEPTED:
        # En accepterad offert är en affärshandling - den arkiveras, inte raderas.
        messages.error(request, "En accepterad offert kan inte tas bort.")
        return redirect("manage:offer_edit", pk=pk)
    quote.delete()
    messages.success(request, "Offerten är borttagen.")
    return redirect("manage:offer_list")


# ------------------------------------------------------------------ rader


@login_required
@require_POST
def line_add(request, pk):
    quote = get_object_or_404(Quote, pk=pk)
    if _locked(quote):
        messages.error(request, "Accepterad offert är låst för ändringar.")
        return redirect("manage:offer_edit", pk=pk)
    last = quote.lines.order_by("-order").first()
    order = (last.order + 1) if last else 1

    product_id = request.POST.get("product_id", "")
    if product_id:
        product = get_object_or_404(Product, pk=product_id, is_active=True)
        QuoteLine.objects.create(
            quote=quote,
            product=product,
            label=product.name,
            description=product.description,
            price=product.default_price,
            period=product.default_period,
            order=order,
        )
    else:
        QuoteLine.objects.create(quote=quote, label="Ny rad", order=order)
    quote.save(update_fields=["updated_at"])
    return redirect("manage:offer_edit", pk=pk)


@login_required
@require_POST
def line_update(request, pk):
    line = get_object_or_404(QuoteLine.objects.select_related("quote"), pk=pk)
    if _locked(line.quote):
        return JsonResponse({"ok": False, "error": "Accepterad offert är låst."}, status=400)
    data = _json_body(request)
    fields = []
    if "label" in data:
        line.label = str(data["label"]).strip()[:200] or "Rad"
        fields.append("label")
    if "description" in data:
        line.description = str(data["description"]).strip()[:2000]
        fields.append("description")
    if "price" in data:
        line.price = _clean_price(data["price"])
        fields.append("price")
    if "period" in data:
        line.period = _clean_period(data["period"])
        fields.append("period")
    if fields:
        # Bara de mottagna fälten: ett autospar som korsar en samtidig
        # dra-och-släpp-omordning får inte skriva tillbaka gammal order.
        line.save(update_fields=fields)
        line.quote.save(update_fields=["updated_at"])
    return JsonResponse({"ok": True, "totals": line.quote.totals()})


@login_required
@require_POST
def line_delete(request, pk):
    line = get_object_or_404(QuoteLine.objects.select_related("quote"), pk=pk)
    if _locked(line.quote):
        messages.error(request, "Accepterad offert är låst för ändringar.")
        return redirect("manage:offer_edit", pk=line.quote_id)
    quote_pk = line.quote_id
    line.delete()
    line.quote.save(update_fields=["updated_at"])
    return redirect("manage:offer_edit", pk=quote_pk)


@login_required
@require_POST
def lines_reorder(request, pk):
    """Dra-och-släpp: klienten skickar radernas id i ny ordning."""
    quote = get_object_or_404(Quote, pk=pk)
    if _locked(quote):
        return JsonResponse({"ok": False, "error": "Accepterad offert är låst."}, status=400)
    ids = _json_body(request).get("order", [])
    lines = {line.pk: line for line in quote.lines.all()}
    if sorted(lines) != sorted(int(i) for i in ids if str(i).isdigit()):
        return JsonResponse({"ok": False}, status=400)
    for position, line_id in enumerate(ids, start=1):
        lines[int(line_id)].order = position
    QuoteLine.objects.bulk_update(lines.values(), ["order"])
    quote.save(update_fields=["updated_at"])
    return JsonResponse({"ok": True})


# ------------------------------------------------------------------ produkter


@login_required
def product_list(request):
    return render(
        request,
        "manage/offers/products.html",
        _ctx(products=Product.objects.all(), periods=PricePeriod),
    )


@login_required
@require_POST
def product_create(request):
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Skriv produktens namn.")
        return redirect("manage:product_list")
    Product.objects.create(
        name=name,
        description=request.POST.get("description", "").strip(),
        default_price=_clean_price(request.POST.get("default_price", "0")),
        default_period=_clean_period(request.POST.get("default_period", "")),
    )
    messages.success(request, f"Produkten {name} är skapad.")
    return redirect("manage:product_list")


@login_required
@require_POST
def product_update(request, pk):
    product = get_object_or_404(Product, pk=pk)
    product.name = request.POST.get("name", "").strip() or product.name
    product.description = request.POST.get("description", "").strip()
    product.default_price = _clean_price(request.POST.get("default_price", "0"))
    product.default_period = _clean_period(request.POST.get("default_period", ""))
    product.is_active = request.POST.get("is_active") == "on"
    product.save()
    messages.success(request, f"Produkten {product.name} är sparad.")
    return redirect("manage:product_list")
