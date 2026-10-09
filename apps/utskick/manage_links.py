"""
Byråns del av länkvärdarna (README E.8, I.1): externa webbplatser som en
kund vill länka till i ett utskick godkänns eller nekas här, på
/manage/utskick/ och på kundkortet. Kunden mejlas aldrig; appen visar
"Väntar på ADX" tills beslutet finns, och Granska blockerar.

    host_decide(request, pk)   /manage/utskick/vardar/<pk>/ (POST)
                               manage:utskick_host_decide: action approve eller
                               refuse, en anteckning (krävs för att neka) och
                               next (kundkortet eller översikten)
    panel_context(now)         översiktens del (manage/utskick/_overview_hosts.html):
                               hosts_pending, hosts_decided
    card_hosts(customer)       kundkortets rader (utskick_tags.utskick_hosts)

Ett beslut kan ändras (en nekad värd kan godkännas senare och tvärtom).
"""

from datetime import timedelta

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.projects.access import staff_required

from .models import AllowedHost

#: Beslut som visas på översikten efter att de fattats.
DECIDED_DAYS = 30
DECIDED_MAX = 20
NOTE_MAX = 200
REFUSE_NOTE_TEXT = "Skriv varför länken nekas (kunden ser inte anteckningen)."


def _row(host):
    customer = getattr(host.account, "customer", None)
    who = host.requested_by
    return {
        "host": host,
        "customer": customer,
        "requested_by": (who.get_full_name() or who.get_username()) if who else "",
        "decided_by": (host.decided_by.get_full_name() or host.decided_by.get_username())
        if host.decided_by
        else "",
    }


def panel_context(now):
    """Översikten: värdar som väntar (äldst först) och de senaste besluten."""
    base = AllowedHost.objects.select_related("account__customer", "requested_by", "decided_by")
    pending = base.filter(status=AllowedHost.Status.PENDING).order_by("requested_at", "pk")
    decided = base.filter(
        decided_at__gte=now - timedelta(days=DECIDED_DAYS),
        status__in=(AllowedHost.Status.APPROVED, AllowedHost.Status.REFUSED),
    ).order_by("-decided_at", "-pk")[:DECIDED_MAX]
    return {
        "hosts_pending": [_row(h) for h in pending],
        "hosts_decided": [_row(h) for h in decided],
    }


def card_hosts(customer):
    """Kundkortet: kundens väntande och nekade värdar (godkända syns inte)."""
    rows = (
        AllowedHost.objects.filter(account__customer=customer)
        .exclude(status=AllowedHost.Status.APPROVED)
        .select_related("account__customer", "requested_by", "decided_by")
        .order_by("status", "requested_at", "pk")
    )
    return [_row(h) for h in rows]


def _back(request, host):
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(reverse("manage:utskick_overview") + "#vardar")


@staff_required
@require_POST
def host_decide(request, pk):
    """Godkänn eller neka en extern länkvärd för en kund (E.8). Nekad: kunden
    ser "ADX har inte godkänt länkar till <värd>." i länkväljaren och i
    Granska. Kunden mejlas inte."""
    host = get_object_or_404(AllowedHost.objects.select_related("account__customer"), pk=pk)
    action = request.POST.get("action", "")
    note = str(request.POST.get("note", "") or "").strip()[:NOTE_MAX]
    if action not in ("approve", "refuse"):
        messages.error(request, "Okänd åtgärd.")
        return _back(request, host)
    if action == "refuse" and not note:
        messages.error(request, REFUSE_NOTE_TEXT)
        return _back(request, host)
    status = AllowedHost.Status.APPROVED if action == "approve" else AllowedHost.Status.REFUSED
    AllowedHost.objects.filter(pk=host.pk).update(
        status=status, decided_by=request.user, decided_at=timezone.now(), note=note
    )
    customer = getattr(host.account, "customer", None)
    name = getattr(customer, "name", "") or f"konto {host.account_id}"
    word = "godkända" if status == AllowedHost.Status.APPROVED else "nekade"
    messages.success(
        request, f"Länkar till {host.host} är {word} för {name}. Kunden har inte mejlats."
    )
    return _back(request, host)
