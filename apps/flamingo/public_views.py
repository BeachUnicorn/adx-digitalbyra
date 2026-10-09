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

Kryssrutorna för utskick (apps/utskick, README C.2 och H.5): när kundens
utskick är på, biträdesavtalet godkänt, kryssrutorna påslagna och en
integritetstext finns, får formuläret "Ja, jag vill få erbjudanden från
<företaget> via sms." (och via e-post när formuläret frågar efter e-post och
byrån klarmarkerat bekräftelsemejlen), aldrig förkryssade, med länken "Så
hanterar <företaget> dina uppgifter" under. Efter förfrågan sparar
utskick.capture.from_lead_form samtycket med exakt text, sida, tid och
besökarens ip_hash, och kopplar förfrågan till kontakten (Lead.contact).
Utan ikryssad ruta skapas ingen kontakt; förfrågan kopplas bara till en
befintlig kontakt med samma nummer eller e-post. Tack-sidan säger att ett
mejl kommer när rutan för e-post var ikryssad (?epost=1, inget annat i
adressen), oavsett om adressen redan fick e-post: sidan avslöjar inte vem
som redan finns hos kunden.

Besök från ett utskick (apps/utskick, README D10, E.4): klicket på
k.adx.se skickar besökaren hit med ?ut=<token>. Ingen kaka och ingen
lagring: token prövas mot sidans konto (utskick.attribution.resolve; en
token från ett annat konto ignoreras helt), besöket loggas på klicket (inte
för byrån, förhandsvisningen eller demot), formuläret bär token i ett dolt
fält och klicket på numret skickar den. Förfrågan får utskicket och spåret
(och går aldrig till Google), med högre gränser (limits). Tiden på sidan
kommer med visit_beacon (/lp/<slug>/besok/, csrf_exempt: token är
behörigheten), högst var tionde sekund. flamingo-lp.js tar bort ut ur
adressfältet, så att en kopierad länk inte bär mottagarens token. Ett
utskicksklick räknar också klicket på numret när Google-kampanjen är
pausad (kontot aktiverat, kunden aktiv, inget demokonto).

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
from django.views.decorators.csrf import csrf_exempt
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
#: ?epost=1 på tack-sidan: rutan för e-post var ikryssad (utskick). Samma
#: text vad som än hände med samtycket.
EMAIL_PENDING_PARAM = "epost"
EMAIL_PENDING_TEXT = (
    "Om du inte redan får e-post från oss skickar vi ett mejl till dig. "
    "Klicka på länken i mejlet för att börja få e-post."
)


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

    def __init__(self, *args, spec=None, consent=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.spec = spec or pagebuilder.FormSpec()
        # Kryssrutorna för utskick (_lp_consent): bara kanalerna som visas.
        self.consent = consent
        self.consent_error_channels = set()
        for channel in consent["channels"] if consent else ():
            self.fields[f"consent_{channel}"] = forms.BooleanField(required=False)
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

    def clean(self):
        cleaned = super().clean()
        #: Kanalerna vars ikryssade ruta fick ett fel (rutan märks i mallen).
        self.consent_error_channels = set()
        if self.consent:
            from apps.utskick import capture

            errors = capture.consent_errors(self.consent["channels"], cleaned)
            for channel, field in capture.ERROR_FIELDS.items():
                if field in errors:
                    self.consent_error_channels.add(channel)
                    if field in self.fields and field not in self.errors:
                        self.add_error(field, errors[field])
        return cleaned

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


def _lp_consent(account, spec):
    """Kryssrutorna för utskick i formuläret (apps/utskick/capture.py), eller
    None när de inte ska visas: kanalerna, deras exakta texter och länken
    till integritetstexten."""
    from apps.utskick import capture
    from apps.utskick.access import settings_for

    row = settings_for(account)
    channels = capture.lp_consent_channels(account, spec, row)
    if not channels:
        return None
    texts = capture.consent_texts(account, row)
    return {
        "channels": channels,
        "boxes": [{"channel": channel, "text": texts[channel]} for channel in channels],
        "texts": {channel: texts[channel] for channel in channels},
        "privacy_url": capture.privacy_url(account, row),
        "display_name": row.display_name,
    }


def _capture(lead, form, request, click=None):
    """Efter förfrågan: samtycket och kopplingen till kontakten (utskick).
    Kastar aldrig. True när rutan för e-post var ikryssad (tack-sidans
    ?epost=1), vad capture än gjorde med den. click är utskickets klick
    (förfrågan kom via ett utskick) eller None."""
    from apps.common.net import client_ip
    from apps.utskick import capture

    consent = form.consent or {}
    try:
        capture.from_lead_form(
            lead,
            form.cleaned_data,
            consent.get("texts") or {},
            request.path,
            limits.ip_hash(client_ip(request)),
            click=click,
        )
    except Exception:  # noqa: BLE001 - förfrågan är redan sparad och ska fram
        logger.exception("flamingo lp: utskick tog inte emot förfrågan %s", lead.pk)
    return "email" in consent.get("channels", ()) and bool(form.cleaned_data.get("consent_email"))


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


def _utskick_click(campaign, *sources):
    """(ut, klicket) när besöket kommer från ett utskick hos samma konto,
    annars ("", None). Felaktiga och främmande token ignoreras helt (E.4)."""
    ut = leads.ut_from(*sources)
    if not ut:
        return "", None
    from apps.utskick import attribution

    click = attribution.resolve(ut, campaign)
    return (ut, click) if click is not None else ("", None)


def _counted_visit(request, campaign, preview):
    """Loggas besöket från ett utskick? Inte för byrån, förhandsvisningen
    eller demokontot (E.4)."""
    return not preview and not campaign.account.is_demo and not is_agency_user(request.user)


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
    consent = _lp_consent(account, spec)
    status = 200
    post = request.POST if request.method == "POST" else None
    ut, click = _utskick_click(campaign, post, request.GET)
    counted = _counted_visit(request, campaign, preview)
    if request.method == "GET" and click is not None and counted:
        from apps.utskick import attribution

        attribution.record_lp_visit(click, campaign)

    if request.method == "POST":
        if request.POST.get(HONEYPOT, ""):
            # Samma svar som en lyckad förfrågan, utan sidoeffekter: boten
            # ska inte lära sig vad som stoppade den.
            logger.warning("flamingo lp: honungsfältet ifyllt (kampanj %s)", campaign.pk)
            return redirect("flamingo_public:thanks", slug=campaign.page_slug)
        form = LeadForm(request.POST, spec=spec, consent=consent)
        if form.is_valid():
            if preview:
                # Förhandsvisningen skapar ingen förfrågan och skickar inget.
                return redirect("flamingo_public:thanks", slug=campaign.page_slug)
            lead, refused = limits.create_form_lead(
                campaign, form.lead_data(), request, click=click
            )
            if lead is not None:
                email_ticked = _capture(lead, form, request, click if lead.utskick_id else None)
                sms.notify_new_lead(lead)
                thanks_url = reverse("flamingo_public:thanks", args=[campaign.page_slug])
                if email_ticked:
                    thanks_url += f"?{EMAIL_PENDING_PARAM}=1"
                return redirect(thanks_url)
            logger.warning(
                "flamingo lp: för många förfrågningar (%s, kampanj %s)", refused, campaign.pk
            )
            site = pagebuilder.render.site_info(page, account, blocks)
            form.add_error(None, _limit_message(site, refused))
            status = 429
        tracking = leads.tracking_from(request.POST, request.GET)
    else:
        form = LeadForm(spec=spec, consent=consent)

    extra = _layout_extra(request, campaign, preview, page, which)
    extra.update(_measure_context(request, campaign, preview))
    extra.update({"tracking": tracking, "action": request.get_full_path(), "lp_consent": consent})
    # Utskicket (E.4): det dolda fältet ut och besöksanropet för tiden på sidan.
    extra["ut"] = ut
    extra["visit_beacon"] = (
        reverse("flamingo_public:visit_beacon", args=[campaign.page_slug])
        if click is not None and counted
        else ""
    )
    html = pagebuilder.render_page_html(
        page, account, campaign, which=which, request=request, form=form, extra=extra
    )
    return _respond(html, status=status)


def _live_campaign(slug, data=None):
    """(kampanjen, utskickets klick eller None) när klicket på numret räknas,
    annars 404: live, aktiverat konto, aktiv kund och inget demokonto. Med
    ett ut-klick från samma konto (data["ut"], E.4) räknas det också när
    Google-kampanjen inte är live. Sidan är öppen ändå (_campaign_for); det
    här villkoret gäller bara räkningen."""
    campaign = (
        Campaign.objects.select_related("account__customer", "service")
        .filter(page_slug=slug)
        .first()
    )
    if campaign is None:
        raise Http404
    account = campaign.account
    if not (account.is_enabled and account.customer.is_active and not account.is_demo):
        raise Http404
    _ut, click = _utskick_click(campaign, data)
    if not campaign.is_public and click is None:
        raise Http404
    return campaign, click


@require_POST
def call_click(request, slug):
    """Ett klick på telefonnumret, skickat av flamingo-lp.js med sendBeacon.

    Svarar alltid 204 utan innehåll för en live-sida (ingen läser svaret,
    och en bot ska inte se om klicket räknades). Bara live-sidor; CSRF som
    formuläret. Ett klick blir en förfrågan "Klick på telefonnumret" med
    klick-id och utm, högst en per besökare och kampanj och timme
    (limits.create_call_click_lead). Inget sms: ägaren får samtalet. Byrån
    räknas inte. Med utskickets ut (flamingo-lp.js skickar den) får
    förfrågan utskicket och spåret."""
    campaign, click = _live_campaign(slug, request.POST)
    response = HttpResponse(status=204)
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    if is_agency_user(request.user):
        return response
    lead, refused = limits.create_call_click_lead(campaign, request.POST, request, click=click)
    if lead is None and refused != limits.LIMIT_DUPLICATE:
        logger.warning(
            "flamingo lp: för många klick på numret (%s, kampanj %s)", refused, campaign.pk
        )
    return response


#: Besöksanropet är litet: ut och sekunderna.
VISIT_BEACON_MAX_BYTES = 512


@csrf_exempt
@require_POST
def visit_beacon(request, slug):
    """Tiden på sidan för ett besök från ett utskick (README C.2, E.4),
    skickat av flamingo-lp.js med sendBeacon. Kroppen: ut och s (sekunder).
    csrf_exempt: den signerade token är behörigheten (H.2), och sidan sätter
    ingen kaka. Svarar alltid 204 utan innehåll (ingen ska kunna se om
    token gällde). Token från ett annat konto, byrån och demokontot
    ignoreras; högst en skrivning per klick och tio sekunder
    (attribution.record_beacon)."""
    response = HttpResponse(status=204)
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    if len(request.body or b"") > VISIT_BEACON_MAX_BYTES or is_agency_user(request.user):
        return response
    campaign = Campaign.objects.select_related("account").filter(page_slug=slug).first()
    if campaign is None or campaign.account.is_demo:
        return response
    _ut, click = _utskick_click(campaign, request.POST)
    if click is not None:
        from apps.utskick import attribution

        attribution.record_beacon(click, request.POST.get("s", ""))
    return response


@require_http_methods(["GET", "HEAD"])
def thanks(request, slug):
    campaign, preview = _campaign_for(request, slug)
    page, which = _page_for(request, campaign, preview)
    extra = _layout_extra(request, campaign, preview, page, which)
    extra["email_pending_text"] = (
        EMAIL_PENDING_TEXT if request.GET.get(EMAIL_PENDING_PARAM) == "1" else ""
    )
    context = pagebuilder.page_view_context(
        page, campaign.account, campaign, which=which, request=request, extra=extra
    )
    response = render(request, pagebuilder.render.THANKS_TEMPLATE, context)
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
