"""
Kom igång (kundresan steg 2, 4 och 5) och inställningarna.

    app/forslag/        Förslaget: hemsidan läses av (scan.py, places.py) och
                        kunden väljer vilka tjänster som ska vara med.
    app/foretaget/      Företaget: uppgifterna med källa, som kunden bekräftar,
                        rättar eller stryker, och tjänsterna.
    app/google/         Google: kundens eget Google Ads-konto, kopplat under
                        ADX. Kunden anger kontots id eller ber om ett nytt;
                        byrån skickar kopplingsförfrågan eller skapar kontot
                        (google_accounts.py), eller bockar av för hand.
                        Betalningen stoppar ingenting: utan den visar Google
                        bara inte annonserna.
    app/installningar/  Sms till kunden vid ny förfrågan och autosvaret.

Allt hämtas via kontot (app_view): ett id ur formuläret slås alltid upp med
account=account, så en annan kunds rad ger 404. Byrån i kundvyn är
skrivskyddad; grinden stoppar en POST innan den når hit, och mallarna visar
inga formulär när read_only är satt.

Inget här skickar mejl eller sms. Sms-valen är kundens egna och används först
när 46elks är inkopplat (ELKS_API_USERNAME, ELKS_API_PASSWORD och
ELKS_SENDER, se sms.is_configured).
"""

import math

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.common.security import sanitize_multiline_text, sanitize_plain_text
from apps.tools.analyzer import AnalysError, normalize_url

from .. import checks, exports, generator, google_accounts, limits, places, scan, sms
from ..models import (
    AUTOREPLY_DEFAULT,
    Fact,
    FlamingoAccount,
    Service,
    format_google_ads_id,
    google_id_taken,
)
from ..rules import when_text
from . import app_view, render_app
from .campaigns import DEFAULT_BUDGET, DEFAULT_RADIUS, area_text, confirmed_place, fact_segments

#: Autosvarets längd. Tre sms-delar (3 x 153 tecken) räcker gott; längre
#: blir dyrt och läses inte.
AUTOREPLY_MAX = 450
#: En läsning som stått som "hämtas" längre än så har avbrutits.
SCAN_STALE_AFTER = limits.SCAN_STALE_AFTER

# ---------------------------------------------------------------------------
# Hjälpare
# ---------------------------------------------------------------------------


def _owned(model, account, raw_pk):
    """Kundens egen rad, eller 404. Id:t kommer från formuläret och prövas
    alltid mot kontot."""
    try:
        pk = int(raw_pk)
    except (TypeError, ValueError):
        raise Http404 from None
    return get_object_or_404(model, pk=pk, account=account)


def sms_configured():
    """Samma fråga som sms.py ställer innan något skickas (alla tre
    46elks-inställningarna), så att sidan aldrig säger "inkopplat" när
    inget skulle gå iväg."""
    return sms.is_configured()


#: GSM 03.38, grunduppsättningen (ett tecken var) och tillägget (två).
_GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXTENDED = set("^{}\\[~]|€\f")


def sms_parts(text):
    """(tecken, antal sms) för en text. Samma räkning som teckenräknaren i
    static/js/flamingo-app-onboarding.js."""
    text = text or ""
    if all(ch in _GSM_BASIC or ch in _GSM_EXTENDED for ch in text):
        length = sum(2 if ch in _GSM_EXTENDED else 1 for ch in text)
        single, multi = 160, 153
    else:
        length = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)
        single, multi = 70, 67
    if length == 0:
        return 0, 0
    return length, 1 if length <= single else math.ceil(length / multi)


def price_key(name):
    return (scan.PRICE_PREFIX + slugify(name))[:64].rstrip("-")


def ensure_price_fact(account, service):
    """Prisraden för en tjänst: tom, obekräftad, "Fyll i, eller lämna tomt".
    Ett tomt pris skrivs aldrig som en gissning (README)."""
    key = price_key(service.name)
    if key == scan.PRICE_PREFIX.rstrip("-"):
        return
    Fact.objects.get_or_create(
        account=account,
        key=key,
        defaults={
            "label": f"Pris, {service.name}"[:120],
            "value": "",
            "source": Fact.SOURCE_CUSTOMER,
            "confirmed": False,
            "order": scan.PRICE_ORDER,
        },
    )


def _unique_fact_key(account, label):
    base = slugify(label)[:56].strip("-") or "uppgift"
    key, n = base, 2
    while account.facts.filter(key=key).exists():
        key = f"{base}-{n}"
        n += 1
    return key


# ---------------------------------------------------------------------------
# Formulär
# ---------------------------------------------------------------------------


class WebsiteForm(forms.Form):
    website_url = forms.CharField(label="Din hemsida", max_length=200)

    def clean_website_url(self):
        try:
            url = normalize_url(self.cleaned_data["website_url"])
        except AnalysError as exc:
            raise ValidationError(str(exc)) from None
        if len(url) > 200:
            raise ValidationError("Adressen är för lång.")
        return url


class FactValueForm(forms.Form):
    value = forms.CharField(required=False, max_length=scan.VALUE_MAX)

    def clean_value(self):
        return sanitize_plain_text(self.cleaned_data["value"], max_length=scan.VALUE_MAX)


class NewFactForm(forms.Form):
    label = forms.CharField(label="Uppgift", max_length=60)
    value = forms.CharField(label="Värde", max_length=scan.VALUE_MAX)

    def clean_label(self):
        label = sanitize_plain_text(self.cleaned_data["label"], max_length=60)
        if not label:
            raise ValidationError("Skriv vad uppgiften gäller.")
        return label[:1].upper() + label[1:]

    def clean_value(self):
        value = sanitize_plain_text(self.cleaned_data["value"], max_length=scan.VALUE_MAX)
        if not value:
            raise ValidationError("Skriv uppgiften.")
        return value


class ServiceForm(forms.Form):
    name = forms.CharField(label="Tjänst", max_length=scan.SERVICE_NAME_MAX)
    sales_mode = forms.ChoiceField(
        label="Hur köper kunderna den?",
        choices=Service.SALES_CHOICES,
        initial=Service.SALES_QUOTE,
    )
    is_active = forms.BooleanField(required=False)

    def __init__(self, *args, account, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.account = account
        self.instance = instance

    def clean_name(self):
        name = sanitize_plain_text(self.cleaned_data["name"], max_length=scan.SERVICE_NAME_MAX)
        if not name:
            raise ValidationError("Skriv tjänstens namn.")
        if checks.starts_like_formula(name):
            raise ValidationError(checks.FORMULA_MESSAGE)
        others = self.account.services.all()
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if any(n.casefold() == name.casefold() for n in others.values_list("name", flat=True)):
            raise ValidationError("Den tjänsten finns redan i listan.")
        return name[:1].upper() + name[1:]


class GoogleIdForm(forms.Form):
    google_ads_customer_id = forms.CharField(label="Kontots id", max_length=20)

    def clean_google_ads_customer_id(self):
        formatted = format_google_ads_id(self.cleaned_data["google_ads_customer_id"])
        if not formatted:
            raise ValidationError("Skriv kontots id som tio siffror, till exempel 123-456-7890.")
        return formatted


class SettingsForm(forms.Form):
    notify_phone = forms.CharField(label="Din mobil", max_length=30, required=False)
    notify_sms = forms.BooleanField(label="Sms till mig vid ny förfrågan", required=False)
    autoreply_enabled = forms.BooleanField(
        label="Autosvar med sms till den som frågar", required=False
    )
    autoreply_text = forms.CharField(
        label="Autosvarets text", max_length=AUTOREPLY_MAX, required=False, widget=forms.Textarea
    )

    def clean_notify_phone(self):
        raw = self.cleaned_data["notify_phone"].strip()
        if not raw:
            return ""
        phone = scan.normalize_se_phone(raw, mobile_only=True)
        if phone is None:
            raise ValidationError("Skriv ett svenskt mobilnummer, till exempel 070-123 45 67.")
        return phone

    def clean_autoreply_text(self):
        return sanitize_multiline_text(
            self.cleaned_data["autoreply_text"], max_length=AUTOREPLY_MAX
        )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("notify_sms") and not cleaned.get("notify_phone"):
            if "notify_phone" not in self.errors:
                self.add_error("notify_phone", "Skriv din mobil för att få sms.")
        if cleaned.get("autoreply_enabled") and not cleaned.get("autoreply_text"):
            if "autoreply_text" not in self.errors:
                self.add_error("autoreply_text", "Skriv autosvarets text.")
        return cleaned


# ---------------------------------------------------------------------------
# Förslaget (steg 2)
# ---------------------------------------------------------------------------


def _host(url):
    host = (url or "").split("//", 1)[-1].split("/", 1)[0].lower()
    return host.removeprefix("www.")


def _count(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _join(parts):
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " och " + parts[-1]


def _scan_state(account):
    """Läsningens läge för sidan: none, running, done, failed eller
    interrupted (stod som "hämtas" när processen dog)."""
    status = account.scan_status
    started = account.scan_started_at or account.updated_at
    if status == FlamingoAccount.SCAN_RUNNING and started < timezone.now() - SCAN_STALE_AFTER:
        return "interrupted"
    return status


def _timeline(account, facts, services, state):
    """Kundresan 02: Läste ..., Hittade ... hos Google."""
    host = _host(account.website_url)
    site_facts = {f.key for f in facts if f.source == Fact.SOURCE_SITE}
    google = [f for f in facts if f.source == Fact.SOURCE_GOOGLE]
    items = []

    if state == FlamingoAccount.SCAN_DONE:
        found = []
        if scan.KEY_PHONE in site_facts:
            found.append("telefonnummer")
        if scan.KEY_ADDRESS in site_facts:
            found.append("adress")
        if scan.KEY_HOURS in site_facts:
            found.append("öppettider")
        if services:
            found.append("tjänster")
        others = len(site_facts - {scan.KEY_PHONE, scan.KEY_ADDRESS, scan.KEY_HOURS})
        if others:
            found.append(_count(others, "annan uppgift", "andra uppgifter"))
        meta = ("Hittade " + _join(found)) if found else "Hittade inga tjänster eller uppgifter"
        meta += "."
        if account.scanned_at:
            meta += f" Läst {when_text(account.scanned_at)}."
        items.append({"state": "done", "title": f"Läste {host}", "meta": meta})
    elif state == FlamingoAccount.SCAN_FAILED:
        items.append(
            {
                "state": "now",
                "title": f"Kunde inte läsa {host}" if host else "Kunde inte läsa hemsidan",
                "meta": account.scan_error,
            }
        )
    elif state == "interrupted":
        items.append(
            {"state": "now", "title": f"Läsningen av {host} avbröts", "meta": "Försök igen."}
        )
    else:
        items.append(
            {
                "state": "now",
                "title": "Läs av din hemsida",
                "meta": "Vi letar efter telefon, adress, öppettider och tjänster.",
            }
        )

    if google:
        by_key = {f.key: f.value for f in google}
        parts = []
        if by_key.get(scan.KEY_ADDRESS):
            parts.append(by_key[scan.KEY_ADDRESS] + ".")
        if by_key.get(scan.KEY_RATING):
            parts.append(f"Betyg {by_key[scan.KEY_RATING]}.")
        meta = " ".join(parts)
        items.append({"state": "done", "title": "Hittade företaget hos Google", "meta": meta})
    elif not places.is_configured():
        items.append(
            {
                "state": "todo",
                "title": "Uppgifter från Google",
                "meta": "Inte inkopplat ännu. Fyll i det som saknas under Företaget.",
            }
        )
    elif state == FlamingoAccount.SCAN_DONE:
        items.append(
            {
                "state": "todo",
                "title": "Hittade inte företaget hos Google",
                "meta": "Fyll i adress och öppettider under Företaget.",
            }
        )
    else:
        items.append(
            {
                "state": "todo",
                "title": "Företaget hos Google",
                "meta": "Slås upp när hemsidan är läst.",
            }
        )
    return items


def _scan_message(result, google):
    found = []
    if result.services:
        found.append(_count(result.services, "nytt tjänsteförslag", "nya tjänsteförslag"))
    if result.facts:
        found.append(_count(result.facts, "uppgift", "uppgifter"))
    if google is not None and google.found and google.facts:
        found.append("uppgifter från Google")
    text = f"Klart. Vi läste {_count(result.pages, 'sida', 'sidor')}"
    text += f" och hittade {_join(found)}." if found else " men hittade inget nytt."
    return text + " Inget är publicerat."


def _save_service_choice(account, raw_ids):
    """Kryssade tjänster blir aktiva, okryssade inte. Id:n som inte är
    kontots egna ignoreras (de finns aldrig bland account.services)."""
    chosen = set()
    for raw in raw_ids:
        try:
            chosen.add(int(raw))
        except (TypeError, ValueError):
            continue
    active = 0
    for service in account.services.all():
        wanted = service.pk in chosen
        if service.is_active != wanted:
            service.is_active = wanted
            service.save(update_fields=["is_active"])
        if wanted:
            ensure_price_fact(account, service)
            active += 1
    return active


def _example(account, services):
    """Kundresan 02: en exempelannons för den första tjänsten som är med
    (alla är med första gången, innan något valts) och den föreslagna
    starten. Budgeten och radien är kampanjformulärets förval, orten kunden
    bekräftade uppgift om området. Inget påhittat: texterna är mallarnas,
    ur bekräftade uppgifter, och de understrukna bitarna är uppgifterna."""
    service = next((s for s in services if s.is_active), None) or (services[:1] or [None])[0]
    if service is None:
        return None
    place = confirmed_place(account)
    area = area_text(place, DEFAULT_RADIUS)
    headlines, descriptions = generator.example_texts(account, service, area)
    # Första rubriken och den första som inte bara upprepar tjänsten
    # ("Rörjour i Nacka | Ring Lindqvist Rör", som i kundresan).
    name = service.name.casefold()
    second = next((h for h in headlines[1:] if not h.casefold().startswith(name)), None)
    shown = headlines[:1] + ([second] if second else headlines[1:2])
    fact_values = list(account.confirmed_facts().values())
    options = [fact_segments(d, fact_values) for d in descriptions]
    # Helst en beskrivning med en uppgift i, så att exemplet visar vad som
    # kommer från kunden; annars den första.
    description = next((o for o in options if any(is_fact for _, is_fact in o)), None)
    if description is None:
        description = options[0] if options else []
    return {
        "service": service,
        "host": exports.landing_host(),
        "headline": " | ".join(shown),
        "description": description,
        "marks_facts": any(is_fact for _, is_fact in description),
        "budget": DEFAULT_BUDGET,
        "area": area,
    }


@app_view
def proposal(request, account):
    form = None
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "scan":
            form = WebsiteForm(request.POST)
            if form.is_valid():
                # Spärren först: en läsning åt gången, inte för tätt och
                # högst limits.SCAN_DAILY_MAX om dagen (varje läsning tar en
                # arbetare i upp till scan.TIME_BUDGET sekunder och kan kosta
                # ett AI-anrop). Ett demokonto läses aldrig (scan.demo_refusal).
                refused = scan.demo_refusal(account) or limits.reserve_scan(account)
                if refused:
                    messages.info(request, refused)
                    return redirect("flamingo:app_proposal")
                result = scan.scan_website(
                    account, form.cleaned_data["website_url"], user=request.user
                )
                if result.ok:
                    google = places.update_from_google(account)
                    messages.success(request, _scan_message(result, google))
                else:
                    messages.error(request, "Det gick inte att läsa hemsidan. Se varför nedan.")
                return redirect("flamingo:app_proposal")
        elif action == "services":
            active = _save_service_choice(account, request.POST.getlist("service"))
            if active:
                messages.success(
                    request,
                    f"{_count(active, 'tjänst är', 'tjänster är')} med. "
                    "Bekräfta nu det vi får säga om företaget.",
                )
            else:
                messages.info(request, "Ingen tjänst är vald. Lägg till dem här under Företaget.")
            return redirect("flamingo:app_business")
        else:
            return redirect("flamingo:app_proposal")

    if form is None:
        form = WebsiteForm(
            initial={"website_url": account.website_url or account.customer.website or ""}
        )
    facts = list(account.facts.all())
    services = list(account.services.all())
    any_active = any(s.is_active for s in services)
    state = _scan_state(account)
    rating = next((f.value for f in facts if f.key == scan.KEY_RATING and f.value), "")
    return render_app(
        request,
        "flamingo/app/onboarding/proposal.html",
        "",
        {
            "form": form,
            "scan_state": state,
            "host": _host(account.website_url),
            "timeline": _timeline(account, facts, services, state),
            "services": [
                {
                    "service": s,
                    # Första gången (inget valt än) är hela förslaget ikryssat.
                    "checked": s.is_active or not any_active,
                    "mode": scan.SALES_SHORT.get(s.sales_mode, ""),
                }
                for s in services
            ],
            "rating": rating,
            "example": _example(account, services),
            "show_steps": True,
        },
    )


# ---------------------------------------------------------------------------
# Företaget (steg 4)
# ---------------------------------------------------------------------------


def _business_url(anchor=""):
    return reverse("flamingo:app_business") + (f"#{anchor}" if anchor else "")


def _confirm(request, account):
    fact = _owned(Fact, account, request.POST.get("fact"))
    if not fact.confirmed:
        fact.confirmed = True
        fact.save(update_fields=["confirmed", "updated_at"])
    messages.success(request, f"{fact.label}: bekräftad.")
    return redirect(_business_url(f"uppgift-{fact.pk}"))


def _edit(request, account):
    fact = _owned(Fact, account, request.POST.get("fact"))
    form = FactValueForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"{fact.label}: högst {scan.VALUE_MAX} tecken.")
        return redirect(_business_url(f"uppgift-{fact.pk}"))
    fact.value = form.cleaned_data["value"]
    fact.source = Fact.SOURCE_CUSTOMER
    fact.confirmed = True
    fact.save(update_fields=["value", "source", "confirmed", "updated_at"])
    if fact.value:
        messages.success(request, f"{fact.label}: sparad.")
    else:
        messages.success(request, f"{fact.label}: lämnas tom och skrivs inte i annonserna.")
    return redirect(_business_url(f"uppgift-{fact.pk}"))


def _delete(request, account):
    fact = _owned(Fact, account, request.POST.get("fact"))
    label = fact.label
    fact.delete()
    messages.success(request, f"{label}: struken.")
    return redirect(_business_url("uppgifter"))


def _add_fact(request, account):
    form = NewFactForm(request.POST)
    if not form.is_valid():
        return {"fact_form": form}
    fact = Fact.objects.create(
        account=account,
        key=_unique_fact_key(account, form.cleaned_data["label"]),
        label=form.cleaned_data["label"],
        value=form.cleaned_data["value"],
        source=Fact.SOURCE_CUSTOMER,
        confirmed=True,
        order=scan.CUSTOMER_ORDER,
    )
    messages.success(request, f"{fact.label}: tillagd.")
    return redirect(_business_url(f"uppgift-{fact.pk}"))


def _confirm_all(request, account):
    count = account.facts.filter(confirmed=False).update(confirmed=True, updated_at=timezone.now())
    if count:
        messages.success(request, f"Klart. {_count(count, 'uppgift', 'uppgifter')} bekräftade.")
    return redirect("flamingo:app_google")


def _add_service(request, account):
    form = ServiceForm(request.POST, account=account)
    if not form.is_valid():
        return {"service_form": form}
    service = Service.objects.create(
        account=account,
        name=form.cleaned_data["name"],
        sales_mode=form.cleaned_data["sales_mode"],
        is_active=True,
        order=account.services.count() + 1,
    )
    ensure_price_fact(account, service)
    messages.success(request, f"{service.name}: tillagd.")
    return redirect(_business_url("tjanster"))


def _edit_service(request, account):
    service = _owned(Service, account, request.POST.get("service"))
    form = ServiceForm(request.POST, account=account, instance=service)
    if not form.is_valid():
        errors = [e for field_errors in form.errors.values() for e in field_errors]
        messages.error(request, f"{service.name}: {' '.join(errors)}")
        return redirect(_business_url(f"tjanst-{service.pk}"))
    old_key = price_key(service.name)
    service.name = form.cleaned_data["name"]
    service.sales_mode = form.cleaned_data["sales_mode"]
    service.is_active = form.cleaned_data["is_active"]
    service.save(update_fields=["name", "sales_mode", "is_active"])
    # Prisraden följer med ett nytt namn så länge kunden inte fyllt i den.
    new_key = price_key(service.name)
    if new_key != old_key and not account.facts.filter(key=new_key).exists():
        account.facts.filter(key=old_key, value="", confirmed=False).update(
            key=new_key, label=f"Pris, {service.name}"[:120], updated_at=timezone.now()
        )
    if service.is_active:
        ensure_price_fact(account, service)
    messages.success(request, f"{service.name}: sparad.")
    return redirect(_business_url(f"tjanst-{service.pk}"))


_BUSINESS_ACTIONS = {
    "confirm": _confirm,
    "edit": _edit,
    "delete": _delete,
    "add_fact": _add_fact,
    "confirm_all": _confirm_all,
    "add_service": _add_service,
    "edit_service": _edit_service,
}


@app_view
def business(request, account):
    bound = {}
    if request.method == "POST":
        handler = _BUSINESS_ACTIONS.get(request.POST.get("action", ""))
        if handler is None:
            return redirect("flamingo:app_business")
        result = handler(request, account)
        if not isinstance(result, dict):
            return result
        bound = result  # ett formulär med fel ritas om med det kunden skrev
    facts = list(account.facts.all())
    services = list(account.services.all())
    return render_app(
        request,
        "flamingo/app/onboarding/business.html",
        "business",
        {
            "facts": facts,
            "unconfirmed_count": sum(1 for f in facts if not f.confirmed),
            "services": services,
            "sales_choices": Service.SALES_CHOICES,
            "fact_form": bound.get("fact_form") or NewFactForm(),
            "service_form": bound.get("service_form")
            or ServiceForm(account=account, initial={"sales_mode": Service.SALES_QUOTE}),
            "value_max": scan.VALUE_MAX,
            "show_steps": True,
        },
    )


# ---------------------------------------------------------------------------
# Google (steg 5)
# ---------------------------------------------------------------------------


#: Ett id som ett annat Flamingo-konto har. Säger inte vems (inget läckage
#: av vilka konton ADX förvaltar).
GOOGLE_ID_TAKEN = (
    "Det id:t går inte att använda här. Kontrollera att det är ditt kontos id, eller skriv "
    "till ADX."
)


def _link_note(account):
    if account.google_status == FlamingoAccount.GOOGLE_REQUESTED_NEW:
        return "ADX skapar kontot i ditt namn och ger dig tillgång till det som administratör."
    if account.google_link_requested_at:
        return (
            f"ADX skickade förfrågan {when_text(account.google_link_requested_at)}. Godkänn den "
            f"i Google Ads under {google_accounts.MANAGERS_PATH}."
        )
    return (
        "Du får en förfrågan om att koppla kontot till ADX. Godkänn den i Google Ads under "
        f"{google_accounts.MANAGERS_PATH}."
    )


def _billing_note(account):
    if account.google_ready:
        return "Klart. Pengarna går direkt till Google."
    if account.google_linked and account.google_billing_status == "PENDING":
        return "Google granskar betalningen. Annonserna visas när den är godkänd."
    if account.google_linked:
        # Läget överst säger redan att betalningen inte stoppar kampanjerna.
        return "Pengarna går direkt till Google, med ditt eget kort."
    return "Pengarna går direkt till Google. Annonserna visas först när betalningen är inlagd."


def _google_state(account):
    """Läget i klartext överst: {tone, text}, eller None innan kunden valt."""
    status = account.google_status
    if account.google_ready:
        return {"tone": "ok", "text": "Kopplat, och betalningen är klar."}
    if account.google_linked:
        return {
            "tone": "warn",
            "text": "Kopplat. Betalning saknas: annonserna visas först när betalningen är "
            "inlagd hos Google. Det stoppar inte kampanjerna, de kan gå live ändå.",
        }
    if status == FlamingoAccount.GOOGLE_ID_GIVEN and account.google_link_requested_at:
        return {
            "tone": "warn",
            "text": "Förfrågan skickad: godkänn ADX:s förfrågan i Google Ads under "
            f"{google_accounts.MANAGERS_PATH}.",
        }
    if status == FlamingoAccount.GOOGLE_ID_GIVEN:
        return {"tone": "info", "text": "ADX har kontots id och kopplar det under förvaltarkontot."}
    if status == FlamingoAccount.GOOGLE_REQUESTED_NEW:
        return {"tone": "info", "text": "ADX skapar ett konto i ditt namn."}
    return None


def _google_timeline(account, campaign_count=0):
    status = account.google_status
    started = status != FlamingoAccount.GOOGLE_NOT_STARTED
    linked = account.google_linked
    ready = account.google_ready
    if status == FlamingoAccount.GOOGLE_REQUESTED_NEW:
        first = ("Du har bett oss skapa ett konto", "Det skapas i ditt namn. Du äger det.")
    elif account.google_ads_customer_id:
        first = (
            f"Konto-id angivet: {account.google_ads_customer_id}",
            "Du är administratör. ADX hanterar kampanjerna.",
        )
    elif started:
        # Kopplat (eller avbockat av byrån) utan id här: valen ovanför
        # tidslinjen visas inte längre, så "välj ovan" vore fel.
        first = ("Kontot är valt", "ADX har kontots uppgifter. Du är administratör.")
    else:
        first = ("Välj ett av sätten ovan", "Har du ett konto, eller ska vi skapa ett?")
    # Betalningen stoppar inte kampanjerna: när kontot är kopplat kan de
    # skapas och gå live. Annonserna visas när betalningen finns.
    if campaign_count:
        last = {
            "state": "done" if linked else "todo",
            "title": "Kampanjerna",
            "meta": f"Du har {campaign_count} {'kampanj' if campaign_count == 1 else 'kampanjer'}.",
            "campaigns_link": True,
        }
    else:
        last = {
            "state": "done" if linked else "todo",
            "title": "Redo att skapa första kampanjen",
            "meta": "",
            "campaign_link": linked,
        }
    pending = status == FlamingoAccount.GOOGLE_ID_GIVEN and account.google_link_requested_at
    return [
        {"state": "done" if started else "now", "title": first[0], "meta": first[1]},
        {
            "state": "done" if linked else ("now" if started else "todo"),
            "title": "Godkänn ADX:s förfrågan i Google Ads"
            if pending
            else "ADX kopplar kontot under förvaltarkontot",
            "meta": "Klart." if linked else _link_note(account),
        },
        {
            "state": "done" if ready else ("now" if linked else "todo"),
            "title": "Lägg in betalning hos Google",
            "meta": _billing_note(account),
            "billing_link": linked and not ready,
        },
        last,
    ]


def _save_google_id(account, new_id, changed):
    """Spara kundens id. False om ett annat konto hann ta id:t (databasens
    regel), och då sparas ingenting."""
    fields = ["google_ads_customer_id", "google_status"]
    if changed:
        # Betalningen, förfrågan och läsningen gällde det förra kontot.
        fields += google_accounts.forget_previous_account(account)
    account.google_ads_customer_id = new_id
    if changed or not account.google_linked:
        # Ett nytt id betyder en ny koppling, även efter en gammal.
        account.google_status = FlamingoAccount.GOOGLE_ID_GIVEN
    try:
        with transaction.atomic():
            account.save(update_fields=[*fields, "updated_at"])
    except IntegrityError:
        account.refresh_from_db()
        return False
    if changed:
        google_accounts.clear_campaign_errors(account)
    return True


@app_view
def google(request, account):
    id_form = None
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "id":
            id_form = GoogleIdForm(request.POST)
            if id_form.is_valid():
                new_id = id_form.cleaned_data["google_ads_customer_id"]
                changed = new_id != account.google_ads_customer_id
                # Ett Google Ads-konto hör till en kund: ett id som ett annat
                # Flamingo-konto har tas aldrig emot (också en regel i databasen).
                if changed and not account.is_demo and google_id_taken(new_id, account.pk):
                    id_form.add_error("google_ads_customer_id", GOOGLE_ID_TAKEN)
                elif _save_google_id(account, new_id, changed):
                    messages.success(
                        request,
                        "Tack. ADX kopplar kontot under förvaltarkontot, och läget syns här.",
                    )
                    return redirect("flamingo:app_google")
                else:
                    id_form.add_error("google_ads_customer_id", GOOGLE_ID_TAKEN)
        elif action == "new":
            if not account.google_linked:
                account.google_status = FlamingoAccount.GOOGLE_REQUESTED_NEW
                account.save(update_fields=["google_status", "updated_at"])
                messages.success(
                    request,
                    "Tack. ADX skapar kontot i ditt namn och ger dig tillgång till det.",
                )
            return redirect("flamingo:app_google")
        else:
            return redirect("flamingo:app_google")
    if id_form is None:
        id_form = GoogleIdForm(initial={"google_ads_customer_id": account.google_ads_customer_id})
    state = _google_state(account)
    # Förfrågans notering säger samma sak som läget överst.
    note = account.google_note
    if state and note == google_accounts.NOTE_LINK:
        note = ""
    return render_app(
        request,
        "flamingo/app/onboarding/google.html",
        "google",
        {
            "id_form": id_form,
            "timeline": _google_timeline(account, account.campaigns.count()),
            "google_state": state,
            "adx_note": note,
            "show_steps": True,
        },
    )


# ---------------------------------------------------------------------------
# Inställningarna
# ---------------------------------------------------------------------------


@app_view
def settings_view(request, account):
    if request.method == "POST":
        form = SettingsForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            account.notify_phone = data["notify_phone"]
            account.notify_sms = data["notify_sms"]
            account.autoreply_enabled = data["autoreply_enabled"]
            account.autoreply_text = data["autoreply_text"] or AUTOREPLY_DEFAULT
            account.save(
                update_fields=[
                    "notify_phone",
                    "notify_sms",
                    "autoreply_enabled",
                    "autoreply_text",
                    "updated_at",
                ]
            )
            messages.success(request, "Inställningarna är sparade.")
            return redirect("flamingo:app_settings")
    else:
        form = SettingsForm(
            initial={
                "notify_phone": account.notify_phone,
                "notify_sms": account.notify_sms,
                "autoreply_enabled": account.autoreply_enabled,
                "autoreply_text": account.autoreply_text,
            }
        )
    text = form["autoreply_text"].value() or ""
    length, parts = sms_parts(text)
    contacts = account.customer.users.order_by("first_name", "last_name", "email")
    return render_app(
        request,
        "flamingo/app/onboarding/settings.html",
        "settings",
        {
            "form": form,
            "sms_configured": sms_configured(),
            "sms_length": length,
            "sms_count": parts,
            "autoreply_max": AUTOREPLY_MAX,
            "quiet_from": f"{sms.QUIET_FROM:%H}",
            "quiet_until": f"{sms.QUIET_UNTIL:%H}",
            "contacts": contacts,
        },
    )
