"""
Svaren i Inkorgen (README C.2 app_views/inbox.py, G.1 punkt 8, G.2, I.4).

    lead_reply        inkorg/<pk>/svara/ (POST)         app_lead_reply
    lead_unsubscribe  inkorg/<pk>/avregistrera/ (POST)  app_lead_unsubscribe
    thread_context(request, account, lead, text="", error="")
                      det flamingo/app/utskick/_thread.html behöver
                      (flamingo.app_views.inbox.lead_detail tar med det)

Båda vyerna går genom utskick_view (404 när utskick är av, också för byrån
i kundvyn) och owned (förfrågan ska vara kontots, och ha en svarstråd).
Svaret skickas direkt, ett sms från svarsnumret med källan reply
(threads.send_reply); demokontot skickar aldrig. Byrån i kundvyn svarar på
riktigt men måste kryssa i "Jag svarar som ADX åt Exempelrör." först, och
meddelandet sparas med vem som skickade (sent_by, sent_as_staff). Ett nekat
svar visar förfrågan igen med texten kvar i rutan och felet ovanför.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.flamingo.models import Lead

from .. import threads
from ..access import NOT_ENABLED_TEXT, actor_for, is_enabled, owned, utskick_view
from ..models import CHANNEL_SMS, Thread

STAFF_TEXT = "Jag svarar som ADX åt {name}."
STAFF_REQUIRED_TEXT = "Kryssa i att du svarar som ADX åt kunden. Svaret skickades inte."
SENT_TEXT = "Svaret är skickat."
SUPPRESSED_NOTE = "Numret är avregistrerat från sms. Svar kan inte skickas härifrån."


def _thread(account, pk):
    lead = owned(Lead, account, pk)
    thread = (
        Thread.objects.filter(lead=lead, account=account)
        .select_related("account__customer", "contact", "utskick", "lead")
        .first()
    )
    if thread is None:
        raise Http404
    return lead, thread


def _company(account):
    return threads.display_name(account) or account.customer.name


def reply_number_text():
    """Svarsnumret som kunden läser det: "0766 86 00 46"."""
    number = threads.reply_number()
    digits = number.lstrip("+")
    if number.startswith("+46") and len(digits) == 11:
        national = "0" + digits[2:]
        return f"{national[:4]} {national[4:6]} {national[6:8]} {national[8:]}"
    return number


def thread_context(request, account, lead, text="", error=""):
    """Kontexten för svarstråden på förfrågans sida, eller {} när förfrågan
    inte har någon tråd. Kunden (inte byrån) som öppnar tråden har läst den."""
    thread = (
        Thread.objects.filter(lead=lead, account=account)
        .select_related("contact", "utskick")
        .first()
    )
    if thread is None:
        return {}
    actor = actor_for(request)
    if thread.unread and not actor.staff:
        Thread.objects.filter(pk=thread.pk).update(unread=False)
    enabled = is_enabled(account)
    access = getattr(request, "flamingo", None)
    read_only = bool(getattr(access, "read_only", False))
    suppressed = threads.is_suppressed(thread)
    is_sms = thread.channel == CHANNEL_SMS
    company = _company(account)
    suffix = f" /{company}" if company else ""
    ore = threads.part_ore(account) if is_sms else None
    can_reply = enabled and is_sms and not suppressed and not read_only
    return {
        "thread": thread,
        "th_rows": threads.rows(thread),
        "th_enabled": enabled,
        "th_disabled_text": NOT_ENABLED_TEXT,
        "th_is_sms": is_sms,
        "th_can_reply": can_reply,
        "th_can_unsubscribe": enabled and is_sms and not suppressed and not read_only,
        "th_suppressed": suppressed,
        "th_suppressed_note": SUPPRESSED_NOTE,
        "th_demo": account.is_demo,
        "th_staff": actor.staff,
        "th_staff_text": STAFF_TEXT.format(name=company),
        "th_company": company,
        "th_suffix": suffix,
        "th_ore": ore,
        "th_number": reply_number_text(),
        "th_counter": threads.counter_text(threads.clean_text(text) + suffix, ore),
        "th_text": text,
        "th_error": error,
        "th_kontakt_url": (
            reverse("flamingo:app_contact", args=[thread.contact_id])
            if enabled and thread.contact_id
            else ""
        ),
        "th_done": lead.status == Lead.STATUS_NEW and not read_only,
    }


def _again(request, account, lead, text, error):
    """Förfrågans sida igen med texten kvar och felet (inget skickades)."""
    from apps.flamingo.app_views.inbox import render_detail

    return render_detail(request, account, lead, reply_text=text, reply_error=error)


def _back(lead):
    return redirect(reverse("flamingo:app_lead", args=[lead.pk]) + "#svar")


@utskick_view
@require_POST
def lead_reply(request, account, pk):
    """Svara på en svarstråd med sms (README G.2)."""
    lead, thread = _thread(account, pk)
    text = str(request.POST.get("text") or "")[:2000]
    actor = actor_for(request)
    if actor.staff and request.POST.get("staff_ok") != "1":
        return _again(request, account, lead, text, STAFF_REQUIRED_TEXT)
    result = threads.send_reply(thread, text, actor=actor, now=timezone.now())
    if not result.ok:
        return _again(request, account, lead, text, result.error)
    messages.success(request, result.error or SENT_TEXT)
    return _back(lead)


@utskick_view
@require_POST
def lead_unsubscribe(request, account, pk):
    """ "Avregistrera från sms" i en svarstråd (README G.1 punkt 8): spärr
    med orsak reply och samtyckesloggen med den som tryckte."""
    lead, thread = _thread(account, pk)
    if thread.channel != CHANNEL_SMS or not thread.address:
        raise Http404
    company = _company(account)
    if threads.is_suppressed(thread):
        messages.info(request, f"Numret var redan avregistrerat från sms från {company}.")
        return _back(lead)
    threads.unsubscribe(thread, actor=actor_for(request), now=timezone.now())
    messages.success(request, f"Numret är avregistrerat från sms från {company}.")
    return _back(lead)
