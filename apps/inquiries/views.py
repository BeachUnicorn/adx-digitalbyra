"""
Förfrågningsflödet: ETT kvalificerande formulär (inte VVS-arvets trestegs-
wizard - en byråkund ska kunna skicka på trettio sekunder).

Botskyddet svarar med TYST fejkad framgång: en fälld bot får exakt samma
redirect som en människa, med en äkta-formaterad referens som aldrig sparats
- den lär sig aldrig vilket lager som tog den (mönsterkatalogen §6).

Trafikattributionen snapshotas vid submit precis som tidigare: raden bär
källan även om analytics-raderna senare gallras.
"""

import logging

from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.common.botcheck import botcheck_passes

from .emails import send_inquiry_confirmation, send_inquiry_notification
from .forms import InquiryForm, NewsletterForm
from .models import Inquiry, NewsletterSignup, _generate_reference

logger = logging.getLogger(__name__)

SESSION_COOKIE = "adx_session"


def _analytics_session(request):
    """Analytics-sessionen för requesten (POST spåras inte av middleware,
    så cookien är enda vägen)."""
    from apps.analytics.models import Session

    session = getattr(request, "analytics_session", None)
    if session is not None:
        return session
    session_uuid = request.COOKIES.get(SESSION_COOKIE)
    if not session_uuid:
        return None
    return Session.objects.filter(uuid=session_uuid).first()


def _attach_attribution(inquiry, request):
    """Best effort - attribution får aldrig stoppa en förfrågan."""
    try:
        from apps.analytics.tracking import source_snapshot

        session = _analytics_session(request)
        if session is None:
            return
        inquiry.analytics_session = session
        snap = source_snapshot(session)
        inquiry.traffic_source = snap.get("source", "")
        inquiry.traffic_source_detail = snap.get("source_detail", "")
        inquiry.traffic_referrer = snap.get("referrer", "")
    except Exception:  # noqa: BLE001
        logger.exception("Kunde inte snapshotta attribution")


def inquiry_submit(request):
    """POST-mål för förfrågningsformuläret (blocket på kontaktsidan).

    GET skickas till kontaktsidan - formuläret bor där, inte här.
    Ogiltig POST renderas på en egen formulärsida med felen kvar.
    """
    if request.method != "POST":
        return redirect("/kontakt/")

    if not botcheck_passes(request):
        # Tyst fejkad framgång: äkta referensformat, ingen databasrad.
        return redirect("inquiries:thank_you", reference=_generate_reference())

    form = InquiryForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "inquiries/form_page.html",
            {"form": form, "page_color": "#7a2b35"},
        )

    inquiry = Inquiry(
        topic=form.cleaned_data["topic"],
        company_name=form.cleaned_data["company_name"],
        name=form.cleaned_data["name"],
        email=form.cleaned_data["email"],
        phone=form.cleaned_data["phone"],
        customer_type=form.cleaned_data.get("customer_type") or "company",
        budget=form.cleaned_data.get("budget", ""),
        timeline=form.cleaned_data.get("timeline", ""),
        description=form.cleaned_data["description"],
    )
    _attach_attribution(inquiry, request)
    inquiry.save()

    send_inquiry_confirmation(inquiry, request)
    send_inquiry_notification(inquiry, request)

    return redirect("inquiries:thank_you", reference=inquiry.reference)


def inquiry_thank_you(request, reference):
    """Tack-sidan. Slår medvetet INTE upp referensen i databasen: en fälld
    bots fejkreferens ska ge exakt samma sida som en äkta."""
    return render(
        request,
        "inquiries/thank_you.html",
        {"reference": reference, "page_color": "#7a2b35"},
    )


@require_POST
def newsletter_signup(request):
    """Nyhetsbrevsblockets POST-mål. Samma botskydd, samma tysta fejk."""
    next_url = "/paket/"
    if botcheck_passes(request):
        form = NewsletterForm(request.POST)
        if form.is_valid():
            NewsletterSignup.objects.get_or_create(
                email=form.cleaned_data["email"],
                defaults={"source_path": request.POST.get("next", "")[:200]},
            )
    return redirect(f"{next_url}?nyhetsbrev=tack")
