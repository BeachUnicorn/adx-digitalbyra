"""
Skicka sms från kontaktkortet (README I.1, I.4, I.7, J S4): kontakter/<pk>/sms/.

    contact_sms     kontakter/<pk>/sms/   GET kortet med sms-rutan öppen (#sms),
                                          POST skickar med contact_sms.send
    card_context(request, account, kontakt, sms=None) -> dict
                    kortets S4-delar (app_views/contacts._render_detail, markerat
                    block): "Svarar oftast" under Sammanfattning, knappen
                    "Skicka sms" och, på kontakter/<pk>/sms/, rutan med
                    formuläret eller varför sms inte kan skickas nu

Rutan följer räknarens kontrakt som Inkorgens svar (S2-HANDOFF.md):
textarea[data-ut-sms] skriver i elementet som data-ut-sms-count pekar på,
och företagets namn (data-ut-sms-suffix) räknas med men skrivs inte; utan
JavaScript står serverns rad kvar och servern prövar längden igen. Byrån i
kundvyn kryssar i "Jag skickar det här som ADX åt Exempelrör." och knappen
heter "Skicka som ADX" (I.4). Ett nekat sms visar kortet igen med texten
kvar och felet ovanför. Reglerna står i apps/utskick/contact_sms.py.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .. import contact_sms as rules
from .. import threads, timeline
from ..access import actor_for, owned, utskick_view
from ..models import Contact
from ..normalize import display_phone

STAFF_TEXT = "Jag skickar det här som ADX åt {name}."
STAFF_REQUIRED_TEXT = "Kryssa i att du skickar som ADX åt kunden. Sms:et skickades inte."
#: Rutan tar emot så här mycket text (högst sex delar prövas sedan).
TEXT_MAX = 2000


def _company(account):
    return threads.display_name(account) or account.customer.name


def card_context(request, account, kontakt, sms=None):
    """Kortets S4-delar. sms är None på kortet och {"text", "error"} på
    kontakter/<pk>/sms/ (rutan öppen): då prövas kontrollerna och rutan
    visar formuläret eller varför sms inte kan skickas nu."""
    from .inbox_reply import reply_number_text

    access = getattr(request, "flamingo", None)
    read_only = bool(getattr(access, "read_only", False))
    context = {
        "reply_habit": timeline.reply_habit(kontakt),
        "cs_button": bool(kontakt.phone) and not read_only,
        "cs_open": sms is not None,
    }
    if sms is None:
        return context
    actor = actor_for(request)
    company = _company(account)
    suffix = f" /{company}" if company else ""
    ore = threads.part_ore(account)
    text = sms.get("text", "")
    context.update(
        {
            "cs_problem": rules.problem(kontakt) if not read_only else "",
            "cs_read_only": read_only,
            "cs_text": text,
            "cs_to": display_phone(kontakt.phone),
            "cs_error": sms.get("error", ""),
            "cs_staff": actor.staff,
            "cs_staff_text": STAFF_TEXT.format(name=company),
            "cs_company": company,
            "cs_suffix": suffix,
            "cs_ore": ore,
            "cs_number": reply_number_text(),
            "cs_counter": threads.counter_text(threads.clean_text(text) + suffix, ore),
            "cs_demo": account.is_demo,
        }
    )
    return context


def _render(request, account, kontakt, text="", error=""):
    from .contacts import _render_detail

    return _render_detail(request, account, kontakt, sms={"text": text, "error": error})


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def contact_sms(request, account, pk):
    kontakt = owned(Contact, account, pk)
    if request.method != "POST":
        return _render(request, account, kontakt)
    text = str(request.POST.get("text") or "")[:TEXT_MAX]
    actor = actor_for(request)
    if actor.staff and request.POST.get("som_adx") != "1":
        return _render(request, account, kontakt, text, STAFF_REQUIRED_TEXT)
    result = rules.send(kontakt, text, actor=actor, now=timezone.now())
    if not result.ok:
        return _render(request, account, kontakt, text, result.error)
    messages.success(request, result.error or rules.SENT_TEXT)
    return redirect(reverse("flamingo:app_contact", args=[kontakt.pk]) + "#tidslinje")
