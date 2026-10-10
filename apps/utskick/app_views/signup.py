"""
Inställningarna för anmälningssidan (README I.7 "Anmälan", I.1, H.1, H.5):
rubrik, ingress, kanal, listan och taggarna som den som anmäler sig hamnar
i, på/av och den publika adressen med Kopiera länk och Förhandsgranska.
Ägs av agenten för de publika sidorna (samma regler som
public_views.signup).

- Sidan (SignupForm, en per kund) skapas avstängd första gången kunden
  sparar; kunden slår på den själv.
- Kanalerna: e-post (den som anmäler sig får ett bekräftelsemejl och
  räknas först efter klicket) och från S2 sms (README L, Security 13 och
  K.2.5): ett sms med en länk till k.adx.se/b/, och sms räknas först efter
  klicket. Sms går att välja när byrån slagit på sms-utskicken och kundens
  sms är aktiverat (optin.sms_signup_block). Formuläret bär
  kanaler_visade, så att en sparning utan rutorna (äldre formulär)
  behåller kanalerna.
- Listan och taggarna kommer ur formuläret som id:n och går genom
  access.owned_ids (ett främmande id ger 400, H.1).
- Rutan "Öppen" säger varför sidan ändå är stängd för besökarna, och hur
  det låses upp: biträdesavtalet, integritetstexten (H.5) eller att
  bekräftelsemejlen inte är påslagna. QR-koden (S4) laddas ned under Adress.
- Förhandsgranska finns när sidan har en adress, också när den är stängd:
  ?forhandsgranska=1 visar sidan för kontots användare och byrån, med en
  remsa och utan att ta emot anmälningar.
- Listan och taggarna läggs på när personen bekräftat i mejlet.

Byrån i kundvyn ändrar på riktigt, som kunden.
"""

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse

from apps.common.security import normalize_typography

from .. import capture, optin
from ..access import collect_block_reason, owned_ids, utskick_view
from ..models import CHANNEL_EMAIL, CHANNEL_SMS, CHANNELS, ContactList, SignupForm, Tag
from . import render_contacts

TEMPLATE = "flamingo/app/kontakter/signup.html"
DEFAULT_TITLE = "Få våra erbjudanden"
DOI_OFF_TEXT = "Bekräftelsemejlen är inte påslagna än. Be ADX slå på dem."
INACTIVE_TEXT = "Sidan är avstängd. Slå på den nedan när du vill ta emot anmälningar."
CHANNEL_NEEDED_TEXT = "Välj minst en kanal."
#: Formulärets markering att kanalrutorna visades (S2).
CHANNELS_SHOWN = "kanaler_visade"


def _text(value, max_length):
    return " ".join(normalize_typography(str(value or "")).split())[:max_length]


class SignupSettingsForm(forms.Form):
    title = forms.CharField(label="Rubrik", max_length=80)
    intro = forms.CharField(
        label="Ingress",
        max_length=400,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    is_active = forms.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "kt-an-%s")
        super().__init__(*args, **kwargs)
        self.fields["title"].error_messages["required"] = "Skriv en rubrik."

    def clean_title(self):
        title = _text(self.cleaned_data.get("title"), 80)
        if not title:
            raise forms.ValidationError("Skriv en rubrik.")
        return title

    def clean_intro(self):
        return _text(self.cleaned_data.get("intro"), 400)


def channel_blocks(account):
    """{kanal: varför den inte kan erbjudas på sidan, eller ""}."""
    return {
        CHANNEL_EMAIL: "" if optin.doi_ready() else DOI_OFF_TEXT,
        CHANNEL_SMS: optin.sms_signup_block(account),
    }


def closed_reasons(account, row, sida):
    """Varför anmälningssidan är stängd för besökarna, i klartext (tom
    lista när den är öppen). Samma villkor som public_views._signup_open:
    stängd när ingen av sidans kanaler kan erbjudas."""
    reasons = []
    block = collect_block_reason(account)
    if block:
        reasons.append(block)
    if not capture.privacy_available(account, row):
        reasons.append(capture.PRIVACY_MISSING_TEXT)
    chosen = [ch for ch in (sida.channels if sida else [CHANNEL_EMAIL]) if ch in CHANNELS]
    blocks = channel_blocks(account)
    if not any(not blocks[ch] for ch in chosen):
        reasons.extend(blocks[ch] for ch in chosen if blocks[ch])
    if sida is None or not sida.is_active:
        reasons.append(INACTIVE_TEXT)
    return reasons


def _posted_channels(request, sida):
    """Kanalerna ur formuläret (bara kända), eller sidans nuvarande när
    formuläret inte visade rutorna. Sms bara när det går att välja; en
    redan vald kanal får stå kvar."""
    current = list(sida.channels) if sida else [CHANNEL_EMAIL]
    if request.POST.get(CHANNELS_SHOWN) != "1":
        return current
    wanted = request.POST.getlist("kanal")
    return [ch for ch in CHANNELS if ch in wanted]


def _channel_rows(request, account, sida):
    chosen = set(sida.channels if sida else [CHANNEL_EMAIL])
    if request.method == "POST" and request.POST.get(CHANNELS_SHOWN) == "1":
        chosen = set(request.POST.getlist("kanal"))
    blocks = channel_blocks(account)
    return [
        {
            "kanal": channel,
            "rubrik": "E-post" if channel == CHANNEL_EMAIL else "Sms",
            "vald": channel in chosen,
            "hinder": blocks[channel],
        }
        for channel in CHANNELS
    ]


@utskick_view
def signup_settings(request, account):
    row = request.utskick_settings
    sida = SignupForm.objects.filter(account=account).first()
    listor = ContactList.objects.filter(account=account).order_by("name")
    taggar = Tag.objects.filter(account=account).order_by("name")
    if request.method == "POST":
        list_ids = owned_ids(
            ContactList, account, [v for v in [request.POST.get("add_to_list", "")] if v], limit=1
        )
        tag_ids = owned_ids(
            Tag, account, [v for v in request.POST.getlist("add_tags") if v], limit=200
        )
        form = SignupSettingsForm(request.POST)
        channels = _posted_channels(request, sida)
        if not channels:
            form.is_valid()
            form.add_error(None, CHANNEL_NEEDED_TEXT)
        if form.is_valid():
            with transaction.atomic():
                if sida is None:
                    sida = SignupForm(account=account, channels=[CHANNEL_EMAIL])
                sida.channels = channels
                sida.title = form.cleaned_data["title"]
                sida.intro = form.cleaned_data["intro"]
                sida.add_to_list_id = list_ids[0] if list_ids else None
                sida.is_active = form.cleaned_data["is_active"]
                sida.save()
                sida.add_tags.set(tag_ids)
            messages.success(request, "Anmälningssidan är sparad.")
            return redirect("flamingo:app_signup")
        chosen_list = list_ids[0] if list_ids else None
        chosen_tags = set(tag_ids)
    else:
        form = SignupSettingsForm(
            initial={
                "title": sida.title if sida else DEFAULT_TITLE,
                "intro": sida.intro if sida else "",
                "is_active": bool(sida and sida.is_active),
            }
        )
        chosen_list = sida.add_to_list_id if sida else None
        chosen_tags = set(sida.add_tags.values_list("pk", flat=True)) if sida else set()
    reasons = closed_reasons(account, row, sida)
    public_url = ""
    if row.public_slug:
        public_url = request.build_absolute_uri(
            reverse("utskick_public:signup", args=[row.public_slug])
        )
    context = {
        "sida": sida,
        "form": form,
        "listor": listor,
        "taggar": taggar,
        "vald_lista": chosen_list,
        "valda_taggar": chosen_tags,
        "publik_url": public_url,
        "oppen": not reasons,
        "skal": reasons,
        # En stängd sida går att förhandsgranska (public_views._signup_page).
        "kan_forhandsgranska": bool(public_url),
        "forhandsgranska_url": f"{public_url}?forhandsgranska=1" if public_url else "",
        # S2: kanalerna sidan erbjuder (e-post, sms) och varför en inte går.
        "kanaler": _channel_rows(request, account, sida),
        "kanaler_visade": CHANNELS_SHOWN,
    }
    return render_contacts(request, TEMPLATE, "signup", context)
