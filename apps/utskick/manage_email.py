"""
Byråns del av e-posten (README I.1 manage-tabellen, D.7, D.9, B.3, J S3).

    health_release(request, pk)   /manage/utskick/konto/<pk>/halsa/ (POST)
                                  manage:utskick_health_release: "Släpp spärren"
                                  för kontot pk (FlamingoAccount), med en anteckning
                                  i loggen. De pausade utskicken står kvar; byrån
                                  fortsätter dem ett i taget från rapporten.
    domain_admin(request, pk)     /manage/utskick/doman/<pk>/ (GET, POST)
                                  manage:utskick_domain_admin: domänens poster och läge,
                                  SES-ögonblicksbilden, "Kontrollera igen" och "Ta bort"
                                  (hjälpen åt kunden, J S3 "Be ADX om hjälp")
    dlq(request)                  /manage/utskick/koer/ (GET, POST action=redrive)
                                  manage:utskick_dlq: köernas och DLQ:ernas antal och
                                  "Skicka tillbaka" (StartMessageMoveTask)
    panel_context(now) -> dict    översiktens del (manage/utskick/_overview_email.html):
                                  SES-kontot (Switchboard.ses_account), takten, köerna,
                                  kontona med hälsospärr, domänerna och provmejlet

Allt bakom staff_required; kunden mejlas aldrig härifrån. "Provmejl till
mig" (J S3 steg 7) ligger i manage_sending.probe (manage:utskick_probe)
bredvid provsms:et, med formuläret i _overview_email.html.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from apps.projects.access import staff_required

from .email import domains, transport
from .inbound import queues
from .models import SenderDomain, Switchboard, UtskickSettings
from .sending import email as email_loop
from .sending import health

logger = logging.getLogger(__name__)

#: Högst så här många rader per tabell i översikten.
ROWS = 50
#: Anteckningen vid "Släpp spärren".
NOTE_MAX = 200


def _back(anchor="epost"):
    return redirect(reverse("manage:utskick_overview") + "#" + anchor)


def _rate_text(snapshot):
    if not snapshot:
        return ""
    rate = snapshot.get("MaxSendRate")
    quota = snapshot.get("Max24HourSend")
    sent = snapshot.get("SentLast24Hours")
    parts = []
    if rate is not None:
        parts.append(f"{rate:g} i sekunden")
    if quota is not None:
        parts.append(f"{quota:,.0f} per dygn".replace(",", " "))
    if sent is not None:
        parts.append(f"{sent:,.0f} skickade senaste dygnet".replace(",", " "))
    return ", ".join(parts)


def probe_senders():
    """Kunderna ett provmejl kan skickas från: utskick aktiverat och inte
    demokontot. [(customer_pk, namn)]."""
    return list(
        UtskickSettings.objects.filter(is_enabled=True, account__is_demo=False)
        .exclude(account__customer__isnull=True)
        .order_by("account__customer__name")
        .values_list("account__customer_id", "account__customer__name")[:200]
    )


def panel_context(now):
    """Översiktens e-postdel. Nycklarna börjar med "email"."""
    switch = Switchboard.get_solo()
    snapshot = switch.ses_account if isinstance(switch.ses_account, dict) else {}
    blocked = list(
        UtskickSettings.objects.filter(email_blocked_at__isnull=False)
        .select_related("account__customer")
        .order_by("-email_blocked_at")[:ROWS]
    )
    for row in blocked:
        row.verdict = health.account_health(row.account, now)
    domain_rows = list(
        SenderDomain.objects.filter(
            status__in=(
                SenderDomain.Status.PENDING,
                SenderDomain.Status.VERIFIED,
                SenderDomain.Status.FAILED,
            )
        )
        .select_related("account__customer")
        .order_by("status", "-created_at")[:ROWS]
    )
    for row in domain_rows:
        row.dns_ok = domains.dns_ok(row)
        row.expires_at = row.created_at + timedelta(days=SenderDomain.PENDING_DAYS)
    own = health.adx_numbers(now)
    warnings = _warnings(switch, snapshot, own, now)
    return {
        "email_warnings": warnings,
        "email_switch_on": switch.email_enabled,
        "email_live_setting": bool(getattr(settings, "UTSKICK_EMAIL_LIVE", False)),
        "email_ses": snapshot,
        "email_ses_checked_at": switch.ses_checked_at,
        "email_ses_rate_text": _rate_text(snapshot),
        "email_rate": email_loop.rate_for(switch),
        "email_config_set": transport.configuration_set(),
        "email_events_on": queues.events_enabled(),
        "email_inbound_on": queues.inbound_enabled(),
        "email_last_poll": switch.last_queue_poll_at,
        "email_blocked": blocked,
        "email_domains": domain_rows,
        "email_own": own,
        "email_own_bounce": round(own.bounce_rate * 100, 2),
        "email_own_complaint": round(own.complaint_rate * 100, 3),
        "email_adx_cap": email_loop.adx_cap(),
        "email_probe_payers": probe_senders(),
    }


def _warnings(switch, snapshot, own, now):
    """Det byrån ska se först på /manage/utskick/#epost (S3, integrationen):
    en DLQ som larmat i dag, SES som inte är friskt, ADX egna tal nära AWS
    gränser, och e-postutskick som är påslagna utan händelsekön (ticken
    väntar då med e-posten). Bara texter; inget anrop till AWS här."""
    from . import limits

    out = []
    if limits.count("alert", "dlq", limits.day_window(now)):
        out.append(
            "En DLQ hade meddelanden vid dygnskörningen i dag. Se köerna och loggen, och "
            "skicka tillbaka när felet är rättat."
        )
    if snapshot and "error" not in snapshot:
        status = str(snapshot.get("EnforcementStatus") or "").upper()
        if status and status != "HEALTHY":
            out.append(f"SES i eu-west-1 har läget {status}.")
        if snapshot.get("SendingEnabled") is False:
            out.append("SES har stängt av sändningen för kontot i eu-west-1.")
    if own.outcomes and own.bounce_rate >= health.ADX_ALERT_BOUNCE:
        out.append(
            f"Studsarna de senaste 30 dagarna är {round(own.bounce_rate * 100, 2)} %, "
            "nära AWS gräns på 5 %."
        )
    if own.complained and own.complaint_rate >= health.ADX_ALERT_COMPLAINT:
        out.append(
            f"Klagomålen de senaste 30 dagarna är {round(own.complaint_rate * 100, 3)} %, "
            "nära AWS gräns på 0,1 %."
        )
    if switch.email_enabled and not queues.events_enabled() and not settings.DEBUG:
        out.append(
            "E-postutskick är påslagna men händelsekön saknas (UTSKICK_SQS_EVENTS_URL): "
            "e-postutskicken väntar tills den är satt."
        )
    return out


@staff_required
@require_POST
def health_release(request, pk):
    """Släpp kontots e-postspärr (D.9). Bara utfall efter släppet räknas i
    kontots hälsa; de pausade utskicken fortsätter byrån själv."""
    from apps.flamingo.models import FlamingoAccount

    account = get_object_or_404(FlamingoAccount, pk=pk)
    note = " ".join(str(request.POST.get("note") or "").split())[:NOTE_MAX]
    if health.release(account, actor=request.user):
        logger.warning(
            "Konto %s: e-postspärren släppt av användare %s (%s)",
            account.pk,
            request.user.pk,
            "med anteckning" if note else "utan anteckning",
        )
        messages.success(
            request,
            "Spärren är släppt. De pausade utskicken fortsätter du från rapporten. "
            "Kunden har inte mejlats.",
        )
    else:
        messages.info(request, "Kontot hade ingen e-postspärr.")
    back = str(request.POST.get("back") or "")
    if back == "kund" and account.customer_id:
        card = reverse("manage:customer_detail", args=[account.customer_id])
        return redirect(card + "#utskick")
    return _back()


@staff_required
@require_http_methods(["GET", "HEAD", "POST"])
def domain_admin(request, pk):
    """En kunds avsändardomän: posterna, läget och SES (J S3, "Be ADX om
    hjälp"). POST action=check kontrollerar igen, action=remove tar bort."""
    row = get_object_or_404(SenderDomain.objects.select_related("account__customer"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "check":
            row = domains.check(row)
            messages.info(request, f"Kontrollerad: {row.get_status_display()}.")
        elif action == "remove":
            try:
                domains.remove(row, user=request.user)
            except domains.DomainRefused as exc:
                messages.error(request, exc.text)
            else:
                messages.success(request, "Domänen är borttagen. Kunden har inte mejlats.")
        return redirect(reverse("manage:utskick_domain_admin", args=[row.pk]))
    checks = row.checks if isinstance(row.checks, dict) else {}
    records = []
    for record in domains.records(row):
        found = checks.get(record.purpose) or {}
        records.append(
            {"record": record, "state": found.get("state", ""), "seen": found.get("seen", "")}
        )
    return render(
        request,
        "manage/utskick/domain.html",
        {
            "active": "flamingo",
            "title": f"Domän {row.domain}",
            "row": row,
            "records": records,
            "snapshot": row.ses_snapshot if isinstance(row.ses_snapshot, dict) else {},
            "expires_at": row.created_at + timedelta(days=SenderDomain.PENDING_DAYS),
        },
    )


@staff_required
@require_http_methods(["GET", "HEAD", "POST"])
def dlq(request):
    """Köerna och DLQ:erna (D.7). "Skicka tillbaka" flyttar en DLQ till sin kö."""
    if request.method == "POST":
        which = str(request.POST.get("which") or "")
        if request.POST.get("action") == "redrive" and which in dict(queues.QUEUES):
            text = queues.redrive(which)
            logger.warning("Utskick: DLQ %s tillbaka av användare %s", which, request.user.pk)
            messages.info(request, text)
        return redirect(reverse("manage:utskick_dlq"))
    counts = queues.dlq_counts()
    rows = []
    for which, setting in queues.QUEUES:
        url = queues.queue_url(which)
        rows.append(
            {
                "which": which,
                "label": "SES-händelser" if which == "events" else "Svar på mejl",
                "setting": setting,
                "on": bool(url),
                "queued": counts.get(which),
                "dlq": counts.get(f"{which}_dlq"),
            }
        )
    switch = Switchboard.get_solo()
    return render(
        request,
        "manage/utskick/queues.html",
        {
            "active": "flamingo",
            "title": "Köerna för e-post",
            "rows": rows,
            "last_poll": switch.last_queue_poll_at,
        },
    )


# ---------------------------------------------------------------------------
# Provmejl till mig (J S3 steg 7), anropat från manage_sending.probe
# ---------------------------------------------------------------------------

PROBES_PER_HOUR = 10
PROBE_BAD_ADDRESS = "Skriv en giltig e-postadress."
PROBE_NO_SENDER = "Välj kunden som mejlet ska komma från."


def probe_email(request):
    """Provmejl till mig: från den valda kundens avsändare på ADX-domänen
    till adressen byrån skriver, också före Switchboard.email_enabled (men
    bara med UTSKICK_EMAIL_LIVE). Högst tio provsms och provmejl per
    byråanvändare och timme. Kunden mejlas inte."""
    from apps.flamingo.models import FlamingoAccount

    from . import limits, normalize

    back = _back("epost-prov")
    try:
        address = normalize.email(request.POST.get("to"))
    except normalize.InvalidValue:
        address = ""
    if not address:
        messages.error(request, PROBE_BAD_ADDRESS)
        return back
    customer = str(request.POST.get("customer") or "")
    allowed = dict(probe_senders())
    if not customer.isdigit() or int(customer) not in allowed:
        messages.error(request, PROBE_NO_SENDER)
        return back
    account = FlamingoAccount.objects.filter(customer_id=int(customer)).first()
    if account is None or account.is_demo:
        messages.error(request, PROBE_NO_SENDER)
        return back
    if limits.hit("probe", str(request.user.pk), limits.hour_window(), PROBES_PER_HOUR):
        messages.error(request, "Du har skickat tio prov den här timmen. Vänta en stund.")
        return back
    first = (request.user.first_name or request.user.get_username()).strip()
    mail = email_loop.probe_mail(account, address, staff_name=f"ADX ({first})")
    from . import keys

    try:
        sent = email_loop.deliver(account, mail, kind=transport.PROBE)
    except keys.KeyMismatch:
        messages.error(request, email_loop.ERROR_TEXTS["keys"])
        return back
    logger.warning(
        "Provmejl av användare %s från konto %s: %s",
        request.user.pk,
        account.pk,
        "skickat" if sent.ok else ("oklart" if sent.unknown else sent.error),
    )
    if sent.ok:
        messages.success(
            request,
            f"Provmejl skickat till {address} från {mail.from_addr}. Svara på det för att "
            "prova Inkorgen, och prova Avsluta prenumerationen i Gmail.",
        )
    elif sent.unknown:
        messages.warning(request, email_loop.error_text(sent))
    else:
        messages.error(request, email_loop.error_text(sent))
    return back
