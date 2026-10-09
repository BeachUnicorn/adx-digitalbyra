"""
Sidorna på länkvärdarna k.adx.se och klick.adx.se (README E.1, E.3, E.5),
routade av config/urls_links.py (links.LinkHostMiddleware).

Foundation (färdiga, behålls av ägaren):

    home        "/" på båda värdarna: en neutral förklaring och ADX integritetspolicy
    robots      "/robots.txt": Disallow: /
    not_found   404 på länkvärdarna (handler404 i urls_links): "Länken har gått ut."

Länk-byggaren (S2):

    click             k.adx.se/<kod>        302 till målet med ut (E.3)
    sms_unsubscribe   k.adx.se/s/<kod>      GET sida, POST avregistrerar, Ångra (E.5)
    sms_preferences   k.adx.se/p/<kod>      GET sida, POST sparar (E.5)
    confirm           k.adx.se/b/<kod>      GET knapp, POST bekräftar (E.5)

Regler här: inga kakor (ingen {% csrf_token %}, POST:ar är csrf_exempt och
bär den signerade nonce:n fn, tokens.form_nonce, E.1), maskade uppgifter,
aldrig ett namn, och links.private(svar) bara på 302:orna till målet (HTML-
sidorna behåller Djangos same-origin, men sparas inte i någon cache). Ett
GET ändrar aldrig något (förhandsvisningar och skannrar öppnar länkar).
Missar räknas per besökare (Counter link_miss): fler än MISS_LIMIT i
timmen ger 429 för allt från den besökaren, också för koder som finns,
så att svaret inte avslöjar vilka koder som finns. Kontot tas bara ur
koden (H.1). Reglerna bakom sidorna står i link_actions.py och
attribution.py.
"""

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_safe

from apps.common.botcheck import botcheck_passes
from apps.common.net import client_ip

from . import attribution, capture, limits, link_actions, normalize, tokens
from .access import settings_for
from .keys import KeyMismatch
from .links import (
    KIND_SMS,
    bare_destination,
    build_destination,
    destination_ok,
    on_link_host,
    private,
    site_urls,
)
from .models import CHANNEL_SMS, LinkCode

#: ADX integritetspolicy på adx.se (startsidan på länkvärdarna länkar dit).
ADX_PRIVACY_PATH = "/integritetspolicy/"


def adx_privacy_url():
    base = (getattr(settings, "SITE_BASE_URL", "") or "https://adx.se").rstrip("/")
    return f"{base}{ADX_PRIVACY_PATH}"


@require_safe
def home(request):
    return render(
        request,
        "utskick/links/home.html",
        {"foretag": "ADX Flamingo", "adx_privacy_url": adx_privacy_url()},
    )


@require_safe
def robots(request):
    return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")


def not_found(request, exception=None):
    """404 för allt på länkvärdarna. Samma svar för en kod som aldrig
    funnits och en som gått ut (ingen uppräkning)."""
    return render(request, "utskick/links/not_found.html", {"foretag": "ADX Flamingo"}, status=404)


# ---------------------------------------------------------------------------
# Gemensamt
# ---------------------------------------------------------------------------

#: Missar per besökare (ip_hash) och timme innan allt från besökaren får 429 (E.2).
MISS_LIMIT = 20

STALE_TEXT = "Sidan hann bli för gammal. Tryck på knappen en gång till."
SAVE_FAILED_TEXT = "Det gick inte att spara just nu. Försök igen lite senare."
UNDO_GONE_TEXT = "Det går inte att ångra längre."
EMAIL_INVALID_TEXT = "Skriv en e-postadress som namn@exempel.se."

#: Vad /p/ säger efter en sparning (?klart=).
DONE_TEXTS = {
    "sparat": "Dina val är sparade.",
    "avregistrerad": "Du är avregistrerad.",
}


def _ip_hash(request):
    from apps.flamingo.limits import ip_hash

    return ip_hash(client_ip(request))


def _blocked(ip_hash):
    """Har besökaren redan passerat gränsen för missar den här timmen?"""
    return limits.count("link_miss", ip_hash, limits.hour_window()) > MISS_LIMIT


def _too_many(request):
    response = render(
        request, "utskick/links/too_many.html", {"foretag": "ADX Flamingo"}, status=429
    )
    response["Retry-After"] = "3600"
    return response


def _miss(request, ip_hash):
    """En kod som inte finns: 404 med samma sida som en gammal kod. Räknas
    mot besökarens gräns; över den 429."""
    if limits.hit("link_miss", ip_hash, limits.hour_window(), MISS_LIMIT):
        return _too_many(request)
    return not_found(request)


def _no_store(response):
    """En personlig sida: ingen cache sparar den (Referrer-Policy ändras inte)."""
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


def _page(request, template, context, status=200):
    return _no_store(render(request, template, context, status=status))


def _base(account, row):
    """Sidans skal: kundens namn och integritetstexten (H.5). Länken går
    alltid till adx.se (eller kundens egen policy), aldrig till länkvärden."""
    with site_urls():
        privacy = capture.privacy_url(account, row, absolute=True)
    return {
        "foretag": row.display_name or getattr(account.customer, "name", "") or "Företaget",
        "integritet_url": privacy,
    }


class _PathWithoutCode:
    """Det botskyddet läser av förfrågan (POST och sökvägen till loggen),
    med koden bortklippt ur sökvägen: koden är en behörighet och hör inte
    hemma i journalen (H.3)."""

    def __init__(self, request):
        self.POST = request.POST
        self.path = request.path.rsplit("/", 1)[0] + "/"


def _botcheck(request):
    return botcheck_passes(_PathWithoutCode(request))


def _redirect(url):
    return _no_store(HttpResponseRedirect(url))


# ---------------------------------------------------------------------------
# Klicket (E.3)
# ---------------------------------------------------------------------------


@csrf_exempt
@on_link_host(KIND_SMS)
def click(request, code):
    """k.adx.se/<kod>: 302 till målet. HEAD ger målet utan ut och loggas
    inte, och en kod utan mottagare (testsms) räknas inte. Bottar och
    förhandsvisningar räknas men sparas inte och får inget ut. Människor och
    skannrar sparas (attribution.record_click), och en Flamingo-sida får
    ut. En extern adress prövas igen (links.destination_ok): en värd som
    ADX nekat eller som väntar ger samma sida som en gammal kod. Inga anrop
    utåt: svaret ska ta millisekunder."""
    if request.method not in ("GET", "HEAD"):
        return not_found(request)
    ip_hash = _ip_hash(request)
    if _blocked(ip_hash):
        return _too_many(request)
    row = (
        LinkCode.objects.select_related("recipient", "link__campaign", "link__account__customer")
        .filter(code=code, kind=LinkCode.Kind.LINK)
        .first()
    )
    link = row.link if row is not None else None
    if link is None or link.account_id != row.account_id:
        return _miss(request, ip_hash)
    if not destination_ok(link):
        return not_found(request)
    if request.method == "HEAD":
        return private(HttpResponseRedirect(bare_destination(link)))
    recipient = row.recipient
    if recipient is None:
        # Ett testsms (F.8): koden har ingen mottagare. Målet, men inget räknas
        # och inget ut (en provförfrågan ska inte se ut som ett utskick).
        return private(HttpResponseRedirect(build_destination(link, None, None)))
    kind = attribution.classify(request, recipient, link)
    if kind == attribution.BOT:
        attribution.count_bot(row)
        return private(HttpResponseRedirect(build_destination(link, recipient, None)))
    click_row = attribution.record_click(row, kind, request, ip_hash)
    return private(HttpResponseRedirect(build_destination(link, recipient, click_row)))


# ---------------------------------------------------------------------------
# Avregistrera (/s/, E.5)
# ---------------------------------------------------------------------------


def _undo_context(suppression_id, nonce):
    """Ångra-knappen om värdet gäller, annars None."""
    try:
        suppression_id = int(suppression_id)
    except (TypeError, ValueError):
        return None
    if not nonce or not tokens.read_undo(suppression_id, nonce):
        return None
    return {"sparr": suppression_id, "nonce": nonce}


@csrf_exempt
@on_link_host(KIND_SMS)
@require_http_methods(["GET", "HEAD", "POST"])
def sms_unsubscribe(request, code):
    """k.adx.se/s/<kod>: "Vill du sluta få sms från <företaget>?" med
    knappen "Avregistrera mig". POST avregistrerar (spärr, orsak link) och
    visar Ångra (30 minuter) och frågan om e-posten. Fungerar också när
    utskick är avstängt för kontot (D.8)."""
    ip_hash = _ip_hash(request)
    if _blocked(ip_hash):
        return _too_many(request)
    row_code = link_actions.person_code(code)
    if row_code is None:
        return _miss(request, ip_hash)
    account = row_code.account
    row = settings_for(account)
    contact = link_actions.contact_for(account, CHANNEL_SMS, row_code.value_hash)
    context = _base(account, row)
    status = 200
    state = ""
    email_done = False
    if request.method == "POST":
        action = request.POST.get("action", "")
        if not tokens.read_form_nonce(code, request.POST.get("fn", "")):
            context["fel"] = STALE_TEXT
            status = 400
        else:
            try:
                if action == "avregistrera":
                    suppression, created = link_actions.unsubscribe_sms(row_code, ip_hash=ip_hash)
                    state = "klar"
                    if created:
                        context["angra"] = {
                            "sparr": suppression.pk,
                            "nonce": tokens.undo_nonce(suppression.pk),
                        }
                elif action == "angra":
                    if link_actions.undo(
                        row_code,
                        request.POST.get("sparr"),
                        request.POST.get("un", ""),
                        ip_hash=ip_hash,
                    ):
                        state = "angrad"
                    else:
                        context["fel"] = UNDO_GONE_TEXT
                elif action == "epost":
                    email_done = link_actions.unsubscribe_email(row_code, contact, ip_hash=ip_hash)
                    context["angra"] = _undo_context(
                        request.POST.get("sparr"), request.POST.get("un", "")
                    )
            except KeyMismatch:
                context["fel"] = SAVE_FAILED_TEXT
                status = 503
    suppressed = link_actions.is_suppressed(account, CHANNEL_SMS, row_code.value_hash)
    if state != "angrad":
        state = "klar" if suppressed else "fraga"
    context.update(
        {
            "tillstand": state,
            "fn": tokens.form_nonce(code),
            "maskerat": link_actions.masked_number(row_code, contact),
            "epost_klar": email_done,
            "epost_fraga": link_actions.email_question(row_code, contact)
            if state == "klar" and not email_done
            else "",
            "extra_text": row.unsubscribe_text if state == "klar" else "",
        }
    )
    return _page(request, "utskick/links/sms_unsubscribe.html", context, status=status)


# ---------------------------------------------------------------------------
# Dina val (/p/, E.5)
# ---------------------------------------------------------------------------


@csrf_exempt
@on_link_host(KIND_SMS)
@require_http_methods(["GET", "HEAD", "POST"])
def sms_preferences(request, code):
    """k.adx.se/p/<kod>: "Vad vill du få från <företaget>?" Sms och e-post
    med erbjudanden, raden om information och "Avregistrera mig från allt".
    Att stänga av går alltid; att slå på kräver botskyddet och skickar en
    bekräftelse (sms till /b/, eller ett mejl). Bara maskerade uppgifter."""
    ip_hash = _ip_hash(request)
    if _blocked(ip_hash):
        return _too_many(request)
    row_code = link_actions.person_code(code)
    if row_code is None:
        return _miss(request, ip_hash)
    account = row_code.account
    row = settings_for(account)
    contact = link_actions.contact_for(account, CHANNEL_SMS, row_code.value_hash)
    context = _base(account, row)
    status = 200
    if request.method == "POST":
        action = request.POST.get("action", "")
        if not tokens.read_form_nonce(code, request.POST.get("fn", "")):
            context["fel"] = STALE_TEXT
            status = 400
        else:
            try:
                if action == "allt":
                    link_actions.unsubscribe_all(
                        account, contact, row_code.value_hash, ip_hash=ip_hash
                    )
                    return _redirect(f"{request.path}?klart=avregistrerad")
                if action == "spara" and contact is not None:
                    rows, _blocked_channels = link_actions.preference_rows(account, row, contact)
                    turning_on = any(
                        (item["anmal"] and request.POST.get("epost", "").strip())
                        or (
                            item["tillstand"] == "off"
                            and item["kan_andras"]
                            and request.POST.get(item["kanal"]) == "1"
                        )
                        for item in rows
                    )
                    done = link_actions.save_preferences(
                        account,
                        row,
                        contact,
                        rows,
                        request.POST,
                        botcheck_ok=turning_on and _botcheck(request),
                        ip_hash=ip_hash,
                    )
                    return _redirect(f"{request.path}?klart={done}")
            except KeyMismatch:
                context["fel"] = SAVE_FAILED_TEXT
                status = 503
    if contact is not None:
        rows, blocked = link_actions.preference_rows(account, row, contact)
        masked = [
            normalize.mask_phone(contact.phone) if contact.phone else "",
            normalize.mask_email(contact.email) if contact.email else "",
        ]
        addressed = [item for item in rows if not item["anmal"]]
        all_blocked = bool(addressed) and len(blocked) == len(addressed)
    else:
        rows, masked = [], [link_actions.masked_number(row_code)]
        all_blocked = link_actions.is_suppressed(account, CHANNEL_SMS, row_code.value_hash)
    done = request.GET.get("klart", "")
    if done == "fel-epost":
        context["fel"] = EMAIL_INVALID_TEXT
    context.update(
        {
            "rader": rows,
            "maskerat": " · ".join(m for m in masked if m),
            "allt_sparrat": all_blocked,
            "kan_spara": any(item["kan_andras"] or item["anmal"] for item in rows),
            "klart": done,
            "klart_text": DONE_TEXTS.get(done, ""),
            "sms_till": normalize.mask_phone(contact.phone)
            if contact is not None and done in ("sms", "sms-mejl")
            else "",
            "mejl_till": normalize.mask_email(contact.email)
            if contact is not None and contact.email and done in ("mejl", "sms-mejl")
            else "",
            "avregistrering_text": row.unsubscribe_text if done == "avregistrerad" else "",
            "fn": tokens.form_nonce(code),
            "botcheck": bool(rows),
        }
    )
    return _page(request, "utskick/links/sms_preferences.html", context, status=status)


# ---------------------------------------------------------------------------
# Bekräfta (/b/, E.5)
# ---------------------------------------------------------------------------


def _confirm_text(foretag, purpose):
    again = " igen" if purpose == LinkCode.Purpose.START else ""
    return f"Bekräfta att du vill få sms från {foretag}{again}."


@csrf_exempt
@on_link_host(KIND_SMS)
@require_http_methods(["GET", "HEAD", "POST"])
def confirm(request, code):
    """k.adx.se/b/<kod>: "Bekräfta att du vill få sms från <företaget>." med
    en knapp. POST sätter sms till ja (källa confirm, eller start efter
    START), tar bort en spärr och använder koden. Gäller 24 timmar."""
    ip_hash = _ip_hash(request)
    if _blocked(ip_hash):
        return _too_many(request)
    row_code = link_actions.confirm_code(code)
    if row_code is None:
        return _miss(request, ip_hash)
    account = row_code.account
    row = settings_for(account)
    context = _base(account, row)
    text = _confirm_text(context["foretag"], row_code.purpose)
    status = 200
    state = link_actions.confirm_state(row_code)
    if request.method == "POST" and state == link_actions.CONFIRM_ASK:
        if not tokens.read_form_nonce(code, request.POST.get("fn", "")):
            context["fel"] = STALE_TEXT
            status = 400
        else:
            try:
                state = link_actions.confirm(row_code, text_shown=text, ip_hash=ip_hash)
            except KeyMismatch:
                context["fel"] = SAVE_FAILED_TEXT
                status = 503
            else:
                if state == link_actions.CONFIRM_DONE:
                    return _redirect(request.path)
    if state in (link_actions.CONFIRM_EXPIRED, link_actions.CONFIRM_INVALID) and status == 200:
        status = 410
    contact = link_actions.confirm_contact(row_code)
    context.update(
        {
            "tillstand": state,
            "rubrik": text,
            "igen": row_code.purpose == LinkCode.Purpose.START,
            "maskerat": normalize.mask_phone(contact.phone) if contact is not None else "",
            "timmar": LinkCode.CONFIRM_HOURS,
            "fn": tokens.form_nonce(code),
        }
    )
    return _page(request, "utskick/links/confirm.html", context, status=status)
