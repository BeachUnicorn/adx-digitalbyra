"""
Kundens offertsida: /offert/<token>/.

Länken är behörigheten. Ingen inloggning, inget id i adressen - bara en
slumpad token med 190+ bitar entropi. Sidan är noindex och /offert/ är
spärrad i robots.txt; en offert är en affärshandling, inte innehåll.

Öppnad-spårningen: första GET från någon som inte är inloggad flyttar
statusen skickad -> öppnad. Giovannis egna förhandstittar (inloggad i
/manage/ i samma webbläsare) ska inte se ut som att kunden öppnat.

Accepten är två steg: offertsidan (välj tillval) -> acceptsidan (beställarens
uppgifter, bekräfta) -> kvitto på offertsidan. Tillvalen följer med som
query-parametrar mellan stegen, så acceptsidan går att ladda om.
"""

import ipaddress

from django.contrib import messages
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.website.models import SiteSettings

from .emails import send_accepted_notification, send_question_to_staff
from .forms import AcceptForm
from .models import PricePeriod, Quote, QuoteAttachment, QuoteLine, QuoteStatus


def _get_quote(token):
    return get_object_or_404(Quote.objects.prefetch_related("lines"), token=token)


def client_ip(request):
    """
    Kundens IP bakom nginx: första hoppet i X-Forwarded-For (nginx sätter
    den, och nginx är enda vägen in), annars REMOTE_ADDR. Bara giltiga
    adresser släpps igenom - fältet är ett GenericIPAddressField.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
    candidates.append(request.META.get("REMOTE_ADDR", ""))
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def _chosen_ids(request):
    """Valda tillval ur query (steg 1 -> 2) eller POST (steg 2 -> kvitto)."""
    source = request.POST if request.method == "POST" else request.GET
    return {int(raw) for raw in source.getlist("tillval") if str(raw).isdigit()}


def _base_totals(lines):
    base = {p.value: 0 for p in PricePeriod}
    for line in lines:
        if not line.is_optional:
            base[line.period] += line.price
    return base


def offer_public(request, token):
    quote = _get_quote(token)
    if not request.user.is_authenticated:
        quote.mark_opened()
    lines = list(quote.lines.all())
    answerable = quote.is_answerable()
    # Medan offerten går att besvara visas tillvalen som togglar och
    # tabellen bara de fasta raderna; efteråt visas det som faktiskt
    # ingår (fasta rader + valda tillval).
    if answerable:
        table_lines = [ln for ln in lines if not ln.is_optional]
    else:
        table_lines = [ln for ln in lines if not ln.is_optional or ln.is_selected]
    return render(
        request,
        "offers/public.html",
        {
            "quote": quote,
            "site_settings": SiteSettings.load(),
            "totals": quote.totals(),
            "base": _base_totals(lines),
            "table_lines": table_lines,
            "optional_lines": [ln for ln in lines if ln.is_optional] if answerable else [],
            "attachments": list(quote.attachments.all()),
            "question_sent": request.GET.get("fraga") == "tack",
            "question_failed": request.GET.get("fraga") == "fel",
        },
    )


def _render_accept(request, quote, form, chosen):
    """Acceptsidan: sammanställning av det valda + beställarens uppgifter."""
    lines = list(quote.lines.all())
    included = [ln for ln in lines if not ln.is_optional or ln.pk in chosen]
    sums = {p.value: 0 for p in PricePeriod}
    for line in included:
        sums[line.period] += line.price
    from .models import format_kr

    return render(
        request,
        "offers/accept.html",
        {
            "quote": quote,
            "site_settings": SiteSettings.load(),
            "form": form,
            "included": included,
            "chosen": sorted(chosen),
            "sums": {key: format_kr(value) for key, value in sums.items()},
            "raw_sums": sums,
            "client_ip": client_ip(request),
        },
    )


def offer_accept(request, token):
    """GET: acceptsidan. POST: accepten själv."""
    quote = _get_quote(token)
    if not quote.is_answerable():
        return redirect("offers:public", token=token)
    valid_optional = set(quote.lines.filter(is_optional=True).values_list("pk", flat=True))
    chosen = _chosen_ids(request) & valid_optional

    if request.method != "POST":
        form = AcceptForm(initial={"email": quote.customer_email, "company": quote.customer_name})
        return _render_accept(request, quote, form, chosen)

    form = AcceptForm(request.POST)
    if not form.is_valid():
        return _render_accept(request, quote, form, chosen)

    # Villkorad UPDATE gör accepten atomär: ett dubbelklick (eller två
    # samtidiga POST) ger exakt EN statusövergång och exakt ETT mejl -
    # bara den request vars UPDATE träffade en rad skickar notisen.
    # Kundens tillvalsval och uppgifter skrivs i samma transaktion, och
    # bara av den request som vann övergången - en förlorad request får
    # inte ändra vad som redan beställts.
    with transaction.atomic():
        updated = Quote.objects.filter(
            pk=quote.pk, status__in=(QuoteStatus.SENT, QuoteStatus.OPENED)
        ).update(
            status=QuoteStatus.ACCEPTED,
            accepted_at=timezone.now(),
            accepted_ip=client_ip(request),
            accepted_user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
            updated_at=timezone.now(),
            **form.as_quote_fields(),
        )
        if updated:
            options = QuoteLine.objects.filter(quote=quote, is_optional=True)
            options.filter(pk__in=chosen).update(is_selected=True)
            options.exclude(pk__in=chosen).update(is_selected=False)
    if updated:
        quote.refresh_from_db()
        send_accepted_notification(quote)
    return redirect("offers:public", token=token)


def offer_question(request, token):
    if request.method != "POST":
        return redirect("offers:public", token=token)
    quote = _get_quote(token)
    message = request.POST.get("message", "").strip()[:5000]
    if not message:
        return redirect("offers:public", token=token)
    if send_question_to_staff(quote, message):
        messages.success(request, "Frågan är skickad.")
        return redirect(quote.get_public_url() + "?fraga=tack")
    # Mejlet gick inte iväg - säg det ärligt i stället för att kvittera
    # en fråga som aldrig kom fram.
    return redirect(quote.get_public_url() + "?fraga=fel")


def offer_attachment(request, token, pk):
    """Bilagan lämnas ut mot offertens token - ingen annan adress finns."""
    quote = _get_quote(token)
    attachment = get_object_or_404(QuoteAttachment, pk=pk, quote=quote)
    inline = attachment.content_type == "application/pdf"
    return FileResponse(
        attachment.file.open("rb"), as_attachment=not inline, filename=attachment.original_name
    )
