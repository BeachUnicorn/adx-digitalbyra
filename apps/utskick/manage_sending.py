"""
Byråns del av sändningen (README D.4, D.9, H.5, I.1, J S2 steg 6), på
/manage/utskick/ (#sandning).

    panel_context(now)           översiktens del (manage/utskick/_overview_sending.html):
                                 nödbromsen, pågående och pausade utskick, kön,
                                 informationsutskicken och provsms
    info_override(request, pk)   /manage/utskick/utskick/<pk>/undantag/ (POST),
                                 manage:utskick_info_override: byråns undantag från
                                 reglerna för information (H.5), med skäl; loggas.
                                 Aldrig kunden. Undantaget släpper bara reklamorden
                                 i texten det gavs för, aldrig länkarna eller
                                 fältvärdena (checks.information_problems)
    probe(request)               /manage/utskick/prov/ (POST), manage:utskick_probe:
                                 "Provsms till mig" från svarsnumret till ett nummer
                                 byrån skriver (J S2 steg 6), också före
                                 Switchboard.sms_enabled

Vem som betalar provsms:et: en kund som byrån väljer i formuläret, bland
kunderna med aktiverat SMS (inte demokontot). Välj ADX egen interna
testkund; sms:et står på dess underlag med källan test. Svar på provsms:et
routas till den kundens Inkorg (den skickade senast från numret dit). Högst
tio provsms per byråanvändare och timme. Kunden mejlas aldrig.
"""

import logging
import secrets
import time
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Min, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.projects.access import staff_required
from apps.sms import numbers, service
from apps.sms.models import SmsAccount

from . import limits
from .models import INFORMATION, Recipient, Switchboard, Utskick
from .sending import checks, sms_wrapper

logger = logging.getLogger(__name__)

#: Högst så här många rader per tabell i översikten.
ROWS = 50
#: Provsms per byråanvändare och timme.
PROBES_PER_HOUR = 10
REASON_MAX = 200

PROBE_TEXT = (
    "Provsms från ADX Flamingo för {name}. Svara på sms:et för att prova Inkorgen, "
    "eller svara STOPP och sedan START."
)
OVERRIDE_NEEDS_REASON = "Skriv varför undantaget behövs."
OVERRIDE_LOCKED = "Undantag går bara för informationsutskick som inte har skickats."
PROBE_BAD_NUMBER = "Numret går inte att tolka. Skriv ett mobilnummer."
PROBE_NO_PAYER = "Välj en kund med aktiverat SMS som betalar provsms:et."
PROBE_LIMIT = "Du har skickat tio provsms den här timmen. Vänta en stund."


def _back(anchor="sandning"):
    return redirect(reverse("manage:utskick_overview") + "#" + anchor)


def _actor_label(user):
    first = (user.first_name or user.get_username()).strip()
    return f"ADX ({first})"[:120]


def probe_payers():
    """Kunderna som kan betala ett provsms: aktiverat SMS och ett
    Flamingo-konto som inte är demokontot. [(customer_pk, namn)]."""
    return list(
        SmsAccount.objects.filter(is_enabled=True)
        .filter(customer__flamingo__isnull=False, customer__flamingo__is_demo=False)
        .order_by("customer__name")
        .values_list("customer_id", "customer__name")[:200]
    )


def panel_context(now):
    """Översiktens del för sändningen. Nycklarna börjar med "sending"."""
    switch = Switchboard.get_solo()
    breaker = switch.sms_paused_until if checks.breaker_active(row=switch) else None
    R = Recipient.Status
    rows = list(
        Utskick.objects.listed()
        .filter(status__in=(*Utskick.ACTIVE, *Utskick.PAUSED_STATES))
        .select_related("account__customer")
        .annotate(
            n_queued=Count("recipients", filter=Q(recipients__status=R.QUEUED)),
            n_sent=Count("recipients", filter=Q(recipients__status__in=Recipient.SENT_LIKE)),
            n_failed=Count("recipients", filter=Q(recipients__status=R.FAILED)),
            next_at=Min("recipients__not_before", filter=Q(recipients__status=R.QUEUED)),
        )
        .order_by("status", "-status_changed_at")[:ROWS]
    )
    for row in rows:
        row.label = row.get_pause_reason_display() if row.is_paused else row.get_status_display()
        row.note = ((row.stats or {}).get("pause") or {}).get("note", "")
    info = list(
        Utskick.objects.listed()
        .filter(purpose=INFORMATION, created_at__gte=now - timedelta(days=30))
        .select_related("account__customer")
        .annotate(n_recipients=Count("recipients", filter=~Q(recipients__status=R.SKIPPED)))
        .order_by("-created_at")[:ROWS]
    )
    for row in info:
        override = row.content_override if isinstance(row.content_override, dict) else {}
        row.override_reason = override.get("reason", "")
        row.override_by = override.get("by", "")
        row.override_stale = bool(row.override_reason) and not checks.override_valid(row)
        # S3 (sändnings-byggaren): mejlets text kan också ha ändrats.
        if row.override_reason and not row.override_stale and row.has_email:
            from .email import checks as email_checks

            row.override_stale = override.get("email_fingerprint") != (
                email_checks.email_fingerprint(row)
            )
        # --- slut S3
        row.can_override = row.status in (*Utskick.EDITABLE, *Utskick.PAUSED_STATES)
    return {
        "sending_breaker_until": breaker,
        "sending_sms_on": switch.sms_enabled,
        "sending_rows": rows,
        "sending_paused": sum(1 for r in rows if r.is_paused),
        "sending_info": info,
        "sending_payers": probe_payers(),
        "sending_reply_number": checks.reply_number(),
    }


@staff_required
@require_POST
def info_override(request, pk):
    """Byråns undantag från reglerna för information (H.5): skälet sparas på
    utskicket med vem och när, och loggas. action=remove tar bort det."""
    utskick = get_object_or_404(Utskick, pk=pk)
    back = _back("sandning-information")
    if utskick.purpose != INFORMATION or utskick.status not in (
        *Utskick.EDITABLE,
        *Utskick.PAUSED_STATES,
    ):
        messages.error(request, OVERRIDE_LOCKED)
        return back
    if request.POST.get("action") == "remove":
        Utskick.objects.filter(pk=utskick.pk).update(content_override={})
        logger.warning(
            "Utskick %s: byråns undantag för information togs bort av användare %s",
            utskick.pk,
            request.user.pk,
        )
        messages.success(request, "Undantaget är borttaget.")
        return back
    reason = " ".join(str(request.POST.get("reason") or "").split())[:REASON_MAX]
    if not reason:
        messages.error(request, OVERRIDE_NEEDS_REASON)
        return back
    # Undantaget gäller texten som står nu (checks.content_fingerprint):
    # ändrar kunden texten, reservtexterna eller skälet gäller det inte längre.
    override = {
        "by": _actor_label(request.user),
        "user": request.user.pk,
        "at": timezone.now().isoformat(),
        "reason": reason,
        "fingerprint": checks.content_fingerprint(utskick),
    }
    # S3 (sändnings-byggaren, renderarens begäran 1): mejlets innehåll har
    # ett eget fingeravtryck (email.checks.email_fingerprint).
    if utskick.has_email:
        from .email import checks as email_checks

        override["email_fingerprint"] = email_checks.email_fingerprint(utskick)
    # --- slut S3
    Utskick.objects.filter(pk=utskick.pk).update(content_override=override)
    logger.warning(
        "Utskick %s: byråns undantag för information av användare %s",
        utskick.pk,
        request.user.pk,
    )
    messages.success(
        request,
        "Undantaget är sparat. Kunden granskar och bekräftar utskicket igen. Kunden har "
        "inte mejlats.",
    )
    return back


@staff_required
@require_POST
def probe(request):
    """Provsms till mig (J S2 steg 6): från svarsnumret till numret byrån
    skriver, betalt av kunden som väljs, källa test. Går före
    Switchboard.sms_enabled men inte förbi nödbromsen."""
    from apps.flamingo.models import FlamingoAccount

    # S3 (sändnings-byggaren): "Provmejl till mig" postar hit med kind=email
    # (J S3 steg 7); resten av vyn är provsms:et.
    if request.POST.get("kind") == "email":
        from .manage_email import probe_email

        return probe_email(request)
    # --- slut S3
    back = _back("sandning-prov")
    try:
        number = numbers.parse(str(request.POST.get("to") or ""))
    except numbers.InvalidNumber:
        messages.error(request, PROBE_BAD_NUMBER)
        return back
    payer = str(request.POST.get("customer") or "")
    allowed = dict(probe_payers())
    if not payer.isdigit() or int(payer) not in allowed:
        messages.error(request, PROBE_NO_PAYER)
        return back
    account = FlamingoAccount.objects.filter(customer_id=int(payer)).first()
    if account is None or account.is_demo:
        messages.error(request, PROBE_NO_PAYER)
        return back
    if checks.breaker_active():
        messages.error(request, checks.BREAKER_TEXT)
        return back
    if limits.hit("probe", str(request.user.pk), limits.hour_window(), PROBES_PER_HOUR):
        messages.error(request, PROBE_LIMIT)
        return back
    from .access import settings_for

    name = settings_for(account).display_name or allowed[int(payer)]
    reference = f"p{int(time.time())}{secrets.token_hex(3)}"
    try:
        outcome = sms_wrapper.send(
            account,
            to=number.e164,
            body=PROBE_TEXT.format(name=name),
            sender=checks.reply_number(),
            source="test",
            reference=reference,
        )
    except sms_wrapper.DemoRefused:
        messages.error(request, sms_wrapper.DEMO_TEXT)
        return back
    logger.warning(
        "Provsms från svarsnumret av användare %s, betalt av konto %s: %s",
        request.user.pk,
        account.pk,
        outcome.error or ("oklart" if outcome.unknown else "skickat"),
    )
    if outcome.unknown:
        messages.warning(
            request,
            "46elks svarade oklart. Sms:et kan ha skickats; stäm av det på /manage/sms/.",
        )
    elif outcome.ok:
        messages.success(
            request,
            f"Provsms skickat från {checks.reply_number()}, betalt av {allowed[int(payer)]}. "
            "Svara på det för att prova Inkorgen.",
        )
    else:
        messages.error(
            request,
            service.ERROR_TEXTS.get(outcome.error, "Provsms:et gick inte att skicka."),
        )
    return back
