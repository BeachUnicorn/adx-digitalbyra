"""
Kundens landningssidor (/lp/<slug>/, kundresan steg 9): publika, till för
kundens kunder.

- Bara en kampanj som är live, på ett aktiverat konto hos en aktiv kund, har
  en publik sida. Allt annat är 404 (sajtens vanliga 404-sida).
- Byrån (staff) kan förhandsvisa varje kampanj, oavsett status, med en
  synlig remsa överst. Formuläret skickar inget i förhandsvisningen.
- Sidan är kundens egen: kundens namn, ingen Flamingo- eller ADX-design.
  Innehållet kommer från campaign.page och kundens bekräftade uppgifter.
  Ett betyg visas bara om uppgiften är bekräftad OCH kommer från Google
  (eller ADX): ett betyg från hemsidan eller som kunden skrivit själv visas
  aldrig för allmänheten.
- Formuläret skapar en Lead (limits.create_form_lead, leads.create_lead)
  med klick-id och utm ur adressen, och sms.notify_new_lead skickar det
  kunden slagit på. Inga mejl.
- Ett klick på telefonnumret (varje tel:-länk med data-fl-call) skickas av
  static/js/flamingo-lp.js med sendBeacon till call_click
  (/lp/<slug>/ring/) och blir en förfrågan "Klick på telefonnumret". Inget
  sms: ägaren får själva samtalet. Utan skript räknas inget, och länken
  fungerar ändå.
- Mätningen hos Google: sidan frågar inte om samtycke (beslut 2026-10-03).
  En förfrågan eller ett klick på numret med gclid köas som konvertering
  (Lead.can_send_to_google), och Lead.ad_consent lämnas tomt: sidan tar
  inte emot något svar om samtycke, så inget kan hittas på. Inga kakor.

Skydd: CSRF, ett osynligt honungsfält (en bot som fyller det får samma
tack-sida, men ingen förfrågan skapas), spärrarna i limits.py (högst
RATE_LIMIT förfrågningar i timmen per besökare och kampanj och högst
CAMPAIGN_RATE_LIMIT per kampanj, räknade i databasen; klicken på numret
har egna gränser och räknas en gång per besökare och timme), maxlängder och
sanering i leads.py.
"""

import logging

from django import forms
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from apps.projects.access import is_agency_user

from . import leads, limits, sms
from .generator import company_name
from .manage_review import staff_state
from .models import PAGE_QUESTION_KINDS, RATING_SOURCES, Campaign, Service

logger = logging.getLogger("security")

#: Förfrågningar i timmen från samma besökare till samma kampanj.
RATE_LIMIT = limits.LEADS_PER_IP
#: Förfrågningar i timmen till en kampanj, från alla tillsammans.
CAMPAIGN_RATE_LIMIT = limits.LEADS_PER_CAMPAIGN
#: Formulärets frågor (Campaign.page["questions"]) visas högst så här många.
QUESTIONS_MAX = 8
#: Honungsfältet: osynligt för människor, ifyllt av botar.
HONEYPOT = "webbplats"

#: Uppgifter (Fact.key) som sidan läser, i turordning.
FACT_NAME = ("foretagsnamn", "foretag", "namn")
FACT_PHONE = ("telefon",)
FACT_ADDRESS = ("adress",)
FACT_RATING = ("betyg", "google-betyg", "omdomen", "rating")

#: Formulärets rubrik när sidan inte har någon egen, per sätt att sälja.
FORM_TITLES = {
    Service.SALES_CALL: "Hellre att vi ringer dig?",
    Service.SALES_QUOTE: "Berätta om jobbet",
    Service.SALES_BOOK: "Boka en tid",
}
SUBMIT_LABELS = {
    Service.SALES_CALL: "Ring upp mig",
    Service.SALES_QUOTE: "Skicka",
    Service.SALES_BOOK: "Skicka",
}


# ---------------------------------------------------------------------------
# Kampanjen och sidans innehåll
# ---------------------------------------------------------------------------


def _campaign_for(request, slug):
    """(kampanjen, förhandsvisning?) eller 404.

    Publik: live på ett aktiverat konto hos en aktiv kund. Byrån ser allt
    annat som förhandsvisning; alla andra får 404. Ett demokonto har aldrig
    en publik sida: ett påhittat företag ska inte gå att hitta, inte ens
    för demokundens kontakt."""
    campaign = (
        Campaign.objects.select_related("account__customer", "service")
        .filter(page_slug=slug)
        .first()
    )
    if campaign is None:
        raise Http404
    account = campaign.account
    public = (
        campaign.is_public
        and account.is_enabled
        and account.customer.is_active
        and not account.is_demo
    )
    if public:
        return campaign, False
    if is_agency_user(request.user):
        return campaign, True
    raise Http404


def _first(facts, keys):
    for key in keys:
        value = (facts.get(key) or "").strip()
        if value:
            return value
    return ""


def _questions(page):
    """Sidans extra frågor, bara de som går att rita."""
    questions, seen = [], set()
    for raw in page.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        label = str(raw.get("label") or "").strip()
        kind = raw.get("kind") if raw.get("kind") in PAGE_QUESTION_KINDS else "text"
        if not key or not label or key in seen:
            continue
        seen.add(key)
        questions.append({"key": key, "label": label[:120], "kind": kind, "field": f"q_{key}"})
        if len(questions) >= QUESTIONS_MAX:
            break
    return questions


def _rating(account):
    """Ett bekräftat betyg från Google (eller ADX), annars "". Ett betyg från
    hemsidan, eller som kunden skrivit själv, visas aldrig."""
    rows = {
        key: value.strip()
        for key, value in account.facts.filter(
            confirmed=True, key__in=FACT_RATING, source__in=RATING_SOURCES
        ).values_list("key", "value")
    }
    return _first(rows, FACT_RATING)


def page_content(campaign):
    """Det sidan visar, ur campaign.page och de bekräftade uppgifterna."""
    page = campaign.page if isinstance(campaign.page, dict) else {}
    facts = campaign.account.confirmed_facts()
    # Samma namn som annonserna (generator.company_name): utan bolagsform och
    # utan byråns anteckningar i parentes, "Lindqvist Rör AB (demo)" blir
    # "Lindqvist Rör".
    business = _first(facts, FACT_NAME) or company_name(campaign.account.customer)
    # "phone" på sidan vinner, även tom (tom = ingen ringknapp). Saknas
    # nyckeln helt används den bekräftade uppgiften.
    phone = str(page.get("phone") or "").strip() if "phone" in page else _first(facts, FACT_PHONE)
    mode = campaign.service.sales_mode
    points = [str(p).strip() for p in page.get("points") or [] if str(p).strip()]
    return {
        "business": business,
        "title": str(page.get("title") or "").strip() or campaign.service.name,
        "lead": str(page.get("lead") or "").strip(),
        "points": points[:6],
        "phone": phone,
        "tel": sms.tel_href(phone) if phone else "",
        "rating": _rating(campaign.account),
        "address": _first(facts, FACT_ADDRESS),
        "form_title": str(page.get("form_title") or "").strip()
        or FORM_TITLES.get(mode, "Berätta om jobbet"),
        "submit_label": SUBMIT_LABELS.get(mode, "Skicka"),
        "note": str(page.get("note") or "").strip(),
        "questions": _questions(page),
        "mode": mode,
    }


# ---------------------------------------------------------------------------
# Formuläret
# ---------------------------------------------------------------------------


def _valid_phone(value):
    digits = sum(ch.isdigit() for ch in value)
    if digits < 6 or digits > 15:
        raise forms.ValidationError("Skriv ett telefonnummer vi kan ringa.")


class LeadForm(forms.Form):
    """Landningssidans formulär. Fälten ritas för hand i lp/page.html;
    formuläret står för gränserna och felen. Frågorna läggs till per sida."""

    name = forms.CharField(label="Namn", max_length=leads.NAME_MAX, required=False)
    phone = forms.CharField(
        label="Telefon",
        max_length=leads.PHONE_MAX,
        validators=[_valid_phone],
        error_messages={"required": "Skriv ditt telefonnummer."},
    )
    email = forms.EmailField(
        label="E-post",
        max_length=254,
        required=False,
        error_messages={"invalid": "Skriv en e-postadress som namn@exempel.se, eller lämna tomt."},
    )
    message = forms.CharField(label="Meddelande", max_length=leads.MESSAGE_MAX, required=False)

    def __init__(self, *args, content, **kwargs):
        super().__init__(*args, **kwargs)
        self.content = content
        if content["mode"] != Service.SALES_CALL:
            self.fields["name"].required = True
            self.fields["name"].error_messages["required"] = "Skriv ditt namn."
        for question in content["questions"]:
            if question["kind"] == "date":
                field = forms.DateField(
                    label=question["label"],
                    required=False,
                    input_formats=["%Y-%m-%d"],
                    error_messages={"invalid": "Välj ett datum."},
                )
            else:
                field = forms.CharField(
                    label=question["label"], max_length=leads.ANSWER_MAX, required=False
                )
            self.fields[question["field"]] = field
        for name, field in self.fields.items():
            if isinstance(field, forms.CharField) and field.max_length:
                field.error_messages["max_length"] = (
                    f"Högst {field.max_length} tecken, det här är för långt."
                )
            field.widget.attrs.setdefault("id", f"lp-{name}")

    def lead_data(self):
        data = {
            "name": self.cleaned_data.get("name", ""),
            "phone": self.cleaned_data.get("phone", ""),
            "email": self.cleaned_data.get("email", ""),
            "message": self.cleaned_data.get("message", ""),
            "answers": {},
        }
        for question in self.content["questions"]:
            value = self.cleaned_data.get(question["field"])
            if value:
                text = value.isoformat() if hasattr(value, "isoformat") else str(value)
                data["answers"][question["label"]] = text
        data.update({key: self.data.get(key, "") for key in leads.TRACKING_KEYS})
        data[leads.KEYWORD_KEY] = self.data.get(leads.KEYWORD_KEY, "")
        return data


def _limit_message(content, reason):
    """Lugnt besked när en spärr slagit till, med numret om det finns."""
    if reason == limits.LIMIT_CAMPAIGN:
        text = "Formuläret tar inte emot fler förfrågningar just nu. "
    else:
        text = "Det har kommit många förfrågningar från din uppkoppling. "
    if content["phone"]:
        return text + f"Ring {content['business']} på {content['phone']} i stället."
    return text + "Försök igen lite senare."


def _render(request, template, context, status=200):
    response = render(request, template, context, status=status)
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _base_context(request, campaign, preview):
    content = page_content(campaign)
    return {
        "campaign": campaign,
        "content": content,
        "preview": preview,
        "is_staff": is_agency_user(request.user),
        # Remsan är byråns: byråns ord för läget ("Att granska", "Hos
        # kunden"), inte kundens ("Väntar på dig").
        "status_label": staff_state(campaign, campaign.pending_review())[0],
    }


def _measure_context(request, campaign, preview):
    """Mätningen på sidan (flamingo-lp.js):

    call_beacon   adressen klicken på numret skickas till, eller "" när
                  inget ska räknas: förhandsvisningen, demokonton och byrån
                  (ett klick för att kontrollera numret är ingen förfrågan)"""
    counted = not preview and not campaign.account.is_demo and not is_agency_user(request.user)
    return {
        "call_beacon": reverse("flamingo_public:call_click", args=[campaign.page_slug])
        if counted
        else "",
    }


@require_http_methods(["GET", "HEAD", "POST"])
def landing(request, slug):
    campaign, preview = _campaign_for(request, slug)
    context = _base_context(request, campaign, preview)
    content = context["content"]
    tracking = leads.tracking_from(request.GET)
    status = 200

    if request.method == "POST":
        if request.POST.get(HONEYPOT, ""):
            # Samma svar som en lyckad förfrågan, utan sidoeffekter: boten
            # ska inte lära sig vad som stoppade den.
            logger.warning("flamingo lp: honungsfältet ifyllt (kampanj %s)", campaign.pk)
            return redirect("flamingo_public:thanks", slug=campaign.page_slug)
        form = LeadForm(request.POST, content=content)
        if form.is_valid():
            if preview:
                # Förhandsvisningen skapar ingen förfrågan och skickar inget.
                return redirect("flamingo_public:thanks", slug=campaign.page_slug)
            lead, refused = limits.create_form_lead(campaign, form.lead_data(), request)
            if lead is not None:
                sms.notify_new_lead(lead)
                return redirect("flamingo_public:thanks", slug=campaign.page_slug)
            logger.warning(
                "flamingo lp: för många förfrågningar (%s, kampanj %s)", refused, campaign.pk
            )
            form.add_error(None, _limit_message(content, refused))
            status = 429
        tracking = leads.tracking_from(request.POST, request.GET)
    else:
        form = LeadForm(content=content)

    questions = [{**q, "bound": form[q["field"]]} for q in content["questions"]]
    context.update(_measure_context(request, campaign, preview))
    context.update(
        {
            "form": form,
            "questions": questions,
            "tracking": tracking,
            "honeypot": HONEYPOT,
            "action": request.get_full_path(),
            "review_url": reverse("manage:flamingo_review", args=[campaign.pk])
            if context["is_staff"]
            else "",
        }
    )
    return _render(request, "flamingo/lp/page.html", context, status=status)


def _live_campaign(slug):
    """Kampanjen bakom en publik sida, annars 404. Samma villkor som
    _campaign_for utan byråns förhandsvisning: live, aktiverat konto, aktiv
    kund och inget demokonto."""
    campaign = (
        Campaign.objects.select_related("account__customer", "service")
        .filter(page_slug=slug)
        .first()
    )
    if campaign is None:
        raise Http404
    account = campaign.account
    if not (
        campaign.is_public
        and account.is_enabled
        and account.customer.is_active
        and not account.is_demo
    ):
        raise Http404
    return campaign


@require_POST
def call_click(request, slug):
    """Ett klick på telefonnumret, skickat av flamingo-lp.js med sendBeacon.

    Svarar alltid 204 utan innehåll för en publik sida (ingen läser svaret,
    och en bot ska inte se om klicket räknades). Bara live-sidor; CSRF som
    formuläret. Ett klick blir en förfrågan "Klick på telefonnumret" med
    klick-id och utm, högst en per besökare och kampanj och timme
    (limits.create_call_click_lead). Inget sms: ägaren får samtalet. Byrån
    räknas inte."""
    campaign = _live_campaign(slug)
    response = HttpResponse(status=204)
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    if is_agency_user(request.user):
        return response
    lead, refused = limits.create_call_click_lead(campaign, request.POST, request)
    if lead is None and refused != limits.LIMIT_DUPLICATE:
        logger.warning(
            "flamingo lp: för många klick på numret (%s, kampanj %s)", refused, campaign.pk
        )
    return response


@require_http_methods(["GET", "HEAD"])
def thanks(request, slug):
    campaign, preview = _campaign_for(request, slug)
    context = _base_context(request, campaign, preview)
    context["review_url"] = (
        reverse("manage:flamingo_review", args=[campaign.pk]) if context["is_staff"] else ""
    )
    return _render(request, "flamingo/lp/thanks.html", context)
