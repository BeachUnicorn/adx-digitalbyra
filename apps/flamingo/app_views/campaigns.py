"""
Kampanjerna (kundresan steg 6-8): ny kampanj, förslaget, inskicket och
kundens godkännande.

Granskningen är kundens val (beslut 2026-10-03): vid inskicket kan kunden
bocka i "Jag vill att ADX granskar kampanjen innan den publiceras" (av från
början, Campaign.review_requested). Flödet, och vem som gör vad:

    utkast (draft)          kunden ändrar fritt och skickar när kontrollerna
                            (checks.validate) går igenom
    utan granskning         inskicket är kundens godkännande: approved_at och
                            approved_by sätts, ingen granskningsrunda, och
                            kampanjen publiceras direkt när det går
                            (google_publish.publish_approved)
    hos ADX (in_review)     med granskning: låst för kunden; byrån granskar i
                            /manage/flamingo/ och sätter status
                            needs_customer när den är klar
    väntar på dig           kunden ser ändringarna och godkänner, eller ändrar
      (needs_customer)      något (då blir kampanjen ett utkast igen)
    godkänd                 status needs_customer med approved_at och
                            approved_by satta. Godkännandet publicerar direkt
                            med Google Ads API när kontot är kopplat under
                            ADX; annars står kampanjen kvar i byråns kö
                            ("Godkänd av dig, ADX publicerar") med orsaken
    live / pausad           publicerad, med API:t eller av byrån för hand

Allt hämtas via kundens konto (account=account), aldrig på ett id ensamt.
Byrån i kundvyn läser bara: grinden skickar tillbaka varje POST, och
mallarna döljer formulären ({{ read_only }}). Inget här mejlar kunden.
Inskick och godkännande larmar byrån (INQUIRY_NOTIFICATION_EMAIL) med vad
som hände, vilket är fritt (CLAUDE.md: larm till byrån är fria). Ett
demokonto larmar inte och anropar aldrig Google.
"""

import logging
import re

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.common.security import sanitize_multiline_text, sanitize_plain_text

from .. import checks, generator, google_publish
from ..alerts import send_agency_alert
from ..models import (
    DESCRIPTION_COUNT,
    DESCRIPTION_MAX,
    HEADLINE_COUNT,
    HEADLINE_MAX,
    MATCH_CHOICES,
    MATCH_PHRASE,
    PAGE_QUESTION_KINDS,
    Campaign,
    Review,
    Service,
)
from ..rules import when_text
from . import app_view, render_app

logger = logging.getLogger(__name__)

RADIUS_CHOICES = (5, 10, 15, 25, 50)
DEFAULT_RADIUS = 15
#: Förslagen på budget per dag. flamingo-app-campaigns.css har en regel per
#: belopp (.fl-camp-month--<kr>) som visar månadsbeloppet för det valda.
BUDGET_PRESETS = (100, 150, 200, 300, 500)
DEFAULT_BUDGET = 150
BUDGET_MIN_MESSAGE = f"Lägsta budget är {checks.BUDGET_MIN} kr per dag."
BUDGET_MAX_MESSAGE = f"Högsta budget är {checks.BUDGET_MAX} kr per dag."
DAYS_PER_MONTH = 30.4

MAX_KEYWORDS = 50
MAX_NEGATIVES = 100
MAX_POINTS = 6
MAX_QUESTIONS = 6
BLANK_KEYWORD_ROWS = 3
#: Tomma rubrikrader att fylla i: tre till, minst fem rader totalt (högst 15).
BLANK_TEXT_ROWS = 3
MIN_TEXT_ROWS = 5

TABS = (
    ("annonser", "Annonser"),
    ("sokord", "Sökord"),
    ("sidan", "Sidan"),
    ("granskning", "Granskning"),
)
TAB_KEYS = tuple(key for key, _ in TABS)
#: Var ett problem i kampanjen rättas (checks.Problem.field -> flik).
FIELD_TABS = {
    "headlines": "annonser",
    "descriptions": "annonser",
    "keywords": "sokord",
    "negatives": "sokord",
    "page": "sidan",
    "area": "",
    "daily_budget_kr": "",
    "service": "",
}
FIELD_LABELS = {
    "headlines": "Rubriker",
    "descriptions": "Beskrivningar",
    "keywords": "Sökord",
    "negatives": "Negativa sökord",
    "page": "Sidan",
    "area": "Område",
    "daily_budget_kr": "Budget",
    "service": "Tjänsten",
}
QUESTION_KIND_LABELS = {"text": "Kort svar", "textarea": "Längre text", "date": "Datum"}
PAGE_PART_LABELS = {
    "title": "Rubrik",
    "lead": "Ingress",
    "points": "Punkter",
    "phone": "Telefon",
    "form_title": "Formulärets rubrik",
    "questions": "Frågor",
    "note": "Text under formuläret",
}
#: Sorteringen i listan: det som väntar på kunden först.
STATUS_ORDER = {
    Campaign.STATUS_NEEDS_CUSTOMER: 0,
    Campaign.STATUS_DRAFT: 1,
    Campaign.STATUS_IN_REVIEW: 2,
    Campaign.STATUS_LIVE: 3,
    Campaign.STATUS_PAUSED: 4,
}


# ---------------------------------------------------------------------------
# Hjälpare
# ---------------------------------------------------------------------------


def monthly_kr(daily_kr):
    """Ungefär en månad: 30,4 dagar, hela kronor (samma som Campaign)."""
    return round((daily_kr or 0) * DAYS_PER_MONTH)


def area_text(place, radius_km):
    place = (place or "").strip()
    return f"{place} + {radius_km} km" if place else ""


def _when(moment):
    """'i dag 09:12', 'i går 14:10' eller '2 okt 09:12' i svensk tid."""
    if moment is None:
        return ""
    text = when_text(moment)
    if ":" not in text:
        text += f" {timezone.localtime(moment):%H:%M}"
    return text


def _first_name(user):
    if user is None:
        return ""
    return (user.first_name or "").strip()


def _detail_url(campaign, tab=""):
    url = reverse("flamingo:app_campaign", args=[campaign.pk])
    return f"{url}?flik={tab}" if tab else url


def _locked(campaign):
    """Kampanjen låst för ändring medan vyn arbetar (inskick, granskning)."""
    return (
        Campaign.objects.select_for_update()
        .select_related("service", "account__customer")
        .get(pk=campaign.pk)
    )


def is_approved(campaign):
    """Kunden har godkänt (det byrån granskat, eller vid inskicket utan
    granskning) men kampanjen är inte publicerad än: byrån publicerar."""
    return campaign.status == Campaign.STATUS_NEEDS_CUSTOMER and campaign.approved_at is not None


def state_label(campaign):
    if is_approved(campaign):
        if not campaign.review_requested:
            return "Skickad av dig, ADX publicerar"
        return "Godkänd av dig, ADX publicerar"
    if campaign.status == Campaign.STATUS_NEEDS_CUSTOMER:
        return "Klart för dig"
    if campaign.status == Campaign.STATUS_DRAFT:
        return "Utkast, inget publicerat"
    if campaign.status == Campaign.STATUS_IN_REVIEW:
        return "Hos ADX för granskning"
    return campaign.get_status_display()


def _landing(request, campaign):
    """Landningssidans adress som text och länk, när den är publik."""
    if not campaign.is_public:
        return None
    return {"href": campaign.landing_url, "text": f"{request.get_host()}{campaign.landing_url}"}


def _alert_agency(request, campaign, subject, lines):
    """Larm till byrån (aldrig till kunden) med länken till granskningen.
    Fäller aldrig förfrågan. Ett demokonto skickar ingenting, och samma
    larm om samma kampanj går högst en gång i timmen (alerts.py)."""
    link = request.build_absolute_uri(reverse("manage:flamingo_review", args=[campaign.pk]))
    return send_agency_alert(campaign, subject, [*lines, "", f"Granska i panelen: {link}"])


# ---------------------------------------------------------------------------
# Listan
# ---------------------------------------------------------------------------


@app_view
def campaign_list(request, account):
    campaigns = sorted(
        account.campaigns.select_related("service"),
        key=lambda c: (STATUS_ORDER.get(c.status, 9), -c.updated_at.timestamp()),
    )
    rows = [
        {"campaign": c, "state": state_label(c), "landing": _landing(request, c)} for c in campaigns
    ]
    return render_app(
        request,
        "flamingo/app/campaigns/list.html",
        "campaigns",
        {
            "rows": rows,
            "has_services": account.services.filter(is_active=True).exists(),
        },
    )


# ---------------------------------------------------------------------------
# Ny kampanj (kundresan 06)
# ---------------------------------------------------------------------------


class NewCampaignForm(forms.Form):
    """Tjänsten väljs i adressen (?tjanst=<id> eller ?tjanst=ny) och skickas
    som ett dolt fält; vyn prövar den mot kundens egna tjänster."""

    new_service = forms.CharField(
        label="Tjänstens namn",
        required=False,
        max_length=120,
    )
    sales_mode = forms.ChoiceField(
        label="Hur köper kunderna den?",
        choices=Service.SALES_CHOICES,
        error_messages={"required": "Välj hur kunderna köper tjänsten."},
    )
    place = forms.CharField(
        label="Ort eller orter",
        max_length=160,
        error_messages={"required": "Skriv var annonserna ska visas."},
    )
    radius_km = forms.TypedChoiceField(
        label="Och runt omkring",
        choices=[(r, f"{r} km") for r in RADIUS_CHOICES],
        coerce=int,
        error_messages={"invalid_choice": "Välj ett avstånd i listan."},
    )
    budget = forms.TypedChoiceField(
        label="Budget per dag",
        choices=[(b, f"{b} kr") for b in BUDGET_PRESETS],
        coerce=int,
        required=False,
        empty_value=None,
    )
    budget_own = forms.IntegerField(
        label="Eget belopp per dag",
        required=False,
        min_value=checks.BUDGET_MIN,
        max_value=checks.BUDGET_MAX,
        error_messages={
            "invalid": "Skriv beloppet i hela kronor.",
            "min_value": BUDGET_MIN_MESSAGE,
            "max_value": BUDGET_MAX_MESSAGE,
        },
    )

    def __init__(self, *args, service=None, **kwargs):
        self.service = service
        super().__init__(*args, **kwargs)

    def clean_new_service(self):
        name = sanitize_plain_text(self.cleaned_data.get("new_service"), max_length=120)
        if checks.starts_like_formula(name):
            raise forms.ValidationError(checks.FORMULA_MESSAGE)
        return name

    def clean_place(self):
        place = sanitize_plain_text(self.cleaned_data.get("place"), max_length=160)
        place = generator.place_of(place)
        if not place:
            raise forms.ValidationError("Skriv var annonserna ska visas.")
        return place

    def clean(self):
        data = super().clean()
        if self.service is None and not data.get("new_service"):
            self.add_error("new_service", "Skriv vilken tjänst kampanjen gäller.")
        own, preset = data.get("budget_own"), data.get("budget")
        if own is None and preset is None and "budget_own" not in self.errors:
            self.add_error("budget_own", "Välj en budget per dag.")
        data["daily_budget_kr"] = own if own is not None else preset
        mode = data.get("sales_mode")
        if self.service is not None and mode and mode != self.service.sales_mode:
            busy = self.service.campaigns.exclude(status=Campaign.STATUS_DRAFT).exists()
            if busy:
                self.add_error(
                    "sales_mode",
                    "Tjänsten har en kampanj hos ADX eller live. Be ADX ändra "
                    "hur den säljs, så följer sidorna med.",
                )
        return data


def _chosen_service(account, value, services):
    """(tjänst, ny?) ur ?tjanst= eller det dolda fältet. Ett id prövas alltid
    mot kundens egna aktiva tjänster: någon annans id ger 404."""
    value = (value or "").strip()
    if value == "ny" or not services:
        return None, True
    if not value:
        # Förval: första tjänsten som inte redan har en kampanj.
        busy = set(account.campaigns.values_list("service_id", flat=True))
        free = [s for s in services if s.pk not in busy]
        return (free or services)[0], False
    try:
        pk = int(value)
    except ValueError:
        raise Http404 from None
    service = next((s for s in services if s.pk == pk), None)
    if service is None:
        raise Http404
    return service, False


def confirmed_place(account):
    """Orten ur kundens bekräftade uppgift om området, eller ""."""
    for fact in generator.confirmed_fact_rows(account):
        if generator.fact_kind(fact) == "area":
            return sanitize_plain_text(fact.value, max_length=160)
    return ""


def _default_place(account):
    last = account.campaigns.exclude(area="").order_by("-created_at", "-id").first()
    if last:
        return generator.place_of(last.area)
    return confirmed_place(account)


@app_view
def campaign_new(request, account):
    services = list(account.services.filter(is_active=True).order_by("order", "id"))
    picked = request.POST.get("service") if request.method == "POST" else request.GET.get("tjanst")
    service, is_new = _chosen_service(account, picked, services)

    if request.method == "POST":
        form = NewCampaignForm(request.POST, service=service)
        if form.is_valid():
            campaign, proposal = _create_campaign(request, account, service, form.cleaned_data)
            if proposal.source == generator.SOURCE_AI:
                text = "Förslaget är klart. AI skrev texterna från dina bekräftade uppgifter."
            else:
                text = "Förslaget är klart. Texterna bygger på dina bekräftade uppgifter."
                if proposal.note:
                    text += f" {proposal.note}"
            messages.success(
                request, f"{text} Ändra det som känns fel och skicka det när du är klar."
            )
            return redirect(_detail_url(campaign, "annonser"))
    else:
        form = NewCampaignForm(
            service=service,
            initial={
                "sales_mode": service.sales_mode if service else Service.SALES_QUOTE,
                "place": _default_place(account),
                "radius_km": DEFAULT_RADIUS,
                "budget": DEFAULT_BUDGET,
            },
        )

    def value(name):
        if form.is_bound:
            return form.data.get(name, "")
        return form.initial.get(name, "")

    selected_budget = str(value("budget") or "")
    return render_app(
        request,
        "flamingo/app/campaigns/new.html",
        "campaigns",
        {
            "form": form,
            "services": services,
            "service": service,
            "is_new_service": is_new,
            "values": {
                "new_service": value("new_service"),
                "sales_mode": value("sales_mode"),
                "place": value("place"),
                "radius_km": str(value("radius_km") or DEFAULT_RADIUS),
                "budget": selected_budget,
                "budget_own": value("budget_own"),
            },
            "sales_choices": Service.SALES_CHOICES,
            "radius_choices": RADIUS_CHOICES,
            "budget_presets": [{"kr": kr, "monthly": monthly_kr(kr)} for kr in BUDGET_PRESETS],
            "budget_min": checks.BUDGET_MIN,
            "budget_max": checks.BUDGET_MAX,
            "unconfirmed_count": account.facts.filter(confirmed=False).count(),
            "confirmed_count": len(account.confirmed_facts()),
        },
    )


def _create_campaign(request, account, service, data):
    place, radius = data["place"], data["radius_km"]
    with transaction.atomic():
        if service is None:
            order = (account.services.aggregate(m=Max("order"))["m"] or 0) + 1
            service = Service.objects.create(
                account=account,
                name=data["new_service"],
                sales_mode=data["sales_mode"],
                order=order,
            )
        elif service.sales_mode != data["sales_mode"]:
            # Sättet att sälja hör till tjänsten (Campaign.sales_mode läser
            # därifrån). Formuläret har redan stoppat bytet om tjänsten har
            # en kampanj som inte är ett utkast.
            service.sales_mode = data["sales_mode"]
            service.save(update_fields=["sales_mode"])
        first_place = generator.places_of(place)[:1]
        name = " ".join([service.name, *first_place])[:120]
        campaign = Campaign.objects.create(
            account=account,
            service=service,
            name=name,
            status=Campaign.STATUS_DRAFT,
            area=area_text(place, radius),
            radius_km=radius,
            daily_budget_kr=data["daily_budget_kr"],
            created_by=request.user,
        )
    # Utanför transaktionen: AI-anropet kan ta några sekunder.
    proposal = generator.build_proposal(campaign, user=request.user)
    return campaign, proposal


# ---------------------------------------------------------------------------
# Förslaget och granskningen (kundresan 07 och 08)
# ---------------------------------------------------------------------------


def _text_rows(items, slots, field, limit, problems):
    """En rad per text och några tomma att fylla i (högst slots rader, men
    aldrig färre än det som finns)."""
    rows = []
    count = max(len(items), min(slots, max(len(items) + BLANK_TEXT_ROWS, MIN_TEXT_ROWS)))
    for i in range(count):
        text = str(items[i]) if i < len(items) else ""
        rows.append(
            {
                "number": i + 1,
                "value": text,
                "length": len(text),
                "limit": limit,
                "over": len(text) > limit,
                "errors": [p.message for p in problems if p.field == field and p.index == i],
            }
        )
    return rows


def _field_errors(problems, field):
    return [p.message for p in problems if p.field == field and p.index is None]


_PHONE_LIKE = re.compile(r"\+?[\d][\d \-]{4,}\d")


def fact_segments(text, needles):
    """Texten i bitar, där bekräftade uppgifter är markerade (förhandsvisningen
    stryker under dem, som i kundresan): [(text, "" | "fact" | "phone")]."""
    needles = sorted({n for n in needles if len(n) >= 3}, key=len, reverse=True)
    if not needles:
        return [(text, "")]
    pattern = re.compile("|".join(re.escape(n) for n in needles), re.IGNORECASE)
    out, pos = [], 0
    for match in pattern.finditer(text):
        if match.start() > pos:
            out.append((text[pos : match.start()], ""))
        fact = match.group()
        # Ett telefonnummer bryts inte mitt i på en smal skärm (.fl-fact--phone).
        out.append((fact, "phone" if _PHONE_LIKE.fullmatch(fact) else "fact"))
        pos = match.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out


def _ad_previews(request, campaign, fact_values):
    """Två till tre kombinationer av rubriker och beskrivningar, som Google
    kan visa dem. Google väljer själv; det här är exempel."""
    h = [str(x) for x in campaign.headlines or []]
    d = [str(x) for x in campaign.descriptions or []]
    if not h:
        return []
    combos = [((0, 1), (0,)), ((2, 0), (1,)), ((1, 3), (2,))]
    previews, seen = [], set()
    for heads, descs in combos:
        heads = [i for i in heads if i < len(h)]
        descs = [i for i in descs if i < len(d)] or ([0] if d else [])
        key = (tuple(heads), tuple(descs))
        if not heads or key in seen:
            continue
        seen.add(key)
        previews.append(
            {
                "headline": " | ".join(h[i] for i in heads),
                "description": [
                    seg for i in descs for seg in fact_segments(d[i] + " ", fact_values)
                ],
            }
        )
    host = request.get_host()
    for preview in previews:
        preview["url"] = f"{host}{campaign.landing_url}".rstrip("/")
    return previews


def _page_for_form(campaign):
    page = campaign.page if isinstance(campaign.page, dict) else {}
    questions = []
    for q in page.get("questions") or []:
        if isinstance(q, dict) and q.get("label"):
            kind = q.get("kind") if q.get("kind") in PAGE_QUESTION_KINDS else "text"
            questions.append({"label": str(q["label"]), "kind": kind})
    phone = str(page.get("phone") or "")
    return {
        "title": str(page.get("title") or ""),
        "lead": str(page.get("lead") or ""),
        "points": [str(p) for p in page.get("points") or [] if str(p).strip()],
        "phone": phone,
        "form_title": str(page.get("form_title") or ""),
        "questions": questions,
        "note": str(page.get("note") or ""),
    }


#: Mallen landningssidan ritas med (apps/flamingo/public_views.py).
LANDING_TEMPLATE = "flamingo/lp/page.html"


def landing_preview(request, campaign):
    """Landningssidan som besökaren ser den: samma mall och samma innehåll
    (public_views.page_content och LeadForm), i förhandsvisningsläge. Ritas
    i en iframe med sandbox (inga skript, formuläret går inte att skicka).
    None om den inte går att rita; då visar sidan telefonbilden
    (_page_preview.html) i stället, så redigeraren aldrig fälls av den."""
    try:
        from ..public_views import HONEYPOT, LeadForm, page_content

        content = page_content(campaign)
        form = LeadForm(content=content)
        context = {
            "campaign": campaign,
            "content": content,
            "preview": True,
            "is_staff": False,
            "status_label": "",
            "form": form,
            "questions": [{**q, "bound": form[q["field"]]} for q in content["questions"]],
            "tracking": {},
            "honeypot": HONEYPOT,
            "action": "#",
            "review_url": "",
        }
        return render_to_string(LANDING_TEMPLATE, context, request=request)
    except Exception:  # noqa: BLE001 - förhandsvisningen får aldrig fälla redigeraren
        logger.exception("Landningssidans förhandsvisning gick inte att rita")
        return None


def _page_errors(problems):
    errors = {part: [] for part in PAGE_PART_LABELS}
    for p in problems:
        if p.field != "page":
            continue
        message = p.message
        if p.index is not None:
            message = f"Rad {p.index + 1}: {message}"
        errors.setdefault(p.part or "title", []).append(message)
    return errors


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_as_text(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_as_text(v)}" for k, v in value.items())
    return str(value)


#: Granskarens anteckning när inget ändrats upprepar ofta bara rubriken
#: ("Inga ändringar"); en sådan visas inte.
_NOTHING_CHANGED_NOTES = frozenset(
    {"inget att ändra", "inga ändringar", "inget ändrat", "ingenting att ändra", "inget"}
)


def review_note(note, changes):
    """Anteckningen att visa för kunden, eller "" om den inte säger något
    mer än att inget ändrades."""
    note = (note or "").strip()
    if not note or changes:
        return note
    plain = " ".join(re.sub(r"[^\w ]+", " ", note.casefold()).split())
    return "" if plain in _NOTHING_CHANGED_NOTES else note


def _earlier(reviews):
    return [{"round": r.round, "submitted": _when(r.submitted_at), "review": r} for r in reviews]


def _direct_timeline(campaign):
    """Tidslinjen för en kampanj som skickades utan granskning: inskicket
    var godkännandet, sedan live eller "ADX publicerar"."""
    who = _first_name(campaign.approved_by)
    timeline = [
        {
            "state": "done",
            "title": f"Skickad av {who}" if who else "Skickad av dig",
            "meta": f"{_when(campaign.approved_at)}, utan granskning".strip(", "),
        },
        {"state": "done", "title": "Kontrollerna gick igenom", "meta": ""},
    ]
    if campaign.published_at is not None:
        timeline.append({"state": "done", "title": "Live", "meta": _when(campaign.published_at)})
    else:
        timeline.append(_publish_step(campaign))
    return timeline


def _publish_step(campaign):
    """Steget efter godkännandet när kampanjen inte är live än. Inga tider:
    ADX publicerar, eller kontot ska kopplas först."""
    if not campaign.account.google_linked:
        return {
            "state": "now",
            "title": "Väntar på att ditt Google Ads-konto kopplas under ADX",
            "meta": "",
        }
    return {"state": "now", "title": "ADX publicerar", "meta": ""}


def _review_context(campaign):
    reviews = list(campaign.reviews.select_related("reviewer", "submitted_by").order_by("-round"))
    latest = reviews[0] if reviews else None
    direct = (
        not campaign.review_requested
        and campaign.approved_at is not None
        and campaign.status != Campaign.STATUS_DRAFT
    )
    if direct:
        # Skickad utan granskning: inskicket var godkännandet. Tidigare
        # rundor (från ett inskick med granskning) står kvar under.
        return {
            "review": None,
            "direct": True,
            "changes": [],
            "change_groups": [],
            "timeline": _direct_timeline(campaign),
            "earlier": _earlier(reviews),
        }
    if latest is None:
        return {"review": None, "changes": [], "change_groups": [], "timeline": [], "earlier": []}
    changes = []
    for change in latest.changes or []:
        if not isinstance(change, dict):
            continue
        changes.append(
            {
                "label": _as_text(change.get("label")) or FIELD_LABELS.get(change.get("field"), ""),
                "part": _as_text(change.get("part")),
                "before": _as_text(change.get("before")),
                "after": _as_text(change.get("after")),
                "reason": _as_text(change.get("reason")),
            }
        )
    # Flera ändringar i samma del med samma skäl (granskningen skriver ett
    # skäl per del) visas under en rubrik, med skälet en gång.
    change_groups = []
    for change in changes:
        key = (change["part"] or change["label"], change["reason"])
        if change_groups and change_groups[-1]["key"] == key:
            change_groups[-1]["items"].append(change)
        else:
            change_groups.append(
                {
                    "key": key,
                    "label": change["label"],
                    "reason": change["reason"],
                    "items": [change],
                }
            )
    reviewer = _first_name(latest.reviewer)
    reviewer_text = f"{reviewer} på ADX" if reviewer else "ADX"
    # Byrån kan ta tillbaka en godkänd kampanj till granskning (Google sa nej
    # till något): då skickade inte kunden den. Byrån som skickar in i
    # kundvyn skickar som kunden och räknas inte hit.
    taken_back = latest.taken_back
    timeline = [
        {
            "state": "done",
            "title": "ADX tog tillbaka kampanjen för granskning"
            if taken_back
            else "Skickat till granskning",
            "meta": _when(latest.submitted_at),
        }
    ]
    if latest.state == Review.STATE_PENDING:
        timeline.append(
            {
                "state": "now",
                "title": "ADX granskar förslaget",
                "meta": "Inget publiceras utan ditt godkännande",
            }
        )
        timeline.append({"state": "todo", "title": "Ditt godkännande", "meta": ""})
    else:
        count = len(changes)
        changed = (
            "inga ändringar" if not count else f"{count} {'ändring' if count == 1 else 'ändringar'}"
        )
        timeline.append(
            {
                "state": "done",
                "title": f"{reviewer_text} granskade",
                "meta": f"{_when(latest.reviewed_at)}, {changed}".strip(", "),
            }
        )
        if campaign.status == Campaign.STATUS_LIVE:
            timeline.append(
                {"state": "done", "title": "Live", "meta": _when(campaign.published_at)}
            )
        elif is_approved(campaign):
            who = _first_name(campaign.approved_by)
            timeline.append(
                {
                    "state": "done",
                    "title": f"Godkänd av {who}" if who else "Godkänd av dig",
                    "meta": _when(campaign.approved_at),
                }
            )
            timeline.append(_publish_step(campaign))
        elif campaign.status == Campaign.STATUS_NEEDS_CUSTOMER:
            timeline.append({"state": "now", "title": "Väntar på ditt godkännande", "meta": ""})
    return {
        "review": latest,
        "review_note": review_note(latest.note, changes),
        "reviewer_text": reviewer_text,
        "reviewer_name": reviewer,
        "changes": changes,
        "change_groups": change_groups,
        "timeline": timeline,
        "earlier": _earlier(reviews[1:]),
    }


def _default_tab(campaign):
    if campaign.status in (Campaign.STATUS_NEEDS_CUSTOMER, Campaign.STATUS_IN_REVIEW):
        return "granskning"
    return "annonser"


def _summary(problems):
    """Problemen som en lista att rätta, med länk till fliken."""
    out = []
    for p in problems:
        tab = FIELD_TABS.get(p.field, "annonser")
        label = FIELD_LABELS.get(p.field, "")
        if p.field == "page" and p.part:
            label = f"Sidan, {PAGE_PART_LABELS.get(p.part, '').lower()}"
        if p.index is not None and p.field in ("headlines", "descriptions", "keywords"):
            label = f"{label}, rad {p.index + 1}"
        out.append({"tab": tab, "label": label, "message": p.message})
    return out


@app_view
def campaign_detail(request, account, pk):
    campaign = get_object_or_404(
        Campaign.objects.select_related("service", "account__customer", "approved_by"),
        pk=pk,
        account=account,
    )
    if request.method == "POST":
        return _edit(request, campaign)

    tab = request.GET.get("flik", "")
    if tab not in TAB_KEYS:
        tab = _default_tab(campaign)
    read_only = request.flamingo.read_only
    can_edit = campaign.customer_can_edit and not read_only
    problems = checks.validate(campaign)
    fact_values = list(account.confirmed_facts().values())
    tab_problems = {key: 0 for key in TAB_KEYS}
    for p in problems:
        if FIELD_TABS.get(p.field):
            tab_problems[FIELD_TABS[p.field]] += 1

    page = _page_for_form(campaign)
    previews = _ad_previews(request, campaign, fact_values)
    keywords = [
        {
            "text": str(k.get("text", "")),
            "match": k.get("match") or MATCH_PHRASE,
            "match_label": dict(MATCH_CHOICES).get(k.get("match"), "Fras"),
            "errors": [p.message for p in problems if p.field == "keywords" and p.index == i],
        }
        for i, k in enumerate(k for k in campaign.keywords or [] if isinstance(k, dict))
    ]
    context = {
        "campaign": campaign,
        "tab": tab,
        "tabs": [
            {
                "key": key,
                "label": label,
                "url": _detail_url(campaign, key),
                "problems": tab_problems[key],
            }
            for key, label in TABS
        ],
        "can_edit": can_edit,
        "state_label": state_label(campaign),
        "is_approved": is_approved(campaign),
        "landing": _landing(request, campaign),
        "monthly_kr": monthly_kr(campaign.daily_budget_kr),
        "place": generator.place_of(campaign.area),
        "radius_choices": RADIUS_CHOICES,
        "problems": problems,
        "problem_summary": _summary(problems),
        "settings_errors": _field_errors(problems, "area")
        + _field_errors(problems, "daily_budget_kr"),
        "budget_min": checks.BUDGET_MIN,
        "budget_max": checks.BUDGET_MAX,
        "headline_rows": _text_rows(
            campaign.headlines or [], HEADLINE_COUNT, "headlines", HEADLINE_MAX, problems
        ),
        "description_rows": _text_rows(
            campaign.descriptions or [],
            DESCRIPTION_COUNT,
            "descriptions",
            DESCRIPTION_MAX,
            problems,
        ),
        "headline_errors": _field_errors(problems, "headlines"),
        "description_errors": _field_errors(problems, "descriptions"),
        "headline_max": HEADLINE_MAX,
        "description_max": DESCRIPTION_MAX,
        "headline_count": HEADLINE_COUNT,
        "description_count": DESCRIPTION_COUNT,
        "previews": previews,
        "previews_mark_facts": any(is_fact for ad in previews for _, is_fact in ad["description"]),
        "keywords": keywords,
        "keyword_errors": _field_errors(problems, "keywords"),
        "blank_keyword_rows": range(BLANK_KEYWORD_ROWS),
        "match_choices": MATCH_CHOICES,
        "negatives": [str(n) for n in campaign.negatives or []],
        "negatives_text": "\n".join(str(n) for n in campaign.negatives or []),
        "page": page,
        "page_errors": _page_errors(problems),
        "points_text": "\n".join(page["points"]),
        "question_rows": page["questions"] + [{"label": "", "kind": "text"}],
        "question_kinds": [(k, QUESTION_KIND_LABELS[k]) for k in PAGE_QUESTION_KINDS],
        "company": generator.company_name(account.customer),
        "page_host": request.get_host(),
        "landing_html": landing_preview(request, campaign) if tab == "sidan" else None,
        # Kampanjen kan gå live när kontot är kopplat under ADX; betalningen
        # stoppar inte (beslut 2026-10-03).
        "google_linked": account.google_linked,
        # Vad inskicket utan granskning leder till (knappen och texten).
        "approval_path": google_publish.approval_path(account),
    }
    context.update(_review_context(campaign))
    return render_app(request, "flamingo/app/campaigns/detail.html", "campaigns", context)


# ---------------------------------------------------------------------------
# Ändringar i förslaget (POST till kampanjsidan)
# ---------------------------------------------------------------------------

#: section i formuläret -> fliken man kommer tillbaka till.
SECTION_TABS = {
    "ads": "annonser",
    "keywords": "sokord",
    "page": "sidan",
    "settings": "annonser",
    "regenerate": "annonser",
}


def _lines(text, max_items, max_length):
    out = []
    for line in sanitize_multiline_text(text or "", max_length=10_000).splitlines():
        line = sanitize_plain_text(line, max_length=max_length).strip(" -*")
        if line and line not in out:
            out.append(line)
    return out[:max_items]


def _apply_ads(campaign, post):
    def texts(name, count):
        values = [sanitize_plain_text(v, max_length=200) for v in post.getlist(name)]
        return [v for v in values if v][:count]

    campaign.headlines = texts("headline", HEADLINE_COUNT)
    campaign.descriptions = texts("description", DESCRIPTION_COUNT)
    return ["headlines", "descriptions"]


def _keyword_text(value):
    """Sökordet som det söks: gemener, utan Googles skrivsätt för typ
    ("..." och hakparenteser, + och ett inledande minus), ett mellanslag
    mellan orden."""
    value = sanitize_plain_text(value, max_length=200).lower()
    value = re.sub(r'["\[\]+]', " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value.lstrip("-= @").strip()


def _apply_keywords(campaign, post):
    valid = dict(MATCH_CHOICES)
    keywords = []
    for text, match in zip(post.getlist("kw_text"), post.getlist("kw_match"), strict=False):
        text = _keyword_text(text)
        entry = {"text": text, "match": match if match in valid else MATCH_PHRASE}
        if text and entry not in keywords:
            keywords.append(entry)
    campaign.keywords = keywords[:MAX_KEYWORDS]
    negatives = []
    for line in _lines(post.get("negatives"), MAX_NEGATIVES, 80):
        text = _keyword_text(line)
        if text and text not in negatives:
            negatives.append(text)
    campaign.negatives = negatives
    return ["keywords", "negatives"]


def _apply_page(campaign, post):
    page = dict(campaign.page) if isinstance(campaign.page, dict) else {}
    page["title"] = sanitize_plain_text(post.get("title"), max_length=120)
    page["lead"] = sanitize_multiline_text(post.get("lead"), max_length=500)
    page["points"] = _lines(post.get("points"), MAX_POINTS, 120)
    page["phone"] = sanitize_plain_text(post.get("phone"), max_length=40)
    page["form_title"] = sanitize_plain_text(post.get("form_title"), max_length=120)
    page["note"] = sanitize_multiline_text(post.get("note"), max_length=500)
    questions, keys = [], set()
    for label, kind in zip(post.getlist("q_label"), post.getlist("q_kind"), strict=False):
        label = sanitize_plain_text(label, max_length=120)
        if not label:
            continue
        key = slugify(label)[:40] or f"fraga-{len(questions) + 1}"
        base, n = key, 2
        while key in keys:
            key, n = f"{base}-{n}", n + 1
        keys.add(key)
        kind = kind if kind in PAGE_QUESTION_KINDS else "text"
        questions.append({"key": key, "label": label, "kind": kind})
    page["questions"] = questions[:MAX_QUESTIONS]
    campaign.page = page
    return ["page"]


def budget_error(budget):
    """Samma besked som formuläret för en ny kampanj, eller "" när beloppet
    ligger mellan BUDGET_MIN och BUDGET_MAX."""
    if budget < checks.BUDGET_MIN:
        return BUDGET_MIN_MESSAGE
    if budget > checks.BUDGET_MAX:
        return BUDGET_MAX_MESSAGE
    return ""


def _apply_settings(campaign, post):
    place = generator.place_of(sanitize_plain_text(post.get("place"), max_length=160))
    try:
        radius = int(post.get("radius_km", ""))
        budget = int(str(post.get("daily_budget_kr", "")).replace(" ", ""))
    except ValueError:
        return None, "Skriv budgeten i hela kronor."
    if radius not in RADIUS_CHOICES:
        radius = DEFAULT_RADIUS
    # Prövas innan något tilldelas: ett för stort tal ska ge ett besked, inte
    # ett serverfel när databasen inte rymmer det.
    error = budget_error(budget)
    if error:
        return None, error
    campaign.area = area_text(place, radius)
    campaign.radius_km = radius
    campaign.daily_budget_kr = budget
    return ["area", "radius_km", "daily_budget_kr"], ""


def _reopen(campaign):
    """En ändring efter granskningen gör kampanjen till ett utkast igen (och
    ett godkännande gäller inte längre): ny runda hos ADX."""
    if campaign.status != Campaign.STATUS_NEEDS_CUSTOMER:
        return []
    campaign.status = Campaign.STATUS_DRAFT
    campaign.approved_at = None
    campaign.approved_by = None
    return ["status", "approved_at", "approved_by"]


def _locked_message(campaign):
    if campaign.status == Campaign.STATUS_IN_REVIEW:
        return "Kampanjen är hos ADX för granskning. Du kan ändra den när granskningen är klar."
    return "Kampanjen är publicerad. Vill du ändra den: skriv till ADX, så går ändringen samma väg."


REOPENED_NOTE = " Kampanjen är ett utkast igen: skicka den på nytt när du är klar."


def _edit(request, campaign):
    section = request.POST.get("section", "")
    tab = SECTION_TABS.get(section, "annonser")
    target = _detail_url(campaign, tab)
    if section == "regenerate":
        return _regenerate(request, campaign, target)
    with transaction.atomic():
        campaign = _locked(campaign)
        if not campaign.customer_can_edit:
            messages.error(request, _locked_message(campaign))
            return redirect(target)

        if section == "ads":
            fields, text = _apply_ads(campaign, request.POST), "Annonserna är sparade."
        elif section == "keywords":
            fields, text = _apply_keywords(campaign, request.POST), "Sökorden är sparade."
        elif section == "page":
            fields, text = _apply_page(campaign, request.POST), "Sidan är sparad."
        elif section == "settings":
            fields, error = _apply_settings(campaign, request.POST)
            if fields is None:
                messages.error(request, error)
                return redirect(target)
            text = "Område och budget är sparade."
        else:
            return redirect(target)

        reopened = _reopen(campaign)
        campaign.save(update_fields=[*fields, *reopened, "updated_at"])
    if reopened:
        text += REOPENED_NOTE
    messages.success(request, text)
    return redirect(target)


def _regenerate(request, campaign, target):
    """Ett nytt förslag. Modellanropet kan ta flera sekunder, så det görs
    utan lås: låsa och läsa (får kampanjen ändras?), släppa, skriva
    förslaget, och sedan låsa igen och spara bara om kampanjen fortfarande
    går att ändra (den kan ha skickats till granskning under tiden)."""
    with transaction.atomic():
        current = _locked(campaign)
        if not current.customer_can_edit:
            messages.error(request, _locked_message(current))
            return redirect(target)
    proposal = generator.build_proposal(current, user=request.user, save=False)
    with transaction.atomic():
        current = _locked(campaign)
        if not current.customer_can_edit:
            messages.error(request, _locked_message(current))
            return redirect(target)
        generator.apply_proposal(current, proposal)
        reopened = _reopen(current)
        current.save(update_fields=[*generator.PROPOSAL_FIELDS, *reopened, "updated_at"])
    text = "Ett nytt förslag är skrivet."
    if proposal.note:
        text += f" {proposal.note}"
    if reopened:
        text += REOPENED_NOTE
    messages.success(request, text)
    return redirect(target)


# ---------------------------------------------------------------------------
# Inskick och godkännande
# ---------------------------------------------------------------------------


#: Kundens kryssruta vid inskicket (av från början).
REVIEW_FIELD = "review"


def _billing_note(account):
    """Påminnelsen om betalningen när kampanjen just blev live."""
    if google_publish.billing_missing(account):
        return " Annonserna visas när betalningen är inlagd hos Google."
    return ""


def _published_text(outcome, account, prefix):
    """Kundens besked efter godkännandet. Lugnt och utan tider: live, eller
    att ADX publicerar (kön, med orsaken för byrån i google_error)."""
    if outcome.is_live:
        return (
            f"{prefix} Kampanjen är live. Google granskar varje annons innan den visas."
            + _billing_note(account)
        )
    if outcome.kind == google_publish.OUTCOME_NOT_LINKED:
        return f"{prefix} ADX publicerar kampanjen när ditt Google Ads-konto är kopplat under ADX."
    return f"{prefix} ADX publicerar kampanjen, och du ser här när den är live."


def _outcome_lines(outcome):
    """Vad som hände efter godkännandet, för byråns larm."""
    queue = "Den ligger under Godkända, ej publicerade i kön."
    if outcome.is_live:
        return [
            f"Den är live hos Google (kampanj {outcome.campaign_id}), och landningssidan är "
            "öppen. Inget mer att göra."
        ]
    if outcome.kind == google_publish.OUTCOME_FAILED:
        return [
            f"Den publicerades inte hos Google: {outcome.reason}",
            f"{queue} Publicera från granskningssidan när det är rättat.",
        ]
    if outcome.kind == google_publish.OUTCOME_NOT_LINKED:
        return [
            "Den är inte publicerad: kundens Google Ads-konto är inte kopplat under ADX med "
            "ett id.",
            f"{queue} Koppla kontot och publicera sedan från granskningssidan.",
        ]
    return [
        "Google Ads API är inte inkopplat, så den publiceras för hand med Editor-filen på "
        "granskningssidan.",
        queue,
    ]


def _outcome_subject(outcome, campaign, customer):
    if outcome.is_live:
        return f"Flamingo: {campaign.name} är live ({customer.name})"
    return f"Flamingo: publicera {campaign.name} ({customer.name})"


@app_view
def campaign_submit(request, account, pk):
    """Skicka in (POST): bara från utkast och bara när kontrollerna går
    igenom. GET (till exempel efter grindens skrivskydd) visar bara
    kampanjen.

    Med kryssrutan "Jag vill att ADX granskar kampanjen innan den
    publiceras" (review=1): en Review-runda (pending) med en ögonblicksbild
    av innehållet och status in_review, som förut.

    Utan kryssrutan (beslut 2026-10-03, granskningen är kundens val):
    inskicket är kundens godkännande. approved_at och approved_by sätts,
    ingen runda skapas, och google_publish.publish_approved publicerar direkt
    när API:t är inkopplat och kontot kopplat under ADX. Annars, eller om
    Google säger nej, står kampanjen som godkänd men inte publicerad i
    byråns kö.

    Byrån larmas i båda fallen, med vad som hände; kunden mejlas inte."""
    campaign = get_object_or_404(Campaign, pk=pk, account=account)
    if request.method != "POST":
        return redirect(_detail_url(campaign))
    wants_review = request.POST.get(REVIEW_FIELD) == "1"
    with transaction.atomic():
        campaign = _locked(campaign)
        if campaign.status != Campaign.STATUS_DRAFT:
            messages.info(request, "Kampanjen är redan skickad.")
            return redirect(_detail_url(campaign, "granskning"))
        problems = checks.validate(campaign)
        if problems:
            count = len(problems)
            word = "sak" if count == 1 else "saker"
            messages.error(
                request,
                f"Rätta {count} {word} först. Det som behöver ändras står vid fälten.",
            )
            first_tab = next(
                (FIELD_TABS.get(p.field) for p in problems if FIELD_TABS.get(p.field)), ""
            )
            return redirect(_detail_url(campaign, first_tab or "annonser"))
        review = None
        campaign.review_requested = wants_review
        if wants_review:
            review = Review.objects.create(
                campaign=campaign,
                round=campaign.next_round(),
                submitted_by=request.user,
                snapshot=campaign.content_snapshot(),
            )
            campaign.status = Campaign.STATUS_IN_REVIEW
            campaign.approved_at = None
            campaign.approved_by = None
        else:
            # Inskicket är godkännandet: samma läge som efter ett godkännande
            # av en granskad kampanj (godkänd, inte publicerad).
            campaign.status = Campaign.STATUS_NEEDS_CUSTOMER
            campaign.approved_at = timezone.now()
            campaign.approved_by = request.user
        campaign.save(
            update_fields=["status", "review_requested", "approved_at", "approved_by", "updated_at"]
        )

    customer = account.customer
    about = [
        f"Tjänst: {campaign.service.name}, {campaign.get_sales_mode_display().lower()}.",
        f"Område: {campaign.area}. Budget: {campaign.daily_budget_kr} kr per dag.",
    ]
    if review is not None:
        _alert_agency(
            request,
            campaign,
            f"Flamingo: granska {campaign.name} ({customer.name})",
            [
                f"{customer.name} har skickat kampanjen {campaign.name} och bett ADX granska "
                f"den innan den publiceras (runda {review.round}).",
                *about,
            ],
        )
        messages.success(
            request,
            "Skickat till granskning. En person på ADX går igenom kampanjen, och du godkänner "
            "ändringarna innan den publiceras.",
        )
        return redirect(_detail_url(campaign, "granskning"))

    outcome = google_publish.publish_approved(campaign, request.user)
    _alert_agency(
        request,
        campaign,
        _outcome_subject(outcome, campaign, customer),
        [
            f"{customer.name} har skickat kampanjen {campaign.name} utan att be om granskning. "
            "Kontrollerna gick igenom, och inskicket är kundens godkännande.",
            *_outcome_lines(outcome),
            *about,
        ],
    )
    messages.success(request, _published_text(outcome, account, "Skickad."))
    return redirect(_detail_url(campaign, "granskning"))


@app_view
def campaign_approve(request, account, pk):
    """Kundens godkännande (POST) av det ADX granskat, bara när granskningen
    är klar (status needs_customer och senaste rundan done).

    Godkännandet sätter approved_at och approved_by, och publicerar sedan
    direkt med google_publish.publish_approved när API:t är inkopplat och
    kontot kopplat under ADX. Annars (eller om Google säger nej) står status
    kvar needs_customer i byråns kö, och sidan visar "Godkänd av dig, ADX
    publicerar". Ändrar kunden något efter godkännandet blir kampanjen ett
    utkast igen och godkännandet nollställs (_reopen). Byrån larmas med vad
    som hände; kunden mejlas inte."""
    campaign = get_object_or_404(Campaign, pk=pk, account=account)
    if request.method != "POST":
        return redirect(_detail_url(campaign))
    with transaction.atomic():
        campaign = _locked(campaign)
        latest = campaign.latest_review()
        ready = (
            campaign.status == Campaign.STATUS_NEEDS_CUSTOMER
            and latest is not None
            and latest.state == Review.STATE_DONE
        )
        if not ready:
            messages.info(request, "Det finns inget att godkänna just nu.")
            return redirect(_detail_url(campaign))
        if campaign.approved_at is not None:
            messages.info(request, "Du har redan godkänt kampanjen. ADX publicerar den.")
            return redirect(_detail_url(campaign, "granskning"))
        campaign.approved_at = timezone.now()
        campaign.approved_by = request.user
        campaign.save(update_fields=["approved_at", "approved_by", "updated_at"])
    outcome = google_publish.publish_approved(campaign, request.user)
    customer = account.customer
    _alert_agency(
        request,
        campaign,
        _outcome_subject(outcome, campaign, customer),
        [
            f"{customer.name} har godkänt kampanjen {campaign.name} efter runda {latest.round}.",
            *_outcome_lines(outcome),
        ],
    )
    messages.success(request, _published_text(outcome, account, "Godkänd."))
    return redirect(_detail_url(campaign, "granskning"))
