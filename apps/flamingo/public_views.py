"""
Kundens landningssidor (/lp/<slug>/, kundresan steg 9): publika, till för
kundens kunder.

- Varje kampanjs sida är alltid öppen utan inloggning, vilket läge kampanjen
  än har (utkast, granskning, pausad, live), också för demokontot och för
  ett avstängt konto eller en inaktiv kund (Giovanni 2026-10-04). Bara en
  okänd adress är 404 (sajtens vanliga 404-sida). Sidorna ska inte hittas:
  noindex, "Disallow: /lp/" i robots.txt och aldrig i sitemapen.
- Sidan är kampanjens LandingPage (sidbyggaren, apps/flamingo/pagebuilder/),
  ritad i designen Ren av pagebuilder.render_page_html. Besökarna ser den
  publicerade versionen; en sida som aldrig publicerats visar utkastet.
  Flera kampanjer kan dela en sida, men adressen är kampanjens egen, så
  förfrågningarna och klicken räknas till rätt kampanj.
- Byrån (staff) ser en kampanj som inte är live som förhandsvisning, med en
  synlig remsa överst: den publicerade versionen, eller utkastet med
  ?utkast=1 (och alltid för en sida som inte publicerats). Formuläret
  skickar inget i förhandsvisningen.
- Sidan är kundens egen: kundens namn, ingen Flamingo- eller ADX-design.
  Innehållet kommer från blocken och kundens bekräftade uppgifter. Ett
  betyg visas bara om det kommer från Google-profilen eller är en bekräftad
  uppgift från Google (eller ADX): ett betyg från hemsidan eller som kunden
  skrivit själv visas aldrig för allmänheten.
- Formuläret (sidans formulärblock, pagebuilder.form_spec) skapar en Lead
  (limits.create_form_lead, leads.create_lead) med klick-id och utm ur
  adressen, och sms.notify_new_lead skickar det kunden slagit på. Svaren på
  frågorna sparas som Lead.answers[frågans etikett]. Inga mejl.
- Ett klick på telefonnumret (varje tel:-länk med data-fl-call) skickas av
  static/js/flamingo-lp.js med sendBeacon till call_click
  (/lp/<slug>/ring/) och blir en förfrågan "Klick på telefonnumret". Bara
  för en live-kampanj utanför demokontot (_live_campaign). Inget
  sms: ägaren får själva samtalet. Utan skript räknas inget, och länken
  fungerar ändå.
- Mätningen hos Google: sidan frågar inte om samtycke (beslut 2026-10-03).
  En förfrågan eller ett klick på numret med gclid köas som konvertering
  (Lead.can_send_to_google), och Lead.ad_consent lämnas tomt: sidan tar
  inte emot något svar om samtycke, så inget kan hittas på. Inga kakor
  utöver formulärets CSRF-nyckel, ingen statistik från ADX, noindex.

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

from . import leads, limits, pagebuilder, sms
from .manage_review import staff_state
from .models import Campaign

logger = logging.getLogger("security")

#: Förfrågningar i timmen från samma besökare till samma kampanj.
RATE_LIMIT = limits.LEADS_PER_IP
#: Förfrågningar i timmen till en kampanj, från alla tillsammans.
CAMPAIGN_RATE_LIMIT = limits.LEADS_PER_CAMPAIGN
#: Honungsfältet: osynligt för människor, ifyllt av botar.
HONEYPOT = "webbplats"
#: ?utkast=1 visar byrån utkastet i stället för den publicerade sidan.
DRAFT_PARAM = "utkast"


# ---------------------------------------------------------------------------
# Kampanjen och sidan
# ---------------------------------------------------------------------------


def _campaign_for(request, slug):
    """(kampanjen, förhandsvisning?) eller 404 för en okänd adress.

    Alla landningssidor är alltid öppna utan inloggning, vilket läge
    kampanjen än har och även för demokontot (Giovanni 2026-10-04). De
    hittas inte av sökmotorer: robots.txt nekar /lp/, sidorna har noindex och
    står aldrig i sitemapen. Byrån ser en sida som inte är live som
    förhandsvisning (remsan överst, formuläret skickar inget)."""
    campaign = (
        Campaign.objects.select_related("account__customer", "service", "landing_page")
        .filter(page_slug=slug)
        .first()
    )
    if campaign is None:
        raise Http404
    account = campaign.account
    live = (
        campaign.is_public
        and account.is_enabled
        and account.customer.is_active
        and not account.is_demo
    )
    if not live and is_agency_user(request.user):
        return campaign, True
    return campaign, False


def _page_for(request, campaign, preview):
    """(sidan, which) för kampanjen. which är "published" för besökarna;
    byrån ser utkastet med ?utkast=1, och alla ser utkastet på en sida som
    aldrig publicerats."""
    page = campaign.landing_page
    staff = is_agency_user(request.user)
    if page is None:
        page = pagebuilder.ensure_own_page(campaign)
        campaign.landing_page = page
    wants_draft = staff and request.GET.get(DRAFT_PARAM) == "1"
    if not page.is_published:
        # En sida som aldrig publicerats visas som den är (utkastet): alla
        # landningssidor är öppna.
        return page, "draft"
    return page, "draft" if wants_draft else "published"


# ---------------------------------------------------------------------------
# Formuläret
# ---------------------------------------------------------------------------


def _valid_phone(value):
    digits = sum(ch.isdigit() for ch in value)
    if digits < 6 or digits > 15:
        raise forms.ValidationError("Skriv ett telefonnummer vi kan ringa.")


class LeadForm(forms.Form):
    """Landningssidans formulär. Fälten ritas för hand i
    lp/ren/blocks/form.html; formuläret står för gränserna och felen.
    Frågorna kommer från sidans formulärblock (pagebuilder.FormSpec).

    Formulärets variant styr: "short" (som "ringer direkt" förut) har namnet
    valfritt och bara mobilen; "questions" och "booking" kräver namnet."""

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

    def __init__(self, *args, spec=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.spec = spec or pagebuilder.FormSpec()
        if self.spec.name_required:
            self.fields["name"].required = True
            self.fields["name"].error_messages["required"] = "Skriv ditt namn."
        for question in self.spec.questions:
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
            field.widget.attrs.setdefault("id", f"rn-{name}")

    def lead_data(self):
        data = {
            "name": self.cleaned_data.get("name", ""),
            "phone": self.cleaned_data.get("phone", ""),
            "email": self.cleaned_data.get("email", ""),
            "message": self.cleaned_data.get("message", ""),
            "answers": {},
        }
        for question in self.spec.questions:
            value = self.cleaned_data.get(question["field"])
            if value:
                text = value.isoformat() if hasattr(value, "isoformat") else str(value)
                data["answers"][question["label"]] = text
        data.update({key: self.data.get(key, "") for key in leads.TRACKING_KEYS})
        data[leads.KEYWORD_KEY] = self.data.get(leads.KEYWORD_KEY, "")
        return data


def _limit_message(site, reason):
    """Lugnt besked när en spärr slagit till, med numret om det finns."""
    if reason == limits.LIMIT_CAMPAIGN:
        text = "Formuläret tar inte emot fler förfrågningar just nu. "
    else:
        text = "Det har kommit många förfrågningar från din uppkoppling. "
    if site.phone:
        return text + f"Ring {site.business} på {site.phone} i stället."
    return text + "Försök igen lite senare."


def _layout_extra(request, campaign, preview, page, which):
    """Layoutens kontext utöver sidan: remsan för byrån och länkarna mellan
    utkastet och den publicerade sidan."""
    is_staff = is_agency_user(request.user)
    extra = {
        "campaign": campaign,
        "preview": preview,
        "is_staff": is_staff,
        # Remsan är byråns: byråns ord för läget ("Att granska", "Hos
        # kunden"), inte kundens ("Väntar på dig").
        "status_label": staff_state(campaign, campaign.pending_review())[0] if is_staff else "",
        "review_url": reverse("manage:flamingo_review", args=[campaign.pk]) if is_staff else "",
        "draft_url": "",
        "published_url": "",
    }
    if is_staff:
        extra["draft_url"] = f"{campaign.landing_url}?{DRAFT_PARAM}=1"
        if page.is_published:
            extra["published_url"] = campaign.landing_url
    return extra


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


def _respond(html, status=200):
    response = HttpResponse(html, status=status)
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@require_http_methods(["GET", "HEAD", "POST"])
def landing(request, slug):
    campaign, preview = _campaign_for(request, slug)
    page, which = _page_for(request, campaign, preview)
    account = campaign.account
    blocks = page.blocks_for(which)
    # Sidan utan formulär (bara ringknappar) tar ändå emot en postning med
    # det korta formulärets fält, så att ingen förfrågan tappas.
    spec = pagebuilder.form_spec(blocks) or pagebuilder.FormSpec()
    tracking = leads.tracking_from(request.GET)
    status = 200

    if request.method == "POST":
        if request.POST.get(HONEYPOT, ""):
            # Samma svar som en lyckad förfrågan, utan sidoeffekter: boten
            # ska inte lära sig vad som stoppade den.
            logger.warning("flamingo lp: honungsfältet ifyllt (kampanj %s)", campaign.pk)
            return redirect("flamingo_public:thanks", slug=campaign.page_slug)
        form = LeadForm(request.POST, spec=spec)
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
            site = pagebuilder.render.site_info(page, account, blocks)
            form.add_error(None, _limit_message(site, refused))
            status = 429
        tracking = leads.tracking_from(request.POST, request.GET)
    else:
        form = LeadForm(spec=spec)

    extra = _layout_extra(request, campaign, preview, page, which)
    extra.update(_measure_context(request, campaign, preview))
    extra.update({"tracking": tracking, "action": request.get_full_path()})
    html = pagebuilder.render_page_html(
        page, account, campaign, which=which, request=request, form=form, extra=extra
    )
    return _respond(html, status=status)


def _live_campaign(slug):
    """Kampanjen vars klick på numret räknas, annars 404: live, aktiverat
    konto, aktiv kund och inget demokonto. Sidan är öppen ändå
    (_campaign_for); det här villkoret gäller bara räkningen."""
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

    Svarar alltid 204 utan innehåll för en live-sida (ingen läser svaret,
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
    page, which = _page_for(request, campaign, preview)
    extra = _layout_extra(request, campaign, preview, page, which)
    context = pagebuilder.page_view_context(
        page, campaign.account, campaign, which=which, request=request, extra=extra
    )
    response = render(request, pagebuilder.render.THANKS_TEMPLATE, context)
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
