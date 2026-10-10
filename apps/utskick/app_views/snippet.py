"""
Spårningsskript (README I.1, I.9, E.6, J S4) under Inställningar för
utskick: domänen, taggen med Kopiera, provlänken och läget ("Installerat,
senast sett i går" eller "Inte sett än"). Länk-byggaren i S4.

    snippet_settings   utskick/installningar/skript/   GET, POST action=add | remove

Taggen kommer från links.snippet_tag(site) (adressen med versionen och
SRI-hashen). Högst SiteSnippet.MAX_PER_ACCOUNT domäner; domänen prövas av
site_snippet.clean_domain. Raden att ta bort kommer som id ur formuläret
och går genom owned_ids (ett främmande id ger 400, H.1).

En domän som inte redan är kundens egen eller godkänd (links.host_status)
begärs hos byrån som en ny värd (links.request_host, ett larm till byrån,
aldrig ett mejl till kunden): skriptets domäner gör inga länkar fria från
granskningen (S4-HANDOFF.md, avvikelse 2). Skriptet fungerar på domänen
ändå, men länkar dit i utskick och namngivna länkar väntar på ADX.

Skriptet läser adx= bara när adressen bär den: länkar får den först när
skriptet har setts (last_seen_at, E.3), och det första besöket kommer från
provlänken (site_snippet.install_url). Demokontot visar sidan men ändrar
inget (D12).
"""

import logging

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .. import links, site_snippet
from ..access import actor_for, owned_ids, utskick_view
from ..models import SiteSnippet
from . import render_utskick

logger = logging.getLogger(__name__)

DEMO_TEXT = "Demokontot visar bara hur sidan ser ut. Inget här ändras eller skickas."
ANALYTICS_TEXT = (
    "Uteslut parametern adx i din webbanalys (till exempel Google Analytics), "
    "så att den inte syns i dina rapporter."
)
#: Steget för adxFlamingo.track(namn): det enda sättet att få "Gjorde på
#: webbplatsen: bokning" på kontaktkortet (Event site_visit med "mal").
TRACK_TEXT = (
    "Vill du se när någon gör något på sidan som länken ledde till, till exempel "
    "skickar bokningsformuläret? Låt webbplatsen köra raden nedan just då. "
    "Kontaktkortet visar Gjorde på webbplatsen: bokning. Det räknas bara på sidan "
    "som länken öppnade, inte på en ny sida efter den. Namnet får ha små "
    "bokstäver, siffror, - och _."
)
TRACK_EXAMPLE = "adxFlamingo.track('bokning');"
#: När läget står kvar på Inte sett än efter provlänken.
TROUBLE_TEXTS = (
    "Följ provlänken till slutet och se att adressen fortfarande har ?adx= när "
    "sidan har laddat. En omdirigering som tar bort den, till exempel till en "
    "annan adress för startsidan, gör att skriptet inte ser något.",
    "Webbplatsen får inte skicka Referrer-Policy no-referrer eller same-origin. "
    "Då berättar webbläsaren inte varifrån anropet kommer, och det räknas inte. "
    "Webbläsarnas standard, strict-origin-when-cross-origin, fungerar.",
    "Raden ska stå i sidhuvudet på sidan som provlänken öppnar, och ingen "
    "inställning för innehållssäkerhet (Content-Security-Policy) får stoppa "
    "skriptet eller anropet till {host}.",
)
#: Värdens läge hos byrån för länkar till domänen (E.8).
HOST_TEXTS = {
    links.STATUS_ALLOWED: "",
    links.STATUS_PENDING: "Länkar hit i utskick och dina länkar väntar på ADX:s godkännande.",
    links.STATUS_NEW: "Länkar hit i utskick och dina länkar godkänns av ADX först.",
}


def _rows(account, now):
    rows = []
    for site in SiteSnippet.objects.filter(account=account).order_by("domain", "pk"):
        label, tone = site_snippet.status(site, now)
        host = links.host_status(account, site.domain)
        rows.append(
            {
                "site": site,
                "label": label,
                "tone": tone,
                "seen": site_snippet.seen_text(site, now),
                "tag": links.snippet_tag(site),
                "install_url": site_snippet.install_url(site),
                "host_text": links.refused_text(site.domain)
                if host == links.STATUS_REFUSED
                else HOST_TEXTS.get(host, ""),
            }
        )
    return rows


def _log(request, account, site, what):
    actor = actor_for(request)
    logger.info(
        "Utskick: spårningsskript %s %s (konto %s, användare %s%s)",
        site.pk,
        what,
        account.pk,
        getattr(actor.user, "pk", None),
        ", byrån i kundvyn" if actor.staff else "",
    )


def _add(request, account, errors, values):
    """Lägg till domänen; byrån får frågan om värden efter sparningen."""
    raw = request.POST.get("domain", "")
    values["domain"] = str(raw or "").strip()[:300]
    try:
        domain = site_snippet.clean_domain(raw)
    except site_snippet.DomainRefused as exc:
        errors["domain"] = str(exc)
        return None
    if SiteSnippet.objects.filter(account=account, domain=domain).exists():
        errors["domain"] = site_snippet.EXISTS_TEXT
        return None
    if SiteSnippet.objects.filter(account=account).count() >= SiteSnippet.MAX_PER_ACCOUNT:
        errors["domain"] = site_snippet.MAX_TEXT.format(n=SiteSnippet.MAX_PER_ACCOUNT)
        return None
    try:
        with transaction.atomic():
            site = SiteSnippet.objects.create(account=account, domain=domain)
    except IntegrityError:
        errors["domain"] = site_snippet.EXISTS_TEXT
        return None
    _log(request, account, site, "tillagt")
    if links.host_status(account, domain) == links.STATUS_NEW:
        user = request.user if request.user.is_authenticated else None
        links.request_host(account, domain, user)
    return site


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def snippet_settings(request, account):
    now = timezone.now()
    errors = {}
    values = {"domain": ""}
    if request.method == "POST":
        if account.is_demo:
            messages.info(request, DEMO_TEXT)
            return redirect("flamingo:app_utskick_snippet")
        action = request.POST.get("action", "")
        if action == "add":
            site = _add(request, account, errors, values)
            if site is not None:
                messages.success(
                    request,
                    f"{site.domain} är tillagd. Lägg in raden på din webbplats och testa med "
                    "provlänken.",
                )
                return redirect("flamingo:app_utskick_snippet")
        elif action == "remove":
            ids = owned_ids(SiteSnippet, account, [request.POST.get("site", "")], limit=1)
            site = SiteSnippet.objects.filter(pk__in=ids, account=account).first()
            if site is not None:
                _log(request, account, site, "borttaget")
                domain = site.domain
                site.delete()
                messages.success(
                    request, f"{domain} är borttagen. Ta också bort raden från webbplatsen."
                )
            return redirect("flamingo:app_utskick_snippet")
        else:
            return redirect("flamingo:app_utskick_snippet")
    rows = _rows(account, now)
    beacon_host = links.beacon_url().split("://", 1)[-1].split("/", 1)[0]
    context = {
        "rows": rows,
        "errors": errors,
        "values": values,
        "can_add": len(rows) < SiteSnippet.MAX_PER_ACCOUNT,
        "max_sites": SiteSnippet.MAX_PER_ACCOUNT,
        "analytics_text": ANALYTICS_TEXT,
        "track_text": TRACK_TEXT,
        "track_example": TRACK_EXAMPLE,
        "trouble_texts": [text.format(host=beacon_host) for text in TROUBLE_TEXTS],
        "beacon_host": beacon_host,
    }
    status = 400 if errors else 200
    return render_utskick(
        request, "flamingo/app/utskick/snippet.html", "settings", context, status=status
    )
