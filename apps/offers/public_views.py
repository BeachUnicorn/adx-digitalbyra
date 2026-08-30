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
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.website.models import SiteSettings

from .emails import send_accepted_notification, send_question_to_staff
from .models import PricePeriod, Quote, QuoteStatus


def _get_quote(token):
    return get_object_or_404(Quote.objects.prefetch_related("lines"), token=token)


def offer_public(request, token):
    quote = _get_quote(token)
    if not request.user.is_authenticated:
        quote.mark_opened()
    lines = list(quote.lines.all())
    return render(
        request,
        "offers/public.html",
        {
            "quote": quote,
            "site_settings": SiteSettings.load(),
            "totals": quote.totals(),
            "one_time_lines": [ln for ln in lines if ln.period == PricePeriod.ONE_TIME],
            "recurring_lines": [ln for ln in lines if ln.period != PricePeriod.ONE_TIME],
            "question_sent": request.GET.get("fraga") == "tack",
            "question_failed": request.GET.get("fraga") == "fel",
        },
    )


@require_POST
def offer_accept(request, token):
    quote = _get_quote(token)
    # Villkorad UPDATE gör accepten atomär: ett dubbelklick (eller två
    # samtidiga POST) ger exakt EN statusövergång och exakt ETT mejl -
    # bara den request vars UPDATE träffade en rad skickar notisen.
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
