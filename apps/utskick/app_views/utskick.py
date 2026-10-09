"""
Utskick i verktyget (README I.1, I.4 till I.10, F.8, D.8, H.1, H.5): listan,
guiden i fem steg, Granska, bekräftelsen, läget, rapporten, mottagarna,
testsändningen och Inställningar för utskick.

    utskick_list         utskick/                       app_utskick_list
    utskick_new          utskick/ny/ (POST)             app_utskick_new
    utskick_step         utskick/<pk>/steg/<steg>/      app_utskick_step
    utskick_count        utskick/<pk>/antal/ (JSON)     app_utskick_count
    utskick_sms_preview  utskick/<pk>/sms/ (JSON)       app_utskick_sms_preview
    utskick_link_check   utskick/<pk>/lankkontroll/     app_utskick_link_check
    utskick_test         utskick/<pk>/test/ (POST)      app_utskick_test
    utskick_confirm      utskick/<pk>/skicka/ (POST)    app_utskick_confirm
    utskick_state        utskick/<pk>/lage/ (POST)      app_utskick_state
    utskick_report       utskick/<pk>/                  app_utskick
    utskick_recipients   utskick/<pk>/mottagare/        app_utskick_recipients
    utskick_save_list    utskick/<pk>/mottagare/lista/  app_utskick_save_list
    utskick_settings     utskick/installningar/         app_utskick_settings

Steg: mottagare, kanal, innehall, tid, granska (STEPS, i ordning). I S2 är
bara kanalen "Bara sms" byggd; e-postens lägen visas inte förrän S3.

Regler som gäller varje vy här:

- Allt hämtas via kontot: ett id ur adressen med access.owned (404), ett id
  ur ett formulär eller en JSON-fråga med access.owned_ids eller
  audience.clean (400 för hela förfrågan).
- Inget skickas i förfrågan utom testsms:et (F.8): bekräftelsen lämnar
  utskicket till sändningsmotorn (sending.state.confirm, sedan ticken).
- Varje val som skickar (bekräfta, skicka nu, fortsätt, test till kunden)
  kräver av byrån i kundvyn kryssrutan "Jag skickar det här som ADX åt
  Exempelrör." och sparas med den som gjorde det (access.actor_for, I.4).
- Ett schemalagt utskick som ändras blir ett utkast igen och behöver en ny
  bekräftelse (sending.state.unconfirm, B.2). Det gäller också en länk som
  läggs till eller tas bort. Ändringen görs med raden låst och läget prövat
  igen under låset (_editing), så att ticken aldrig hinner börja frysa ett
  utskick mitt i en ändring.
- Demokontot skickar aldrig (D12): Granska säger det, testsms nekas.
- Kunden mejlas aldrig härifrån; "Be ADX ..." är ett larm till byrån.
"""

import logging
import re
import secrets
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from apps.flamingo.models import Campaign
from apps.sms import numbers, pricing
from apps.sms.models import UNITS_PER_KR
from apps.sms.pricing import STOCKHOLM

from .. import alerts, audience, composer, keys, limits, links, reports, timing
from .. import contacts as register
from .. import suppression as suppressions
from ..access import actor_for, owned, owned_ids, utskick_view
from ..models import (
    CHANNEL_SMS,
    INFORMATION,
    REKLAM,
    SMS_WINDOW_DEFAULT,
    Contact,
    ContactList,
    ListMembership,
    Recipient,
    Tag,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from ..normalize import display_phone
from ..sending import checks, sms_wrapper, state
from ..templatetags.utskick_tags import procent
from . import render_utskick
from .contacts import clean_name, day_text

logger = logging.getLogger(__name__)

#: Guidens steg i ordning (adressens del och rubriken, README I.8).
STEPS = (
    ("mottagare", "Mottagare"),
    ("kanal", "Kanal"),
    ("innehall", "Innehåll"),
    ("tid", "Tid"),
    ("granska", "Granska"),
)
STEP_KEYS = tuple(key for key, _label in STEPS)

PER_PAGE = 30
RECIPIENTS_PER_PAGE = 50
#: Så många mottagare visas i rapporten; resten på mottagarsidan.
REPORT_ROWS = 25
#: Testsms per konto och svenskt dygn (F.8).
TEST_SENDS_PER_DAY = 10
#: Så många kontakter visas när kunden söker en enstaka mottagare.
SEARCH_LIMIT = 20
#: Kontakterna förhandsvisningen kan bläddra mellan ("byt kontakt").
PREVIEW_CONTACTS = 50
#: Hur långt fram ett utskick får schemaläggas, och minsta marginal.
MAX_AHEAD = timedelta(days=183)
MIN_AHEAD = timedelta(minutes=5)
#: Formuläret i varje steg (id i mallen) och knappen som tar Enter i det:
#: (name, value, text). Granska har inget eget formulär.
STEP_FORMS = {
    "mottagare": ("ut-mottagare", ("action", "sok", "Sök")),
    "kanal": ("ut-kanal", ("nasta", "innehall", "Nästa")),
    "innehall": ("ut-innehall", ("nasta", "innehall", "Spara")),
    "tid": ("ut-tid", ("nasta", "granska", "Nästa")),
}

#: ?visa= i listan: (rubrik, lägen). Avbrutna syns bara under Alla.
LIST_FILTERS = {
    "utkast": ("Utkast", (Utskick.Status.DRAFT,)),
    "schemalagda": ("Schemalagda", (Utskick.Status.SCHEDULED, Utskick.Status.FREEZING)),
    "skickade": (
        "Skickade",
        (Utskick.Status.SENDING, *Utskick.PAUSED_STATES, Utskick.Status.SENT),
    ),
}

#: Pauser som bara byrån släpper (README I.5: "staff resumes").
STAFF_RESUMES = (
    Utskick.PauseReason.COMPLAINTS,
    Utskick.PauseReason.STOPS,
    Utskick.PauseReason.STAFF,
    Utskick.PauseReason.BLOCKED,
    Utskick.PauseReason.ACCOUNT_HEALTH,
    Utskick.PauseReason.EMAIL_DISABLED,
    Utskick.PauseReason.BOUNCES,
    Utskick.PauseReason.ADX_MAIL_CAP,
)
#: Steget "Ändra utskicket" i pausens ruta leder till (state.reopen).
REOPEN_STEPS = {
    Utskick.PauseReason.LATE: "tid",
    Utskick.PauseReason.CONTENT: "innehall",
}
#: Pauser där kunden själv kan trycka Fortsätt (I.5).
CUSTOMER_RESUMES = (
    Utskick.PauseReason.SMS_COST_CAP,
    Utskick.PauseReason.SMS_DISABLED,
    Utskick.PauseReason.PROVIDER,
    Utskick.PauseReason.CUSTOMER,
)

#: Hoppades över, kort i Granskas mening ("12 utan samtycke, 3 veckotaket").
SKIP_SHORT = {
    "no_consent": "utan samtycke",
    "declined": "vill inte ha erbjudanden",
    "pending_doi": "väntar på bekräftelse",
    "suppressed": "avregistrerade",
    "bounced": "studsade",
    "weekly_cap": "veckotaket",
    "no_address": "saknar nummer",
    "invalid_number": "ogiltiga nummer",
    "country": "land som inte är tillåtet",
    "duplicate": "dubbletter",
    "deleted": "borttagna",
    "address_changed": "nytt nummer",
    "reply_collision": "fick nyss sms från en annan ADX-kund",
    "recent": "fick ett utskick nyligen",
    "ses_suppressed": "spärrade hos e-posttjänsten",
    "adx_cap": "taket för ADX-domänen",
}

STAFF_TEXT = "Jag skickar det här som ADX åt {name}."
STAFF_MISSING = "Kryssa i att du skickar som ADX åt {name}."
CHANGED_TEXT = "Utskicket har ändrats sedan du öppnade Granska. Granska det igen."
BLOCKING_TEXT = "Rätta det som är markerat innan utskicket kan skickas."
NOT_EDITABLE = "Utskicket går inte att ändra nu."
UNCONFIRMED = "Utskicket är ett utkast igen. Granska och bekräfta det på nytt."
REOPENED = "Utskicket är ett utkast igen. Ändra det och bekräfta det på nytt i Granska."
SMS_DISABLED_TEXT = "Sms är inte aktiverat för dig. Be ADX slå på det."
NAMES_LOCKED_TEXT = "ADX godkänner avsändarnamn. Be ADX lägga till ett."
KEY_TEXT = "Det gick inte att skicka just nu. ADX har fått ett larm."
ASKED_TEXT = "ADX har fått din fråga."
EMPTY_AUDIENCE = "Välj vem som ska få utskicket: en lista, en tagg eller enstaka kontakter."

#: Utfallen av ett testsms (apps.sms.service-felkoder) i klartext.
OUTCOME_TEXTS = {
    "sms_not_enabled": SMS_DISABLED_TEXT,
    "monthly_cap_reached": "Månadens kostnadstak är nått. Testet skickades inte.",
    "rate_limited": "Det skickas många sms just nu. Försök igen om en minut.",
    "invalid_number": "Numret kan inte ta emot sms.",
    "country_not_allowed": "Numret hör till ett land du inte får skicka till.",
    "sender_not_allowed": "Avsändarnamnet är inte godkänt för dig.",
    "message_too_long": "Texten blir för lång för ett sms.",
    "invalid_request": "Testet gick inte att skicka.",
    "provider_error": "Sms-leverantören tog inte emot sms:et. Försök igen om en stund.",
}

_WEEKDAYS = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")
_MONTHS = ("jan", "feb", "mars", "april", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")
_MONTH_NAMES = (
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
)
NBSP = chr(0xA0)


# ---------------------------------------------------------------------------
# Tal och tider i klartext
# ---------------------------------------------------------------------------


def _group(n):
    return f"{int(n or 0):,}".replace(",", NBSP)


def _kr(units):
    """Tiotusendels krona -> "151 kr" (avrundat uppåt: hellre för högt)."""
    kr = -(-int(units or 0) // UNITS_PER_KR)
    return f"{_group(kr)}{NBSP}kr"


def _local(moment):
    return timezone.localtime(moment, STOCKHOLM)


def _day(moment, now):
    """ "i dag", "i morgon", "i går", "tisdag 14 okt", "14 okt 2027"."""
    local = _local(moment)
    today = _local(now).date()
    delta = (local.date() - today).days
    if delta == 0:
        return "i dag"
    if delta == 1:
        return "i morgon"
    if delta == -1:
        return "i går"
    if local.year == today.year:
        return f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}"
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"


def day_clock(moment, now=None):
    """ "tisdag 14 okt 09.00", "i morgon 09.00"."""
    if not moment:
        return ""
    return f"{_day(moment, now or timezone.now())} {_local(moment):%H.%M}"


def clock_day(moment, now=None):
    """ "09.00 i morgon", "09.00 tisdag 14 okt" (I.5: "Fortsätter 09.00 i morgon")."""
    if not moment:
        return ""
    return f"{_local(moment):%H.%M} {_day(moment, now or timezone.now())}"


def short_day_clock(moment):
    """ "15 okt 08.00" (listans Schemalagt)."""
    local = _local(moment)
    return f"{local.day} {_MONTHS[local.month - 1]} {local:%H.%M}"


def month_name(moment):
    return _MONTH_NAMES[_local(moment).month - 1]


def reply_number_text():
    """ "0766 86 00 46" (README Deviations: det riktiga numret, så här skrivet)."""
    number = composer.reply_number()
    if number.startswith("+46") and len(number) == 12:
        national = "0" + number[3:]
        return f"{national[:4]} {national[4:6]} {national[6:8]} {national[8:]}"
    return number


def skip_text(by_reason):
    """ "12 utan samtycke, 3 veckotaket, 9 avregistrerade"."""
    parts = []
    for reason, n in sorted((by_reason or {}).items(), key=lambda item: (-item[1], item[0])):
        if n:
            parts.append(f"{_group(n)} {SKIP_SHORT.get(reason, 'av andra skäl')}")
    return ", ".join(parts)


def count_text(counted):
    """ "388 får sms. 24 hoppas över: 12 utan samtycke, 3 veckotaket." """
    sms = int(counted.get("sms") or 0)
    skipped = int(counted.get("skipped") or 0)
    text = f"{_group(sms)} får sms."
    if skipped:
        why = skip_text(counted.get("skipped_by_reason"))
        text += f" {_group(skipped)} hoppas över" + (f": {why}." if why else ".")
    return text


def weekly_cap_text(counted):
    n = int((counted.get("skipped_by_reason") or {}).get("weekly_cap") or 0)
    if not n:
        return ""
    return f"{_group(n)} hoppas över på grund av veckotaket."


# ---------------------------------------------------------------------------
# Kontot, sms och utskicket
# ---------------------------------------------------------------------------


def _sms_state(account):
    """Det vyerna behöver veta om kontots sms: kontot (eller None), om det
    är aktiverat, de godkända avsändarnamnen och byråns brytare."""
    sms_account = sms_wrapper.sms_account_for(account)
    enabled = bool(sms_account and sms_account.is_enabled)
    return {
        "account": sms_account,
        "enabled": enabled,
        "senders": list(sms_account.senders) if sms_account else [],
        "ready": checks.sms_ready(),
        "breaker": checks.breaker_active(),
    }


def _editable(utskick):
    return utskick.status in Utskick.EDITABLE


def _reconfirm(utskick):
    """Pausat på ett sätt som kräver en ny bekräftelse (Granska igen)."""
    return utskick.is_paused and utskick.pause_reason in Utskick.RECONFIRM_REASONS


def _confirmable(utskick):
    return utskick.status == Utskick.Status.DRAFT or _reconfirm(utskick)


def _counted(utskick, now=None):
    """audience.count en gång per förfrågan och utskick."""
    cached = getattr(utskick, "_ut_count", None)
    if cached is None:
        cached = audience.count(utskick, now)
        utskick._ut_count = cached
    return cached


def _step_url(utskick, step):
    return reverse("flamingo:app_utskick_step", args=[utskick.pk, step])


def _report_url(utskick):
    return reverse("flamingo:app_utskick", args=[utskick.pk])


def _wizard(utskick, current, locked=False):
    """Stegchipsen: [{"key", "label", "url", "number", "now"}], föregående
    och nästa steg (Steg 3 av 5: Innehåll under 560 px), stegets formulär
    (chipsen sparar det först) och knappen som tar Enter. locked: ett
    pausat utskick som ska bekräftas igen, där inget steg går att ändra."""
    steps = []
    for number, (key, label) in enumerate(STEPS, start=1):
        steps.append(
            {
                "key": key,
                "label": label,
                "url": _step_url(utskick, key),
                "number": number,
                "now": key == current,
            }
        )
    index = STEP_KEYS.index(current)
    form, default = STEP_FORMS.get(current, ("", None))
    return {
        "steps": steps,
        "number": index + 1,
        "total": len(STEPS),
        "label": STEPS[index][1],
        "prev": steps[index - 1] if index > 0 else None,
        "next": steps[index + 1] if index + 1 < len(STEPS) else None,
        "form": "" if locked else form,
        "default": default,
        "locked": locked,
    }


def _after_save(request, utskick, current):
    """Vart en sparad sida går: knappen nasta (ett steg eller "lista"),
    annars nästa steg."""
    wanted = request.POST.get("nasta") or ""
    if wanted == "lista":
        messages.success(request, "Utkastet är sparat.")
        return redirect("flamingo:app_utskick_list")
    if wanted in STEP_KEYS:
        return redirect(_step_url(utskick, wanted))
    index = STEP_KEYS.index(current)
    following = STEP_KEYS[min(index + 1, len(STEP_KEYS) - 1)]
    return redirect(_step_url(utskick, following))


class NotEditable(Exception):
    """Utskicket gick inte längre att ändra när steget skulle sparas (ticken
    hann börja frysa det, eller det pausades). utskick_step svarar med
    NOT_EDITABLE och rapporten."""


@contextmanager
def _editing(utskick):
    """En ändring i guiden: raden låses (select_for_update) och läget prövas
    igen under låset. Ticken flyttar ett schemalagt utskick med en villkorlig
    UPDATE, som väntar på låset och sedan inte längre träffar ett utskick som
    blivit ett utkast. Inget anrop utåt (larm, sms) görs medan låset hålls.
    Ger den låsta raden; NotEditable när utskicket inte går att ändra."""
    with transaction.atomic():
        row = (
            Utskick.objects.select_for_update(of=("self",))
            .select_related("account__customer")
            .get(pk=utskick.pk)
        )
        if row.status not in Utskick.EDITABLE:
            raise NotEditable
        yield row
    utskick.refresh_from_db()


def _changed(request, row):
    """Något i den låsta raden ändras: ett schemalagt utskick blir ett
    utkast och behöver en ny bekräftelse (B.2)."""
    if row.status == Utskick.Status.SCHEDULED:
        if not state.unconfirm(row):
            raise NotEditable
        messages.info(request, UNCONFIRMED)


def _change(request, row, **fields):
    """Spara fälten på den låsta raden (_editing), som ett utkast."""
    _changed(request, row)
    if fields:
        _save(row, **fields)


def _save(utskick, **fields):
    for name, value in fields.items():
        setattr(utskick, name, value)
    utskick.save(update_fields=[*fields, "updated_at"])


def _staff_ok(request, actor):
    """Byrån i kundvyn har kryssat i rutan (I.4); alltid sant för kunden."""
    return not actor.staff or request.POST.get("som_adx") == "1"


def _display_name(request):
    row = getattr(request, "utskick_settings", None)
    return (row.display_name if row else "") or "kunden"


def _base_context(request, account, utskick, step=None):
    actor = actor_for(request)
    name = _display_name(request)
    context = {
        "utskick": utskick,
        "ut_nav": None,
        "is_staff_actor": actor.staff,
        "staff_text": STAFF_TEXT.format(name=name),
        "display_name": name,
        "is_demo": account.is_demo,
    }
    if step:
        context["wizard"] = _wizard(utskick, step, locked=not _editable(utskick))
    return context


# ---------------------------------------------------------------------------
# Listan
# ---------------------------------------------------------------------------


def status_info(utskick, row=None, now=None):
    """(etikett, ton) för listans Status och rapportens rubrik (I.8, I.5)."""
    now = now or timezone.now()
    status = utskick.status
    if status == Utskick.Status.DRAFT:
        return "Utkast", "draft"
    if status == Utskick.Status.SCHEDULED:
        if utskick.scheduled_at:
            return f"Schemalagt {short_day_clock(utskick.scheduled_at)}", "scheduled"
        return "Schemalagt", "scheduled"
    if status == Utskick.Status.FREEZING:
        return "Förbereds", "sending"
    if status == Utskick.Status.SENDING:
        if row:
            return f"Skickas: {_group(row['sent'])} av {_group(row['total'])}", "sending"
        return "Skickas", "sending"
    if utskick.is_paused:
        label = utskick.get_pause_reason_display() if utskick.pause_reason else "Pausat"
        return label, "paused"
    if status == Utskick.Status.SENT:
        when = utskick.finished_at or utskick.started_at or utskick.status_changed_at
        return f"Skickat {day_text(when, now)}", "sent"
    return "Avbrutet", "cancelled"


def _usage_line(account, sms, now):
    """ "Oktober: 2 640 sms-delar · kostnad hittills 1 186 kr av taket 3 000 kr
    (gemensamt med sms-API:t)" (I.8)."""
    if sms["account"] is None:
        return ""
    used = pricing.usage(sms["account"], now)
    month = month_name(now).capitalize()
    return (
        f"{month}: {_group(used['parts'])} sms-delar · kostnad hittills {_kr(used['cost'])} "
        f"av taket {_kr(used['cap'])} (gemensamt med sms-API:t)"
    )


@utskick_view
def utskick_list(request, account):
    now = timezone.now()
    visa = request.GET.get("visa") or ""
    visa = visa if visa in LIST_FILTERS else ""
    rows = Utskick.objects.listed().filter(account=account)
    if visa:
        rows = rows.filter(status__in=LIST_FILTERS[visa][1])
    page = Paginator(rows.order_by("-created_at", "-pk"), PER_PAGE).get_page(
        request.GET.get("sida")
    )
    items = list(page.object_list)
    numbers = reports.list_numbers(items)
    for utskick in items:
        row = numbers.get(utskick.pk)
        recipient_row = None
        if utskick.status == Utskick.Status.SENDING:
            recipient_row = _sending_numbers(utskick)
        utskick.ut_status, utskick.ut_tone = status_info(utskick, recipient_row, now)
        utskick.ut_audience = audience.describe(utskick)
        utskick.ut_numbers = row
        if row is None and utskick.confirm_summary:
            utskick.ut_planned = audience.confirmed_total(
                utskick.confirm_summary, utskick.channel_mode
            )
        else:
            utskick.ut_planned = None
    sms = _sms_state(account)
    context = {
        "rows": items,
        "page": page,
        "visa": visa,
        "filters": [("", "Alla")] + [(key, value[0]) for key, value in LIST_FILTERS.items()],
        "any_utskick": Utskick.objects.listed().filter(account=account).exists(),
        "usage_line": _usage_line(account, sms, now),
    }
    return render_utskick(request, "flamingo/app/utskick/list.html", "utskick", context)


def _sending_numbers(utskick):
    counts = Recipient.objects.filter(utskick=utskick).aggregate(
        total=Count("pk", filter=~_skipped_q()),
        sent=Count("pk", filter=_sent_q()),
    )
    return {"total": counts["total"] or 0, "sent": counts["sent"] or 0}


def _skipped_q():
    return Q(status=Recipient.Status.SKIPPED)


def _sent_q():
    return Q(status__in=(*Recipient.SENT_LIKE, Recipient.Status.FAILED))


@utskick_view
@require_POST
def utskick_new(request, account):
    """Ett nytt utkast (bara sms i S2), sedan guidens första steg."""
    now = timezone.now()
    local = _local(now)
    name = clean_name(request.POST.get("namn"), 120) or (
        f"Utskick {local.day} {_MONTHS[local.month - 1]}"
    )
    user = request.user if request.user.is_authenticated else None
    utskick = Utskick.objects.create(
        account=account,
        name=name,
        purpose=REKLAM,
        channel_mode=Utskick.ChannelMode.SMS_ONLY,
        sms_sender_kind=Utskick.SenderKind.REPLY,
        send_mode=Utskick.SendMode.NOW,
        created_by=user,
    )
    return redirect(_step_url(utskick, "mottagare"))


# ---------------------------------------------------------------------------
# Guiden
# ---------------------------------------------------------------------------


@utskick_view
def utskick_step(request, account, pk, step):
    utskick = owned(Utskick, account, pk)
    if not _editable(utskick) and not (step == "granska" and _reconfirm(utskick)):
        messages.info(request, NOT_EDITABLE)
        return redirect(_report_url(utskick))
    try:
        return _STEP_VIEWS[step](request, account, utskick)
    except NotEditable:
        messages.info(request, NOT_EDITABLE)
        return redirect(_report_url(utskick))


# --- Steg 1: Mottagare ------------------------------------------------------


def _audience_choices(account, utskick, search=""):
    chosen = audience.stored(utskick)
    lists = ContactList.objects.filter(account=account).annotate(n=Count("memberships"))
    tags = Tag.objects.filter(account=account).annotate(n=Count("contacts"))
    picked = list(
        Contact.objects.filter(account=account, pk__in=chosen["contacts"]).order_by(
            "first_name", "last_name", "pk"
        )
    )
    found = []
    if search:
        found = [
            kontakt
            for kontakt in register.search(Contact.objects.filter(account=account), search)
            .exclude(pk__in=chosen["contacts"])
            .order_by("first_name", "last_name", "pk")[:SEARCH_LIMIT]
        ]
    return {
        "chosen": chosen,
        "lists": lists.order_by("name"),
        "tags": tags.order_by("name"),
        "kontakter_valda": picked,
        "kontakter_hittade": found,
        "search": search,
        "recent_days": audience.RECENT_DAYS,
    }


def _step_mottagare(request, account, utskick):
    if request.method == "POST":
        name = clean_name(request.POST.get("namn"), 120)
        aud = audience.clean(account, request.POST)
        if not name:
            messages.error(request, "Skriv ett namn på utskicket.")
            name = utskick.name
        with _editing(utskick) as row:
            if aud != audience.stored(row) or name != row.name:
                _change(request, row, name=name, audience=aud)
        if request.POST.get("action") == "sok":
            query = " ".join(str(request.POST.get("sok") or "").split())[:100]
            url = _step_url(utskick, "mottagare")
            return redirect(f"{url}?{urlencode({'sok': query})}#ut-kontakter" if query else url)
        # Nästa kräver ett urval; Spara utkast, Tillbaka och chipsen sparar ändå
        # (Granska blockerar ett tomt urval).
        forward = (request.POST.get("nasta") or "") in ("", "kanal")
        if forward and not (aud["lists"] or aud["tags"] or aud["contacts"]):
            messages.error(request, EMPTY_AUDIENCE)
            return redirect(_step_url(utskick, "mottagare"))
        return _after_save(request, utskick, "mottagare")
    search = " ".join(str(request.GET.get("sok") or "").split())[:100]
    context = _base_context(request, account, utskick, "mottagare")
    context.update(_audience_choices(account, utskick, search))
    counted = _counted(utskick)
    context.update(
        {
            "counted": counted,
            "count_text": count_text(counted),
            "weekly_text": weekly_cap_text(counted),
            "empty": audience.is_empty(utskick),
        }
    )
    return render_utskick(request, "flamingo/app/utskick/step_mottagare.html", "utskick", context)


# --- Steg 2: Kanal ----------------------------------------------------------


def _sender_choice(raw, senders):
    """("reply", "") eller ("name", namn) ur formulärets avsandare."""
    raw = str(raw or "")
    if raw.startswith("name:"):
        name = raw[5:]
        if name in senders:
            return Utskick.SenderKind.NAME, name
        return None
    if raw == "reply":
        return Utskick.SenderKind.REPLY, ""
    return None


def _step_kanal(request, account, utskick):
    sms = _sms_state(account)
    errors = {}
    if request.method == "POST":
        purpose = request.POST.get("syfte") or ""
        reason = request.POST.get("info_reason") or ""
        reason_text = clean_name(request.POST.get("info_reason_text"), 200)
        sender = _sender_choice(request.POST.get("avsandare"), sms["senders"])
        if purpose not in (REKLAM, INFORMATION):
            errors["syfte"] = "Välj om utskicket är reklam eller information."
        if purpose == INFORMATION:
            if reason not in Utskick.InfoReason.values:
                errors["info_reason"] = checks.INFO_REASON_TEXT
            elif reason == Utskick.InfoReason.ANNAT and not reason_text:
                errors["info_reason_text"] = checks.INFO_OTHER_TEXT
        else:
            reason, reason_text = "", ""
        if sender is None:
            errors["avsandare"] = "Välj en avsändare."
        if not errors:
            fields = {
                "purpose": purpose,
                "info_reason": reason,
                "info_reason_text": reason_text if reason == Utskick.InfoReason.ANNAT else "",
                "channel_mode": Utskick.ChannelMode.SMS_ONLY,
                "sms_sender_kind": sender[0],
                "sms_sender_name": sender[1],
            }
            with _editing(utskick) as row:
                if any(getattr(row, k) != v for k, v in fields.items()):
                    _change(request, row, **fields)
            return _after_save(request, utskick, "kanal")
    context = _base_context(request, account, utskick, "kanal")
    counted = _counted(utskick)
    sms_only = (counted.get("modes") or {}).get(Utskick.ChannelMode.SMS_ONLY) or counted
    context.update(
        {
            "errors": errors,
            "sms": sms,
            "sms_only": sms_only,
            "reasons": Utskick.InfoReason.choices,
            "reply_number": reply_number_text(),
            "names_locked_text": NAMES_LOCKED_TEXT,
            "sms_disabled_text": SMS_DISABLED_TEXT,
            "sms_off_text": checks.SMS_OFF_TEXT,
            "form": {
                "syfte": request.POST.get("syfte") if errors else utskick.purpose,
                "info_reason": request.POST.get("info_reason") if errors else utskick.info_reason,
                "info_reason_text": request.POST.get("info_reason_text")
                if errors
                else utskick.info_reason_text,
                "avsandare": request.POST.get("avsandare")
                if errors
                else (
                    f"name:{utskick.sms_sender_name}"
                    if utskick.sms_sender_kind == Utskick.SenderKind.NAME
                    else "reply"
                ),
            },
        }
    )
    return render_utskick(request, "flamingo/app/utskick/step_kanal.html", "utskick", context)


# --- Steg 3: Innehåll -------------------------------------------------------


def _preview_contacts(utskick):
    return list(audience.contacts(utskick).order_by("pk")[:PREVIEW_CONTACTS])


def _preview_contact(request, account, utskick, candidates):
    """Kontakten förhandsvisningen visar: ?kontakt= (kontots egen, annars
    400), annars den första i urvalet."""
    raw = request.POST.get("kontakt") or request.GET.get("kontakt")
    if raw:
        pk = owned_ids(Contact, account, [raw])[0]
        return Contact.objects.get(pk=pk, account=account)
    return candidates[0] if candidates else None


def _next_contact(candidates, current):
    if not candidates or current is None:
        return None
    ids = [c.pk for c in candidates]
    if current.pk not in ids:
        return candidates[0]
    following = candidates[(ids.index(current.pk) + 1) % len(candidates)]
    return following if following.pk != current.pk else None


def _lp_campaigns(account):
    """Flamingo-sidorna en länk kan gå till: kontots kampanjer med en
    publicerad sida (live eller pausad i Google)."""
    return Campaign.objects.filter(
        account=account, status__in=(Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED)
    ).order_by("name")


def _link_rows(account, utskick):
    """Utskickets länkar med var de går och värdens läge (E.8)."""
    rows = []
    for link in TrackedLink.objects.filter(utskick=utskick).select_related("campaign"):
        host = _host_of(link.destination)
        status = "allowed"
        if link.kind == TrackedLink.Kind.EXTERNAL and host:
            status = links.host_status(account, host)
        token = "{" + composer.LINK_PREFIX + link.key + "}"
        rows.append({"link": link, "host": host, "status": status, "token": token})
    return rows


def _host_of(url):
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _link_key(raw):
    """Platshållarens nyckel: gemener, siffror och bindestreck."""
    text = unicodedata.normalize("NFKD", str(raw or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40].strip("-")
    return text


def _add_link(request, account, utskick):
    """Lägg till en länk (E.3, E.8) på den låsta raden. Returnerar (nyckeln,
    länken), eller (None, None) med ett meddelande. En ny värd begärs av
    den som anropar efter låset (links.request_if_new: larmet är ett mejl)."""
    kind = request.POST.get("lank_typ") or "lp"
    if str(request.POST.get("lank_adress") or "").strip() and not request.POST.get("lank_kampanj"):
        kind = "extern"
    key = _link_key(request.POST.get("lank_nyckel") or request.POST.get("lank_etikett"))
    label = clean_name(request.POST.get("lank_etikett"), 120)
    if not key:
        messages.error(request, "Skriv ett namn på länken, till exempel boka.")
        return None, None
    if TrackedLink.objects.filter(utskick=utskick, key=key).exists():
        messages.error(request, f"Det finns redan en länk som heter {key}.")
        return None, None
    try:
        if kind == "lp":
            raw = request.POST.get("lank_kampanj")
            if not raw:
                messages.error(request, "Välj vilken Flamingo-sida länken ska gå till.")
                return None, None
            pk = owned_ids(Campaign, account, [raw])[0]
            campaign = Campaign.objects.get(pk=pk, account=account)
            link = links.add_link(utskick, key=key, campaign=campaign, label=label or campaign.name)
        else:
            raw = str(request.POST.get("lank_adress") or "").strip()
            if not raw:
                messages.error(request, "Skriv adressen länken ska gå till.")
                return None, None
            # add_link prövar adressen (E.8); en ny värd blir en väntande
            # AllowedHost med byråns larm efter låset (_step_innehall), och
            # Granska blockerar tills byrån godkänt den.
            link = links.add_link(utskick, key=key, destination=raw, label=label, request_new=False)
            host = _host_of(link.destination)
            if host and links.host_status(account, host) in ("pending", "new"):
                messages.info(
                    request, "Väntar på ADX: länkar till nya webbplatser godkänns av ADX."
                )
    except links.LinkRefused as exc:
        messages.error(request, str(exc))
        return None, None
    return key, link


def _apply_template(request, utskick, key):
    template = composer.template_for(key)
    if template is None:
        return None
    fields = {"sms_body": composer.template_body(key, _display_name(request))}
    if template["purpose"] == INFORMATION:
        fields.update(purpose=INFORMATION, info_reason=template["info_reason"])
    elif utskick.purpose == INFORMATION:
        fields.update(purpose=REKLAM, info_reason="", info_reason_text="")
    if template["reply"]:
        fields.update(sms_sender_kind=Utskick.SenderKind.REPLY, sms_sender_name="")
    return fields


def _fallbacks_from(request, body):
    """Utskickets reservtexter för värdena texten använder ("Om förnamn saknas")."""
    found = composer.placeholders(body)
    out = {}
    for tag in found.tags:
        value = clean_name(request.POST.get(f"reserv_{tag}"), composer.VALUE_MAX)
        if value:
            out[tag] = value
    return out


def _insert_tags(account):
    """Knapparna som infogar en platshållare: (platshållare, rubrik)."""
    tags = [
        ("{förnamn}", "Förnamn"),
        ("{efternamn}", "Efternamn"),
        ("{namn}", "Namn"),
        ("{företag}", "Företag"),
    ]
    for definition in composer.field_defs(account).values():
        tags.append(("{" + composer.FIELD_PREFIX + definition.key + "}", definition.label))
    tags.append(("{" + composer.UNSUBSCRIBE + "}", "Avregistrering"))
    return tags


def _content_action(post):
    """Vilken knapp i Innehåll som trycktes. Knapparna för mallar, för att
    ta bort en länk och för "byt kontakt" bär sitt värde i namnet (mall=,
    ta_bort_lank=, byt_kontakt=)."""
    if "ta_bort_lank" in post:
        return "ta_bort_lank"
    if "mall" in post:
        return "mall"
    if "byt_kontakt" in post:
        return "byt"
    if "till" in post:
        return "test"
    action = post.get("action") or "save"
    return action if action in ("save", "fix", "lank", "test") else "save"


def _step_innehall(request, account, utskick):
    if request.method == "POST":
        post = request.POST
        action = _content_action(post)
        anchor = ""
        if action == "mall" and composer.template_for(post.get("mall")) is None:
            return HttpResponseBadRequest("Okänd mall.", content_type="text/plain")
        if action == "ta_bort_lank":
            link_pk = owned_ids(TrackedLink, account, [post.get("ta_bort_lank")])[0]
        if action == "byt":
            kontakt_pk = owned_ids(Contact, account, [post.get("byt_kontakt")])[0]
        new_link = None
        with _editing(utskick) as row:
            body = str(post.get("sms_body", row.sms_body) or "").replace("\r\n", "\n")
            fields = {"sms_body": body[: composer.MAX_BODY * 2]}
            if "sms_body" in post:
                fields["merge_fallbacks"] = _fallbacks_from(request, body)
            links_changed = False
            if action == "fix":
                fields["sms_body"] = composer.gsm_fix(fields["sms_body"])
                messages.success(request, "Tecknen är utbytta.")
            elif action == "mall":
                fields.update(_apply_template(request, row, post.get("mall")))
                fields["merge_fallbacks"] = {}
                messages.success(request, "Mallen är inlagd. Ändra texten så att den passar dig.")
            elif action == "lank":
                key, new_link = _add_link(request, account, row)
                anchor = "#ut-lankar"
                if key:
                    links_changed = True
                    token = "{" + composer.LINK_PREFIX + key + "}"
                    if token not in fields["sms_body"]:
                        fields["sms_body"] = (fields["sms_body"].rstrip() + " " + token).strip()
                    messages.success(request, f"Länken {token} är tillagd i texten.")
            elif action == "ta_bort_lank":
                deleted, _ = TrackedLink.objects.filter(pk=link_pk, utskick=row).delete()
                links_changed = bool(deleted)
                anchor = "#ut-lankar"
            changed = {k: v for k, v in fields.items() if getattr(row, k) != v}
            # En länk som läggs till eller tas bort är en ändring (B.2), också
            # när texten står kvar: målet kan vara ett annat.
            if changed or links_changed:
                _change(request, row, **changed)
        if new_link is not None and new_link.kind == TrackedLink.Kind.EXTERNAL:
            user = request.user if request.user.is_authenticated else None
            links.request_if_new(account, new_link.destination, user or utskick.created_by)
        if action == "test":
            # Texten sparas först: testet skickar det kunden ser.
            _send_test(request, account, utskick)
            return redirect(_step_url(utskick, "innehall") + "#ut-test")
        if action == "byt":
            return redirect(
                _step_url(utskick, "innehall") + "?" + urlencode({"kontakt": kontakt_pk})
            )
        if action == "save":
            return _after_save(request, utskick, "innehall")
        if post.get("tillbaka") == "granska":
            return redirect(_step_url(utskick, "granska"))
        return redirect(_step_url(utskick, "innehall") + anchor)
    now = timezone.now()
    sms = _sms_state(account)
    candidates = _preview_contacts(utskick)
    kontakt = _preview_contact(request, account, utskick, candidates)
    shown = composer.preview(utskick, contact=kontakt, sms_account=sms["account"])
    found = composer.placeholders(utskick.sms_body)
    context = _base_context(request, account, utskick, "innehall")
    context.update(
        {
            "sms": sms,
            "preview": shown,
            "errors": composer.validate(account, utskick.sms_body, utskick),
            "non_gsm": shown["non_gsm"],
            "non_gsm_text": composer.non_gsm_text(shown["non_gsm"]),
            "kontakt": kontakt,
            "next_kontakt": _next_contact(candidates, kontakt),
            "insert_tags": _insert_tags(account),
            "used_tags": [
                {
                    "tag": tag,
                    "label": tag.replace(composer.FIELD_PREFIX, "fältet "),
                    "value": (utskick.merge_fallbacks or {}).get(tag, ""),
                }
                for tag in found.tags
            ],
            "link_rows": _link_rows(account, utskick),
            "campaigns": _lp_campaigns(account),
            "templates": [
                {
                    "key": t["key"],
                    "label": t["label"],
                    "text": composer.template_body(t["key"], _display_name(request)),
                    "information": t["purpose"] == INFORMATION,
                }
                for t in composer.TEMPLATES
            ],
            "total_text": _kr(
                int(_counted(utskick, now).get("sms") or 0)
                * max(shown["parts"], 1)
                * composer.part_units(sms["account"])
            ),
            "sender_identified": checks.sender_identified(utskick),
            "too_long": shown["longest_parts"] > composer.MAX_PARTS,
            "max_parts": composer.MAX_PARTS,
            "planned": int(_counted(utskick, now).get("sms") or 0),
            "test": _test_targets(request, account),
            "opt_out": composer.opt_out_line(
                Utskick.SenderKind.REPLY
                if composer.is_reply_sender(shown["sender"])
                else Utskick.SenderKind.NAME
            ),
            "reply_sender": composer.is_reply_sender(shown["sender"]),
            "sender_text": _sender_text(shown["sender"]),
            "sample_link": links.sms_link(composer.SAMPLE_CODE),
        }
    )
    return render_utskick(request, "flamingo/app/utskick/step_innehall.html", "utskick", context)


# --- Steg 4: Tid ------------------------------------------------------------


def _parse_when(day_raw, clock_raw):
    try:
        day = date.fromisoformat(str(day_raw or "").strip())
        clock = time.fromisoformat(str(clock_raw or "").strip())
    except ValueError:
        return None
    return datetime.combine(day, clock.replace(second=0, microsecond=0), tzinfo=STOCKHOLM)


def _step_tid(request, account, utskick):
    now = timezone.now()
    row = request.utskick_settings
    errors = {}
    if request.method == "POST":
        mode = request.POST.get("nar") or ""
        scheduled = None
        if mode == Utskick.SendMode.AT:
            scheduled = _parse_when(request.POST.get("datum"), request.POST.get("klockan"))
            if scheduled is None:
                errors["datum"] = "Välj dag och klockslag."
            elif scheduled < now:
                errors["datum"] = "Välj en tid som inte har passerat."
            elif scheduled < now + MIN_AHEAD:
                errors["datum"] = "Välj en tid minst 5 minuter fram."
            elif scheduled > now + MAX_AHEAD:
                errors["datum"] = "Välj en tid inom ett halvår."
        elif mode != Utskick.SendMode.NOW:
            errors["nar"] = "Välj när utskicket ska gå i väg."
        if not errors:
            fields = {"send_mode": mode, "scheduled_at": scheduled}
            with _editing(utskick) as row:
                if any(getattr(row, k) != v for k, v in fields.items()):
                    _change(request, row, **fields)
            return _after_save(request, utskick, "tid")
    context = _base_context(request, account, utskick, "tid")
    when = utskick.scheduled_at if utskick.send_mode == Utskick.SendMode.AT else None
    local = _local(when) if when else None
    context.update(
        {
            "errors": errors,
            "form": {
                "nar": request.POST.get("nar") if errors else utskick.send_mode,
                "datum": request.POST.get("datum")
                if errors
                else (local.date().isoformat() if local else ""),
                "klockan": request.POST.get("klockan")
                if errors
                else (f"{local:%H:%M}" if local else ""),
            },
            "today": _local(now).date().isoformat(),
            "window": _window_info(row, when or now, now),
            "window_rule": window_rule_text(row),
            "weekly": _weekly_info(request, utskick, now),
        }
    )
    return render_utskick(request, "flamingo/app/utskick/step_tid.html", "utskick", context)


def _window_info(row, when, now):
    """Granskas och Tids rad om tidsfönstret (I.6, I.8)."""
    local_day = _local(when).date()
    span = timing.window_text(row, local_day)
    if timing.sms_window_open(row, when):
        return {"open": True, "text": f"Inom tidsfönstret {span}."}
    start = timing.next_window_start(row, when)
    return {
        "open": False,
        "text": f"Utanför tidsfönstret: sms:en går i väg {clock_day(start, now)}.",
    }


def window_rule_text(row):
    """ "Vardagar 09.00 till 20.00, helger och helgdagar 10.00 till 18.00." """
    window = row.sms_window if isinstance(row.sms_window, dict) else {}
    parts = []
    for key, label in (("weekday", "Vardagar"), ("weekend", "helger och helgdagar")):
        try:
            start, end = (int(v) for v in window.get(key) or SMS_WINDOW_DEFAULT[key])
        except (TypeError, ValueError):
            start, end = SMS_WINDOW_DEFAULT[key]
        parts.append(f"{label} {start:02d}.00 till {end:02d}.00")
    return ", ".join(parts) + "."


def _weekly_info(request, utskick, now):
    row = request.utskick_settings
    if utskick.purpose == INFORMATION:
        return "Information räknas inte mot veckotaket."
    cap = int(row.weekly_cap_sms or 0)
    text = f"Högst {cap} reklam-sms per kontakt och vecka."
    extra = weekly_cap_text(_counted(utskick, now))
    return f"{text} {extra}" if extra else text


# --- Steg 5: Granska --------------------------------------------------------


def _item(level, text, **extra):
    return {"level": level, "text": text, **extra}


def _sends_now(utskick, now):
    """Skickas utskicket när det bekräftas? Skicka nu, och ett pausat som
    bekräftas igen när tiden redan har passerat (sent, urvalet växte vid
    utskickstiden): då går det i väg nu i stället för att fastna på tiden."""
    if utskick.send_mode == Utskick.SendMode.NOW:
        return True
    if _reconfirm(utskick):
        return not utskick.scheduled_at or utskick.scheduled_at < now + MIN_AHEAD
    return False


def review(request, account, utskick, now=None):
    """Granskas kontroller (I.6) och siffrorna som sparas i confirm_summary.

    items: [{"level": "info" | "ok" | "warn" | "block", "text", ...}] i
    tabellens ordning; blocking när något blockerar."""
    now = now or timezone.now()
    row = request.utskick_settings
    sms = _sms_state(account)
    counted = _counted(utskick, now)
    shown = composer.preview(utskick, sms_account=sms["account"])
    send_now = _sends_now(utskick, now)
    when = now if send_now else utskick.scheduled_at
    items = []
    n_sms = int(counted.get("sms") or 0)
    skipped = int(counted.get("skipped") or 0)

    # Mottagarna
    if audience.is_empty(utskick):
        items.append(
            _item("block", "Välj vem som ska få utskicket under Mottagare.", step="mottagare")
        )
    elif not n_sms:
        items.append(_item("block", "Ingen av kontakterna kan få sms:et.", step="mottagare"))
    else:
        items.append(_item("info", count_text(counted), link="mottagare"))
    if utskick.purpose == REKLAM:
        items.append(_item("ok", "Alla mottagare har samtycke eller är befintliga kunder."))
    else:
        reason = utskick.get_info_reason_display() if utskick.info_reason else ""
        if utskick.info_reason == Utskick.InfoReason.ANNAT and utskick.info_reason_text:
            reason = utskick.info_reason_text
        lead = f"Information: {reason.lower()}. " if reason else "Information. "
        items.append(_item("ok", lead + "Skickas utan samtycke, men aldrig till avregistrerade."))

    # Texten
    reply = composer.is_reply_sender(shown["sender"])
    body_errors = composer.validate(account, utskick.sms_body, utskick)
    for error in body_errors:
        items.append(_item("block", error, step="innehall"))
    if reply:
        if composer.STOP_RE.search(utskick.sms_body or "") or "{avregistrering}" in (
            utskick.sms_body or ""
        ):
            items.append(_item("ok", "Sms:et säger hur mottagaren svarar STOPP."))
        else:
            items.append(_item("ok", "Svara STOPP läggs till sist i sms:et."))
    else:
        items.append(_item("ok", "Avregistreringslänk läggs till."))
    if not checks.sender_identified(utskick):
        items.append(
            _item("block", checks.SENDER_TEXT.format(name=_display_name(request)), step="innehall")
        )
    if shown["longest_parts"] > composer.MAX_PARTS:
        items.append(
            _item(
                "block",
                f"Texten blir längre än {composer.MAX_PARTS} sms-delar för några mottagare. "
                "Korta texten.",
                step="innehall",
            )
        )
    if shown["non_gsm"]:
        items.append(
            _item(
                "warn",
                "Texten innehåller tecken som gör sms:et dyrare: "
                + composer.non_gsm_text(shown["non_gsm"])
                + ".",
                fix=True,
            )
        )
    if shown["parts"] > 1:
        items.append(_item("info", f"Sms:et blir {shown['parts']} delar per mottagare."))
    if shown["longest_count"] and shown["longest_parts"] <= composer.MAX_PARTS:
        items.append(
            _item(
                "warn",
                f"{_group(shown['longest_count'])} mottagare får {shown['longest_parts']} "
                "sms-delar (långa namn).",
            )
        )

    # Länkarna
    link_rows = _link_rows(account, utskick)
    if link_rows:
        items.append(
            _item(
                "warn",
                f"{_group(len(link_rows))} "
                + (
                    "länk. Kontrollera att den svarar."
                    if len(link_rows) == 1
                    else "länkar. Kontrollera att de svarar."
                ),
                linkcheck=True,
            )
        )
    for problem in links.link_problems(utskick):
        items.append(_item("block", problem, step="innehall"))
    for problem in checks.information_problems(utskick):
        items.append(_item("block", problem, step="kanal"))

    # Tiden och fönstret
    if not send_now and not utskick.scheduled_at:
        items.append(_item("block", "Välj när utskicket ska gå i väg under Tid.", step="tid"))
    elif not send_now and utskick.scheduled_at < now:
        items.append(_item("block", "Tiden har passerat. Välj en ny tid under Tid.", step="tid"))
    else:
        window = _window_info(row, when, now)
        items.append(_item("ok" if window["open"] else "warn", window["text"]))

    # Kostnaden och taket
    total_parts = n_sms * max(shown["parts"], 1) + shown["longest_count"] * max(
        shown["longest_parts"] - shown["parts"], 0
    )
    cost_units = total_parts * composer.part_units(sms["account"])
    remaining = None
    if sms["account"] is not None and not account.is_demo:
        used = pricing.usage(sms["account"], now)
        remaining = used["remaining"]
        if n_sms and cost_units > remaining:
            text = (
                f"Ryms inte i taket: {_group(n_sms)} sms kostar cirka {_kr(cost_units)} och "
                f"{_kr(remaining)} är kvar av taket {_kr(used['cap'])}."
            )
            items.append(_item("block" if send_now else "warn", text))
        elif n_sms:
            items.append(_item("ok", f"{_kr(cost_units)} av {_kr(remaining)} kvar."))

    # Kollisionen på svarsnumret
    if reply and n_sms:
        collided = checks.collision_count(utskick, now)
        if collided:
            name_sender = sms["senders"][0] if sms["senders"] else ""
            if name_sender:
                text = (
                    "Några mottagare fick nyss sms från en annan ADX-kund via svarsnumret. "
                    f"De får sms:et från {name_sender} och kan inte svara."
                )
            else:
                text = (
                    "Några mottagare fick nyss sms från en annan ADX-kund via svarsnumret. "
                    "De hoppas över."
                )
            items.append(_item("warn", text))

    # Kanalen och kontot
    if not account.is_demo:
        if not sms["enabled"]:
            items.append(_item("block", SMS_DISABLED_TEXT))
        elif not sms["ready"]:
            items.append(_item("block", checks.SMS_OFF_TEXT))
        if row.sending_blocked:
            items.append(_item("block", checks.BLOCKED_TEXT))
    else:
        items.append(_item("info", "Demokontot skickar aldrig. Utskicket visas som skickat."))

    summary = {
        "sms": n_sms,
        "email": 0,
        "skipped": skipped,
        "skipped_by_reason": dict(counted.get("skipped_by_reason") or {}),
        "total": int(counted.get("total") or 0),
        "parts": shown["parts"],
        "longest_parts": shown["longest_parts"],
        "longest_count": shown["longest_count"],
        "cost_units": int(cost_units),
        "remaining_units": remaining,
        "send_now": send_now,
        "scheduled_at": utskick.scheduled_at.isoformat() if utskick.scheduled_at else None,
        "purpose": utskick.purpose,
        "sender": shown["sender"],
    }
    return {
        "items": items,
        "blocking": any(item["level"] == "block" for item in items),
        "summary": summary,
        "preview": shown,
        "cost_text": _kr(cost_units),
        "send_now": send_now,
        "when_text": day_clock(utskick.scheduled_at, now) if utskick.scheduled_at else "",
        "n_sms": n_sms,
        "n_links": len(link_rows),
    }


def _step_granska(request, account, utskick):
    if request.method == "POST":
        # Granska bekräftas på app_utskick_confirm; "Spara utkast" hit.
        return redirect("flamingo:app_utskick_list")
    now = timezone.now()
    checked = review(request, account, utskick, now)
    nonce = state.issue_nonce(utskick)
    context = _base_context(request, account, utskick, "granska")
    context.update(
        {
            "review": checked,
            "nonce": nonce,
            "reconfirm": _reconfirm(utskick),
            "dialog_text": _dialog_text(checked, account.is_demo),
            "sender_text": _sender_text(checked["preview"]["sender"]),
            "report_url": _report_url(utskick),
        }
    )
    return render_utskick(request, "flamingo/app/utskick/step_review.html", "utskick", context)


def _sender_text(sender):
    """Avsändaren som mottagaren ser den: "0766 86 00 46" eller namnet."""
    return reply_number_text() if composer.is_reply_sender(sender) else sender


def _dialog_text(checked, demo=False):
    """ "388 sms skickas nu. Kostnad cirka 151 kr." (I.4). Demokontot
    skickar aldrig, så dialogen säger det i stället för en kostnad."""
    n = checked["n_sms"]
    if demo:
        return f"Demokontot skickar aldrig. {_group(n)} sms visas som skickade."
    return f"{_group(n)} sms skickas nu. Kostnad cirka {checked['cost_text']}."


_STEP_VIEWS = {
    "mottagare": _step_mottagare,
    "kanal": _step_kanal,
    "innehall": _step_innehall,
    "tid": _step_tid,
    "granska": _step_granska,
}


# ---------------------------------------------------------------------------
# JSON: antal, förhandsvisning och länkkontroll
# ---------------------------------------------------------------------------

_AUDIENCE_KEYS = ("lists", "tags", "contacts", "exclude_lists", "exclude_tags", "exclude_recent")


@utskick_view
def utskick_count(request, account, pk):
    """Antal mottagare (I.8). Med urvalets fält i adressen räknas det
    osparade urvalet (guiden räknar medan kunden kryssar); varje id prövas
    mot kontot (400 för ett främmande)."""
    utskick = owned(Utskick, account, pk)
    if request.GET.get("urval") == "1" or any(k in request.GET for k in _AUDIENCE_KEYS):
        utskick.audience = audience.clean(account, request.GET)
    purpose = request.GET.get("syfte")
    if purpose in (REKLAM, INFORMATION):
        utskick.purpose = purpose
    counted = audience.count(utskick)
    data = dict(counted)
    data["text"] = count_text(counted)
    data["weekly_text"] = weekly_cap_text(counted)
    data["sms_text"] = _group(counted.get("sms") or 0)
    data["total_text"] = _group(counted.get("total") or 0)
    return JsonResponse(data)


@utskick_view
def utskick_sms_preview(request, account, pk):
    """Förhandsvisningen och räknaren (I.8): GET för den sparade texten,
    POST med sms_body för den osparade. ?kontakt= väljer kontakt (kontots)."""
    utskick = owned(Utskick, account, pk)
    body = None
    if request.method == "POST":
        body = str(request.POST.get("sms_body") or "").replace("\r\n", "\n")[
            : composer.MAX_BODY * 2
        ]
    sms = _sms_state(account)
    candidates = _preview_contacts(utskick)
    kontakt = _preview_contact(request, account, utskick, candidates)
    shown = composer.preview(utskick, contact=kontakt, body=body, sms_account=sms["account"])
    errors = composer.validate(account, utskick.sms_body if body is None else body, utskick)
    planned = int(_counted(utskick).get("sms") or 0)
    identified = checks.sender_identified(utskick, body)
    too_long = shown["longest_parts"] > composer.MAX_PARTS
    longest_text = ""
    if shown["longest_count"]:
        who = "person" if shown["longest_count"] == 1 else "personer"
        longest_text = (
            f"Längsta namnet ger {shown['longest_parts']} delar för "
            f"{_group(shown['longest_count'])} {who}."
        )
    notes = [{"level": "error", "text": error} for error in errors]
    # Tecknen som gör sms:et dyrare visas i rutan med Byt automatiskt
    # (step_innehall.html, .fl-ut-gsm), som skriptet visar och fyller.
    if not identified:
        notes.append(
            {"level": "error", "text": checks.SENDER_TEXT.format(name=_display_name(request))}
        )
    if too_long:
        notes.append(
            {
                "level": "error",
                "text": f"Texten blir längre än {composer.MAX_PARTS} sms-delar för några "
                "mottagare. Korta texten.",
            }
        )
    elif longest_text:
        notes.append({"level": "warn", "text": longest_text})
    data = dict(shown)
    data.update(
        {
            "errors": errors,
            "notes": notes,
            "too_long": too_long,
            "longest_text": longest_text,
            "total_text": _kr(
                planned * max(shown["parts"], 1) * composer.part_units(sms["account"])
            )
            if planned
            else "",
            "identified": identified,
            "kontakt": kontakt.display_name if kontakt else "",
            "non_gsm_text": composer.non_gsm_text(shown["non_gsm"]),
        }
    )
    return JsonResponse(data)


def _wants_json(request):
    return (
        "application/json" in request.headers.get("Accept", "")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


@utskick_view
@require_POST
def utskick_link_check(request, account, pk):
    """Svarar länkarna (F.5): bara "Svarar" eller "Svarar inte", aldrig
    sidans innehåll eller felet. Gränsen på körningar per timme bor i
    links.check_destinations."""
    utskick = owned(Utskick, account, pk)
    rows = list(TrackedLink.objects.filter(utskick=utskick).order_by("pk"))
    destinations = []
    for link in rows:
        if link.destination and link.destination not in destinations:
            destinations.append(link.destination)
    results = links.check_destinations(account, destinations) if destinations else {}
    # None: inte kontrollerad (gränsen per timme eller tidsbudgeten, links.py).
    answers = [
        {
            "key": link.key,
            "label": link.label or link.key,
            "ok": results.get(link.destination),
        }
        for link in rows
    ]
    ok = sum(1 for a in answers if a["ok"] is True)
    unchecked = sum(1 for a in answers if a["ok"] is None)
    if not answers:
        text = "Utskicket har inga länkar."
    elif unchecked:
        text = "Länkarna kunde inte kontrolleras just nu. Försök igen om en stund."
    else:
        text = f"{ok} av {len(answers)} länkar svarar."
    if _wants_json(request):
        return JsonResponse({"ok": ok, "total": len(answers), "text": text, "links": answers})
    (messages.success if answers and ok == len(answers) else messages.warning)(request, text)
    return redirect(_step_url(utskick, "granska"))


# ---------------------------------------------------------------------------
# Testsms (F.8)
# ---------------------------------------------------------------------------


def _test_targets(request, account):
    """Vart ett testsms kan gå: kundens eget nummer för sms om förfrågningar
    (FlamingoAccount.notify_phone), och för byrån i kundvyn ett nummer
    byrån skriver (sitt eget). Test till kunden kräver kryssrutan (I.4)."""
    actor = actor_for(request)
    owner = ""
    try:
        owner = numbers.parse(account.notify_phone).e164 if account.notify_phone else ""
    except numbers.InvalidNumber:
        owner = ""
    return {
        "owner": owner,
        "owner_text": register_display(owner),
        "staff": actor.staff,
        "demo": account.is_demo,
    }


def register_display(e164):
    return display_phone(e164) if e164 else ""


def _test_address(request, account, actor):
    """Numret testet går till, eller None med ett meddelande. "agare" är
    kundens nummer för förfrågningar; "eget" är byråns eget, skrivet av byrån."""
    target = request.POST.get("till") or ""
    if target == "agare":
        address = _test_targets(request, account)["owner"]
        if not address:
            messages.error(
                request, "Lägg in ditt mobilnummer under Inställningar för att få testet."
            )
            return None
        if actor.staff and not _staff_ok(request, actor):
            messages.error(request, STAFF_MISSING.format(name=_display_name(request)))
            return None
        return address
    if target == "eget" and actor.staff:
        try:
            return numbers.parse(request.POST.get("nummer")).e164
        except numbers.InvalidNumber as exc:
            messages.error(request, str(exc))
            return None
    messages.error(request, "Välj vart testet ska gå.")
    return None


def _send_test(request, account, utskick):
    """Ett testsms med utskickets sparade text (F.8), skickat nu i
    förfrågan. Samma kontroller som en riktig sändning där de gäller
    (brytarna, nödbromsen, texten, företagsnamnet, länkarna som ADX inte
    godkänt, reglerna för information, spärrlistan), högst
    TEST_SENDS_PER_DAY per konto och dygn, aldrig från demot, fakturerat som
    source test. Varje försök loggas med användaren och om det var byrån
    (I.4). Utfallet blir ett meddelande; True när det skickades."""
    now = timezone.now()
    actor = actor_for(request)
    address = _test_address(request, account, actor)
    if address is None:
        return False
    if account.is_demo:
        messages.error(request, sms_wrapper.DEMO_TEXT)
        return False
    sms = _sms_state(account)
    refusal = ""
    if not sms["enabled"]:
        refusal = SMS_DISABLED_TEXT
    elif not sms["ready"]:
        refusal = checks.SMS_OFF_TEXT
    elif sms["breaker"]:
        refusal = checks.BREAKER_TEXT
    else:
        problems = composer.validate(account, utskick.sms_body, utskick)
        if problems:
            refusal = problems[0]
        elif not checks.sender_identified(utskick):
            refusal = checks.SENDER_TEXT.format(name=_display_name(request))
        else:
            # Samma regler som Granska: en länk som ADX inte godkänt, eller
            # information som inte följer reglerna, går inte heller i ett test.
            content = state.content_problems(utskick)
            if content:
                refusal = content[0]
    if refusal:
        messages.error(request, refusal)
        return False
    match = register.match(account, phone=address)
    if match.contact is not None:
        check = checks.reply_checks(account, match.contact, address, now)
        if check.skip or check.defer:
            messages.error(request, check.text or "Numret kan inte få sms från dig just nu.")
            return False
    elif suppressions.is_suppressed(account, CHANNEL_SMS, value=address):
        messages.error(request, "Numret har avregistrerat sig från dina sms.")
        return False
    if limits.hit("test_send", str(account.pk), limits.day_window(now), TEST_SENDS_PER_DAY):
        messages.error(
            request, f"Du har skickat {TEST_SENDS_PER_DAY} test i dag. Försök igen i morgon."
        )
        return False
    candidates = _preview_contacts(utskick) if _editable(utskick) else []
    kontakt = _preview_contact(request, account, utskick, candidates)
    sender = composer.sender_for_kind(utskick)
    values = composer.merge_values(kontakt, composer.field_defs(account)) if kontakt else {}
    try:
        text = composer.render_test_sms(utskick, address, values, sender)
        outcome = sms_wrapper.send(account, to=address, body=text, sender=sender, source="test")
    except sms_wrapper.DemoRefused:
        messages.error(request, sms_wrapper.DEMO_TEXT)
        return False
    except keys.KeyMismatch:
        messages.error(request, KEY_TEXT)
        return False
    logger.info(
        "Utskick %s: testsms (%s) av användare %s (byrån: %s): %s",
        utskick.pk,
        request.POST.get("till") or "",
        getattr(actor.user, "pk", None),
        actor.staff,
        outcome.error or ("oklart" if outcome.unknown else "skickat"),
    )
    if outcome.unknown:
        messages.warning(
            request, "Det är oklart om testet gick i väg. Vänta en stund innan du försöker igen."
        )
        return False
    if not outcome.ok:
        messages.error(request, OUTCOME_TEXTS.get(outcome.error, "Testet gick inte att skicka."))
        return False
    messages.success(request, f"Testet är skickat till {register_display(address)}.")
    if match.contact is not None:
        register.record_event(
            match.contact,
            "test_send",
            data={
                "utskick": utskick.pk,
                "user": getattr(actor.user, "pk", None),
                "staff": actor.staff,
            },
            activity=False,
        )
    return True


@utskick_view
@require_POST
def utskick_test(request, account, pk):
    """Testsms från en sida utan redigeraren (rapporten, Granska). Från
    Innehåll går testet via steget, så att osparad text sparas först."""
    utskick = owned(Utskick, account, pk)
    _send_test(request, account, utskick)
    if _editable(utskick):
        return redirect(_step_url(utskick, "innehall") + "#ut-test")
    return redirect(_report_url(utskick))


# ---------------------------------------------------------------------------
# Bekräftelsen och läget
# ---------------------------------------------------------------------------


@utskick_view
@require_POST
def utskick_confirm(request, account, pk):
    """Bekräfta och schemalägg, eller Skicka nu (I.6, D12). Kontrollerna
    körs om; ett blockerande fel, en gammal Granska-sida eller en saknad
    kryssruta för byrån skickar tillbaka till Granska. Själva sändningen
    gör ticken."""
    utskick = owned(Utskick, account, pk)
    now = timezone.now()
    actor = actor_for(request)
    granska = redirect(_step_url(utskick, "granska"))
    if not _confirmable(utskick):
        messages.info(request, "Utskicket är redan bekräftat.")
        return redirect(_report_url(utskick))
    nonce = str(request.POST.get("nonce") or "")
    if not nonce or not secrets.compare_digest(nonce, utskick.confirm_nonce or ""):
        messages.error(request, CHANGED_TEXT)
        return granska
    if not _staff_ok(request, actor):
        messages.error(request, STAFF_MISSING.format(name=_display_name(request)))
        return granska
    checked = review(request, account, utskick, now)
    if checked["blocking"]:
        messages.error(request, BLOCKING_TEXT)
        return granska
    result = state.confirm(
        utskick,
        actor=actor,
        nonce=nonce,
        summary=checked["summary"],
        send_now=checked["send_now"],
        now=now,
    )
    if not result.ok:
        messages.error(request, result.error or CHANGED_TEXT)
        return granska
    if account.is_demo:
        messages.success(request, "Demokontot skickar aldrig. Utskicket visas som skickat.")
    elif checked["send_now"]:
        messages.success(request, "Utskicket är bekräftat och skickas nu.")
    else:
        messages.success(request, f"Utskicket är schemalagt till {checked['when_text']}.")
    return redirect(_report_url(utskick))


def _ask_adx(request, account, utskick, what):
    subjects = {
        "tak": "Utskick: kunden ber om ett högre kostnadstak",
        "sms": "Utskick: kunden ber om sms",
    }
    lines = [
        f"Konto {account.pk} ({account.customer.name if account.customer_id else ''}) "
        f"frågar från utskick {utskick.pk if utskick else '-'}.",
        f"Av: {actor_for(request).label}",
    ]
    alerts.agency(subjects[what], lines, once=f"ask_{what}:{account.pk}")
    messages.success(request, ASKED_TEXT)


@utskick_view
@require_POST
def utskick_state(request, account, pk):
    """Pausa, fortsätt, avbryt, ändra ett schemalagt, och "Be ADX ..."
    (I.5). Fortsätt kör förkontrollerna igen (sending.state.resume); en paus
    som kräver ny bekräftelse går till Granska."""
    utskick = owned(Utskick, account, pk)
    actor = actor_for(request)
    action = request.POST.get("action") or ""
    report = redirect(_report_url(utskick))
    name = _display_name(request)
    if action == "pausa":
        if utskick.status not in (Utskick.Status.FREEZING, Utskick.Status.SENDING):
            messages.info(request, "Utskicket skickas inte just nu.")
            return report
        reason = Utskick.PauseReason.STAFF if actor.staff else Utskick.PauseReason.CUSTOMER
        if state.pause(utskick, reason, actor=actor):
            messages.success(request, "Utskicket är pausat.")
        return report
    if action == "fortsatt":
        if not utskick.is_paused:
            messages.info(request, "Utskicket är inte pausat.")
            return report
        if utskick.pause_reason in Utskick.RECONFIRM_REASONS:
            return redirect(_step_url(utskick, "granska"))
        if utskick.pause_reason not in CUSTOMER_RESUMES and not actor.staff:
            messages.error(request, "ADX går igenom utskicket innan det kan fortsätta.")
            return report
        if not _staff_ok(request, actor):
            messages.error(request, STAFF_MISSING.format(name=name))
            return report
        result = state.resume(utskick, actor=actor)
        if result.ok:
            messages.success(request, "Utskicket fortsätter.")
        else:
            messages.error(request, result.error or "Utskicket kan inte fortsätta än.")
        return report
    if action == "avbryt":
        if utskick.status in Utskick.FINISHED or utskick.status == Utskick.Status.SENDING:
            messages.info(request, "Pausa utskicket innan du avbryter det.")
            return report
        if state.cancel(utskick, actor=actor):
            messages.success(request, "Utskicket är avbrutet. Inget mer skickas.")
        return report
    if action == "andra":
        if utskick.status == Utskick.Status.SCHEDULED:
            state.unconfirm(utskick)
            messages.info(request, UNCONFIRMED)
            return redirect(_step_url(utskick, "mottagare"))
        if state.can_reopen(utskick):
            # Ett pausat som ska bekräftas igen och inte är fryst: tillbaka
            # till utkast, till steget som troligen behöver ändras.
            step = REOPEN_STEPS.get(utskick.pause_reason, "mottagare")
            if state.reopen(utskick, actor=actor):
                messages.info(request, REOPENED)
                return redirect(_step_url(utskick, step))
        messages.info(request, NOT_EDITABLE)
        return report
    if action in ("be_om_tak", "be_om_sms"):
        _ask_adx(request, account, utskick, "tak" if action == "be_om_tak" else "sms")
        return report
    return HttpResponseBadRequest("Okänd åtgärd.", content_type="text/plain")


# ---------------------------------------------------------------------------
# Rapporten och mottagarna
# ---------------------------------------------------------------------------


def _pause_banner(request, account, utskick, numbers, now):
    """Rutan överst i rapporten för ett pausat utskick (I.5): text och knappar."""
    reason = utskick.pause_reason
    P = Utskick.PauseReason
    actor = actor_for(request)
    sms = _sms_state(account)
    buttons = []
    text = "Utskicket är pausat."
    if reason == P.SMS_COST_CAP:
        if sms["account"] is not None:
            used = pricing.usage(sms["account"], now)
            # Förkontrollens uppskattning när den finns (sending.state), annars räknad här.
            estimate = (utskick.frozen_counts or {}).get("estimate") or {}
            queued = int(estimate.get("sms") or numbers.get("queued") or 0)
            cost = estimate.get("cost")
            if cost is None:
                parts = int((utskick.confirm_summary or {}).get("parts") or 1)
                cost = queued * parts * composer.part_units(sms["account"])
            text = (
                f"Pausat vid taket: {_group(queued)} sms kostar cirka {_kr(cost)} och "
                f"{_kr(used['remaining'])} är kvar av taket {_kr(used['cap'])}."
            )
            if sms["account"].customer_manages_api:
                buttons.append(
                    {"kind": "link", "label": "Höj taket", "url": reverse("sms:dashboard")}
                )
            else:
                buttons.append(
                    {"kind": "post", "action": "be_om_tak", "label": "Be ADX höja taket"}
                )
        buttons += [_resume_button(), _cancel_button()]
    elif reason == P.AUDIENCE_GREW:
        then = audience.confirmed_total(utskick.confirm_summary, utskick.channel_mode) or 0
        grown = int((utskick.frozen_counts or {}).get("sms") or numbers.get("total") or 0)
        text = (
            f"Mottagarna har blivit fler sedan du bekräftade: {_group(grown)} i stället för "
            f"{_group(then)}. Granska och bekräfta igen."
        )
        buttons += [_review_button(utskick), _cancel_button()]
    elif reason == P.LATE:
        when = clock_day(utskick.scheduled_at, now) if utskick.scheduled_at else ""
        text = (
            f"Utskicket skulle ha gått i väg {when} men kunde inte skickas då. "
            "Granska och bekräfta igen om det fortfarande stämmer, så skickas det nu, "
            "eller ändra utskicket och välj en ny tid."
        )
        buttons += [_review_button(utskick), _cancel_button()]
    elif reason == P.CONTENT:
        problems = state.content_problems(utskick)
        text = "Utskicket stoppades innan det skickades."
        if problems:
            text += f" {problems[0]}"
        text += " Granska och bekräfta igen när det är rättat."
        buttons += [_review_button(utskick), _cancel_button()]
    elif reason == P.SMS_DISABLED:
        text = SMS_DISABLED_TEXT
        buttons += [
            {"kind": "post", "action": "be_om_sms", "label": "Be ADX slå på sms"},
            _resume_button(),
        ]
    elif reason == P.PROVIDER:
        text = "Sms-leverantören eller e-posttjänsten svarade med fel. ADX har fått ett larm."
        buttons += [_resume_button(), _cancel_button()]
    elif reason == P.ACCOUNT_DISABLED:
        text = "Utskick var avstängt för kontot. Granska och bekräfta igen."
        buttons += [_review_button(utskick), _cancel_button()]
    elif reason == P.BLOCKED:
        text = checks.BLOCKED_TEXT
    elif reason == P.STAFF:
        note = ((utskick.stats or {}).get("pause") or {}).get("note") or ""
        text = "ADX har pausat utskicket." + (f" {note}" if note else "")
        buttons.append(_cancel_button())
    elif reason == P.CUSTOMER:
        text = "Du har pausat utskicket."
        buttons += [_resume_button(), _cancel_button()]
    elif reason == P.STOPS:
        stopped = int(numbers.get("stopped") or 0)
        delivered = int(numbers.get("delivered") or 0)
        pct = f" ({stopped * 100 / delivered:.1f} %)".replace(".", ",") if delivered else ""
        text = (
            f"{_group(stopped)} av {_group(delivered)} mottagare{pct} har avregistrerat sig. "
            "ADX går igenom utskicket innan det kan fortsätta."
        )
        buttons.append(_cancel_button())
    else:
        buttons.append(_cancel_button())
    if actor.staff and reason in STAFF_RESUMES and reason != P.BLOCKED:
        buttons.insert(0, _resume_button())
    if state.can_reopen(utskick):
        # Inget är fryst än: kunden kan ändra utskicket i stället (ny tid,
        # en annan länk) och bekräfta det på nytt.
        position = 1 if buttons and buttons[0].get("kind") == "link" else 0
        buttons.insert(position, _reopen_button())
    return {"text": text, "buttons": buttons}


def _resume_button():
    return {"kind": "post", "action": "fortsatt", "label": "Fortsätt", "sends": True}


def _reopen_button():
    return {"kind": "post", "action": "andra", "label": "Ändra utskicket", "ghost": True}


def _cancel_button():
    """Avbryt kan inte ångras: rutan frågar först (som knappen överst)."""
    return {"kind": "cancel", "action": "avbryt", "label": "Avbryt utskicket", "ghost": True}


def _review_button(utskick):
    return {"kind": "link", "label": "Granska igen", "url": _step_url(utskick, "granska")}


def _progress(request, account, utskick, numbers, now):
    """ "Skickas: 120 av 388 · fortsätter 09.00 i morgon" och väntan som inte
    är en paus (I.5)."""
    if utskick.status == Utskick.Status.FREEZING:
        return "Förbereds: mottagarna tas fram."
    if utskick.status != Utskick.Status.SENDING:
        return ""
    total = int(numbers.get("total") or 0)
    done = int(numbers.get("sent") or 0) + int(numbers.get("failed") or 0)
    text = f"Skickas: {_group(done)} av {_group(total)}"
    if account.is_demo:
        return text
    sms = _sms_state(account)
    row = request.utskick_settings
    if not sms["ready"] or sms["breaker"]:
        return f"{text} · Väntar: sändningen är tillfälligt stoppad av ADX"
    if not timing.sms_window_open(row, now):
        start = timing.next_window_start(row, now)
        return f"{text} · Fortsätter {clock_day(start, now)}: utanför tidsfönstret"
    return text


def _header_line(utskick, numbers, now):
    """ "Sms · skickat tisdag 8 okt 09.00 · 388 mottagare · kostnad 153 kr"."""
    parts = ["Sms"]
    if utskick.status == Utskick.Status.SCHEDULED and utskick.scheduled_at:
        parts.append(f"schemalagt {day_clock(utskick.scheduled_at, now)}")
    elif utskick.started_at:
        parts.append(f"skickat {day_clock(utskick.started_at, now)}")
    total = int(numbers.get("total") or 0)
    if not total and utskick.confirm_summary:
        total = audience.confirmed_total(utskick.confirm_summary, utskick.channel_mode) or 0
    if total:
        parts.append(f"{_group(total)} mottagare")
    if numbers.get("cost_units"):
        parts.append(f"kostnad {_kr(numbers['cost_units'])}")
    return " · ".join(parts)


def _engaged_text(seconds):
    seconds = int(seconds or 0)
    if not seconds:
        return ""
    minutes, rest = divmod(seconds, 60)
    if minutes:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    return f"{rest} s"


def _result(recipient):
    """Resultatet för en mottagare: (text, ton)."""
    if recipient.has_lead:
        return "Förfrågan", "ok"
    if recipient.stopped_at:
        return "Avregistrerad", "stop"
    if recipient.first_clicked_at:
        return "Klickade", "info"
    if recipient.status == Recipient.Status.SKIPPED:
        return recipient.get_skip_reason_display() or "Hoppades över", "muted"
    if recipient.status in (Recipient.Status.FAILED, Recipient.Status.BOUNCED):
        return "Gick inte fram", "warn"
    return recipient.get_status_display(), "muted"


def decorate_recipients(rows, now):
    for recipient in rows:
        delivered = recipient.delivered_at or (
            recipient.sent_at if recipient.status in Recipient.SENT_LIKE else None
        )
        recipient.ut_delivered = day_clock(delivered, now) if delivered else ""
        recipient.ut_clicked = (
            day_clock(recipient.first_clicked_at, now) if recipient.first_clicked_at else ""
        )
        recipient.ut_engaged = _engaged_text(recipient.engaged)
        if recipient.stopped_at:
            recipient.ut_reply = "STOPP"
        elif recipient.replied_at:
            recipient.ut_reply = "Ja"
        else:
            recipient.ut_reply = ""
        recipient.ut_result, recipient.ut_tone = _result(recipient)
        kontakt = recipient.contact
        recipient.ut_name = kontakt.display_name if kontakt else "Borttagen kontakt"
    return rows


def _report_tiles(numbers):
    delivered_note = ""
    if numbers.get("delivered_pct") is not None:
        delivered_note = f"{_pct_text(numbers['delivered_pct'])}"
        if numbers.get("failed"):
            delivered_note += f" · {_group(numbers['failed'])} gick inte fram"
    replies_note = f"{_group(numbers.get('stopped'))} STOPP" if numbers.get("stopped") else ""
    leads = int(numbers.get("leads") or 0)
    leads_note = ""
    if numbers.get("kr_per_lead") is not None:
        leads_note = f"{str(numbers['kr_per_lead']).replace('.', ',')}{NBSP}kr per förfrågan"
    if numbers.get("leads_late"):
        late = f"+{_group(numbers['leads_late'])} senare"
        leads_note = f"{leads_note} · {late}" if leads_note else late
    click_note = (
        f"{_pct_text(numbers['click_pct'])} av levererade"
        if numbers.get("click_pct") is not None
        else ""
    )
    return [
        {
            "label": "Levererade",
            "value": numbers.get("delivered"),
            "note": delivered_note,
            "visa": "levererade",
        },
        {"label": "Klick", "value": numbers.get("clicked"), "note": click_note, "visa": "klickade"},
        {"label": "Svar", "value": numbers.get("replied"), "note": replies_note, "visa": "svarade"},
        {"label": "Förfrågningar", "value": leads, "note": leads_note, "visa": "forfragan"},
    ]


def _pct_text(value):
    return procent(value)


def _staff_line(utskick):
    if not utskick.confirmed_as_staff:
        return ""
    first = utskick.confirmed_by.first_name if utskick.confirmed_by_id else ""
    who = f"ADX ({first})" if first else "ADX"
    return f"Skickat av {who} åt kunden"


def _info_line(utskick):
    if utskick.purpose != INFORMATION:
        return ""
    if utskick.info_reason == Utskick.InfoReason.ANNAT and utskick.info_reason_text:
        return f"Information: {utskick.info_reason_text}"
    if utskick.info_reason:
        return f"Information: {utskick.get_info_reason_display().lower()}"
    return "Information"


@utskick_view
def utskick_report(request, account, pk):
    utskick = owned(Utskick, account, pk)
    now = timezone.now()
    numbers = reports.summary(utskick, now)
    label, tone = status_info(utskick, numbers, now)
    rows = []
    if numbers.get("has_rows"):
        recipients = reports.recipients_for(utskick, "alla")
        rows = decorate_recipients(list(recipients[:REPORT_ROWS]), now)
    skipped = [
        {
            "reason": reason,
            "label": Recipient.SkipReason(reason).label
            if reason in Recipient.SkipReason.values
            else reason,
            "n": n,
            "visa": f"{reports.SKIPPED_PREFIX}{reason}",
        }
        for reason, n in (numbers.get("skipped_by_reason") or {}).items()
        if n
    ]
    context = _base_context(request, account, utskick)
    context.update(
        {
            "ut_nav": None,
            "numbers": numbers,
            "status_label": label,
            "status_tone": tone,
            "header_line": _header_line(utskick, numbers, now),
            "progress": _progress(request, account, utskick, numbers, now),
            "banner": _pause_banner(request, account, utskick, numbers, now)
            if utskick.is_paused
            else None,
            "tiles": _report_tiles(numbers),
            "skipped_rows": skipped,
            "rows": rows,
            "more_rows": int(numbers.get("total") or 0) > len(rows),
            "staff_line": _staff_line(utskick),
            "info_line": _info_line(utskick),
            "can_pause": utskick.status in (Utskick.Status.FREEZING, Utskick.Status.SENDING),
            "can_cancel": utskick.status
            in (Utskick.Status.DRAFT, Utskick.Status.SCHEDULED, Utskick.Status.FREEZING)
            or utskick.is_paused,
            "is_draft": utskick.status == Utskick.Status.DRAFT,
            "is_scheduled": utskick.status == Utskick.Status.SCHEDULED,
            "planned": audience.confirmed_total(utskick.confirm_summary, utskick.channel_mode)
            if utskick.confirm_summary
            else None,
        }
    )
    return render_utskick(request, "flamingo/app/utskick/report.html", "utskick", context)


#: Mottagarsidan utan rader, per vy (?visa=).
EMPTY_TEXTS = {
    "alla": "Utskicket har inga mottagare än.",
    "levererade": "Inget sms har levererats än.",
    "misslyckade": "Inget sms har misslyckats.",
    "klickade": "Ingen har klickat än.",
    "svarade": "Ingen har svarat än.",
    "stopp": "Ingen har avregistrerat sig.",
    "forfragan": "Ingen har skickat en förfrågan än.",
    "hoppades-over": "Ingen hoppades över.",
}


def _empty_text(view):
    if view.startswith(reports.SKIPPED_PREFIX):
        return EMPTY_TEXTS["hoppades-over"]
    return EMPTY_TEXTS.get(view, "Inga mottagare här just nu.")


@utskick_view
def utskick_recipients(request, account, pk):
    utskick = owned(Utskick, account, pk)
    now = timezone.now()
    view = request.GET.get("visa") or "alla"
    rows = reports.recipients_for(utskick, view)
    if rows is None:
        view = "alla"
        rows = reports.recipients_for(utskick, view)
    page = Paginator(rows, RECIPIENTS_PER_PAGE).get_page(request.GET.get("sida"))
    items = decorate_recipients(list(page.object_list), now)
    numbers = reports.summary(utskick, now)
    views = [
        (key, label)
        for key, label in reports.VIEWS.items()
        if key
        in (
            "alla",
            "levererade",
            "misslyckade",
            "klickade",
            "svarade",
            "stopp",
            "forfragan",
            "hoppades-over",
        )
    ]
    context = _base_context(request, account, utskick)
    context.update(
        {
            "rows": items,
            "page": page,
            "view": view,
            "view_label": reports.view_label(view),
            "empty_text": _empty_text(view),
            "views": views,
            "numbers": numbers,
            "lists": ContactList.objects.filter(account=account).order_by("name"),
            "with_contacts": rows.exclude(contact=None).count() if page.paginator.count else 0,
            "default_list_name": f"{utskick.name[:50]}: {reports.view_label(view).lower()}"[:80],
        }
    )
    return render_utskick(request, "flamingo/app/utskick/recipients.html", "utskick", context)


@utskick_view
@require_POST
def utskick_save_list(request, account, pk):
    """Spara mottagarna bakom en siffra som en lista (I.8): en befintlig
    lista (kontots, annars 400) eller en ny."""
    utskick = owned(Utskick, account, pk)
    view = request.POST.get("visa") or "alla"
    rows = reports.recipients_for(utskick, view)
    back = redirect(
        reverse("flamingo:app_utskick_recipients", args=[utskick.pk])
        + "?"
        + urlencode({"visa": view})
    )
    if rows is None:
        return HttpResponseBadRequest("Okänd vy.", content_type="text/plain")
    raw = str(request.POST.get("lista_id") or "").strip()
    if raw == "ny":
        name = clean_name(request.POST.get("ny_lista"), 80)
        if not name:
            messages.error(request, "Skriv ett namn på den nya listan.")
            return back
        user = request.user if request.user.is_authenticated else None
        try:
            with transaction.atomic():
                target, _ = ContactList.objects.get_or_create(
                    account=account, name=name, defaults={"created_by": user}
                )
        except IntegrityError:
            target = ContactList.objects.get(account=account, name=name)
    elif raw:
        list_pk = owned_ids(ContactList, account, [raw])[0]
        target = ContactList.objects.get(pk=list_pk, account=account)
    else:
        messages.error(request, "Välj en lista.")
        return back
    ids = list(rows.exclude(contact=None).values_list("contact_id", flat=True))
    added = register.add_to_list(target, ids, source=ListMembership.Source.REPORT)
    word = "kontakt" if added == 1 else "kontakter"
    messages.success(request, f"{_group(added)} {word} lades i listan {target.name}.")
    return back


# ---------------------------------------------------------------------------
# Inställningar för utskick (I.9, raderna för S2)
# ---------------------------------------------------------------------------

WINDOW_HOURS = tuple(range(8, 22))
CAP_CHOICES = tuple(range(1, 8))


def _int_in(raw, allowed):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value in allowed else None


def _settings_errors(post):
    errors = {}
    window = {}
    for key, label in (("weekday", "vardagar"), ("weekend", "helger")):
        start = _int_in(post.get(f"{key}_start"), WINDOW_HOURS)
        end = _int_in(post.get(f"{key}_end"), WINDOW_HOURS)
        if start is None or end is None or start >= end:
            errors[key] = (
                f"Välj ett fönster för {label} mellan 08.00 och 21.00 "
                "som slutar efter att det börjar."
            )
        else:
            window[key] = [start, end]
    caps = {}
    for name in ("weekly_cap_sms", "weekly_cap_email"):
        value = _int_in(post.get(name), CAP_CHOICES)
        if value is None:
            errors[name] = "Välj mellan 1 och 7 per vecka."
        else:
            caps[name] = value
    return errors, window, caps


@utskick_view
def utskick_settings(request, account):
    row = request.utskick_settings
    now = timezone.now()
    errors = {}
    if request.method == "POST":
        if request.POST.get("action") == "be_om_tak":
            _ask_adx(request, account, None, "tak")
            return redirect("flamingo:app_utskick_settings")
        errors, window, caps = _settings_errors(request.POST)
        if not errors:
            UtskickSettings.objects.filter(pk=row.pk).update(
                sms_window=window,
                notify_on_reply=request.POST.get("notify_on_reply") == "1",
                updated_at=now,
                **caps,
            )
            messages.success(request, "Inställningarna är sparade.")
            return redirect("flamingo:app_utskick_settings")
    sms = _sms_state(account)
    usage = pricing.usage(sms["account"], now) if sms["account"] is not None else None
    window = row.sms_window if isinstance(row.sms_window, dict) else {}

    def hours(key):
        try:
            start, end = (int(v) for v in window.get(key) or SMS_WINDOW_DEFAULT[key])
        except (TypeError, ValueError):
            start, end = SMS_WINDOW_DEFAULT[key]
        if request.method == "POST" and errors:
            return (
                _int_in(request.POST.get(f"{key}_start"), WINDOW_HOURS) or start,
                _int_in(request.POST.get(f"{key}_end"), WINDOW_HOURS) or end,
            )
        return start, end

    tight = []
    if usage is not None:
        for utskick in Utskick.objects.listed().filter(
            account=account, status=Utskick.Status.SCHEDULED
        ):
            cost = int((utskick.confirm_summary or {}).get("cost_units") or 0)
            if cost and cost > usage["remaining"]:
                tight.append(utskick)
    context = {
        "errors": errors,
        "sms": sms,
        "reply_number": reply_number_text(),
        "usage": usage,
        "usage_text": (
            f"{month_name(now).capitalize()} {_kr(usage['cap_used'])} av {_kr(usage['cap'])}"
            if usage
            else ""
        ),
        "cap_text": f"{_kr(usage['cap'])} · gemensamt med sms-API:t" if usage else "",
        "tight": tight,
        "hours": WINDOW_HOURS,
        "caps": CAP_CHOICES,
        "weekday": hours("weekday"),
        "weekend": hours("weekend"),
        "cap_sms": _int_in(request.POST.get("weekly_cap_sms"), CAP_CHOICES)
        if errors
        else row.weekly_cap_sms,
        "cap_email": _int_in(request.POST.get("weekly_cap_email"), CAP_CHOICES)
        if errors
        else row.weekly_cap_email,
        # E-posten (S3) syns inte förrän den finns (README: ingen obyggd
        # funktion i produkten); det sparade värdet följer med dolt.
        "email_live": state.email_live(),
        "stored_cap_email": min(max(int(row.weekly_cap_email or 1), 1), CAP_CHOICES[-1]),
        "notify_on_reply": (request.POST.get("notify_on_reply") == "1")
        if errors
        else row.notify_on_reply,
        "names_locked_text": NAMES_LOCKED_TEXT,
        "window_rule": window_rule_text(row),
    }
    return render_utskick(request, "flamingo/app/utskick/settings.html", "settings", context)
