"""
Byråns del av inkommande sms (README G.1 punkt 1, 4 och 5, I.1).

    panel_context(now)           kontexten för översiktens del
                                 manage/utskick/_overview_inbound.html
                                 (#inkommande; nycklarna börjar med "inbound")
    inbound_route(request, pk)   /manage/utskick/inkommande/<pk>/ (POST)
                                 manage:utskick_inbound_route: koppla ett
                                 väntande sms (ambiguous eller unroutable) till
                                 en kund, eller lägg det åt sidan

Väntande sms syns för ingen kund förrän byrån kopplat dem. Byrån ser numret
maskerat, texten och kunderna som skickat dit; kopplingen görs som ett
vanligt svar (eller en STOPP hos just den kunden) och loggas med vem som
gjorde den. Bara kunder som skickat från svarsnumret till numret kan väljas
(aldrig demokontot): annars kunde en kund få en tråd, och en kontakt, med
någon som aldrig fått sms från den. Kunden mejlas aldrig.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.flamingo.models import FlamingoAccount
from apps.projects.access import staff_required
from apps.sms.models import SmsMessage

from . import normalize
from .inbound import elks, routing
from .models import CHANNEL_SMS, InboundMessage, Switchboard, ThreadMessage, UtskickSettings
from .threads import answers_queued

#: Så många väntande sms visas (de äldsta efter).
HELD_SHOWN = 50
#: Texten på ett väntande sms kortas så här i listan.
TEXT_SHOWN = 300


def _senders_to(e164):
    """Kontona (FlamingoAccount pk) som någon gång skickat från svarsnumret
    till numret."""
    if not e164:
        return set()
    customers = (
        SmsMessage.objects.filter(to=e164, sender=routing.reply_number())
        .values_list("account__customer_id", flat=True)
        .distinct()
    )
    accounts = FlamingoAccount.objects.filter(customer_id__in=customers)
    return set(accounts.values_list("pk", flat=True))


def _choices(candidate_pks, e164=""):
    """Kunderna byrån kan koppla till: de som skickat till numret de senaste
    30 dagarna först (routningens kandidater), sedan de som gjort det
    tidigare. Aldrig demokontot, aldrig en kund utan utskick."""
    allowed = set(candidate_pks) | _senders_to(e164)
    rows = UtskickSettings.objects.filter(
        account_id__in=allowed, account__is_demo=False
    ).select_related("account__customer")
    names = {row.account_id: row.display_name or row.account.customer.name for row in rows}
    first = [(pk, names[pk]) for pk in candidate_pks if pk in names]
    rest = sorted(
        ((pk, name) for pk, name in names.items() if pk not in candidate_pks),
        key=lambda item: item[1].lower(),
    )
    return first, rest


def panel_context(now):
    """Översiktens del: väntande sms, avstämningen och svaren som inte gick."""
    held = list(
        InboundMessage.objects.filter(channel=CHANNEL_SMS, status__in=routing.HELD).order_by(
            "-received_at", "-pk"
        )[:HELD_SHOWN]
    )
    rows = []
    for message in held:
        meta = message.meta or {}
        candidate_pks = [pk for pk in meta.get("candidates", []) if isinstance(pk, int)]
        first, rest = _choices(candidate_pks, message.from_address)
        rows.append(
            {
                "message": message,
                "masked": normalize.mask_phone(message.from_address),
                "text": message.body[:TEXT_SHOWN],
                "is_stop": meta.get("keyword") == "stop",
                "candidates": first,
                "others": rest,
            }
        )
    week = now - timedelta(days=7)
    switch = Switchboard.get_solo()
    return {
        "inbound_held": rows,
        "inbound_held_count": InboundMessage.objects.filter(
            channel=CHANNEL_SMS, status__in=routing.HELD
        ).count(),
        "inbound_on": bool(getattr(settings, "UTSKICK_ELKS_INBOUND_TOKEN", "")),
        "inbound_ips": bool(elks._allowed_ips()),
        "inbound_last_reconcile": switch.last_elks_reconcile_at,
        "inbound_week": InboundMessage.objects.filter(
            channel=CHANNEL_SMS, received_at__gte=week
        ).count(),
        "inbound_stops_week": InboundMessage.objects.filter(
            channel=CHANNEL_SMS, status=InboundMessage.Status.STOP, received_at__gte=week
        ).count(),
        "inbound_answers_queued": answers_queued().count(),
        "inbound_answers_failed_week": ThreadMessage.objects.filter(
            inbound__isnull=False,
            direction=ThreadMessage.Direction.OUT,
            status=ThreadMessage.Status.FAILED,
            at__gte=week,
        ).count(),
    }


def _back():
    return redirect(reverse("manage:utskick_overview") + "#inkommande")


@staff_required
@require_POST
def inbound_route(request, pk):
    """Koppla ett väntande sms till en kund (action=route, account=<pk>) eller
    lägg det åt sidan (action=ignore)."""
    message = get_object_or_404(InboundMessage, pk=pk, channel=CHANNEL_SMS)
    action = request.POST.get("action", "")
    now = timezone.now()
    if action == "ignore":
        try:
            routing.ignore_held(message.pk, user=request.user, now=now)
        except routing.NotHeld:
            messages.info(request, "Sms:et är redan hanterat.")
            return _back()
        messages.success(request, "Sms:et är lagt åt sidan. Ingen kund ser det.")
        return _back()
    if action != "route":
        messages.error(request, "Okänd åtgärd.")
        return _back()
    raw = str(request.POST.get("account") or "").strip()
    account = None
    candidate_pks = [pk for pk in (message.meta or {}).get("candidates", []) if isinstance(pk, int)]
    first, rest = _choices(candidate_pks, message.from_address)
    allowed = {pk for pk, _name in (*first, *rest)}
    if raw.isdigit() and int(raw) in allowed:
        account = (
            FlamingoAccount.objects.filter(pk=int(raw), utskick__isnull=False, is_demo=False)
            .select_related("customer")
            .first()
        )
    if account is None:
        messages.error(request, "Välj en av kunderna som har skickat sms till numret.")
        return _back()
    try:
        routing.route_held(message.pk, account, user=request.user, now=now)
    except routing.NotHeld:
        messages.info(request, "Sms:et är redan hanterat.")
        return _back()
    messages.success(
        request, f"Sms:et är kopplat till {account.customer.name} och syns i Inkorgen."
    )
    return _back()
