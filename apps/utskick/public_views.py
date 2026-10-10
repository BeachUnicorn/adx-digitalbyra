"""
Publika sidor på adx.se (README I.1 "Public views", E.5, H.2, H.5, I.7):
anmälan, tack, integritet (reserven när kunden saknar egen policy),
bekräfta e-post och Mina utskick.

    /utskick/<public_slug>/              signup          anmälan (e-post, och sms från S2: K.2.5)
    /utskick/<public_slug>/tack/         signup_thanks   tack, med den maskerade adressen
    /utskick/<public_slug>/integritet/   privacy         "Så hanterar <företaget> dina uppgifter"
    /utskick/bekrafta/<token>/           confirm         GET visar, POST bekräftar (14 dagar)
    /utskick/val/<token>/                preferences     Mina utskick

Kontot tas bara ur public_slug (anmälan, tack, integritet) eller ur en
signerad token (bekräfta, Mina utskick), aldrig ur en parameter. Ett GET
bekräftar, avregistrerar eller ändrar aldrig något. Allt här använder
Djangos CSRF (H.2). Sidorna visar bara maskerade uppgifter (070-*** ** 67,
a***@e***.example), aldrig ett namn, och sätter inga andra kakor än
CSRF-nyckeln: ingen statistik från ADX och inga skript utom botskyddets
(static/js/utskick-public.js). Varje svar har X-Robots-Tag: noindex, och
sidorna med en personlig länk också Cache-Control: no-store.

Anmälan är 404 utan can_collect (utskick på, biträdesavtalet godkänt), när
sidan inte är på, utan en integritetstext att länka till (H.5) och när
ingen kanal kan erbjudas: e-post kräver att byrån klarmarkerat
bekräftelsemejlen (optin.offers_email; byrån själv ser sidan ändå, med en
remsa, och dess provanmälan sparas i byråns namn så att ticken skickar
den före klarmarkeringen). Med ?forhandsgranska=1 ser kontots egna
användare och byrån en stängd sida också (I.7 Förhandsgranska), med en
remsa och utan att kunna skicka. Botskyddet (apps.common.botcheck:
honungsfält, tidsstämpel och JS-bevis) ger en tyst låtsad framgång utan
sidoeffekter; högst SIGNUP_PER_IP_HOUR anmälningar per besökare och konto
och timme och SIGNUP_PER_ACCOUNT_HOUR per konto och timme räknas i
databasen (Counter "signup_ip" och "signup_account").

Sidan avslöjar aldrig om en adress redan finns hos kunden: tack-sidan
säger samma sak vad som än hände med samtycket, och listan och taggarna
från anmälningssidan läggs på först när personen bekräftat i mejlet
(händelsen "Anmälde sig" bara när samtycket faktiskt ändrades).

Mina utskick (E.5, H.6): att stänga av en kanal går alltid (declined: inga
erbjudanden, information fortsätter); att slå på e-post sätter pending och
skickar ett bekräftelsemejl, att slå på sms (S2, när byrån slagit på
sms-utskicken) sätter pending och skickar ett sms med en länk till
k.adx.se/b/ (optin.send_due_sms); "Avregistrera mig från allt" lägger en spärr
per kanal. Avstängningar och avregistreringen fungerar också när utskick är
avstängt för kontot (D.8) och går igenom även om botskyddet faller (en
avregistrering ska aldrig tappas); bara att slå på kräver botskyddet,
can_collect och klarmarkerade bekräftelsemejl.
"""

import logging
from dataclasses import dataclass

from django import forms
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.common.botcheck import botcheck_passes
from apps.common.net import client_ip
from apps.projects.access import is_agency_user

from . import branding, capture, contacts, limits, normalize, optin, tokens
from . import consent as consents
from . import suppression as suppressions
from .access import PERSON, actor_for, can_collect, is_enabled, settings_for
from .keys import KeyMismatch
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNELS,
    Consent,
    Contact,
    Event,
    LinkCode,
    ListMembership,
    SignupForm,
    Suppression,
    UtskickSettings,
)

logger = logging.getLogger(__name__)

#: Anmälningar per besökare (ip_hash) och konto och timme.
SIGNUP_PER_IP_HOUR = 5
#: Anmälningar per konto och timme, från alla besökare (README J "DOI in S1").
SIGNUP_PER_ACCOUNT_HOUR = 100

TOO_MANY_TEXT = "Det har kommit många anmälningar från din uppkoppling. Försök igen lite senare."
BUSY_TEXT = "Det har kommit många anmälningar just nu. Försök igen lite senare."
CLOSED_TEXT = "Det går inte att anmäla sig här just nu."
CHOOSE_TEXT = "Kryssa i rutan för att anmäla dig."
EMAIL_TEXT = "Fyll i din e-post för e-post."
PHONE_TEXT = "Fyll i ditt mobilnummer för sms."
NOT_MOBILE_TEXT = "Det här numret kan inte få sms. Skriv ett mobilnummer."
PHONE_INVALID_TEXT = "Skriv ett mobilnummer som 070-123 45 67."
SAVE_FAILED_TEXT = "Det gick inte att spara just nu. Försök igen lite senare."

#: Mina utskick: radernas rubriker (E.5).
ROW_TITLES = {CHANNEL_SMS: "Sms med erbjudanden", CHANNEL_EMAIL: "E-post med erbjudanden"}
#: Mina utskick: vad sidan säger efter en sparning (?klart=).
DONE_TEXTS = {
    "sparat": "Dina val är sparade.",
    "avregistrerad": "Du är avregistrerad.",
}


def _noindex(response, private=False):
    response["X-Robots-Tag"] = "noindex, nofollow"
    if private:
        # En personlig länk: ingen cache ska spara sidan.
        response["Cache-Control"] = "private, no-store, max-age=0"
    return response


def _page(request, template, context, status=200, private=False):
    return _noindex(render(request, template, context, status=status), private=private)


def _ip_hash(request):
    from apps.flamingo.limits import ip_hash

    return ip_hash(client_ip(request))


def _base_context(account, row):
    """Sidans skal: kundens namn, kundens logga överst (branding.logo) och
    integritetstexten."""
    return {
        "foretag": row.display_name,
        "integritet_url": capture.privacy_url(account, row),
        **branding.context(account, row.display_name),
    }


# ---------------------------------------------------------------------------
# Anmälan och tack
# ---------------------------------------------------------------------------


def _settings_for_slug(public_slug):
    row = (
        UtskickSettings.objects.filter(public_slug=public_slug)
        .select_related("account__customer")
        .first()
    )
    if row is None or not is_enabled(row.account, row):
        raise Http404
    return row


@dataclass
class _Signup:
    form_row: SignupForm
    channels: list
    #: Byrån ser sidan innan bekräftelsemejlen är klarmarkerade.
    preview: bool = False
    #: Förhandsgranskning av en stängd sida: visas, men tar inte emot något.
    closed: bool = False


def _signup_open(request, row):
    """Anmälningssidan om den är öppen, annars 404 (I.1, H.5)."""
    account = row.account
    if not can_collect(account):
        raise Http404
    form_row = SignupForm.objects.filter(account=account, is_active=True).first()
    if form_row is None or not capture.privacy_available(account, row):
        raise Http404
    channels = []
    if CHANNEL_EMAIL in (form_row.channels or []) and optin.offers_email(request.user):
        channels.append(CHANNEL_EMAIL)
    # S2 (K.2.5): sms när byrån slagit på sms-utskicken och kundens sms är
    # aktiverat. Sms räknas först när personen klickat på länken i sms:et.
    if CHANNEL_SMS in (form_row.channels or []) and not optin.sms_signup_block(account):
        channels.append(CHANNEL_SMS)
    if not channels:
        raise Http404
    preview = not optin.doi_ready() and is_agency_user(request.user)
    return _Signup(form_row, channels, preview)


def _may_preview(request, account):
    """Får användaren förhandsgranska kontots stängda anmälningssida? Byrån,
    och kontots egna användare (kontakter hos kunden med Flamingo)."""
    from apps.flamingo.access import flamingo_customers

    user = getattr(request, "user", None)
    if is_agency_user(user):
        return True
    if not account.customer_id:
        return False
    return flamingo_customers(user).filter(pk=account.customer_id).exists()


def _signup_page(request, row):
    """Anmälningssidan som den ska visas: öppen, eller (med
    ?forhandsgranska=1 för den som får) en stängd förhandsgranskning."""
    try:
        return _signup_open(request, row)
    except Http404:
        if request.method != "GET" or request.GET.get("forhandsgranska") != "1":
            raise
        if not _may_preview(request, row.account):
            raise
    from .app_views.signup import DEFAULT_TITLE

    form_row = SignupForm.objects.filter(account=row.account).first()
    if form_row is None:
        form_row = SignupForm(account=row.account, title=DEFAULT_TITLE, channels=[CHANNEL_EMAIL])
    channels = [ch for ch in CHANNELS if ch in (form_row.channels or [])] or [CHANNEL_EMAIL]
    return _Signup(form_row, channels, preview=True, closed=True)


class SignupFields(forms.Form):
    """Anmälningssidans fält. Kryssrutans etikett är hela samtyckestexten,
    så att det som sparas (text_shown) är exakt det personen såg. Mobilen
    och rutan för sms finns från S2 när sidan erbjuder sms."""

    first_name = forms.CharField(max_length=80, required=False)
    email = forms.CharField(max_length=254, required=False)
    consent_email = forms.BooleanField(required=False)
    phone = forms.CharField(max_length=40, required=False)
    consent_sms = forms.BooleanField(required=False)

    def __init__(self, *args, channels=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.channels = list(channels)

    def clean_first_name(self):
        try:
            return normalize.first_name(self.cleaned_data.get("first_name"))
        except normalize.InvalidValue as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean_email(self):
        try:
            return normalize.email(self.cleaned_data.get("email"))
        except normalize.InvalidValue as exc:
            raise forms.ValidationError(str(exc)) from None

    def clean_phone(self):
        """E.164, eller "" när fältet är tomt. Bara mobilnummer kan få sms."""
        raw = str(self.cleaned_data.get("phone") or "").strip()
        if not raw or CHANNEL_SMS not in self.channels:
            return ""
        phone = normalize.phone(raw)
        if phone.landline:
            raise forms.ValidationError(NOT_MOBILE_TEXT)
        if phone.error or not phone.e164:
            raise forms.ValidationError(PHONE_INVALID_TEXT)
        return phone.e164

    def clean(self):
        data = super().clean()
        ticked = [ch for ch in self.channels if data.get(f"consent_{ch}")]
        if not ticked:
            self.add_error(f"consent_{self.channels[0]}" if self.channels else None, CHOOSE_TEXT)
        if CHANNEL_EMAIL in ticked and not data.get("email") and "email" not in self.errors:
            self.add_error("email", EMAIL_TEXT)
        if CHANNEL_SMS in ticked and not data.get("phone") and "phone" not in self.errors:
            self.add_error("phone", PHONE_TEXT)
        return data


class _Closed(Exception):
    """Anmälan går inte att ta emot (gränsen, avtalet, nyckeln)."""


def _add_to_list_and_tags(form_row, contact):
    """Anmälningssidans lista och taggar, bara kontots egna (H.1)."""
    target = form_row.add_to_list
    if target is not None and target.account_id == contact.account_id:
        contacts.add_to_list(target, [contact], source=ListMembership.Source.SIGNUP)
    for tag in form_row.add_tags.filter(account_id=contact.account_id):
        contacts.add_tag(tag, [contact])


def _signup_lists(account, contact):
    """Efter bekräftelsen: anmälningssidans lista och taggar."""
    form_row = SignupForm.objects.filter(account=account).first()
    if form_row is not None:
        _add_to_list_and_tags(form_row, contact)


def _sign_up(row, page, data, ip_hash, actor=PERSON):
    """Kontakterna och de väntande samtyckena för en anmälan: {kanal:
    samtycke}, eller _Closed. Varje ikryssad kanal binds till kontakten som
    redan har adressen; adresser som ingen har blir en ny kontakt (en
    befintlig kontakt ändras aldrig härifrån: vem som helst kan skriva vems
    adress som helst). E-post väntar på bekräftelsemejlet, sms (S2) på
    länken i bekräftelse-sms:et. Inget här avslöjar för besökaren om
    adressen redan fanns: listan och taggarna läggs på vid bekräftelsen
    (confirm), händelsen bara när samtycket ändrades."""
    account = row.account
    detail = reverse("utskick_public:signup", args=[row.public_slug])
    ticked = [ch for ch in page.channels if data.get(f"consent_{ch}")]
    addresses = {CHANNEL_EMAIL: data.get("email", ""), CHANNEL_SMS: data.get("phone", "")}
    fields = {CHANNEL_EMAIL: "email", CHANNEL_SMS: "phone"}
    result = {}
    try:
        with transaction.atomic():
            owners = {
                ch: Contact.objects.filter(account=account, **{fields[ch]: addresses[ch]}).first()
                for ch in ticked
            }
            missing = [ch for ch in ticked if owners[ch] is None]
            if missing:
                values = {"first_name": data.get("first_name", "")}
                values.update({fields[ch]: addresses[ch] for ch in missing})
                created = _new_signup_contact(account, values, detail, actor)
                for ch in missing:
                    owners[ch] = created
            for channel in ticked:
                contact = owners[channel]
                if contact.address(channel) != addresses[channel]:
                    continue
                text = row.consent_text(channel)
                outcome = consents.set_status(
                    contact,
                    channel,
                    consents.PENDING,
                    source=Consent.Source.SIGNUP,
                    actor=actor,
                    source_detail=detail,
                    text_shown=text,
                    tracking_ok=capture.tracking_ok(text),
                    ip_hash=ip_hash,
                )
                consent = outcome.consent
                if consent is None:
                    continue
                result[channel] = consent
                if consent.status == consents.PENDING and not outcome.changed:
                    if channel == CHANNEL_SMS:
                        optin.requeue_sms(consent)
                    else:
                        optin.requeue(consent)
                if outcome.changed:
                    contacts.record_event(contact, Event.SIGNUP, data={"kanal": channel})
    except KeyMismatch:
        logger.error("Utskick: anmälan för konto %s nekades (nyckeln)", account.pk)
        raise _Closed from None
    if not result:
        raise _Closed
    return result


def _new_signup_contact(account, values, detail, actor=PERSON):
    """En ny kontakt ur anmälan: förnamnet och de adresser som ingen
    kontakt har (values: first_name, email och/eller phone)."""
    from . import alerts

    try:
        return contacts.create(
            account, values, source=Contact.Source.SIGNUP, actor=actor, source_detail=detail
        )
    except contacts.ContactLimitReached:
        logger.warning("Utskick: kontaktgränsen nådd för konto %s (anmälan)", account.pk)
        alerts.agency(
            "Utskick: kontaktgränsen nådd",
            [
                f"Konto {account.pk} har nått sin gräns för kontakter.",
                "Anmälningssidan tar inte emot nya anmälningar förrän gränsen höjs.",
            ],
            once=f"contact_limit:{account.pk}",
            window="day",
        )
        raise _Closed from None
    except contacts.CollectNotAllowed:
        raise _Closed from None
    except contacts.ContactError:
        # En samtidig anmälan med samma adress hann före.
        for field in ("email", "phone"):
            if values.get(field):
                contact = Contact.objects.filter(account=account, **{field: values[field]}).first()
                if contact is not None:
                    return contact
        raise _Closed from None


def _account_busy(account, window):
    """Räkna anmälan mot kontots timgräns. True när gränsen är passerad;
    byrån larmas en gång per timme och konto."""
    if not limits.hit("signup_account", str(account.pk), window, SIGNUP_PER_ACCOUNT_HOUR):
        return False
    from . import alerts

    logger.warning("Utskick: anmälningssidan för konto %s når timgränsen", account.pk)
    alerts.agency(
        "Utskick: många anmälningar hos en kund",
        [
            f"Konto {account.pk} har fått {SIGNUP_PER_ACCOUNT_HOUR} anmälningar den här timmen.",
            "Fler tas inte emot förrän nästa timme. Titta på kontakterna om det ser konstigt ut.",
        ],
        once=f"signup_account:{account.pk}",
        window="hour",
    )
    return True


@require_http_methods(["GET", "HEAD", "POST"])
def signup(request, public_slug):
    row = _settings_for_slug(public_slug)
    page = _signup_page(request, row)
    account = row.account
    status = 200
    if request.method == "POST":
        thanks = reverse("utskick_public:signup_thanks", args=[row.public_slug])
        if not botcheck_passes(request):
            # Tyst låtsad framgång: samma tack-sida, ingen kontakt, inget mejl.
            return _noindex(redirect(thanks))
        form = SignupFields(request.POST, channels=page.channels)
        if form.is_valid():
            ip_hash = _ip_hash(request)
            window = limits.hour_window()
            if limits.hit("signup_ip", f"{account.pk}:{ip_hash}", window, SIGNUP_PER_IP_HOUR):
                logger.warning("Utskick: för många anmälningar (konto %s)", account.pk)
                form.add_error(None, TOO_MANY_TEXT)
                status = 429
            elif _account_busy(account, window):
                form.add_error(None, BUSY_TEXT)
                status = 429
            else:
                # Byråns provanmälan sparas i byråns namn (optin.due).
                actor = actor_for(request) if is_agency_user(request.user) else PERSON
                try:
                    signed = _sign_up(row, page, form.cleaned_data, ip_hash, actor)
                except _Closed:
                    form.add_error(None, CLOSED_TEXT)
                    status = 503
                else:
                    refs = []
                    if CHANNEL_EMAIL in signed:
                        refs.append(f"r={tokens.thanks_token(signed[CHANNEL_EMAIL].pk)}")
                    if CHANNEL_SMS in signed:
                        refs.append(f"s={tokens.thanks_token(signed[CHANNEL_SMS].pk)}")
                    return _noindex(redirect(f"{thanks}?{'&'.join(refs)}"))
    else:
        form = SignupFields(channels=page.channels)
    context = _base_context(account, row)
    context.update(
        {
            "sida": page.form_row,
            "form": form,
            "kanaler": page.channels,
            "texter": capture.consent_texts(account, row),
            "forhandsvisning": page.preview,
            "stangd": page.closed,
            "botcheck": not page.closed,
        }
    )
    return _page(request, "utskick/public/signup.html", context, status=status, private=page.closed)


@require_http_methods(["GET", "HEAD"])
def signup_thanks(request, public_slug):
    """Tack-sidan. Samma text vad som än hände med samtycket (väntar, fick
    redan erbjudanden, avregistrerad): sidan får inte avslöja vem som redan
    finns hos kunden. Bara de maskerade adresserna som personen skrev
    (?r= e-posten, ?s= sms, S2)."""
    row = _settings_for_slug(public_slug)
    page = _signup_open(request, row)
    masked = {CHANNEL_EMAIL: "", CHANNEL_SMS: ""}
    for param, channel in (("r", CHANNEL_EMAIL), ("s", CHANNEL_SMS)):
        consent_id = tokens.read_thanks(request.GET.get(param, ""))
        if not consent_id:
            continue
        consent = (
            Consent.objects.select_related("contact")
            .filter(pk=consent_id, channel=channel, contact__account=row.account)
            .first()
        )
        if consent is None:
            continue
        if channel == CHANNEL_EMAIL:
            masked[channel] = normalize.mask_email(consent.contact.email)
        else:
            masked[channel] = normalize.mask_phone(consent.contact.phone)
    context = _base_context(row.account, row)
    context.update(
        {
            "maskerad_epost": masked[CHANNEL_EMAIL],
            "maskerat_nummer": masked[CHANNEL_SMS],
            "kanaler": page.channels,
            "dagar": tokens.DOI_DAYS,
            "timmar": LinkCode.CONFIRM_HOURS,
        }
    )
    return _page(request, "utskick/public/thanks.html", context)


# ---------------------------------------------------------------------------
# Integritet
# ---------------------------------------------------------------------------


@require_http_methods(["GET", "HEAD"])
def privacy(request, public_slug):
    """Den genererade integritetstexten (H.5). Har kunden en egen policy
    (https) går sidan dit. Fungerar också när utskick stängts av, så att
    länken i ett gammalt mejl håller. 404 utan tillräckliga uppgifter."""
    row = (
        UtskickSettings.objects.filter(public_slug=public_slug)
        .select_related("account__customer")
        .first()
    )
    if row is None:
        raise Http404
    own = capture.privacy_url(row.account, row)
    if own and not own.startswith("/"):
        return _noindex(redirect(own))
    facts = capture.privacy_facts(row.account, row)
    if not facts.complete:
        raise Http404
    context = {
        "foretag": row.display_name,
        "uppgifter": facts,
        "integritet_url": "",
        **branding.context(row.account, row.display_name),
    }
    return _page(request, "utskick/public/privacy.html", context)


# ---------------------------------------------------------------------------
# Bekräfta e-post
# ---------------------------------------------------------------------------


def _signup_url(request, row):
    """Anmälningssidans adress om den är öppen, annars ""."""
    try:
        _signup_open(request, row)
    except Http404:
        return ""
    return reverse("utskick_public:signup", args=[row.public_slug])


@require_http_methods(["GET", "HEAD", "POST"])
def confirm(request, token):
    ref = tokens.read_doi(token)
    consent = optin.doi_consent(ref)
    if consent is None:
        raise Http404
    contact = consent.contact
    account = contact.account
    row = settings_for(account)
    context = _base_context(account, row)
    context["maskerad_epost"] = normalize.mask_email(contact.email)
    context["dagar"] = tokens.DOI_DAYS
    status = 200
    if consent.status in consents.REKLAM_OK:
        state = "klar"
        context["val_url"] = reverse(
            "utskick_public:preferences",
            args=[tokens.preference_token(account.pk, CHANNEL_EMAIL, consent.value_hash)],
        )
    elif ref.expired:
        state, status = "utgangen", 410
        context["anmalan_url"] = _signup_url(request, row)
    elif consent.status != consents.PENDING:
        state, status = "ogiltig", 410
    elif not can_collect(account):
        state = "stangd"
    elif request.method == "POST":
        from_signup = consent.source == Consent.Source.SIGNUP
        try:
            outcome = optin.confirm(consent, ip_hash=_ip_hash(request))
        except KeyMismatch:
            outcome = consents.Outcome(consent, refused="not_allowed")
        if outcome.ok:
            if from_signup and outcome.changed:
                # Anmälningssidans lista och taggar först nu, när adressen
                # är bevisad (en anmälan kan skrivas av vem som helst).
                _signup_lists(account, contact)
            return _noindex(redirect(request.path), private=True)
        state = "stangd"
    else:
        state = "bekrafta"
    context["tillstand"] = state
    return _page(request, "utskick/public/confirm.html", context, status=status, private=True)


# ---------------------------------------------------------------------------
# Mina utskick
# ---------------------------------------------------------------------------

_ON = consents.REKLAM_OK


def _contact_for(account, channel, value_hash):
    """Kontakten vars nuvarande adress har hashen, eller None (borttagen,
    eller adressen har bytts sedan länken skickades)."""
    row = (
        Consent.objects.select_related("contact")
        .filter(contact__account=account, channel=channel, value_hash=value_hash)
        .first()
    )
    if row is None:
        return None
    from . import keys

    contact = row.contact
    if keys.value_hash(channel, contact.address(channel)) != value_hash:
        return None
    return contact


def _channel_rows(request, account, row, contact):
    """Raderna på sidan per kanal med adress (E.5): state är on, pending
    eller off; can_on säger om en avstängd kanal kan slås på här."""
    consent_by = {c.channel: c for c in contact.consents.all()}
    hashes = {
        ch: consent_by[ch].value_hash for ch in CHANNELS if ch in consent_by and contact.address(ch)
    }
    blocked = {
        ch
        for ch in hashes
        if Suppression.objects.filter(account=account, channel=ch, value_hash=hashes[ch]).exists()
    }
    may_collect = can_collect(account)
    rows = []
    for channel in CHANNELS:
        if not contact.address(channel):
            continue
        consent = consent_by.get(channel)
        status = consent.status if consent is not None else consents.MISSING
        if status in _ON and channel not in blocked:
            state = "on"
        elif status == consents.PENDING:
            state = "pending"
        else:
            state = "off"
        # Att slå på kräver en bekräftelse: e-post ett mejl, sms (S2) ett
        # sms med en länk till k.adx.se/b/ (optin.send_due_sms).
        if channel == CHANNEL_EMAIL:
            can_on = may_collect and optin.offers_email(request.user)
        else:
            can_on = may_collect and optin.offers_sms(request.user)
        rows.append(
            {
                "kanal": channel,
                "rubrik": ROW_TITLES[channel],
                "not": row.pref_email_note if channel == CHANNEL_EMAIL else "",
                "tillstand": state,
                "kan_andras": state != "off" or can_on,
                "sparrad": channel in blocked,
            }
        )
    return rows, blocked


def _save_choices(request, account, row, contact, rows):
    """Spara kryssrutorna. "mejl" när ett bekräftelsemejl väntar, "sms" när
    ett bekräftelse-sms väntar (S2), "sms-mejl" för båda, annars "sparat".
    Botskyddet prövas bara när en kanal ska slås på: en avstängning ska
    aldrig tappas (och botskyddets logg ska inte fyllas av dem)."""
    ip_hash = _ip_hash(request)
    detail = "Mina utskick"
    done = "sparat"
    turning_on = [
        item
        for item in rows
        if item["tillstand"] == "off"
        and item["kan_andras"]
        and request.POST.get(item["kanal"]) == "1"
    ]
    botcheck_ok = bool(turning_on) and botcheck_passes(request)
    with transaction.atomic():
        for item in rows:
            channel = item["kanal"]
            wants = request.POST.get(channel) == "1"
            if item["tillstand"] in ("on", "pending") and not wants:
                consents.set_status(
                    contact,
                    channel,
                    consents.DECLINED,
                    source=Consent.Source.PREFERENCE,
                    actor=PERSON,
                    source_detail=detail,
                    ip_hash=ip_hash,
                )
            elif item["tillstand"] == "off" and wants and item["kan_andras"] and botcheck_ok:
                text = row.consent_text(channel)
                outcome = consents.set_status(
                    contact,
                    channel,
                    consents.PENDING,
                    source=Consent.Source.PREFERENCE,
                    actor=PERSON,
                    source_detail=detail,
                    text_shown=text,
                    tracking_ok=capture.tracking_ok(text),
                    ip_hash=ip_hash,
                )
                consent = outcome.consent
                if consent is not None and consent.status == consents.PENDING:
                    if channel == CHANNEL_SMS:
                        if not outcome.changed:
                            optin.requeue_sms(consent)
                        done = "sms-mejl" if done in ("mejl", "sms-mejl") else "sms"
                    else:
                        if not outcome.changed:
                            optin.requeue(consent)
                        done = "sms-mejl" if done in ("sms", "sms-mejl") else "mejl"
    return done


def _unsubscribe_all(request, account, contact, ref):
    """Avregistrera mig från allt: en spärr per kanal med adress (H.6)."""
    ip_hash = _ip_hash(request)
    with transaction.atomic():
        if contact is None:
            suppressions.add(account, ref.channel, ref.value_hash, Suppression.Reason.PREFERENCE)
            return
        for channel in CHANNELS:
            address = contact.address(channel)
            if not address:
                continue
            suppressions.suppress(
                account,
                channel,
                address,
                Suppression.Reason.PREFERENCE,
                Consent.Source.PREFERENCE,
                source_detail="Mina utskick",
                actor=PERSON,
                ip_hash=ip_hash,
            )


@require_http_methods(["GET", "HEAD", "POST"])
def preferences(request, token):
    from apps.flamingo.models import FlamingoAccount

    ref = tokens.read_preference(token)
    if ref is None:
        raise Http404
    account = FlamingoAccount.objects.select_related("customer").filter(pk=ref.account_id).first()
    if account is None:
        raise Http404
    row = settings_for(account)
    if not row.pk:
        raise Http404
    contact = _contact_for(account, ref.channel, ref.value_hash)
    status = 200
    error = ""
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "allt":
                _unsubscribe_all(request, account, contact, ref)
                return _noindex(redirect(f"{request.path}?klart=avregistrerad"), private=True)
            if action == "spara" and contact is not None:
                rows, _ = _channel_rows(request, account, row, contact)
                done = _save_choices(request, account, row, contact, rows)
                return _noindex(redirect(f"{request.path}?klart={done}"), private=True)
        except KeyMismatch:
            logger.error("Utskick: Mina utskick för konto %s sparades inte (nyckeln)", account.pk)
            error, status = SAVE_FAILED_TEXT, 503
    context = _base_context(account, row)
    if contact is not None:
        rows, blocked = _channel_rows(request, account, row, contact)
        masked = [
            normalize.mask_phone(contact.phone) if contact.phone else "",
            normalize.mask_email(contact.email) if contact.email else "",
        ]
        all_blocked = bool(rows) and len(blocked) == len(rows)
        email = contact.email
    else:
        rows, masked = [], []
        all_blocked = suppressions.is_suppressed(account, ref.channel, value_hash=ref.value_hash)
        email = ""
    done = request.GET.get("klart", "")
    context.update(
        {
            "rader": rows,
            "maskerat": " · ".join(m for m in masked if m),
            "allt_sparrat": all_blocked,
            "kan_spara": any(item["kan_andras"] for item in rows),
            "klart": done,
            "klart_text": DONE_TEXTS.get(done, ""),
            "mejl_till": normalize.mask_email(email)
            if done in ("mejl", "sms-mejl") and email
            else "",
            "sms_till": normalize.mask_phone(contact.phone)
            if contact is not None and contact.phone and done in ("sms", "sms-mejl")
            else "",
            "avregistrering_text": row.unsubscribe_text if done == "avregistrerad" else "",
            "fel": error,
            "botcheck": True,
        }
    )
    return _page(request, "utskick/public/preferences.html", context, status=status, private=True)
