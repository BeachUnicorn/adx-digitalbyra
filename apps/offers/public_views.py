"""
Kundens offertsida: /offert/<token>/.

Länken är behörigheten. Ingen inloggning, inget id i adressen - bara en
slumpad token med 190+ bitar entropi. Sidan är noindex och /offert/ är
spärrad i robots.txt; en offert är en affärshandling, inte innehåll.

Öppnad-spårningen: första GET från någon som inte är inloggad flyttar
statusen skickad -> öppnad. Giovannis egna förhandstittar (inloggad i
/manage/ i samma webbläsare) ska inte se ut som att kunden öppnat.
"""

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.website.models import SiteSettings

from .emails import send_accepted_notification, send_question_to_staff
from .models import PricePeriod, Quote, QuoteLine, QuoteStatus


def _get_quote(token):
    return get_object_or_404(Quote.objects.prefetch_related("lines"), token=token)


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
    mandatory = [ln for ln in lines if not ln.is_optional]
    base = {p.value: 0 for p in PricePeriod}
    for line in mandatory:
        base[line.period] += line.price
    return render(
        request,
        "offers/public.html",
        {
            "quote": quote,
            "site_settings": SiteSettings.load(),
            "totals": quote.totals(),
            "base": base,
            "table_lines": table_lines,
            "optional_lines": [ln for ln in lines if ln.is_optional] if answerable else [],
            "question_sent": request.GET.get("fraga") == "tack",
            "question_failed": request.GET.get("fraga") == "fel",
        },
    )


@require_POST
def offer_accept(request, token):
    quote = _get_quote(token)
    chosen = {int(raw) for raw in request.POST.getlist("tillval") if str(raw).isdigit()}
    # Villkorad UPDATE gör accepten atomär: ett dubbelklick (eller två
    # samtidiga POST) ger exakt EN statusövergång och exakt ETT mejl -
    # bara den request vars UPDATE träffade en rad skickar notisen.
    # Kundens tillvalsval skrivs i samma transaktion, och bara av den
    # request som vann övergången - en förlorad request får inte ändra
    # vad som redan beställts.
    with transaction.atomic():
        updated = Quote.objects.filter(
            pk=quote.pk, status__in=(QuoteStatus.SENT, QuoteStatus.OPENED)
        ).update(
            status=QuoteStatus.ACCEPTED,
            accepted_at=timezone.now(),
            accepted_ip=request.META.get("REMOTE_ADDR"),
            accepted_user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
            updated_at=timezone.now(),
        )
        if updated:
            options = QuoteLine.objects.filter(quote=quote, is_optional=True)
            options.filter(pk__in=chosen).update(is_selected=True)
            options.exclude(pk__in=chosen).update(is_selected=False)
    if updated:
        quote.refresh_from_db()
        send_accepted_notification(quote)
    return redirect("offers:public", token=token)


@require_POST
def offer_question(request, token):
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
