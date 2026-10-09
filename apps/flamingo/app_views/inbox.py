"""
Inkorgen (kundresan steg 10-11): förfrågningarna, var de kom ifrån, status
och belopp.

- Listan: nyast först, filter som chips (Alla visar allt utom skräp), kort
  i alla bredder. Kunden kan lägga till en förfrågan själv (ett samtal, ett
  mejl): källa "manuell", inga sms.
- Förfrågan: meddelandet och svaren, "Var kom hen ifrån?", ringknappen och
  status. Vunnen kräver ett belopp; leads.set_status() köar konverteringen
  till Google när förfrågan har ett gclid (Lead.can_send_to_google).
- Ett klick på telefonnumret på sidan är en förfrågan utan namn och nummer
  ("Klick på telefonnumret"): ägaren fick samtalet och sätter status som
  för vilken förfrågan som helst, och Vunnen med belopp blir en affär.

Allt hämtas via kontot (app_view): en förfrågan som inte är kontots egen är
404. Byrån i kundvyn gör det kunden gör, på riktigt och i byråns namn; bara
utkastförhandsvisningen i /manage/ är skrivskyddad (grinden nekar POST och
mallarna döljer knapparna med read_only). Inga mejl skickas härifrån.

Svar på utskick (apps/utskick, README C.2 och G): varje svarstråd är en
förfrågan med source "reply" (Lead.reply_thread). Listan sorteras på
activity_at (senaste svaret), kortet visar "Klar" för ett hanterat svar och
början av senaste svaret, och förfrågans sida visar tråden
(flamingo/app/utskick/_thread.html) med svarsrutan. När utskick är på för
kontot blir statuschipsen en rad med typer och antal (Alla, Förfrågningar,
Sms-svar, Avregistreringar; ?typ=) och statusen ett val med en Visa-knapp.
Utan utskick är listan som förut.
"""

from datetime import timedelta

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from .. import leads, sms
from ..models import ConversionUpload, Lead
from ..rules import when_text
from ..templatetags.flamingo_app import kr, tal
from . import app_view, render_app

PER_PAGE = 30
#: Förfrågningarna bredvid den öppna (bara i bred skärm).
SIDE_LIST = 12

#: Filterchipsen: (värde i ?status=, rubrik). Tomt = allt utom skräp.
FILTERS = (
    ("", "Alla"),
    (Lead.STATUS_NEW, "Nya"),
    (Lead.STATUS_CONTACTED, "Kontaktade"),
    (Lead.STATUS_QUOTE, "Offert skickad"),
    (Lead.STATUS_WON, "Vunna"),
    (Lead.STATUS_LOST, "Förlorade"),
    (Lead.STATUS_JUNK, "Skräp"),
)

_PAID = ("cpc", "ppc", "paid", "paidsearch", "sem")

#: Typerna i chipsraden när utskick är på (?typ=): (värde, rubrik). Tomt =
#: allt utom skräp. E-postsvar visas bara när det finns några (S3).
TYPES = (
    ("", "Alla"),
    ("forfragningar", "Förfrågningar"),
    ("sms-svar", "Sms-svar"),
    ("e-postsvar", "E-postsvar"),
    ("avregistreringar", "Avregistreringar"),
)
#: Statusvalet under chipsen när utskick är på.
STATUS_OPTIONS = (
    ("", "Alla statusar"),
    (Lead.STATUS_NEW, "Nya"),
    (Lead.STATUS_CONTACTED, "Kontaktade eller klara"),
    (Lead.STATUS_QUOTE, "Offert skickad"),
    (Lead.STATUS_WON, "Vunna"),
    (Lead.STATUS_LOST, "Förlorade"),
    (Lead.STATUS_JUNK, "Skräp"),
)


def _type_filter(value):
    """Q för en typ i chipsraden (TYPES)."""
    from apps.utskick.models import CHANNEL_EMAIL, CHANNEL_SMS, Thread

    reply = Q(source=Lead.SOURCE_REPLY)
    stop = Q(reply_thread__kind=Thread.Kind.STOP)
    return {
        "forfragningar": ~reply,
        "sms-svar": reply & Q(reply_thread__channel=CHANNEL_SMS) & ~stop,
        "e-postsvar": reply & Q(reply_thread__channel=CHANNEL_EMAIL) & ~stop,
        "avregistreringar": reply & stop,
    }.get(value, Q())


def _utskick_on(account):
    from apps.utskick.access import is_enabled

    return is_enabled(account)


# ---------------------------------------------------------------------------
# Hjälpare för mallarna
# ---------------------------------------------------------------------------


def channel(lead):
    """Varifrån förfrågan kom, i ord: Google sök, samtal, manuell, Sms-svar,
    Utskick: Höstservice ..."""
    if lead.source == Lead.SOURCE_REPLY:
        from apps.utskick.threads import channel_label

        return channel_label(getattr(lead, "reply_thread", None))
    if lead.utskick_id:
        attribution = lead.attribution if isinstance(lead.attribution, dict) else {}
        return ("Utskick: " + str(attribution.get("name", "")).strip())[:60].rstrip(": ")
    if lead.source == Lead.SOURCE_MANUAL:
        return "Lagd för hand"
    if lead.source == Lead.SOURCE_CALL:
        return "Samtal"
    utm = lead.utm if isinstance(lead.utm, dict) else {}
    source = str(utm.get("utm_source", "")).strip()
    medium = str(utm.get("utm_medium", "")).strip().lower()
    if lead.has_click_id:
        return "Google sök"
    if source.lower() == "google" and medium in _PAID:
        return "Google sök"
    if source:
        return f"Länk från {source}"[:60]
    if lead.source == Lead.SOURCE_CALL_CLICK:
        return "Numret på sidan"
    return "Formulär på sidan"


def google_note(lead):
    """Vad som händer med beloppet hos Google, i en mening för fältet: ""
    (det går dit), "braid" (bara ett klick-id från iPhone) eller "no_click"."""
    if lead.can_send_to_google:
        return ""
    if lead.has_click_id:
        return "braid"
    return "no_click"


def ago(moment, now):
    """'nyss', '4 min', '3 h', 'i går', '2 okt': kort, som i mockupen."""
    delta = now - moment
    if delta < timedelta(minutes=1):
        return "nyss"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} min"
    local, today = timezone.localtime(moment), timezone.localtime(now)
    if delta < timedelta(hours=24) and local.date() == today.date():
        return f"{int(delta.total_seconds() // 3600)} h"
    text = when_text(moment, now)
    return "i går" if text.startswith("i går") else text


def status_label(lead):
    """Statusen i ord. Ett hanterat svar på utskick är "Klar" (README C.2)."""
    if lead.source == Lead.SOURCE_REPLY:
        from apps.utskick.threads import status_label as reply_label

        return reply_label(lead)
    return lead.get_status_display()


def _cards(lead_list, now):
    """Listans kort: förfrågan plus det kortet visar. Ett svar på utskick
    visar tiden för senaste svaret."""
    for lead in lead_list:
        moment = lead.activity_at if lead.source == Lead.SOURCE_REPLY else lead.created_at
        lead.card_ago = ago(moment or lead.created_at, now)
        lead.card_channel = channel(lead)
        lead.card_status = status_label(lead)
    return lead_list


def _base_queryset(account):
    return account.leads.select_related("service", "campaign__service", "reply_thread")


# ---------------------------------------------------------------------------
# Lägg till för hand
# ---------------------------------------------------------------------------


class ManualLeadForm(forms.Form):
    name = forms.CharField(label="Namn", max_length=leads.NAME_MAX, required=False)
    phone = forms.CharField(label="Telefon", max_length=leads.PHONE_MAX, required=False)
    service = forms.ChoiceField(label="Tjänst", required=False)
    message = forms.CharField(
        label="Vad gäller det?",
        max_length=leads.MESSAGE_MAX,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, account, **kwargs):
        super().__init__(*args, **kwargs)
        services = account.services.filter(is_active=True).order_by("order", "id")
        self.fields["service"].choices = [("", "Ingen särskild")] + [
            (str(s.pk), s.name) for s in services
        ]
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("id", f"fl-add-{name}")
            if getattr(field, "max_length", None):
                field.error_messages["max_length"] = f"Högst {field.max_length} tecken."
        self.fields["name"].widget.attrs["autocomplete"] = "off"
        self.fields["phone"].widget.input_type = "tel"
        self.fields["phone"].widget.attrs["inputmode"] = "tel"

    def clean(self):
        data = super().clean()
        if not (data.get("name") or "").strip() and not (data.get("phone") or "").strip():
            raise forms.ValidationError("Skriv ett namn eller ett telefonnummer.")
        return data


# ---------------------------------------------------------------------------
# Vyerna
# ---------------------------------------------------------------------------


@app_view
def lead_list(request, account):
    if request.method == "POST":
        add_form = ManualLeadForm(request.POST, account=account)
        if add_form.is_valid():
            lead = leads.create_manual_lead(account, add_form.cleaned_data, request.user)
            messages.success(request, f"{lead.display_name} är tillagd i inkorgen.")
            return redirect("flamingo:app_lead", pk=lead.pk)
    else:
        add_form = ManualLeadForm(account=account)

    now = timezone.now()
    status = request.GET.get("status", "")
    if status not in dict(Lead.STATUS_CHOICES):
        status = ""
    utskick_on = _utskick_on(account)
    kind = request.GET.get("typ", "") if utskick_on else ""
    if kind not in dict(TYPES):
        kind = ""

    counts = account.leads.aggregate(
        all=Count("pk", filter=~Q(status=Lead.STATUS_JUNK)),
        **{key: Count("pk", filter=Q(status=key)) for key, _ in Lead.STATUS_CHOICES},
    )
    filters = [
        {
            "value": value,
            "label": label,
            "count": counts["all" if not value else value],
            "active": value == status,
        }
        for value, label in FILTERS
    ]
    types = []
    if utskick_on:
        type_counts = account.leads.exclude(status=Lead.STATUS_JUNK).aggregate(
            **{(value or "alla"): Count("pk", filter=_type_filter(value)) for value, _ in TYPES}
        )
        types = [
            {
                "value": value,
                "label": label,
                "count": type_counts[value or "alla"],
                "active": value == kind,
            }
            for value, label in TYPES
            if value != "e-postsvar" or type_counts[value] or kind == value
        ]

    queryset = _base_queryset(account).order_by("-activity_at", "-id")
    queryset = (
        queryset.filter(status=status) if status else queryset.exclude(status=Lead.STATUS_JUNK)
    )
    if kind:
        queryset = queryset.filter(_type_filter(kind))
    page = Paginator(queryset, PER_PAGE).get_page(request.GET.get("sida"))

    month_start = timezone.localtime(now).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Svar på utskick är ingen förfrågan (README C.2): de räknas inte här.
    this_month = (
        account.leads.filter(created_at__gte=month_start)
        .exclude(status=Lead.STATUS_JUNK)
        .exclude(source=Lead.SOURCE_REPLY)
        .count()
    )

    return render_app(
        request,
        "flamingo/app/inbox/list.html",
        "inbox",
        {
            "page": page,
            "leads": _cards(list(page.object_list), now),
            "filters": filters,
            "status": status,
            "status_label": dict(FILTERS).get(status, "Alla"),
            "utskick_on": utskick_on,
            "types": types,
            "typ": kind,
            "type_label": dict(TYPES).get(kind, "Alla"),
            "status_options": STATUS_OPTIONS,
            "this_month": this_month,
            "total": counts["all"] + counts[Lead.STATUS_JUNK],
            "add_form": add_form,
            "add_open": request.method == "POST",
        },
    )


@app_view
def lead_detail(request, account, pk):
    lead = get_object_or_404(_base_queryset(account), pk=pk)
    error = ""
    posted_status, posted_value = lead.status, ""
    if request.method == "POST":
        posted_status = request.POST.get("status", "")
        posted_value = request.POST.get("value_kr", "").strip()
        try:
            leads.set_status(lead, posted_status, posted_value, request.user)
        except ValueError as exc:
            error = str(exc)
            lead.refresh_from_db()
        else:
            text = f"Sparat: {status_label(lead).lower()}"
            if lead.status == Lead.STATUS_WON:
                text += f", {kr(lead.value_kr)}"
            messages.success(request, text + ".")
            return redirect("flamingo:app_lead", pk=lead.pk)
    return render_detail(
        request, account, lead, error=error, posted_status=posted_status, posted_value=posted_value
    )


def render_detail(
    request,
    account,
    lead,
    *,
    error="",
    posted_status=None,
    posted_value="",
    reply_text="",
    reply_error="",
):
    """Förfrågans sida. Svarsvyn i apps/utskick (inbox_reply) visar den
    igen med svarstexten kvar och felet när ett svar inte gick iväg."""
    now = timezone.now()
    conversions = list(ConversionUpload.objects.filter(lead=lead).order_by("created_at", "pk"))
    side = _cards(
        list(
            _base_queryset(account)
            .exclude(status=Lead.STATUS_JUNK)
            .order_by("-activity_at", "-id")[:SIDE_LIST]
        ),
        now,
    )
    utm = lead.utm if isinstance(lead.utm, dict) else {}
    value_input = posted_value if error else tal(lead.value_kr)
    is_reply = lead.source == Lead.SOURCE_REPLY
    status_names = dict(Lead.STATUS_CHOICES)
    if is_reply:
        status_names[Lead.STATUS_CONTACTED] = "Klar"
    context = {
        "lead": lead,
        "side_leads": side,
        "channel": channel(lead),
        "received": when_text(lead.created_at, now),
        "answers": list((lead.answers or {}).items()) if isinstance(lead.answers, dict) else [],
        "tel": sms.tel_href(lead.phone) if lead.phone else "",
        "first_name": sms.first_name(lead.name) or "hen",
        "has_click_id": lead.has_click_id,
        "to_google": lead.can_send_to_google,
        "google_note": google_note(lead),
        "is_call_click": lead.source == Lead.SOURCE_CALL_CLICK,
        "is_reply": is_reply,
        "lead_status": status_label(lead),
        "utm_campaign": str(utm.get("utm_campaign", "")),
        "statuses": [(key, status_names[key]) for key in leads.INBOX_STATUSES],
        "checked_status": lead.status if posted_status is None else posted_status,
        "value_input": value_input,
        "error": error,
        "conversions": conversions,
        "sms_rows": lead.sms_log.order_by("created_at", "id"),
    }
    if is_reply:
        from apps.utskick.app_views.inbox_reply import thread_context

        context.update(thread_context(request, account, lead, reply_text, reply_error))
    return render_app(request, "flamingo/app/inbox/detail.html", "inbox", context)
