"""
En landningssida i verktyget (README Steg 2, mockups/flamingo-meny.html,
skärmen Avloppsspolning): flikarna Statistik och Länkar per källa, och
"Ändra adressen". Svar i formuläret är pages.page_answers; Redigera öppnar
sidbyggaren (pages.page_detail).

    page_stats      app/sidor/<pk>/statistik/   Statistik (den första fliken):
                    besök, förfrågningar och andelen som frågar för 7, 30
                    eller 90 dagar (?dagar=), och en rad per källa
                    (sources.page_stats)
    page_link_list  app/sidor/<pk>/lankar/      Länkar per källa: sidans egen
                    adress (Direkt), kampanjerna som visar sidan, utskicken som
                    länkat till den och de namngivna länkarna, och "Ny länk för"
    page_address    app/sidor/<pk>/adress/ (POST)  sidans adress före första
                    publiceringen (addresses.change_page_slug)

Överst på varje flik (head_context, pages/_detail_head.html): namnet,
sidans egna adress med Kopiera, Öppna sidan (publicerad) eller
Förhandsvisa, Redigera och, tills sidan publicerats, Ändra adressen.

Allt hämtas via kontot (app_view, account=account). Byrån i kundvyn gör det
kunden gör.
"""

from django.contrib import messages
from django.db.models import Min, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from .. import addresses, sources
from ..models import Campaign, LandingPage
from . import app_view, render_app

UTSKICK_OFF_TEXT = (
    "Länkar för sms, e-post, QR-koder och Instagram finns med Sms och e-post. Be ADX slå på det."
)
UNPUBLISHED_TEXT = "Publicera sidan först, så kan du göra länkar till den."


def head_context(request, account, page):
    """Det som står överst på sidans flikar."""
    return {
        "page": page,
        "page_address": addresses.page_text(page),
        "page_full_url": addresses.page_url(page),
        "page_href": addresses.page_path(page),
        "page_published": page.is_published,
        "can_change_slug": not page.is_published and not request.flamingo.read_only,
    }


def _page(account, pk):
    return get_object_or_404(LandingPage.objects.select_related("account"), pk=pk, account=account)


@app_view
def page_stats(request, account, pk):
    page = _page(account, pk)
    days = sources.clean_days(request.GET.get("dagar"))
    stats = sources.page_stats(page, days)
    context = head_context(request, account, page)
    context.update(
        {
            "stats": stats,
            "periods": [
                {
                    "days": n,
                    "url": f"{reverse('flamingo:app_page_stats', args=[page.pk])}?dagar={n}",
                    "current": n == days,
                }
                for n in sources.PERIODS
            ],
        }
    )
    return render_app(
        request, "flamingo/app/pages/stats.html", "pages/stats", context, tab_args=[page.pk]
    )


# ---------------------------------------------------------------------------
# Länkar per källa
# ---------------------------------------------------------------------------


def _campaign_rows(request, page):
    from .. import exports

    rows = []
    for campaign in Campaign.objects.filter(landing_page=page).order_by("name", "pk"):
        url = exports.landing_page_url(campaign)
        rows.append(
            {
                "campaign": campaign,
                "url": url,
                "text": url.split("://", 1)[-1].rstrip("/"),
                "status": campaign.get_status_display(),
            }
        )
    return rows


def _utskick_rows(account, tracked):
    """En rad per utskick som länkat till sidan, nyast först: kanalerna, en
    av de personliga sms-länkarna (som i Länkar och QR) och hur många."""
    from apps.utskick import links
    from apps.utskick.models import LinkCode, Utskick

    by_utskick = {}
    for link in tracked:
        if link.utskick_id:
            by_utskick.setdefault(link.utskick_id, []).append(link.pk)
    if not by_utskick:
        return []
    link_ids = [pk for ids in by_utskick.values() for pk in ids]
    firsts = {
        row["link__utskick_id"]: row["first"]
        for row in LinkCode.objects.filter(link_id__in=link_ids, kind=LinkCode.Kind.LINK)
        .values("link__utskick_id")
        .annotate(first=Min("pk"))
    }
    codes = dict(LinkCode.objects.filter(pk__in=firsts.values()).values_list("pk", "code"))
    email_host = links.email_link_base().split("://", 1)[-1]
    rows = []
    for utskick in Utskick.objects.filter(account=account, pk__in=by_utskick).order_by(
        "-created_at", "-pk"
    ):
        first = codes.get(firsts.get(utskick.pk))
        chips = []
        if utskick.has_sms:
            chips.append(sources.SOURCE_CHIPS["sms"])
        if utskick.has_email:
            chips.append(sources.SOURCE_CHIPS["email"])
        if first:
            text = links.sms_link(first)
        elif utskick.has_sms:
            text = links.sms_link("...")
        else:
            text = f"{email_host}/m/..."
        rows.append(
            {
                "utskick": utskick,
                "chips": chips,
                "text": text,
                "sent": utskick.status not in Utskick.EDITABLE,
                "href": reverse("flamingo:app_utskick", args=[utskick.pk])
                if utskick.status not in Utskick.EDITABLE
                else reverse("flamingo:app_utskick_step", args=[utskick.pk, "innehall"]),
            }
        )
    return rows


def _named_rows(account, tracked, public_slug, utskick_on):
    from apps.utskick import links

    rows = []
    for link in tracked:
        if link.utskick_id or not link.slug:
            continue
        chip, tone = sources.MEDIUM_CHIPS.get(link.medium or "", sources.MEDIUM_CHIPS[""])
        rows.append(
            {
                "link": link,
                "chip": chip,
                "tone": tone,
                "text": links.named_link_text(link, public_slug) if public_slug else "",
                "url": links.named_link_url(link, public_slug) if public_slug else "",
                "qr": reverse("flamingo:app_link_qr", args=[link.pk, "svg"]) + "?ladda=1"
                if utskick_on and public_slug
                else "",
                "href": reverse("flamingo:app_link", args=[link.pk]) if utskick_on else "",
            }
        )
    return rows


@app_view
def page_link_list(request, account, pk):
    from apps.utskick.access import is_enabled, settings_for
    from apps.utskick.models import TrackedLink
    from apps.utskick.sending import state

    page = _page(account, pk)
    row = settings_for(account)
    utskick_on = is_enabled(account, row)
    tracked = list(
        TrackedLink.objects.filter(account=account, kind=TrackedLink.Kind.LP)
        .filter(Q(landing_page=page) | Q(landing_page__isnull=True, campaign__landing_page=page))
        .order_by("-created_at", "-pk")
    )
    context = head_context(request, account, page)
    can_link = utskick_on and page.is_published and not request.flamingo.read_only
    context.update(
        {
            "direct_chip": sources.SOURCE_CHIPS["direct"],
            "ad_chip": sources.SOURCE_CHIPS["ad"],
            "campaign_rows": _campaign_rows(request, page),
            "utskick_rows": _utskick_rows(account, tracked),
            "named_rows": _named_rows(account, tracked, row.public_slug, utskick_on),
            "utskick_on": utskick_on,
            "can_link": can_link,
            "can_named": can_link and bool(row.public_slug),
            "can_email": can_link and (account.is_demo or state.email_live()),
            "new_link_url": reverse("flamingo:app_link_new") if utskick_on else "",
            "utskick_off_text": UTSKICK_OFF_TEXT,
            "unpublished_text": UNPUBLISHED_TEXT,
        }
    )
    return render_app(
        request, "flamingo/app/pages/links.html", "pages/links", context, tab_args=[page.pk]
    )


# ---------------------------------------------------------------------------
# Ändra adressen
# ---------------------------------------------------------------------------


def _back(request, page):
    """Tillbaka till fliken eller redigeraren som skickade formuläret
    (bara sidans egna adresser i verktyget), annars Statistik."""
    allowed = {
        reverse("flamingo:app_page", args=[page.pk]),
        reverse("flamingo:app_page_stats", args=[page.pk]),
        reverse("flamingo:app_page_links", args=[page.pk]),
        reverse("flamingo:app_page_answers", args=[page.pk]),
    }
    wanted = str(request.POST.get("next") or "")
    if wanted not in allowed:
        wanted = reverse("flamingo:app_page_stats", args=[page.pk])
    return redirect(wanted)


@app_view
@require_POST
def page_address(request, account, pk):
    page = _page(account, pk)
    try:
        addresses.change_page_slug(page, request.POST.get("adress", ""))
    except addresses.SlugRefused as exc:
        messages.error(request, f"Adressen ändrades inte. {exc}")
        return _back(request, page)
    messages.success(request, f"Sidans adress är nu {addresses.page_text(page)}.")
    return _back(request, page)
