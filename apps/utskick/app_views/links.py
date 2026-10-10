"""
Länkar (README I.1, I.11, E.1, E.3, E.8, J S4) och QR-koderna, också
anmälningssidans (I.7 Anmälan). Länk-byggaren i S4.

    link_list     utskick/lankar/                GET: kontots namngivna länkar
                                                 (klick.adx.se/<public_slug>/<slug>) och
                                                 utskickens personliga länkar, en rad per
                                                 utskick ("k.adx.se/a8Kf2X + 387 ·
                                                 personliga", till rapporten), med chipsen
                                                 Flamingo-sida, Skript finns och Extern
    link_new      utskick/lankar/ny/             GET formuläret, POST skapar: beskrivning,
                                                 en Flamingo-sida (kontots kampanj,
                                                 owned_ids) eller en adress
                                                 (links.clean_external, E.8) och slug
                                                 (links.clean_named_slug, ledig hos kontot;
                                                 tom: gjord av beskrivningen)
    link_detail   utskick/lankar/<pk>/           GET en namngiven länk: adressen med
                                                 Kopiera, QR-koden, klick och förfrågningar;
                                                 POST action=save (beskrivning och mål,
                                                 aldrig slugen) eller action=delete
    link_qr       utskick/lankar/<pk>/qr.<svg|png>   QR-koden för en namngiven länk
                                                 (?ladda=1 laddar ned)
    signup_qr     kontakter/anmalan/qr.<svg|png>     anmälningssidans QR-kod

En länk ur adressen hämtas med owned(TrackedLink, account, pk); bara
namngivna länkar (is_named) har en sida och en QR-kod, ett utskicks egna
länkar ger 404. Kampanjens id ur formuläret går genom owned_ids (ett
främmande id ger 400, H.1). En ny extern värd sparas som väntande och
byrån larmas (links.request_if_new, efter sparningen); tills byrån
godkänt den svarar adressen "Länken har gått ut" (links.destination_ok).
Demokontot visar sidorna men ändrar inget (D12). Byrån i kundvyn ändrar
på riktigt; vem som ändrade loggas.

Siffrorna: klick är mänskliga klickrader (Click, kind human), förfrågningar
är förfrågningar med länken i Lead.attribution. Båda räknas levande och
jämförs med summorna ticken räknat upp (links.rollup), som står kvar när
retentionen tagit klicken (E.7).
"""

import logging
import re
import unicodedata
from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, Min, OuterRef, Q, Sum
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_safe

from apps.flamingo.exports import landing_page_url
from apps.flamingo.models import Campaign, Lead

from .. import links, optin, qr
from ..access import actor_for, owned, owned_ids, utskick_view
from ..models import CHANNEL_EMAIL, Click, LinkCode, Recipient, TrackedLink, Utskick
from . import render_utskick
from .contacts import clean_name, day_text, group

logger = logging.getLogger(__name__)

#: Högst så många namngivna länkar per konto (listan visar alla).
MAX_NAMED = 200
#: Utskick per sida i listan över personliga länkar.
PER_PAGE = 20
#: Mål som visas per utskick; resten blir "och 2 till".
DESTINATIONS_SHOWN = 3
#: Hur lång målets adress får vara i listan.
SHORT_MAX = 60
#: Formulärets mål: en Flamingo-sida eller en egen adress.
TARGET_LP = "lp"
TARGET_EXTERNAL = "extern"
LABEL_MAX = 120

DEMO_TEXT = "Demokontot visar bara hur sidan ser ut. Inget här ändras eller skickas."
LABEL_TEXT = "Skriv vad länken är till för, till exempel Affisch i verkstaden."
TARGET_TEXT = "Välj vart länken ska gå."
CAMPAIGN_TEXT = "Välj en av dina Flamingo-sidor."
ADDRESS_TEXT = "Skriv adressen länken ska gå till."
SLUG_TAKEN_TEXT = "Adressen {text} finns redan. Välj en annan."
MAX_TEXT = f"Du kan ha högst {MAX_NAMED} länkar. Ta bort en du inte använder först."
NO_SLUG_TEXT = (
    "Adressen för anmälan är inte satt än. Be ADX sätta den, så går det att skapa länkar."
)
EXPLAINER = "Två klickdomäner: k.adx.se i sms (kort), klick.adx.se i mejl och QR-koder."

#: Målets chips (I.11): (rubrik, ton för .fl-ut-status--<ton>).
CHIP_LP = ("Flamingo-sida", "info")
CHIP_SCRIPT = ("Skript finns", "ok")
CHIP_EXTERNAL = ("Extern", "muted")


# ---------------------------------------------------------------------------
# Hjälp
# ---------------------------------------------------------------------------


def _host(url):
    try:
        return links.normalize_host(urlsplit(str(url or "")).hostname)
    except ValueError:
        return ""


def _cut(text):
    """Högst SHORT_MAX tecken; en avkortad text slutar med tre punkter, så
    att den inte ser ut som en hel adress."""
    if len(text) <= SHORT_MAX:
        return text
    return text[: SHORT_MAX - 3].rstrip("/-.") + "..."


def short_destination(url):
    """Målet som det står i listan: utan schema, www., fråga och fragment,
    högst SHORT_MAX tecken ("exempelror.example/boka"), avkortat med "..."."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return _cut(str(url or ""))
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path if parts.path not in ("", "/") else ""
    return _cut(f"{host}{path}") or _cut(str(url or ""))


def destination_chip(link, seen_domains):
    """(rubrik, ton): Flamingo-sida, Skript finns (en extern adress till en
    domän vars skript har setts) eller Extern."""
    if link.kind == TrackedLink.Kind.LP:
        return CHIP_LP
    host = _host(link.destination)
    if host and any(links._under(host, domain) for domain in seen_domains):
        return CHIP_SCRIPT
    return CHIP_EXTERNAL


def slug_from(text):
    """Ett förslag på slug ur beskrivningen: "Affisch i verkstaden" blir
    "affisch-i-verkstaden" (å, ä och ö utan prickar, högst 40 tecken)."""
    value = unicodedata.normalize("NFKD", str(text or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:40].strip("-")


def _lp_campaigns(account):
    """Flamingo-sidorna en länk kan gå till: kontots kampanjer med en
    publicerad sida (live eller pausad i Google), som i utskickens länkar."""
    return Campaign.objects.filter(
        account=account, status__in=(Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED)
    ).order_by("name")


def _named_or_404(account, pk):
    link = owned(TrackedLink, account, pk)
    if not link.is_named:
        raise Http404
    return link


def _live_counts(account, ids):
    """({länk: mänskliga klick}, {länk: förfrågningar}) för namngivna länkar."""
    if not ids:
        return {}, {}
    clicks = dict(
        Click.objects.filter(account=account, link_id__in=ids, kind=Click.Kind.HUMAN)
        .values("link_id")
        .annotate(n=Count("pk"))
        .values_list("link_id", "n")
    )
    leads = {}
    for value in (
        Lead.objects.filter(
            account=account,
            attribution__channel=Click.Channel.NAMED,
            attribution__link__in=ids,
        )
        .exclude(source=Lead.SOURCE_REPLY)
        .values_list("attribution__link", flat=True)
    ):
        if isinstance(value, int):
            leads[value] = leads.get(value, 0) + 1
    return clicks, leads


def _state_text(state):
    status, text = state
    return text if status != links.STATUS_ALLOWED else ""


def _named_row(link, public_slug, seen, states, clicks, leads):
    label, tone = destination_chip(link, seen)
    state = states.get(link.pk, (links.STATUS_ALLOWED, ""))
    return {
        "link": link,
        "text": links.named_link_text(link, public_slug),
        "url": links.named_link_url(link, public_slug),
        "goes": short_destination(link.destination),
        "chip_label": label,
        "chip_tone": tone,
        "state": state[0],
        "state_text": _state_text(state),
        "clicks": max(link.human_clicks, clicks.get(link.pk, 0)),
        # Förfrågningar syns bara för Flamingo-sidor: en annan sajts
        # formulär ser vi inte.
        "leads": max(link.leads, leads.get(link.pk, 0))
        if link.kind == TrackedLink.Kind.LP
        else None,
    }


def _personal_rows(items, seen):
    """En rad per utskick med personliga länkar: den första sms-koden och
    hur många till, målen med chips, klick och förfrågningar."""
    ids = [u.pk for u in items]
    if not ids:
        return []
    link_rows = {}
    for link in (
        TrackedLink.objects.filter(utskick_id__in=ids)
        .select_related("campaign")
        .order_by("utskick_id", "position", "pk")
    ):
        link_rows.setdefault(link.utskick_id, []).append(link)
    # "+ 387": en per mottagare (koderna är en per mottagare och länk, så
    # två länkar till 388 personer är 776 koder men 388 mottagare).
    code_counts = {
        row["link__utskick_id"]: row
        for row in LinkCode.objects.filter(link__utskick_id__in=ids, kind=LinkCode.Kind.LINK)
        .values("link__utskick_id")
        .annotate(n=Count("recipient", distinct=True), first=Min("pk"))
    }
    email_counts = dict(
        Recipient.objects.filter(utskick_id__in=ids, channel=CHANNEL_EMAIL)
        .exclude(status__in=(Recipient.Status.SKIPPED, Recipient.Status.CANCELLED))
        .values("utskick_id")
        .annotate(n=Count("pk"))
        .values_list("utskick_id", "n")
    )
    first_codes = dict(
        LinkCode.objects.filter(pk__in=[row["first"] for row in code_counts.values()]).values_list(
            "pk", "code"
        )
    )
    live_clicks = dict(
        Click.objects.filter(utskick_id__in=ids, kind=Click.Kind.HUMAN)
        .values("utskick_id")
        .annotate(n=Count("pk"))
        .values_list("utskick_id", "n")
    )
    rolled = dict(
        TrackedLink.objects.filter(utskick_id__in=ids)
        .values("utskick_id")
        .annotate(n=Sum("human_clicks"))
        .values_list("utskick_id", "n")
    )
    lead_counts = dict(
        Lead.objects.filter(utskick_id__in=ids)
        .exclude(source=Lead.SOURCE_REPLY)
        .values("utskick_id")
        .annotate(n=Count("pk"))
        .values_list("utskick_id", "n")
    )
    email_host = urlsplit(links.email_link_base()).netloc
    rows = []
    for utskick in items:
        tracked = link_rows.get(utskick.pk, [])
        codes = code_counts.get(utskick.pk)
        emails = int(email_counts.get(utskick.pk) or 0)
        if codes:
            code = links.sms_link(first_codes.get(codes["first"], ""))
            more = max(int(codes["n"]) - 1, 0)
        elif utskick.has_email:
            # Mejlens länkar är en token per mottagare, ingen kod att visa.
            code, more = f"{email_host}/m/...", 0
        else:
            code, more = links.sms_link(""), 0
        mail_text = f"i mejlet till {group(emails)} mottagare" if emails else "i mejlet"
        if codes and utskick.has_email:
            kind_text = f"personliga, sms och {mail_text}"
        elif utskick.has_sms and codes:
            kind_text = "personliga"
        elif utskick.has_email:
            kind_text = f"personlig länk {mail_text}"
        else:
            kind_text = "personliga"
        destinations = []
        for link in tracked[:DESTINATIONS_SHOWN]:
            label, tone = destination_chip(link, seen)
            destinations.append(
                {"text": short_destination(link.destination), "label": label, "tone": tone}
            )
        rows.append(
            {
                "utskick": utskick,
                "code": code,
                "more": more,
                "kind_text": kind_text,
                "destinations": destinations,
                "more_links": max(len(tracked) - DESTINATIONS_SHOWN, 0),
                "clicks": max(live_clicks.get(utskick.pk, 0), int(rolled.get(utskick.pk) or 0)),
                "leads": lead_counts.get(utskick.pk, 0),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Listan
# ---------------------------------------------------------------------------


@utskick_view
@require_safe
def link_list(request, account):
    public_slug = request.utskick_settings.public_slug
    seen = links.seen_snippet_domains(account.pk)
    named = list(
        TrackedLink.objects.filter(account=account, utskick__isnull=True)
        .exclude(slug="")
        .select_related("campaign")
        .order_by("-created_at", "-pk")[:MAX_NAMED]
    )
    states = links.destination_states(account, named)
    clicks, leads = _live_counts(account, [link.pk for link in named])
    named_rows = [_named_row(link, public_slug, seen, states, clicks, leads) for link in named]
    personal = (
        Utskick.objects.listed()
        .filter(account=account)
        .filter(
            Q(frozen_at__isnull=False) | Q(status__in=(Utskick.Status.SENDING, Utskick.Status.SENT))
        )
        .filter(Exists(TrackedLink.objects.filter(utskick=OuterRef("pk"))))
        .order_by("-created_at", "-pk")
    )
    page = Paginator(personal, PER_PAGE).get_page(request.GET.get("sida"))
    context = {
        "named_rows": named_rows,
        "personal_rows": _personal_rows(list(page.object_list), seen),
        "page": page,
        "public_slug": public_slug,
        "no_slug_text": NO_SLUG_TEXT,
        "explainer": EXPLAINER,
        "email_host": urlsplit(links.email_link_base()).netloc,
    }
    return render_utskick(request, "flamingo/app/utskick/links.html", "links", context)


# ---------------------------------------------------------------------------
# Formuläret (ny länk och en länks mål)
# ---------------------------------------------------------------------------


def _posted(request):
    return {
        "label": request.POST.get("beskrivning", ""),
        "target": request.POST.get("mal", "") or TARGET_LP,
        "campaign": request.POST.get("kampanj", ""),
        "address": request.POST.get("adress", ""),
        "slug": request.POST.get("slug", ""),
    }


def _clean_target(account, values, errors):
    """(kind, campaign, destination) ur formuläret, eller (None, None, "")
    med errors ifyllt. Ett främmande kampanj-id ger ForeignIds (400)."""
    target = values["target"]
    if target == TARGET_LP:
        raw = str(values["campaign"] or "").strip()
        if not raw:
            errors["kampanj"] = CAMPAIGN_TEXT
            return None, None, ""
        pk = owned_ids(Campaign, account, [raw], limit=1)[0]
        campaign = _lp_campaigns(account).filter(pk=pk).first()
        if campaign is None:
            errors["kampanj"] = CAMPAIGN_TEXT
            return None, None, ""
        return TrackedLink.Kind.LP, campaign, landing_page_url(campaign)[: links.URL_MAX]
    if target == TARGET_EXTERNAL:
        raw = str(values["address"] or "").strip()
        if not raw:
            errors["adress"] = ADDRESS_TEXT
            return None, None, ""
        try:
            cleaned = links.clean_external(account, raw, allow_pending=True)
        except links.LinkRefused as exc:
            errors["adress"] = str(exc)
            return None, None, ""
        return TrackedLink.Kind.EXTERNAL, None, cleaned[: links.URL_MAX]
    errors["mal"] = TARGET_TEXT
    return None, None, ""


def _after_save(request, account, link):
    """Efter sparningen, utanför transaktionen: en ny värd blir väntande och
    byrån larmas (ett mejl till byrån, aldrig till kunden)."""
    if link.kind != TrackedLink.Kind.EXTERNAL:
        return
    user = request.user if request.user.is_authenticated else None
    links.request_if_new(account, link.destination, user)
    status = links.destination_states(account, [link]).get(link.pk, ("", ""))[0]
    if status == links.STATUS_PENDING:
        messages.info(request, links.PENDING_TEXT)


def _log(request, account, link, what):
    actor = actor_for(request)
    logger.info(
        "Utskick: namngiven länk %s %s (konto %s, användare %s%s)",
        link.pk,
        what,
        account.pk,
        getattr(actor.user, "pk", None),
        ", byrån i kundvyn" if actor.staff else "",
    )


def _form_context(account, row, values, errors):
    public_slug = row.public_slug
    prefix = f"{urlsplit(links.email_link_base()).netloc}/{public_slug}/"
    return {
        "values": values,
        "errors": errors,
        "campaigns": _lp_campaigns(account),
        "prefix": prefix,
        "public_slug": public_slug,
        "no_slug_text": NO_SLUG_TEXT,
        "target_lp": TARGET_LP,
        "target_external": TARGET_EXTERNAL,
        "suggested_slug": slug_from(values.get("label")),
    }


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def link_new(request, account):
    row = request.utskick_settings
    values = {"label": "", "target": TARGET_LP, "campaign": "", "address": "", "slug": ""}
    errors = {}
    if request.method == "POST":
        if account.is_demo:
            messages.info(request, DEMO_TEXT)
            return redirect("flamingo:app_links")
        values = _posted(request)
        link = _create(request, account, row, values, errors)
        if link is not None:
            _log(request, account, link, "skapad")
            messages.success(request, "Länken är skapad.")
            _after_save(request, account, link)
            return redirect("flamingo:app_link", link.pk)
    context = _form_context(account, row, values, errors)
    status = 400 if errors else 200
    return render_utskick(
        request, "flamingo/app/utskick/link_form.html", "links", context, status=status
    )


def _create(request, account, row, values, errors):
    """Den nya länken, eller None med errors ifyllt."""
    if not row.public_slug:
        errors["form"] = NO_SLUG_TEXT
        return None
    label = clean_name(values["label"], LABEL_MAX)
    values["label"] = label
    if not label:
        errors["beskrivning"] = LABEL_TEXT
    raw_slug = str(values["slug"] or "").strip() or slug_from(label)
    slug = ""
    try:
        slug = links.clean_named_slug(raw_slug)
    except links.LinkRefused as exc:
        if raw_slug or label:
            errors["slug"] = str(exc)
    if slug:
        values["slug"] = slug
        if TrackedLink.objects.filter(account=account, slug=slug).exists():
            errors["slug"] = _taken_text(row, slug)
    kind, campaign, destination = _clean_target(account, values, errors)
    if errors:
        return None
    if TrackedLink.objects.filter(account=account, utskick__isnull=True).count() >= MAX_NAMED:
        errors["form"] = MAX_TEXT
        return None
    try:
        with transaction.atomic():
            return TrackedLink.objects.create(
                account=account,
                utskick=None,
                kind=kind,
                campaign=campaign,
                destination=destination,
                label=label,
                slug=slug,
            )
    except IntegrityError:
        errors["slug"] = _taken_text(row, slug)
        return None


def _taken_text(row, slug):
    host = urlsplit(links.email_link_base()).netloc
    return SLUG_TAKEN_TEXT.format(text=f"{host}/{row.public_slug}/{slug}")


# ---------------------------------------------------------------------------
# En länk
# ---------------------------------------------------------------------------


def _numbers(account, link, seen, now):
    """Siffrorna på länkens sida."""
    clicks, leads = _live_counts(account, [link.pk])
    human = Click.objects.filter(link=link, kind=Click.Kind.HUMAN)
    last = Click.objects.filter(link=link).order_by("-at").values_list("at", flat=True).first()
    chip = destination_chip(link, seen)
    numbers = {
        "clicks": max(link.human_clicks, clicks.get(link.pk, 0)),
        "week": human.filter(at__gte=now - timedelta(days=7)).count(),
        "last": day_text(last, now) if last else "",
        "leads": None,
        "visits": None,
    }
    if link.kind == TrackedLink.Kind.LP:
        numbers["leads"] = max(link.leads, leads.get(link.pk, 0))
        numbers["visits"] = human.filter(lp_visits__gt=0).count()
    elif chip == CHIP_SCRIPT:
        numbers["visits"] = human.filter(lp_visits__gt=0).count()
    return numbers


def _current_values(link):
    return {
        "label": link.label,
        "target": TARGET_LP if link.kind == TrackedLink.Kind.LP else TARGET_EXTERNAL,
        "campaign": str(link.campaign_id or ""),
        "address": link.destination if link.kind == TrackedLink.Kind.EXTERNAL else "",
        "slug": link.slug,
    }


def _save(request, account, link, values, errors):
    """Beskrivningen och målet (aldrig slugen: den kan stå på affischer)."""
    label = clean_name(values["label"], LABEL_MAX)
    values["label"] = label
    if not label:
        errors["beskrivning"] = LABEL_TEXT
    kind, campaign, destination = _clean_target(account, values, errors)
    if errors:
        return False
    TrackedLink.objects.filter(pk=link.pk, account=account, utskick__isnull=True).update(
        label=label, kind=kind, campaign=campaign, destination=destination
    )
    link.label, link.kind, link.campaign, link.destination = label, kind, campaign, destination
    return True


@utskick_view
@require_http_methods(["GET", "HEAD", "POST"])
def link_detail(request, account, pk):
    link = _named_or_404(account, pk)
    row = request.utskick_settings
    now = timezone.now()
    values = _current_values(link)
    errors = {}
    if request.method == "POST":
        if account.is_demo:
            messages.info(request, DEMO_TEXT)
            return redirect("flamingo:app_link", link.pk)
        action = request.POST.get("action", "")
        if action == "delete":
            text = links.named_link_text(link, row.public_slug)
            _log(request, account, link, "borttagen")
            link.delete()
            messages.success(request, f"Länken {text} är borttagen.")
            return redirect("flamingo:app_links")
        if action == "save":
            values = _posted(request)
            values["slug"] = link.slug
            if _save(request, account, link, values, errors):
                _log(request, account, link, "ändrad")
                messages.success(request, "Länken är sparad.")
                _after_save(request, account, link)
                return redirect("flamingo:app_link", link.pk)
        else:
            return redirect("flamingo:app_link", link.pk)
    seen = links.seen_snippet_domains(account.pk)
    state = links.destination_states(account, [link]).get(link.pk, (links.STATUS_ALLOWED, ""))
    chip = destination_chip(link, seen)
    context = {
        "link": link,
        "text": links.named_link_text(link, row.public_slug),
        "url": links.named_link_url(link, row.public_slug),
        "goes": short_destination(link.destination),
        # Länkens egen sida visar hela målet (listan kortar av det).
        "goes_full": link.destination,
        "chip_label": chip[0],
        "chip_tone": chip[1],
        "state": state[0],
        "state_text": _state_text(state),
        "numbers": _numbers(account, link, seen, now),
        "qr_svg": reverse("flamingo:app_link_qr", args=[link.pk, "svg"]),
        "qr_png": reverse("flamingo:app_link_qr", args=[link.pk, "png"]),
        **_form_context(account, row, values, errors),
    }
    status = 400 if errors else 200
    return render_utskick(
        request, "flamingo/app/utskick/link.html", "links", context, status=status
    )


# ---------------------------------------------------------------------------
# QR-koderna
# ---------------------------------------------------------------------------


def _download(request):
    return request.GET.get("ladda") == "1"


@utskick_view
@require_safe
def link_qr(request, account, pk, fmt):
    """QR-koden för en namngiven länk: hela adressen med https (qr.py)."""
    link = _named_or_404(account, pk)
    public_slug = request.utskick_settings.public_slug
    if not public_slug:
        raise Http404
    return qr.response(
        links.named_link_url(link, public_slug),
        fmt,
        filename=f"qr-{public_slug}-{link.slug}",
        download=_download(request),
    )


@utskick_view
@require_safe
def signup_qr(request, account, fmt):
    """Anmälningssidans QR-kod: adx.se/utskick/<public_slug>/ (SITE_BASE_URL).
    Går att hämta också när sidan är stängd, så att den kan tryckas innan."""
    public_slug = request.utskick_settings.public_slug
    if not public_slug:
        raise Http404
    url = optin.absolute(reverse("utskick_public:signup", args=[public_slug]))
    return qr.response(url, fmt, filename=f"qr-anmalan-{public_slug}", download=_download(request))
