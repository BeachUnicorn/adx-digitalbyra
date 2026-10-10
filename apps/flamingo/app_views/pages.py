"""
Sidorna i verktyget (/flamingo/app/sidor/...): kontots landningssidor och
redigeraren i sidbyggaren (UX: adx-marketing/sidbyggaren-mockup.html, skärm
01, 02, 04 och 09).

    page_list         sidorna som kort: en liten bild av sidan, namnet, hur
                      många kampanjer som visar den, läget och när den ändrades
    page_new          POST: en ny sida ur mallarna för en av kundens tjänster
                      (högst pagebuilder.MAX_PAGES sidor per konto)
    page_copy         POST: en kopia av utkastet (inte publicerad, samma gräns)
    page_delete       POST: tar bort en sida som ingen kampanj visar
    page_detail       redigeraren: sidan i Ren i en ram (srcdoc), blocken till
                      vänster, verktygen överst (static/js/flamingo-pb.js)
    page_save         POST JSON {rev, blocks}: hela utkastet med
                      pagebuilder.save_draft. 409 när någon annan hunnit spara
                      (StaleRevision): ingenting skrivs över i tysthet
    page_render_block POST JSON {blocks, ids?, palette?}: blocken ritade i
                      redigeringsläget (render_block_html), eller hela sidan
                      som ett dokument när ids saknas. Blocken prövas mot
                      schemat men ritas som de är (en tom rad som skrivs syns)
    page_block_new    POST JSON {type, variant, fields?}: ett nytt block med
                      mallens innehåll (pagebuilder.new_block), med sidans
                      tjänst och pris. Fält från redigeraren blir en version
                      i den inloggades namn (en källa "ai" från klienten
                      litas aldrig på)
    page_settings     POST JSON {name?, palette?}. Paletten syns direkt på en
                      sida som är live: då larmas byrån
    page_publish      POST {rev}: pagebuilder.publish_page. En ändring på en
                      sida som är live går live direkt när kontrollerna är
                      gröna, och byrån larmas. Kunden mejlas aldrig. 409 när
                      utkastet har sparats från ett annat ställe sedan rev.
                      Med Accept: application/json svarar vyn med JSON
                      (redigeraren), annars med en omdirigering.
    page_answers      Svar i formuläret: hur besökarna svarat på sidans
                      flervalsfrågor, antal och procent per alternativ
                      (answers.page_report), bara läsning. Procenten räknas
                      av förfrågningarna som svarade på frågan; skräp räknas
                      inte. Länkas från sidans kort (när sidan har ett
                      flerval eller svar) och kampanjens flik Sidan.

Allt hämtas via kundens konto (account=account, app_view). Byrån i kundvyn
gör samma sak som kunden, och det sparas i byråns namn: versioner som är
nya eller ändrade får den inloggades id (by) och källan "adx" för byrån,
"customer" för kunden. En version som servern inte känner igen behåller
sin källa bara med serverns signatur (pagebuilder.is_signed); annars blir
den den inloggades (stamp_authorship). Bara utkastförhandsvisningen i
/manage/ är skrivskyddad (grinden skickar tillbaka en POST).

JSON-anropen kräver CSRF-nyckeln i headern X-CSRFToken, bara POST, högst
MAX_BODY byte och högst pagebuilder.MAX_BLOCKS block. Blocken prövas alltid
med validate_blocks(account=account): okända typer, HTML och bilder från
ett annat konto nekas med 400, och JSON som inte går att läsa (också
för djupt nästlad) med 400.

Problemlistan är utkastets problem, och när sidan är publicerad med
ändringar även de problem på den publicerade versionen som utkastet redan
rättat (pagebuilder.published_problems, märkta "på den publicerade
sidan"): inskicket av en kampanj prövar den publicerade versionen.
"""

import copy
import json
import logging

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.templatetags.static import static
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from apps.common.security import sanitize_plain_text

from .. import answers as form_answers
from .. import exports, pagebuilder
from ..models import Campaign, LandingPage, Service
from ..pagebuilder import registry
from ..pagebuilder.ai import default_service
from ..pagebuilder.render import PALETTES
from . import app_view, render_app
from .campaigns import _when

logger = logging.getLogger(__name__)

#: Största JSON-kropp redigeraren får skicka (hela utkastet med versioner).
MAX_BODY = 1_000_000
NAME_MAX = 120
#: Så många block ritas i sidans lilla bild i listan.
THUMB_ROWS = 7

STATE_DRAFT = "draft"
STATE_CHANGED = "changed"
STATE_LIVE = "live"
STATE_PUBLISHED = "published"
STATE_BADGES = {
    STATE_DRAFT: "draft",
    STATE_CHANGED: "needs_customer",
    STATE_LIVE: "live",
    STATE_PUBLISHED: "live",
}

#: Startsidan för en ny sida per sätt att sälja, i ordning. Block som
#: kräver en uppgift som saknas hoppas över (BlockUnavailable).
STARTER_PLANS = {
    Service.SALES_CALL: (
        ("hero", "call"),
        ("reviews_google", "cards"),
        ("certificates", "badges"),
        ("price", "from"),
        ("steps", "three"),
        ("faq", "three"),
        ("area", "list"),
        ("form", "short"),
        ("callbar", "call"),
    ),
    Service.SALES_QUOTE: (
        ("hero", "form"),
        ("form", "questions"),
        ("reviews_google", "cards"),
        ("certificates", "badges"),
        ("price", "from"),
        ("steps", "three"),
        ("faq", "three"),
        ("area", "list"),
    ),
    Service.SALES_BOOK: (
        ("hero", "form"),
        ("form", "booking"),
        ("reviews_google", "cards"),
        ("certificates", "badges"),
        ("price", "from"),
        ("steps", "three"),
        ("faq", "three"),
        ("area", "list"),
    ),
}


# ---------------------------------------------------------------------------
# Hjälpare
# ---------------------------------------------------------------------------


def page_state(page, campaigns):
    """(sort, text) för sidans läge: utkast, ändringar ej publicerade, live
    eller publicerad."""
    if not page.is_published:
        return STATE_DRAFT, "Utkast"
    if page.has_unpublished_changes:
        return STATE_CHANGED, "Ändringar ej publicerade"
    if any(c.status == Campaign.STATUS_LIVE for c in campaigns):
        return STATE_LIVE, "Live"
    return STATE_PUBLISHED, "Publicerad"


def usage_text(count):
    if not count:
        return "Ingen kampanj använder sidan än"
    return f"Används av {count} {'kampanj' if count == 1 else 'kampanjer'}"


def _source_for(request):
    """Källan för en version som den inloggade skriver: byrån eller kunden."""
    return pagebuilder.SOURCE_ADX if request.flamingo.is_agency else pagebuilder.SOURCE_CUSTOMER


def _unique_name(account, name, exclude_pk=None):
    base = (name or "Sida").strip()[: NAME_MAX - 10] or "Sida"
    taken = LandingPage.objects.filter(account=account)
    if exclude_pk:
        taken = taken.exclude(pk=exclude_pk)
    names = set(taken.values_list("name", flat=True))
    candidate, n = base, 2
    while candidate in names:
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def page_problem_list(page):
    """Problemen redigeraren visar: utkastets, och de på den publicerade
    versionen som utkastet redan rättat (märkta, pagebuilder.published_problems)."""
    draft = pagebuilder.page_problems(page)
    if not page.has_unpublished_changes:
        return draft
    return draft + pagebuilder.published_problems(page, draft_problems=draft, only_fixed=True)


def _problem_dicts(problems):
    return [
        {
            "block": p.block,
            "part": p.part,
            "index": p.index,
            "where": p.where,
            "message": p.message,
        }
        for p in problems
    ]


def _palette_style(page):
    pairs = "".join(f"{name}:{value};" for name, value in pagebuilder.palette_vars(page).items())
    return f":root{{{pairs}}}"


def _logo_primary(page):
    colors = page.logo_colors if isinstance(page.logo_colors, dict) else {}
    value = str(colors.get("primary") or "")
    if len(value) == 7 and value.startswith("#"):
        try:
            int(value[1:], 16)
        except ValueError:
            return ""
        return value.upper()
    return ""


def _palettes(page):
    """Sex färger att välja bland. Logotypens färg finns när mediaarkivet
    har läst ut den (page.logo_colors)."""
    logo = _logo_primary(page)
    out = []
    for key, label in LandingPage.PALETTE_CHOICES:
        if key == LandingPage.PALETTE_LOGO:
            out.append(
                {
                    "key": key,
                    "label": label,
                    "color": logo,
                    "enabled": bool(logo),
                    "reason": ""
                    if logo
                    else "Kommer när logotypen finns i mediaarkivet och färgerna är utlästa.",
                }
            )
        else:
            out.append(
                {
                    "key": key,
                    "label": label,
                    "color": PALETTES.get(key, ""),
                    "enabled": True,
                    "reason": "",
                }
            )
    return out


def _optional_url(name, pk):
    """Adressen till en vy som en annan del bygger: med sidans id, utan id,
    eller None när den inte finns än (redigeraren fungerar ändå)."""
    for args in ([pk], []):
        try:
            return reverse(name, args=args)
        except NoReverseMatch:
            continue
    return None


def _editor_urls(page):
    pk = page.pk
    return {
        "save": reverse("flamingo:app_page_save", args=[pk]),
        "renderBlock": reverse("flamingo:app_page_render_block", args=[pk]),
        "newBlock": reverse("flamingo:app_page_block_new", args=[pk]),
        "publish": reverse("flamingo:app_page_publish", args=[pk]),
        "settings": reverse("flamingo:app_page_settings", args=[pk]),
        "pages": reverse("flamingo:app_pages"),
        "ai_build": _optional_url("flamingo:app_page_ai_build", pk),
        "ai_rewrite": _optional_url("flamingo:app_page_ai_rewrite", pk),
        "koll": _optional_url("flamingo:app_page_koll", pk),
        "media_json": _optional_url("flamingo:app_media_json", pk),
        "media_upload": _optional_url("flamingo:app_media_upload", pk),
        "media": _optional_url("flamingo:app_media", pk),
    }


def _static_version(request):
    from apps.manage.context_processors import static_version

    return static_version(request).get("static_version", "1")


def editor_document(page, account, request):
    """Hela sidan i redigeringsläget som ett HTML-dokument, med
    redigerarens stilmall (duken i flamingo-pb.css) i huvudet. Mallarna
    escapar varje fält; länken nedan är det enda som läggs till."""
    html = pagebuilder.render_page_html(
        page, account, None, editing=True, which="draft", request=request, extra={"action": "#"}
    )
    link = format_html(
        '<link rel="stylesheet" href="{}?v={}" data-pb-canvas>',
        static("css/flamingo-pb.css"),
        _static_version(request),
    )
    return html.replace("</head>", f"{link}\n</head>", 1)


def _build_ctx(page, account):
    """Mallarnas BuildContext för ett nytt block: sidans tjänst som AI och
    Konverteringskollen ser den (ai.default_service: den första kampanjens,
    annars kontots första), med den kampanjens orter och sätt att sälja, och
    tjänstens pris (aldrig kontots första pris). Så lägger "Lägg till
    prisblock" i Konverteringskollen in det pris som kollen nämner."""
    service = default_service(page, account)
    for campaign in page.campaigns.select_related("service", "account").order_by("name", "pk"):
        if service is not None and campaign.service_id == service.pk:
            return pagebuilder.build_ctx(campaign)
    return pagebuilder.service_ctx(account, service)


class _BadRequest(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra

    def response(self):
        return JsonResponse({"ok": False, "error": self.message, **self.extra}, status=self.status)


def _json_body(request):
    """Kroppen som ett dict, eller _BadRequest (för stor, inte JSON, eller
    så djupt nästlad att den inte går att läsa)."""
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > MAX_BODY:
        raise _BadRequest("Ändringen är för stor för att sparas på en gång.", status=413)
    body = request.body
    if len(body) > MAX_BODY:
        raise _BadRequest("Ändringen är för stor för att sparas på en gång.", status=413)
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _BadRequest("Förfrågan gick inte att läsa.") from None
    if not isinstance(data, dict):
        raise _BadRequest("Förfrågan gick inte att läsa.")
    return data


def _blocks_from(data, page):
    blocks = data.get("blocks")
    if blocks is None:
        return page.draft_blocks
    if not isinstance(blocks, list):
        raise _BadRequest("Blocken ska vara en lista.")
    if len(blocks) > pagebuilder.MAX_BLOCKS:
        raise _BadRequest(f"Högst {pagebuilder.MAX_BLOCKS} block på en sida.")
    return blocks


def _validated(blocks, account):
    try:
        return pagebuilder.validate_blocks(blocks, account=account)
    except pagebuilder.BlockError as exc:
        raise _BadRequest(
            "Sidan klarar inte schemat: " + "; ".join(exc.errors[:3]), errors=exc.errors[:20]
        ) from None


def stamp_authorship(page, blocks, user, source, now=None):
    """Vem som skrev varje version, avgjort av servern:

    - En version som finns i det sparade utkastet eller på den publicerade
      sidan, med samma fält: det som sparades (källa, by, at), så att ingen
      kan skriva en annan persons namn på en version, och "Ångra" efter
      "Använd förslaget" behåller vem som skrev vad.
    - Samma id med andra fält: den inloggade (by, at) med källan source
      (kunden eller byrån).
    - En ny version med serverns signatur (pagebuilder.is_signed: mallen,
      AI-förslaget, eller en version som servern sparat förut): som den är.
    - Allt annat, också en ny version som påstår att den kommer från mallen
      eller AI: den inloggade med källan source.

    Ändras blocken på fel sätt lämnas de som de är: validate_blocks säger
    vad som är fel."""
    stored = {}
    for block in [*page.published_blocks, *page.draft_blocks]:
        for version in block.get("versions") or []:
            if isinstance(version, dict) and isinstance(version.get("id"), str):
                stored[version["id"]] = version
    by = user.pk if isinstance(getattr(user, "pk", None), int) else None
    at = (now or timezone.now()).isoformat()
    out = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("versions"), list):
            out.append(block)
            continue
        block = dict(block)
        versions = []
        for version in block["versions"]:
            if not isinstance(version, dict):
                versions.append(version)
                continue
            version = dict(version)
            version_id = version.get("id")
            before = stored.get(version_id) if isinstance(version_id, str) else None
            if before is not None and before.get("fields") == version.get("fields"):
                for key in ("source", "by", "at"):
                    version[key] = before.get(key)
            elif before is None and pagebuilder.is_signed(version):
                pass  # skapad av servern och oförändrad sedan dess
            else:
                version["by"], version["at"], version["source"] = by, at, source
            version.pop("sig", None)  # save_draft signerar det som sparas
            versions.append(version)
        block["versions"] = versions
        out.append(block)
    return out


def _state_json(page, campaigns=None):
    campaigns = list(pagebuilder.campaigns_using(page)) if campaigns is None else campaigns
    kind, label = page_state(page, campaigns)
    return {
        "published": page.is_published,
        "changes": page.has_unpublished_changes,
        "kind": kind,
        "label": label,
        "live": [c.name for c in campaigns if c.status == Campaign.STATUS_LIVE],
    }


def _saved_text(moment):
    return f"{timezone.localtime(moment):%H:%M}"


# ---------------------------------------------------------------------------
# Listan, ny sida, kopiera och ta bort
# ---------------------------------------------------------------------------


@app_view
def page_list(request, account):
    rows = []
    answered = form_answers.answered_page_ids(account)
    for page in pagebuilder.pages_for(account):
        campaigns = list(pagebuilder.campaigns_using(page))
        kind, text = page_state(page, campaigns)
        rows.append(
            {
                "page": page,
                "campaigns": campaigns,
                "usage": usage_text(len(campaigns)),
                "state": text,
                "state_kind": kind,
                "badge": STATE_BADGES[kind],
                "changed": _when(page.updated_at),
                "thumb": [b.get("type", "") for b in page.draft_blocks[:THUMB_ROWS]],
                # Länken Svar: sidan har ett flerval, eller svar från förut.
                "has_answers": page.pk in answered or bool(form_answers.questions_for_page(page)),
            }
        )
    services = list(account.services.filter(is_active=True).order_by("order", "id"))
    return render_app(
        request,
        "flamingo/app/pages/list.html",
        "pages",
        {"rows": rows, "services": services},
    )


@app_view
def page_answers(request, account, pk):
    """Svar i formuläret: sidans flervalsfrågor med antal och procent per
    alternativ (answers.page_report). Bara läsning, bara kontots egen sida."""
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    report = form_answers.page_report(page)
    return render_app(
        request,
        "flamingo/app/pages/answers.html",
        "pages",
        {
            "page": page,
            "report": report,
            "draft_only": not report and form_answers.draft_only(page),
        },
    )


def starter_blocks(account, service, user=None):
    """Block för en ny sida ur mallarna för tjänsten, bara ur kontots
    bekräftade uppgifter och bara tjänstens eget pris. Block som kräver en
    uppgift som saknas, och omdömen utan valda omdömen, hoppas över."""
    ctx = pagebuilder.service_ctx(account, service)
    plan = STARTER_PLANS.get(service.sales_mode) or STARTER_PLANS[Service.SALES_QUOTE]
    has_reviews = bool(account.selected_google_reviews())
    blocks = []
    for type_key, variant in plan:
        if type_key == "reviews_google" and not has_reviews:
            continue
        try:
            blocks.append(pagebuilder.new_block(type_key, variant, account, user=user, ctx=ctx))
        except (pagebuilder.BlockUnavailable, pagebuilder.BlockError):
            continue
    return blocks


@app_view
@require_POST
def page_new(request, account):
    value = request.POST.get("service", "")
    service = get_object_or_404(
        Service, pk=int(value) if value.isdigit() else 0, account=account, is_active=True
    )
    try:
        page = pagebuilder.new_page(
            account,
            name=_unique_name(account, service.name),
            draft={"blocks": starter_blocks(account, service, user=request.user)},
            created_by=request.user if getattr(request.user, "pk", None) else None,
        )
    except pagebuilder.PageLimit as exc:
        messages.error(request, exc.message)
        return redirect("flamingo:app_pages")
    messages.success(
        request,
        f"Sidan {page.name} är skapad ur mallarna, med dina bekräftade uppgifter. "
        "Klicka i en text och skriv.",
    )
    return redirect("flamingo:app_page", page.pk)


@app_view
@require_POST
def page_copy(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    try:
        copy_page = pagebuilder.new_page(
            account,
            name=_unique_name(account, f"Kopia av {page.name}"),
            design=page.design,
            palette=page.palette,
            logo_colors=dict(page.logo_colors or {}),
            draft={"blocks": copy.deepcopy(page.draft_blocks)},
            created_by=request.user if getattr(request.user, "pk", None) else None,
        )
    except pagebuilder.PageLimit as exc:
        messages.error(request, exc.message)
        return redirect("flamingo:app_pages")
    messages.success(
        request,
        f"Kopian {copy_page.name} är skapad. Den är inte publicerad och ingen kampanj visar den.",
    )
    return redirect("flamingo:app_page", copy_page.pk)


@app_view
@require_POST
def page_delete(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    campaigns = list(pagebuilder.campaigns_using(page))
    if campaigns:
        names = ", ".join(c.name for c in campaigns)
        messages.error(
            request,
            f"Sidan {page.name} används av {names} och kan inte tas bort. Välj en annan sida "
            "för kampanjerna under fliken Sidan först.",
        )
        return redirect("flamingo:app_pages")
    name = page.name
    page.delete()
    messages.success(request, f"Sidan {name} är borttagen.")
    return redirect("flamingo:app_pages")


# ---------------------------------------------------------------------------
# Redigeraren
# ---------------------------------------------------------------------------


#: Redigerarens profil för sidorna (static/js/flamingo-pb.js, README för
#: utskick F.6): det som skiljer sidbyggaren från e-postredigeraren i Brev,
#: som monterar samma skript med profilen "brev" (apps/utskick/app_views/
#: brev.py). Värdena är de som stod fast i skriptet innan profilerna fanns;
#: test_pagebuilder_editor prövar att sidornas profil inte ändras.
PAGE_PROFILE = {
    "profile": "page",
    # Blocken är barn till sidans <main>; sidhuvudet och sidfoten byts när de ändrats.
    "canvasRoot": "main.rn-main",
    "chromeSelectors": {"top": ".rn-top", "foot": ".rn-foot"},
    # Färgernas <style> i ramen känns igen på Rens variabel.
    "paletteMarker": "--rn-primary",
    # Dator: hela duken, aldrig smalare än Rens brytpunkt för två spalter.
    "devices": {"desktop": 1024, "phone": 390, "fit": "fill"},
    # Toppen med formulär och formulärblocket står ihop; avslutet sist.
    "placement": {"pairs": [["hero", "form", "form"]], "endGroup": "end"},
    "panelKinds": [],
    # Ordet för en ny rad i en lista ("Lägg till fråga").
    "addWords": {
        "hero.points": "punkt",
        "area.places": "ort",
        "guarantee.terms": "villkor",
        "steps.steps": "steg",
        "faq.items": "fråga",
        "certificates.items": "certifikat",
        "price.items": "prisexempel",
        "form.questions": "fråga",
    },
    # Skisserna i variantväljaren (mockupen .wf): ett ord per rad, "row:"
    # delar i två spalter med |, "bar:" är en färgad remsa.
    "wireframes": {
        "hero": {"call": "h l b", "form": "row:h l|l l b", "image": "img h b", "text": "h l l"},
        "price": {"from": "row:h l|big", "examples": "h cards", "fixed": "card"},
        "reviews_google": {"cards": "h cards", "quote": "quote", "line": "stars"},
        "reviews_reco": {
            "stor": "h quote",
            "medel": "h stars",
            "liten": "stars",
            "staende": "h card",
            "utvalda_kort": "h cards",
            "utvalda_citat": "quote",
            "utvalda_rad": "stars",
        },
        "certificates": {"badges": "h chips", "icons": "h cards"},
        "guarantee": {"short": "icon h l", "terms": "icon h checks"},
        "person": {"image": "row:img|h l", "noimage": "row:circle|h l"},
        "steps": {"three": "h nums3", "four": "h nums4"},
        "before_after": {"slider": "h split", "pair": "h pair"},
        "area": {"list": "h chips", "map": "row:h chips|map"},
        "faq": {"three": "h rows3", "six": "h rows6"},
        "form": {"short": "h in in b", "questions": "h in in in b", "booking": "h date in b"},
        "callbar": {"call": "bar:b", "call_write": "bar:b b2"},
    },
}


def editor_config(request, account, page, campaigns, problems):
    available = registry.available(account, ctx=_build_ctx(page, account))
    access = request.flamingo
    return {
        **copy.deepcopy(PAGE_PROFILE),
        "pageId": page.pk,
        "name": page.name,
        "rev": page.rev,
        "palette": page.palette,
        "blocks": page.draft_blocks,
        "state": _state_json(page, campaigns),
        "problems": _problem_dicts(problems),
        "schema": pagebuilder.schema(),
        "groups": [{"key": k, "name": n} for k, n in pagebuilder.GROUPS],
        "available": {
            k: {
                "ok": ok,
                "reason": reason,
                "link": "" if ok else registry.requires_url(registry.TYPES[k].requires),
            }
            for k, (ok, reason) in available.items()
        },
        "palettes": _palettes(page),
        "campaigns": [
            {"name": c.name, "live": c.status == Campaign.STATUS_LIVE, "id": c.pk}
            for c in campaigns
        ],
        "urls": _editor_urls(page),
        "me": {
            "id": request.user.pk,
            "source": _source_for(request),
            "agency": access.is_agency,
        },
        "readOnly": access.read_only,
        "maxBlocks": pagebuilder.MAX_BLOCKS,
        "maxVersions": pagebuilder.MAX_VERSIONS,
    }


@app_view
def page_detail(request, account, pk):
    page = get_object_or_404(LandingPage.objects.select_related("account"), pk=pk, account=account)
    campaigns = list(pagebuilder.campaigns_using(page))
    kind, text = page_state(page, campaigns)
    try:
        canvas = editor_document(page, account, request)
    except Exception:  # noqa: BLE001 - redigeraren ska visas även om duken inte går att rita
        logger.exception("Sidbyggaren: sidan %s gick inte att rita", page.pk)
        canvas = None
    problems = page_problem_list(page)
    live = [c for c in campaigns if c.status == Campaign.STATUS_LIVE]
    return render_app(
        request,
        "flamingo/app/pages/editor.html",
        "pages",
        {
            "page": page,
            "campaigns": campaigns,
            "live_campaigns": live,
            "shared": len(campaigns) > 1,
            "usage": usage_text(len(campaigns)),
            "state": text,
            "state_kind": kind,
            "state_badge": STATE_BADGES[kind],
            "canvas_html": canvas,
            "saved": _saved_text(page.updated_at),
            "palettes": _palettes(page),
            "groups": _library(account, page),
            "config": editor_config(request, account, page, campaigns, problems),
        },
    )


def _library(account, page):
    """Biblioteket i grupper: ikon, namn, varianter och om blocket går att
    lägga till (registry.available, med sidans tjänst och pris). Ett
    omdömesblock utan profil får en länk till Omdömen (registry.requires_url)."""
    available = registry.available(account, ctx=_build_ctx(page, account))
    groups = []
    for key, name in pagebuilder.GROUPS:
        items = []
        for block_type in registry.TYPES_LIST:
            if block_type.group != key:
                continue
            ok, reason = available.get(block_type.key, (True, ""))
            link = "" if ok else registry.requires_url(block_type.requires)
            items.append({"type": block_type, "ok": ok, "reason": reason, "link": link})
        if items:
            groups.append({"key": key, "name": name, "items": items})
    return groups


@app_view
@require_POST
def page_save(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    try:
        data = _json_body(request)
        rev = data.get("rev")
        if isinstance(rev, bool) or not isinstance(rev, int):
            raise _BadRequest("Sidans version saknas. Ladda om sidan.")
        if "blocks" not in data:
            raise _BadRequest("Blocken saknas.")
        blocks = _blocks_from(data, page)
        blocks = stamp_authorship(page, blocks, request.user, _source_for(request))
        try:
            new_rev = pagebuilder.save_draft(
                page, blocks, rev=rev, user=request.user, account=account
            )
        except pagebuilder.BlockError as exc:
            raise _BadRequest(
                "Sidan klarar inte schemat: " + "; ".join(exc.errors[:3]), errors=exc.errors[:20]
            ) from None
        except pagebuilder.StaleRevision as exc:
            raise _BadRequest(exc.message, status=409, rev=exc.current_rev) from None
    except _BadRequest as exc:
        return exc.response()
    page.refresh_from_db(fields=["updated_at", "published", "published_at"])
    return JsonResponse(
        {
            "ok": True,
            "rev": new_rev,
            "saved_at": page.updated_at.isoformat(),
            "saved_text": _saved_text(page.updated_at),
            "state": _state_json(page),
            "problems": _problem_dicts(page_problem_list(page)),
        }
    )


@app_view
@require_POST
def page_render_block(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    try:
        data = _json_body(request)
        blocks = _blocks_from(data, page)
        # Schemat prövas (typer, fält, längder, bilder på kontot), men det som
        # ritas är blocken som de är: en tom punkt eller fråga som kunden
        # just lagt till ska synas medan den skrivs. Mallarna escapar allt.
        _validated(blocks, account)
        ids = data.get("ids")
        if ids is not None and (
            not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)
        ):
            raise _BadRequest("ids ska vara en lista med blockens id.")
        palette = data.get("palette")
        if palette is not None and palette not in dict(LandingPage.PALETTE_CHOICES):
            raise _BadRequest("Okänd palett.")
    except _BadRequest as exc:
        return exc.response()
    preview = copy.copy(page)
    preview.draft = {"blocks": blocks}
    if palette:
        preview.palette = palette
    if ids is not None:
        wanted = set(ids)
        out = {}
        for block in blocks:
            if block["id"] in wanted:
                out[block["id"]] = str(
                    pagebuilder.render_block_html(
                        preview, block, account, editing=True, request=request, blocks=blocks
                    )
                )
        return JsonResponse({"ok": True, "blocks": out})
    return JsonResponse(
        {
            "ok": True,
            "html": editor_document(preview, account, request),
            "palette_style": _palette_style(preview),
        }
    )


@app_view
@require_POST
def page_block_new(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    try:
        data = _json_body(request)
        type_key = data.get("type")
        variant = data.get("variant") or None
        if not isinstance(type_key, str) or pagebuilder.get_type(type_key) is None:
            raise _BadRequest("Okänd blocktyp.")
        if variant is not None and not isinstance(variant, str):
            raise _BadRequest("Okänd variant.")
        try:
            block = pagebuilder.new_block(
                type_key, variant, account, user=request.user, ctx=_build_ctx(page, account)
            )
        except pagebuilder.BlockUnavailable as exc:
            raise _BadRequest(str(exc)) from None
        except pagebuilder.BlockError as exc:
            raise _BadRequest("; ".join(exc.errors)) from None
        fields = data.get("fields")
        if fields is not None:
            if not isinstance(fields, dict):
                raise _BadRequest("Fälten ska vara ett dict.")
            # Fälten kommer från redigeraren: versionen är den inloggades,
            # vad klienten än säger om källan (en källa "ai" eller "template"
            # från klienten går inte att lita på).
            merged = {**pagebuilder.active_fields(block), **fields}
            try:
                pagebuilder.add_version(block, merged, _source_for(request), request.user)
            except pagebuilder.BlockError as exc:
                raise _BadRequest(
                    "Fälten klarar inte schemat: " + "; ".join(exc.errors[:3]),
                    errors=exc.errors[:20],
                ) from None
        _validated([block], account)
    except _BadRequest as exc:
        return exc.response()
    return JsonResponse({"ok": True, "block": block})


@app_view
@require_POST
def page_settings(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    before = page.palette
    try:
        data = _json_body(request)
        fields = []
        if "name" in data:
            name = sanitize_plain_text(str(data.get("name") or ""), max_length=NAME_MAX * 2)
            name = " ".join(name.split())
            if not name:
                raise _BadRequest("Skriv ett namn på sidan.", field="name")
            if len(name) > NAME_MAX:
                raise _BadRequest(f"Högst {NAME_MAX} tecken i namnet.", field="name")
            if LandingPage.objects.filter(account=account, name=name).exclude(pk=page.pk).exists():
                raise _BadRequest("Det finns redan en sida med det namnet.", field="name")
            page.name = name
            fields.append("name")
        if "palette" in data:
            palette = data.get("palette")
            if palette not in dict(LandingPage.PALETTE_CHOICES):
                raise _BadRequest("Okänd palett.", field="palette")
            if palette == LandingPage.PALETTE_LOGO and not _logo_primary(page):
                raise _BadRequest(
                    "Färgerna från logotypen finns inte än. Lägg in logotypen i "
                    "mediaarkivet först.",
                    field="palette",
                )
            page.palette = palette
            fields.append("palette")
        if not fields:
            raise _BadRequest("Inget att ändra.")
    except _BadRequest as exc:
        return exc.response()
    page.save(update_fields=[*fields, "updated_at"])
    if page.palette != before and page.is_published:
        _alert_palette(request, account, page)
    return JsonResponse(
        {
            "ok": True,
            "name": page.name,
            "palette": page.palette,
            "palette_label": page.get_palette_display(),
            "palette_style": _palette_style(page),
            "saved_text": _saved_text(page.updated_at),
        }
    )


def _alert_palette(request, account, page):
    """Paletten syns direkt på en publicerad sida: byrån larmas när en
    kampanj som visar sidan är live (pagebuilder.alert_live_change)."""
    live = pagebuilder.live_campaigns([page])
    if not live:
        return
    who = request.user.get_full_name() or request.user.get_username()
    pagebuilder.alert_live_change(
        account,
        live,
        f"Flamingo: ny palett ({page.get_palette_display()}) på sidan {page.name} "
        f"({account.customer.name})",
        [
            f"Sidan {page.name} för {account.customer.name} har fått paletten "
            f"{page.get_palette_display()} av {who}.",
            "Paletten syns direkt för besökarna i de här kampanjerna, som är live "
            "(kontrasten prövas mot WCAG AA när sidan ritas):",
            *[f"- {c.name}: {exports.landing_page_url(c)}" for c in live],
        ],
    )


def _wants_json(request):
    return "application/json" in request.headers.get("Accept", "")


def _publish_rev(request):
    """Det rev redigeraren publicerar (JSON {"rev": n}, eller fältet rev i
    ett formulär), eller None när inget skickades."""
    if "application/json" in request.content_type:
        data = _json_body(request)
        rev = data.get("rev")
    else:
        raw = request.POST.get("rev", "")
        rev = int(raw) if raw.isdigit() else (None if raw == "" else raw)
    if rev is None:
        return None
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise _BadRequest("Sidans version saknas. Ladda om sidan.")
    return rev


@app_view
@require_POST
def page_publish(request, account, pk):
    page = get_object_or_404(LandingPage, pk=pk, account=account)
    wants_json = _wants_json(request)
    back = redirect(reverse("flamingo:app_page", args=[page.pk]))
    try:
        rev = _publish_rev(request)
        result = pagebuilder.publish_page(page, request.user, rev=rev)
    except _BadRequest as exc:
        if wants_json:
            return exc.response()
        messages.error(request, f"Inget publicerades. {exc.message}")
        return back
    except pagebuilder.StaleRevision as exc:
        if wants_json:
            return JsonResponse(
                {
                    "ok": False,
                    "error": f"Inget publicerades. {exc.message}",
                    "rev": exc.current_rev,
                },
                status=409,
            )
        messages.error(request, f"Inget publicerades. {exc.message}")
        return back
    except pagebuilder.PageProblems as exc:
        if wants_json:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Inget publicerades. Rätta det här först.",
                    "problems": _problem_dicts(exc.problems),
                },
                status=400,
            )
        messages.error(request, f"Inget publicerades. {exc.message}")
        return back
    except pagebuilder.PageError as exc:
        if wants_json:
            return JsonResponse(
                {"ok": False, "error": f"Inget publicerades. {exc.message}", "problems": []},
                status=400,
            )
        messages.error(request, f"Inget publicerades. {exc.message}")
        return back
    if result.live_campaigns:
        names = ", ".join(c.name for c in result.live_campaigns)
        text = f"Publicerad. Ändringen syns nu på sidan för {names}."
    else:
        text = "Publicerad. Sidan syns för besökarna när en kampanj som använder den är live."
    if wants_json:
        page.refresh_from_db()
        return JsonResponse(
            {
                "ok": True,
                "message": text,
                "published_at": page.published_at.isoformat() if page.published_at else None,
                "state": _state_json(page),
                "problems": [],
            }
        )
    messages.success(request, text)
    return back
