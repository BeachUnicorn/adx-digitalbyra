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

S3 (klick.adx.se, mejlen; avsnittet sist i filen, inkorg-byggaren i S3):

    email_click, email_unsubscribe, email_preferences, web_view,
    open_pixel, calendar

Regler här: inga kakor (ingen {% csrf_token %}, POST:ar är csrf_exempt och
bär den signerade nonce:n fn, tokens.form_nonce, E.1), maskade uppgifter,
aldrig ett namn, och links.private(svar) bara på 302:orna till målet (HTML-
sidorna behåller Djangos same-origin, men sparas inte i någon cache). Ett
GET ändrar aldrig något (förhandsvisningar och skannrar öppnar länkar).
Missar räknas per besökare (Counter link_miss): fler än MISS_LIMIT i
timmen ger 429 för allt från den besökaren, också för koder som finns,
så att svaret inte avslöjar vilka koder som finns. Det gäller k.adx.se
(korta koder). Mejlens token på klick.adx.se bär en HMAC-signatur (8 till
16 tecken) som inte går att gissa: där räknas inga missar och ingen
besökare spärras, så att en delad adress (Gmails servrar som gör
ettklicket, ett företags NAT, en länkskanner) aldrig kan spärras från att
avregistrera sig. Kontot tas bara ur koden (H.1). Reglerna bakom sidorna står i link_actions.py och
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
    KIND_EMAIL,
    KIND_SMS,
    bare_destination,
    build_destination,
    destination_ok,
    on_link_host,
    private,
    site_urls,
)
from .models import CHANNEL_SMS, LinkCode, Recipient

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


# ---------------------------------------------------------------------------
# S3, klick.adx.se (mejl): inkorg-byggaren (/a/, /w/) och integrationen
# (/m/, /v/, /o/, /c/).
#
#   email_click        klick.adx.se/m/<token>       302 till målet (E.3); test-token räknas inte
#   email_unsubscribe  klick.adx.se/a/<token>       GET sida med knapp; POST med knappen (fn)
#                                                   eller List-Unsubscribe=One-Click (utan fn,
#                                                   200 och tom kropp); GET avregistrerar aldrig
#   email_preferences  klick.adx.se/v/<token>       Dina val för e-post (som /utskick/val/)
#   web_view           klick.adx.se/w/<token>       Visa i webbläsaren, med CSP (F.4)
#   open_pixel         klick.adx.se/o/<token>.gif   1x1 gif, opened_at (bara tracking_ok)
#   calendar           klick.adx.se/c/<token>.ics   händelseblocket som kalenderfil
#
# Token läses med tokens.read_email_click, read_unsubscribe, read_preference,
# read_web_view, read_pixel och read_calendar. Kontot tas bara ur token
# (H.1). Inga kakor; POST:arna är csrf_exempt (E.1, H.2).
# ---------------------------------------------------------------------------


class _EmailCode:
    """Det attribution.record_click och count_bot läser av en klickkod, för
    ett klick i ett mejl (ingen LinkCode-rad: token bär mottagaren och
    länken, E.2)."""

    pk = None

    def __init__(self, link, recipient):
        self.link = link
        self.link_id = link.pk
        self.recipient = recipient
        self.recipient_id = recipient.pk if recipient is not None else None
        self.account_id = link.account_id


class _EmailChannel:
    """Mottagaren för build_destination när raden är borta: utm_medium email."""

    channel = "email"
    pk = None


@csrf_exempt
@on_link_host(KIND_EMAIL)
def email_click(request, token):
    """klick.adx.se/m/<token>: 302 till målet, som k.adx.se (E.3). Token bär
    mottagaren och länken (TrackedLink, fryst vid frysningen). HEAD ger
    målet utan ut och loggas inte. Ett testmejl (mottagare 0) och en
    mottagare som retentionen tagit bort leder rätt men räknas inte. En
    mottagare som inte hör till länkens utskick ger samma sida som en gammal
    länk. Inga anrop utåt."""
    from .models import CHANNEL_EMAIL, TrackedLink

    if request.method not in ("GET", "HEAD"):
        return not_found(request)
    ip_hash = _ip_hash(request)
    ref = tokens.read_email_click(token)
    if ref is None:
        return not_found(request)
    link = (
        TrackedLink.objects.select_related("campaign", "account__customer")
        .filter(pk=ref.link_id)
        .first()
    )
    if link is None or not destination_ok(link):
        return not_found(request)
    recipient = None
    if ref.recipient_id:
        recipient = (
            Recipient.objects.select_related("utskick")
            .filter(pk=ref.recipient_id, channel=CHANNEL_EMAIL)
            .first()
        )
        if recipient is not None and recipient.utskick_id != link.utskick_id:
            return not_found(request)
    if request.method == "HEAD":
        return private(HttpResponseRedirect(bare_destination(link)))
    if recipient is None:
        return private(HttpResponseRedirect(build_destination(link, _EmailChannel(), None)))
    code = _EmailCode(link, recipient)
    kind = attribution.classify(request, recipient, link)
    if kind == attribution.BOT:
        attribution.count_bot(code)
        return private(HttpResponseRedirect(build_destination(link, recipient, None)))
    click_row = attribution.record_click(code, kind, request, ip_hash)
    return private(HttpResponseRedirect(build_destination(link, recipient, click_row)))


# --- S3: avregistreringen i mejlet (/a/), inkorg-byggaren (svar och avregistrering) ---

#: Ettklicket i mejlprogrammet (RFC 8058): POST-kroppen List-Unsubscribe=One-Click.
ONE_CLICK_FIELD = "List-Unsubscribe"
ONE_CLICK_VALUE = "One-Click"


def _one_click(request):
    return request.method == "POST" and request.POST.get(ONE_CLICK_FIELD) == ONE_CLICK_VALUE


def _empty(status=200):
    """Svaret på ettklicket: tom kropp (RFC 8058), sparas ingenstans."""
    return _no_store(HttpResponse(b"", status=status, content_type="text/plain; charset=utf-8"))


@csrf_exempt
@on_link_host(KIND_EMAIL)
@require_http_methods(["GET", "HEAD", "POST"])
def email_unsubscribe(request, token):
    """klick.adx.se/a/<token> (E.5, H.6): "Vill du sluta få e-post från
    <företaget>?" med knappen "Avregistrera mig". Ett GET ändrar ingenting.
    POST med knappen (signerad nonce fn) eller med List-Unsubscribe=One-Click
    från mejlprogrammet (utan nonce, svaret 200 med tom kropp) lägger en
    spärr på adressen (orsak link respektive list_unsub). Token bär konto
    och adressens hash: avregistreringen fungerar utan mottagarraden (efter
    retention och GDPR) och när utskick är avstängt för kontot (D.8)."""
    from apps.flamingo.models import FlamingoAccount

    from .models import CHANNEL_EMAIL, Consent, Suppression

    ip_hash = _ip_hash(request)
    ref = tokens.read_unsubscribe(token)
    account = None
    if ref is not None:
        account = (
            FlamingoAccount.objects.select_related("customer").filter(pk=ref.account_id).first()
        )
    if account is None:
        return _empty(404) if _one_click(request) else not_found(request)
    if _one_click(request):
        try:
            link_actions.unsubscribe_email_hash(
                account,
                ref.value_hash,
                reason=Suppression.Reason.LIST_UNSUB,
                source=Consent.Source.LIST_UNSUB,
                detail=link_actions.EMAIL_ONE_CLICK_DETAIL,
                ip_hash=ip_hash,
            )
        except KeyMismatch:
            # Mejlprogrammet försöker igen; inget är sparat.
            return _empty(503)
        return _empty()
    row = settings_for(account)
    context = _base(account, row)
    status = 200
    if request.method == "POST":
        if request.POST.get("action") != "avregistrera" or not tokens.read_form_nonce(
            token, request.POST.get("fn", "")
        ):
            context["fel"] = STALE_TEXT
            status = 400
        else:
            try:
                link_actions.unsubscribe_email_hash(
                    account,
                    ref.value_hash,
                    reason=Suppression.Reason.LINK,
                    source=Consent.Source.LINK,
                    detail=link_actions.EMAIL_LINK_DETAIL,
                    ip_hash=ip_hash,
                )
            except KeyMismatch:
                context["fel"] = SAVE_FAILED_TEXT
                status = 503
            else:
                return _redirect(request.path)
    suppressed = link_actions.is_suppressed(account, CHANNEL_EMAIL, ref.value_hash)
    from .links import email_preferences_url

    context.update(
        {
            "tillstand": "klar" if suppressed else "fraga",
            "maskerat": link_actions.masked_email(account, ref.value_hash),
            "fn": tokens.form_nonce(token),
            "extra_text": row.unsubscribe_text if suppressed else "",
            "val_url": "" if suppressed else email_preferences_url(account.pk, ref.value_hash),
        }
    )
    return _page(request, "utskick/links/email_unsubscribe.html", context, status=status)


#: source_detail i samtyckesloggen för Dina val på klick.adx.se/v/.
EMAIL_PREFERENCES_DETAIL = "Dina val (länk i mejl)"


@csrf_exempt
@on_link_host(KIND_EMAIL)
@require_http_methods(["GET", "HEAD", "POST"])
def email_preferences(request, token):
    """klick.adx.se/v/<token> (E.5): "Vad vill du få från <företaget>?" för
    adressen i token, med samma rader som k.adx.se/p/ och samma regler: att
    stänga av går alltid, att slå på kräver botskyddet och en bekräftelse.
    Token är samma som /utskick/val/<token>/ på adx.se (S1) och går aldrig
    ut. Fungerar utan mottagarraden och när utskick är avstängt (D.8)."""
    from apps.flamingo.models import FlamingoAccount

    ip_hash = _ip_hash(request)
    ref = tokens.read_preference(token)
    account = None
    if ref is not None:
        account = (
            FlamingoAccount.objects.select_related("customer").filter(pk=ref.account_id).first()
        )
    if account is None:
        return not_found(request)
    row = settings_for(account)
    contact = link_actions.contact_for(account, ref.channel, ref.value_hash)
    context = _base(account, row)
    status = 200
    if request.method == "POST":
        action = request.POST.get("action", "")
        if not tokens.read_form_nonce(token, request.POST.get("fn", "")):
            context["fel"] = STALE_TEXT
            status = 400
        else:
            try:
                if action == "allt":
                    link_actions.unsubscribe_all(
                        account,
                        contact,
                        ref.value_hash,
                        ip_hash=ip_hash,
                        channel=ref.channel,
                        detail=EMAIL_PREFERENCES_DETAIL,
                    )
                    return _redirect(f"{request.path}?klart=avregistrerad")
                if action == "spara" and contact is not None:
                    rows, _blocked_channels = link_actions.preference_rows(account, row, contact)
                    turning_on = any(
                        item["tillstand"] == "off"
                        and item["kan_andras"]
                        and request.POST.get(item["kanal"]) == "1"
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
                        detail=EMAIL_PREFERENCES_DETAIL,
                    )
                    return _redirect(f"{request.path}?klart={done}")
            except KeyMismatch:
                context["fel"] = SAVE_FAILED_TEXT
                status = 503
    if contact is not None:
        rows, blocked = link_actions.preference_rows(account, row, contact)
        rows = [item for item in rows if not item["anmal"]]
        masked = [
            normalize.mask_phone(contact.phone) if contact.phone else "",
            normalize.mask_email(contact.email) if contact.email else "",
        ]
        all_blocked = bool(rows) and len(blocked) == len(rows)
    else:
        rows, masked = [], []
        all_blocked = link_actions.is_suppressed(account, ref.channel, ref.value_hash)
    done = request.GET.get("klart", "")
    context.update(
        {
            "rader": rows,
            "maskerat": " · ".join(m for m in masked if m),
            "allt_sparrat": all_blocked,
            "kan_spara": any(item["kan_andras"] for item in rows),
            "klart": done,
            "klart_text": DONE_TEXTS.get(done, ""),
            "sms_till": normalize.mask_phone(contact.phone)
            if contact is not None and contact.phone and done in ("sms", "sms-mejl")
            else "",
            "mejl_till": normalize.mask_email(contact.email)
            if contact is not None and contact.email and done in ("mejl", "sms-mejl")
            else "",
            "avregistrering_text": row.unsubscribe_text if done == "avregistrerad" else "",
            "fn": tokens.form_nonce(token),
            "botcheck": bool(rows),
            "adressen": "det här numret" if ref.channel == CHANNEL_SMS else "den här adressen",
        }
    )
    return _page(request, "utskick/links/email_preferences.html", context, status=status)


# --- S3: webbversionen (/w/), inkorg-byggaren (svar och avregistrering) ---

#: Webbversionens CSP (F.4): bara bilder över https och inline-stilar, inga
#: skript, inga formulär, ingen bas. Lokalt (DEBUG) också http-bilder, eftersom
#: bilderna där har adresser på http://localhost.
WEB_VIEW_CSP = (
    "default-src 'none'; img-src https:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'"
)


def web_view_csp():
    if settings.DEBUG:
        return WEB_VIEW_CSP.replace("img-src https:", "img-src https: http:")
    return WEB_VIEW_CSP


@on_link_host(KIND_EMAIL)
@require_safe
def web_view(request, token):
    """klick.adx.se/w/<token> "Visa i webbläsaren" (F.4): mejlet som det
    skickades (email.render.web_view, utan pixeln), med CSP och
    X-Frame-Options DENY. Token bär utskicket och mottagaren (0 för ett
    testmejl); en mottagare som inte hör till utskicket ger 404, och en
    mottagare som inte längre finns (retention) ger mejlet utan personliga
    värden. Inget räknas här."""
    from .email import render
    from .models import CHANNEL_EMAIL, Utskick

    ref = tokens.read_web_view(token)
    if ref is None:
        return not_found(request)
    utskick = (
        Utskick.objects.select_related("account__customer", "sender_domain")
        .filter(pk=ref.utskick_id)
        .first()
    )
    if utskick is None:
        return not_found(request)
    recipient = None
    if ref.recipient_id:
        recipient = (
            Recipient.objects.select_related("contact")
            .filter(pk=ref.recipient_id, channel=CHANNEL_EMAIL)
            .first()
        )
        if recipient is not None and recipient.utskick_id != utskick.pk:
            return not_found(request)
    html = render.web_view(utskick, recipient)
    if not html:
        return not_found(request)
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = web_view_csp()
    response["X-Frame-Options"] = "DENY"
    return _no_store(response)


#: 1 x 1 genomskinlig gif (43 byte).
PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
    b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


#: Mottagare som aldrig fick mejlet: en pixel för dem räknas inte.
_NOT_SENT = (
    Recipient.Status.QUEUED,
    Recipient.Status.SENDING,
    Recipient.Status.SKIPPED,
    Recipient.Status.CANCELLED,
)


def _gif():
    return private(HttpResponse(PIXEL_GIF, content_type="image/gif"))


@on_link_host(KIND_EMAIL)
@require_safe
def open_pixel(request, token):
    """klick.adx.se/o/<token>.gif (E.1, H.5, D.7): en indikation på att
    mejlet öppnats. Bara en mottagare som fick pixeln räknas (utskicket med
    Spåra öppningar och mottagaren med tracking_ok): opened_at sätts en gång
    och händelsen opened skrivs en gång per mottagare. Bilden svarar alltid
    likadant för en äkta token (en bildproxy ska aldrig se skillnad), och
    HEAD ändrar ingenting. Inga missar räknas här: bildproxyer delar adress
    med många mottagare."""
    from django.utils import timezone

    from .models import CHANNEL_EMAIL, Event

    recipient_id = tokens.read_pixel(token)
    if recipient_id is None:
        return private(not_found(request))
    if request.method == "HEAD":
        return _gif()
    recipient = (
        Recipient.objects.select_related("utskick")
        .filter(pk=recipient_id, channel=CHANNEL_EMAIL, tracking_ok=True)
        .exclude(status__in=_NOT_SENT)
        .first()
    )
    if recipient is not None and recipient.utskick.open_tracking:
        now = timezone.now()
        opened = Recipient.objects.filter(pk=recipient.pk, opened_at__isnull=True).update(
            opened_at=now
        )
        if opened and recipient.contact_id:
            Event.objects.create(
                account_id=recipient.utskick.account_id,
                contact_id=recipient.contact_id,
                kind=Event.OPENED,
                at=now,
                utskick_id=recipient.utskick_id,
                recipient_id=recipient.pk,
            )
    return _gif()


@on_link_host(KIND_EMAIL)
@require_safe
def calendar(request, token):
    """klick.adx.se/c/<token>.ics (F.1 element 14): händelseblocket som en
    iCalendar-fil (email.render.calendar_ics, det frysta mejlet när det
    finns). Ett block som saknas eller inte erbjuder kalendern ger samma
    sida som en gammal länk. Inget räknas."""
    from .email import render
    from .models import Utskick

    ref = tokens.read_calendar(token)
    if ref is None:
        return not_found(request)
    utskick = Utskick.objects.select_related("account__customer").filter(pk=ref.utskick_id).first()
    text = render.calendar_ics(utskick, ref.block_id) if utskick is not None else None
    if not text:
        return not_found(request)
    response = HttpResponse(text.encode("utf-8"), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="kalender.ics"'
    return private(response)
