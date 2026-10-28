"""
Inställningar för kontakter (README I.9, raderna markerade S1, och H.5).

Sedan menyn med sex punkter (2026-10-28, apps/flamingo/app_nav.py) är det
en del av den samlade sidan under Företaget > Inställningar
(flamingo:app_settings, #kontakter): settings_context ger delens kontext,
contacts_settings tar emot formuläret.

Läses: företagsnamnet i utskick och adressen för anmälan (byrån sätter
dem: "Be ADX ändra det."), biträdesavtalet ("Godkänt av ... · Visa"),
senast exporterad och hur många kontakter som ryms.

Ändras av kunden (eller byrån i kundvyn, på riktigt):
samtyckestexterna för sms och e-post (företagsnamnet måste stå i dem),
kryssrutorna på landningssidor, integritetspolicyn (https, tomt ger den
genererade sidan), texten under E-post med erbjudanden på Mina utskick och
den extra texten på avregistreringssidan.

Bara de fälten sparas (update_fields): på/av, gränser och byråns fält rörs
aldrig härifrån.
"""

from django import forms
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse

from apps.common.security import normalize_typography

from .. import capture, optin
from ..access import can_collect, collect_block_reason, dpa_ok, latest_acceptance, utskick_view
from ..models import Contact, ExportLog, UtskickSettings
from .contacts import date_text, export_text, group
from .dpa import who_accepted

EDITABLE = (
    "consent_text_sms",
    "consent_text_email",
    "lp_consent",
    "privacy_url",
    "pref_email_note",
    "unsubscribe_text",
)


def _text(value, max_length):
    return " ".join(normalize_typography(str(value or "")).split())[:max_length]


class ContactSettingsForm(forms.ModelForm):
    class Meta:
        model = UtskickSettings
        fields = list(EDITABLE)
        widgets = {
            "consent_text_sms": forms.Textarea(attrs={"rows": 2}),
            "consent_text_email": forms.Textarea(attrs={"rows": 2}),
            "unsubscribe_text": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "kt-set-%s")
        super().__init__(*args, **kwargs)
        self.fields["privacy_url"].widget.attrs["placeholder"] = "https://"

    def _consent_text(self, name):
        text = _text(self.cleaned_data.get(name), 200)
        if not text:
            raise forms.ValidationError("Skriv texten som står vid kryssrutan.")
        company = self.instance.display_name
        if company and company.casefold() not in text.casefold():
            raise forms.ValidationError(
                f"Skriv {company} i texten, så att personen vet vem som skickar."
            )
        return text

    def clean_consent_text_sms(self):
        return self._consent_text("consent_text_sms")

    def clean_consent_text_email(self):
        return self._consent_text("consent_text_email")

    def clean_privacy_url(self):
        url = (self.cleaned_data.get("privacy_url") or "").strip()
        if url and not url.lower().startswith("https://"):
            raise forms.ValidationError("Adressen ska börja med https://")
        return url

    def clean_pref_email_note(self):
        return _text(self.cleaned_data.get("pref_email_note"), 120)

    def clean_unsubscribe_text(self):
        return _text(self.cleaned_data.get("unsubscribe_text"), 300)


def _public_url(request, name, slug):
    if not slug:
        return ""
    try:
        return request.build_absolute_uri(reverse(name, args=[slug]))
    except NoReverseMatch:
        return ""


def _lp_state(account, row, privacy_ok):
    """Visas kryssrutorna på landningssidorna just nu, och om inte: varför
    (samma villkor som capture.lp_consent_channels)."""
    if not row.lp_consent:
        return False, "Av. Formulären på landningssidorna har inga kryssrutor."
    if not can_collect(account):
        return False, collect_block_reason(account)
    if not privacy_ok:
        return False, capture.PRIVACY_MISSING_TEXT
    if optin.doi_ready():
        return True, "Visas i formulären: sms, och e-post där formuläret frågar efter e-post."
    return (
        True,
        "Visas i formulären för sms. Rutan för e-post visas inte än: "
        "bekräftelsemejlen är inte påslagna. Be ADX slå på dem.",
    )


def _dpa_text(account):
    """ "Godkänt av Anna Lindqvist 2 okt 2026", eller vad som saknas."""
    acceptance = latest_acceptance(account)
    if acceptance is None:
        return "Demokontot behöver inget godkännande." if account.is_demo else "Inte godkänt än."
    who = who_accepted(acceptance)
    if acceptance.accepted_as_staff:
        who = f"{who} åt kunden"
    text = f"Godkänt av {who} {date_text(acceptance.accepted_at)}"
    if not acceptance.version.is_current:
        text += ". En ny version finns och behöver godkännas."
    return text


#: Inställningarna för kontakter är en del av den samlade sidan under
#: Företaget > Inställningar (flamingo:app_settings, ankaret #kontakter).
SETTINGS_ANCHOR = "#kontakter"


@utskick_view
def contacts_settings(request, account):
    """Inställningar för kontakter. Delen ritas på den samlade sidan under
    Företaget > Inställningar (flamingo:app_settings); adressen finns kvar
    för formuläret och för gamla länkar: GET skickar dit, till #kontakter,
    och ett formulär med fel visas där med felen kvar."""
    row = request.utskick_settings
    page = redirect(reverse("flamingo:app_settings") + SETTINGS_ANCHOR)
    if request.method != "POST":
        return page
    form = ContactSettingsForm(request.POST, instance=row)
    if form.is_valid():
        saved = form.save(commit=False)
        saved.save(update_fields=[*EDITABLE, "updated_at"])
        messages.success(request, "Inställningarna är sparade.")
        return page
    from apps.flamingo.app_views.onboarding import render_settings

    return render_settings(request, account, contacts_form=form)


def settings_context(request, account, row, form=None):
    """Kontexten för delen Kontakter på den samlade inställningssidan
    (templates/flamingo/app/kontakter/_settings.html). form är ett postat
    formulär med fel, annars ett nytt. Formuläret heter kt_form i
    kontexten (flamingo-delen har redan ett form)."""
    form = form or ContactSettingsForm(instance=row)
    privacy_ok = capture.privacy_available(account, row)
    # Den genererade sidan visas bara när den finns (ingen egen policy och
    # tillräckliga uppgifter under Företaget).
    privacy_page_url = ""
    if privacy_ok and not row.privacy_url:
        privacy_page_url = capture.privacy_url(account, row, absolute=True)
    lp_on, lp_text = _lp_state(account, row, privacy_ok)
    count = Contact.objects.filter(account=account).count()
    last = (
        ExportLog.objects.filter(account=account)
        .select_related("user")
        .order_by("-at", "-pk")
        .first()
    )
    return {
        "kt_form": form,
        "signup_url": _public_url(request, "utskick_public:signup", row.public_slug),
        "privacy_page_url": privacy_page_url,
        "privacy_ok": privacy_ok,
        "privacy_text": capture.PRIVACY_MISSING_TEXT,
        "lp_on": lp_on,
        "lp_text": lp_text,
        "dpa_ok": dpa_ok(account),
        "dpa_text": _dpa_text(account),
        "export_text": export_text(last) or "Ingen export än.",
        "contact_count": group(count),
        "contact_limit": group(row.contact_limit),
    }
