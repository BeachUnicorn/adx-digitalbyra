"""
Biträdesavtalet (README D6, B.1 DpaAcceptance, H.9 och I.4).

Kunden godkänner den aktuella versionen (DpaVersion.is_current) innan den
första kontakten läggs till eller importeras; efter en ny version krävs ett
nytt godkännande för allt som tar in kontakter (access.can_collect), medan
det som redan finns fungerar som vanligt.

- Sidan visar avtalets text som den publicerades (DpaVersion.text), vem som
  godkände och när, och texten som godkändes om den skiljer sig.
- Byrån i kundvyn får godkänna åt kunden, men måste skriva vem hos kunden
  som godkände och hur (staff_statement); raden sparas som byråns.
- Formuläret bär versionens id: har avtalet bytts medan sidan var öppen
  nekas godkännandet ("Avtalet har ändrats medan du läste.").
- Utan aktuell version: "Avtalssidan saknas. Kontakta ADX." och ett larm
  till byrån högst en gång per dygn (alerts.agency med once). Kunden mejlas
  aldrig. Demokontot behöver inget godkännande (access.dpa_ok) och larmar
  inte.
- ?nasta=ny eller ?nasta=import skickar tillbaka till det kunden var på väg
  att göra (bara de två; ingen adress tas från förfrågan).
"""

from django import forms
from django.contrib import messages
from django.shortcuts import redirect

from apps.common.net import client_ip
from apps.flamingo.limits import ip_hash

from .. import alerts
from ..access import (
    DPA_MISSING_TEXT,
    actor_for,
    current_dpa,
    dpa_ok,
    latest_acceptance,
    user_label,
    utskick_view,
)
from ..models import DpaAcceptance
from . import render_contacts
from .contacts import date_text

#: ?nasta= -> vart kunden skickas efter godkännandet.
NEXT = {"ny": "flamingo:app_contact_new", "import": "flamingo:app_import"}
CHANGED_TEXT = "Avtalet har ändrats medan du läste. Läs igenom den nya versionen och godkänn igen."
STATEMENT_TEXT = "Skriv vem hos kunden som godkände avtalet, och hur."


class DpaForm(forms.Form):
    version = forms.IntegerField(widget=forms.HiddenInput)
    accept = forms.BooleanField(
        error_messages={"required": "Kryssa i att du har läst avtalet och godkänner det."}
    )
    staff_statement = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, staff=False, **kwargs):
        kwargs.setdefault("auto_id", "kt-dpa-%s")
        super().__init__(*args, **kwargs)
        self.staff = staff

    def clean_staff_statement(self):
        statement = " ".join(str(self.cleaned_data.get("staff_statement") or "").split())
        if self.staff and not statement:
            raise forms.ValidationError(STATEMENT_TEXT)
        return statement if self.staff else ""


def who_accepted(acceptance):
    """Vem som godkände: namnet, eller "ADX (Giovanni)" för byrån i kundvyn."""
    if acceptance.accepted_as_staff:
        first = acceptance.accepted_by.first_name if acceptance.accepted_by else ""
        return f"ADX ({first})" if first else "ADX"
    return user_label(acceptance.accepted_by) or "en användare som tagits bort"


def _alert_missing(account):
    alerts.agency(
        "Utskick: biträdesavtalet saknas",
        [
            f"Konto {account.pk} öppnade biträdesavtalet i Kontakter, men ingen version är "
            "publicerad.",
            "Publicera avtalet från sidan /bitradesavtal/ under /manage/utskick/.",
        ],
        once="dpa_missing",
        window="day",
    )


@utskick_view
def dpa(request, account):
    current = current_dpa()
    actor = actor_for(request)
    nasta = request.POST.get("nasta") or request.GET.get("nasta") or ""
    nasta = nasta if nasta in NEXT else ""
    if current is None and not account.is_demo:
        # Demokontot behöver inget avtal (README L): inget larm för det.
        _alert_missing(account)
    form = None
    if current is not None:
        if request.method == "POST":
            form = DpaForm(request.POST, staff=actor.staff)
            if form.is_valid():
                if form.cleaned_data["version"] != current.pk:
                    form.add_error(None, CHANGED_TEXT)
                else:
                    DpaAcceptance.objects.create(
                        account=account,
                        version=current,
                        accepted_by=actor.user,
                        accepted_as_staff=actor.staff,
                        staff_statement=form.cleaned_data["staff_statement"][:300],
                        ip_hash=ip_hash(client_ip(request)),
                    )
                    messages.success(
                        request, "Biträdesavtalet är godkänt. Nu kan du lägga till kontakter."
                    )
                    return redirect(NEXT.get(nasta, "flamingo:app_contacts"))
        else:
            form = DpaForm(initial={"version": current.pk}, staff=actor.staff)
    elif request.method == "POST":
        messages.error(request, DPA_MISSING_TEXT)
    acceptance = latest_acceptance(account)
    accepted_current = bool(acceptance and current and acceptance.version_id == current.pk)
    context = {
        "dpa": current,
        "published_text": date_text(current.published_at) if current else "",
        "acceptance": acceptance,
        "accepted_current": accepted_current,
        "accepted_who": who_accepted(acceptance) if acceptance else "",
        "accepted_text": date_text(acceptance.accepted_at) if acceptance else "",
        "old_version": acceptance.version if acceptance and not accepted_current else None,
        "dpa_ok": dpa_ok(account),
        "form": form,
        "is_staff_actor": actor.staff,
        "nasta": nasta,
        "missing_text": DPA_MISSING_TEXT,
    }
    return render_contacts(request, "flamingo/app/kontakter/dpa.html", "settings", context)
