"""
Rapportens siffror (README I.8, S2-delen; S4 bygger ut med tratten, klick
per timme, länktabellen, beteendet på landningssidan och exporten, se
avsnittet "S4" sist i filen).

    summary(utskick, now=None) -> dict      rapportens rutor och rader, levande
    final_stats(utskick) -> dict            det som sparas i Utskick.stats när
                                            utskicket blir klart (ticken anropar
                                            den i fas 9) och finns kvar efter
                                            retentionen av mottagarna (E.7)
    list_numbers(rows) -> {pk: dict}        listans kolumner för en sida utskick
    recipients_for(utskick, view) -> QuerySet   mottagarna bakom en siffra (?visa=)

Bara mänskliga klick räknas (Recipient.click_count och first_clicked_at,
E.3). Förfrågningar är kontots förfrågningar med Lead.utskick satt, utom
svarstrådarnas egna rader (source "reply"); en förfrågan efter mer än 30
dagar (attribution["late"]) räknas för sig ("+1 senare"). Kostnaden är
summan av SmsMessage.customer_price för mottagarnas sms, i tiotusendels
krona (apps.sms.models.UNITS_PER_KR).

När retentionen tagit mottagarna (13 månader efter att utskicket blev klart)
läser summary Utskick.stats i stället, med samma nycklar.
"""

from datetime import timedelta
from urllib.parse import urlsplit

from django.db.models import Count, Exists, Max, OuterRef, Q, Sum
from django.utils import timezone

from apps.flamingo.models import Lead
from apps.sms.models import UNITS_PER_KR, SmsMessage
from apps.sms.pricing import STOCKHOLM

from .models import CHANNEL_EMAIL, CHANNEL_SMS, Click, Recipient, TrackedLink

R = Recipient.Status

#: Nycklarna summary och final_stats delar (heltal).
COUNT_KEYS = (
    "total",
    "skipped",
    "queued",
    "sent",
    "attempted",
    "delivered",
    "failed",
    "unknown",
    "cancelled",
    "clicked",
    "clicks",
    "engaged",
    "replied",
    "stopped",
    "parts",
    "simulated",
    "leads",
    "leads_late",
    "cost_units",
)

#: ?visa= på mottagarsidan: (rubrik, filter). "hoppades-over-<orsak>" läggs
#: till av recipients_for.
VIEWS = {
    "alla": "Alla mottagare",
    "levererade": "Levererade",
    "misslyckade": "Gick inte fram",
    "kvar": "Inte skickade än",
    "klickade": "Klickade",
    "klickade-inte": "Klickade inte",
    "stannade": "Stannade 30 s eller mer",
    "svarade": "Svarade",
    "stopp": "Avregistrerade",
    "forfragan": "Skickade en förfrågan",
    "hoppades-over": "Hoppades över",
    "avbrutna": "Avbrutna",
    # S4 (rapport-byggaren): trattens första steg, e-postens tal och
    # landningssidan (I.8).
    "skickade": "Skickade",
    "studsade": "Studsade",
    "klagomal": "Markerade som skräppost",
    "oppnade": "Öppnade (indikation)",
    "besokte": "Besökte landningssidan",
    "ringde": "Ringde från landningssidan",
    "formular": "Skickade formuläret",
}
SKIPPED_PREFIX = "hoppades-over-"
#: Orsaker vars värde i databasen inte ska stå i adressfältet: värdet
#: nämner e-posttjänstens leverantör (Giovanni 2026-10-10). Det gamla värdet
#: fungerar ändå i ?visa=, så att sparade länkar inte går sönder.
SKIP_SLUGS = {Recipient.SkipReason.SES_SUPPRESSED: "sparrad-hos-e-posttjansten"}


def skipped_view(reason):
    """?visa= för mottagarna som hoppades över av orsaken."""
    return SKIPPED_PREFIX + SKIP_SLUGS.get(reason, reason)


def skipped_reason(view):
    """Orsaken i en vy "hoppades-over-<orsak>", eller None för en okänd."""
    slug = view[len(SKIPPED_PREFIX) :]
    reason = next((str(r) for r, s in SKIP_SLUGS.items() if s == slug), slug)
    return reason if reason in Recipient.SkipReason.values else None


#: S4: "lank-<id>" klickade på länken, "lank-<id>-forfragan" förfrågan via den.
LINK_PREFIX = "lank-"
LINK_LEADS_SUFFIX = "-forfragan"
#: S4: vyer som gäller alla kanaler, vilken kanal sidan än visar (talen i
#: rapporten räknar dem så).
CHANNEL_FREE = ("besokte", "ringde", "formular")
#: Skickade: allt som gick i väg, också det som inte kom fram.
ATTEMPTED = (*Recipient.SENT_LIKE, R.FAILED)


def _leads(utskick):
    return Lead.objects.filter(account_id=utskick.account_id, utskick=utskick).exclude(
        source=Lead.SOURCE_REPLY
    )


def _lead_exists(**filters):
    """En förfrågan via mottagaren (inte svarstrådarnas egna). filters
    (S4) smalnar av: source="form", attribution__link=<id>."""
    return Exists(
        Lead.objects.filter(utskick_recipient=OuterRef("pk"), **filters).exclude(
            source=Lead.SOURCE_REPLY
        )
    )


def _engaged_exists():
    return Exists(
        Click.objects.filter(
            recipient=OuterRef("pk"), kind=Click.Kind.HUMAN, engaged_seconds__gte=30
        )
    )


def _counts(utskick):
    rows = Recipient.objects.filter(utskick=utskick)
    agg = rows.aggregate(
        total=Count("pk", filter=~Q(status=R.SKIPPED)),
        skipped=Count("pk", filter=Q(status=R.SKIPPED)),
        queued=Count("pk", filter=Q(status__in=(R.QUEUED, R.SENDING))),
        sent=Count("pk", filter=Q(status__in=Recipient.SENT_LIKE)),
        attempted=Count("pk", filter=Q(status__in=(*Recipient.SENT_LIKE, R.FAILED))),
        delivered=Count("pk", filter=Q(status=R.DELIVERED)),
        failed=Count("pk", filter=Q(status__in=(R.FAILED, R.BOUNCED))),
        unknown=Count("pk", filter=Q(status=R.UNKNOWN)),
        cancelled=Count("pk", filter=Q(status=R.CANCELLED)),
        clicked=Count("pk", filter=Q(first_clicked_at__isnull=False)),
        clicks=Sum("click_count"),
        replied=Count("pk", filter=Q(replied_at__isnull=False)),
        stopped=Count("pk", filter=Q(stopped_at__isnull=False)),
        parts=Sum("parts"),
        simulated=Count("pk", filter=Q(simulated=True)),
    )
    out = {key: int(agg.get(key) or 0) for key in agg}
    out["engaged"] = rows.filter(_engaged_exists()).count() if out["clicked"] else 0
    reasons = (
        rows.filter(status=R.SKIPPED)
        .values("skip_reason")
        .annotate(n=Count("pk"))
        .order_by("-n", "skip_reason")
    )
    out["skipped_by_reason"] = {r["skip_reason"] or "": r["n"] for r in reasons}
    messages = SmsMessage.objects.filter(
        pk__in=rows.exclude(sms_message=None).values("sms_message")
    )
    out["cost_units"] = int(messages.aggregate(s=Sum("customer_price"))["s"] or 0)
    leads = _leads(utskick)
    # exclude() på en JSON-nyckel tappar raderna utan nyckeln (NULL i SQL):
    # räkna de sena och dra av dem.
    late = leads.filter(attribution__late=True).count()
    out["leads"] = leads.count() - late
    out["leads_late"] = late
    return out


def _pct(part, whole):
    """Andelen i procent med en decimal (procent-filtret skriver den), eller None."""
    if not whole:
        return None
    return round(100 * part / whole, 1)


def _derived(numbers):
    """Det rapportens rutor visar utöver de räknade talen."""
    sent = numbers["sent"]
    # Andelen levererade av alla som försöktes, de som inte gick fram inräknade.
    attempted = numbers.get("attempted") or sent
    delivered = numbers["delivered"]
    leads = numbers["leads"] + numbers["leads_late"]
    cost_kr = numbers["cost_units"] / UNITS_PER_KR
    return {
        "delivered_pct": _pct(delivered, attempted),
        "click_pct": _pct(numbers["clicked"], delivered or sent),
        "cost_kr": round(cost_kr),
        "kr_per_lead": round(cost_kr / leads, 1) if leads and numbers["cost_units"] else None,
    }


def summary(utskick, now=None):
    """Rapportens siffror. Läser mottagarna; finns de inte längre
    (retentionen) läses Utskick.stats. from_stats säger vilket."""
    now = now or timezone.now()
    has_rows = Recipient.objects.filter(utskick=utskick).exists()
    stats = utskick.stats if isinstance(utskick.stats, dict) else {}
    if not has_rows and stats.get("total"):
        numbers = {key: int(stats.get(key) or 0) for key in COUNT_KEYS}
        numbers["skipped_by_reason"] = dict(stats.get("skipped_by_reason") or {})
        numbers["from_stats"] = True
    else:
        numbers = _counts(utskick)
        numbers["from_stats"] = False
    numbers.update(_derived(numbers))
    numbers["has_rows"] = has_rows
    return numbers


def final_stats(utskick):
    """Summorna som sparas i Utskick.stats när utskicket är klart (fas 9).
    Bara tal och orsaker: inga adresser, inga namn."""
    numbers = _counts(utskick)
    stats = {key: int(numbers.get(key) or 0) for key in COUNT_KEYS}
    stats["skipped_by_reason"] = dict(numbers["skipped_by_reason"])
    stats["at"] = timezone.now().isoformat()
    return stats


def list_numbers(rows):
    """Listans kolumner (Mottagare, Klick, Svar, Förfrågningar) för en sida
    utskick, med två frågor. Ett utskick utan mottagare (utkast,
    schemalagt, efter retentionen) får talen ur stats, annars inga."""
    ids = [u.pk for u in rows]
    if not ids:
        return {}
    by_utskick = {
        row["utskick"]: row
        for row in Recipient.objects.filter(utskick_id__in=ids)
        .values("utskick")
        .annotate(
            total=Count("pk", filter=~Q(status=R.SKIPPED)),
            delivered=Count("pk", filter=Q(status=R.DELIVERED)),
            sent=Count("pk", filter=Q(status__in=Recipient.SENT_LIKE)),
            clicked=Count("pk", filter=Q(first_clicked_at__isnull=False)),
            replied=Count("pk", filter=Q(replied_at__isnull=False)),
        )
    }
    leads = {
        row["utskick"]: row["n"]
        for row in Lead.objects.filter(utskick_id__in=ids)
        .exclude(source=Lead.SOURCE_REPLY)
        .values("utskick")
        .annotate(n=Count("pk"))
    }
    out = {}
    for utskick in rows:
        row = by_utskick.get(utskick.pk)
        if row is None:
            stats = utskick.stats if isinstance(utskick.stats, dict) else {}
            if not stats.get("total"):
                out[utskick.pk] = None
                continue
            row = {key: int(stats.get(key) or 0) for key in COUNT_KEYS}
        out[utskick.pk] = {
            "recipients": row["total"],
            "clicked": row["clicked"],
            "click_pct": _pct(row["clicked"], row["delivered"] or row["sent"]),
            "replied": row["replied"],
            "leads": leads.get(utskick.pk, 0),
        }
    return out


def recipients_for(utskick, view, channel=None):
    """Mottagarna bakom siffran ?visa=view (VIEWS, "hoppades-over-<orsak>",
    och från S4 "lank-<id>" och "lank-<id>-forfragan"), med det tabellen
    läser: kontakten, längsta tid på sidan (mänskliga klick) och om en
    förfrågan kom. None för en okänd vy.

    channel (S4) är "sms", "email" eller None för alla kanaler, som
    summary räknar. Vyerna om landningssidan och länkarna (CHANNEL_FREE)
    gäller alla kanaler, som deras tal i rapporten."""
    rows = Recipient.objects.filter(utskick=utskick)
    q = view_q(utskick, view, channel)
    if q is None:
        return None
    if channel in (CHANNEL_SMS, CHANNEL_EMAIL) and not _channel_free(view):
        rows = rows.filter(channel=channel)
    return (
        rows.filter(q)
        .select_related("contact")
        .annotate(
            engaged=Max(
                "clicks__engaged_seconds", filter=Q(clicks__kind=Click.Kind.HUMAN), default=0
            ),
            has_lead=_lead_exists(),
        )
        .order_by("pk")
    )


def view_q(utskick, view, channel=None):
    """Villkoret för vyn på en mottagare i utskicket, eller None för en
    okänd vy (S4 bröt ut det ur recipients_for så att talen och listorna
    räknas med samma villkor)."""
    view = str(view or "")
    if view.startswith(SKIPPED_PREFIX):
        reason = skipped_reason(view)
        if reason is None:
            return None
        return Q(status=R.SKIPPED, skip_reason=reason)
    if view.startswith(LINK_PREFIX):
        return _link_q(utskick, view)
    if view == "alla":
        return ~Q(status=R.SKIPPED)
    if view == "skickade":
        return Q(status__in=ATTEMPTED)
    if view == "levererade":
        return delivered_q(channel)
    if view == "misslyckade":
        return Q(status__in=(R.FAILED, R.BOUNCED))
    if view == "kvar":
        return Q(status__in=(R.QUEUED, R.SENDING))
    if view == "klickade":
        return Q(first_clicked_at__isnull=False)
    if view == "klickade-inte":
        return delivered_q(channel) & Q(first_clicked_at__isnull=True)
    if view == "stannade":
        return Q(_engaged_exists())
    if view == "svarade":
        return Q(replied_at__isnull=False)
    if view == "stopp":
        if channel == CHANNEL_EMAIL:
            # Ett klagomål avregistrerar adressen (inbound/events.py).
            return Q(stopped_at__isnull=False) | Q(status=R.COMPLAINED)
        return Q(stopped_at__isnull=False)
    if view == "forfragan":
        return Q(_lead_exists())
    if view == "hoppades-over":
        return Q(status=R.SKIPPED)
    if view == "avbrutna":
        return Q(status=R.CANCELLED)
    # --- S4 (rapport-byggaren): e-postens tal och landningssidan (I.8) ---
    if view == "studsade":
        return Q(status=R.BOUNCED)
    if view == "klagomal":
        return Q(status=R.COMPLAINED)
    if view == "oppnade":
        return Q(opened_at__isnull=False)
    if view == "besokte":
        return Q(_lp_click_exists(lp_visits__gt=0))
    if view == "ringde":
        return Q(_lp_click_exists(called=True))
    if view == "formular":
        return Q(_lead_exists(source=Lead.SOURCE_FORM))
    # --- slut S4
    return None


def view_label(view, utskick=None):
    if view.startswith(SKIPPED_PREFIX):
        reason = skipped_reason(view)
        if reason is None:
            return ""
        return "Hoppades över: " + Recipient.SkipReason(reason).label.lower()
    # S4: en länks egna vyer heter efter länken.
    if view.startswith(LINK_PREFIX) and utskick is not None:
        link, leads = _view_link(utskick, view)
        if link is None:
            return ""
        name = link_name(link)
        return f"Förfrågan via {name}" if leads else f"Klickade på {name}"
    return VIEWS.get(view, "")


# ---------------------------------------------------------------------------
# S3 (redigerar-byggaren): e-postens siffror i rapporten (README I.8)
# ---------------------------------------------------------------------------


def email_numbers(utskick):
    """Mejlens siffror: mottagare, levererade, öppnade (en indikation, bara
    mottagare med pixeln), klick, studsar, klagomål och avregistreringar
    (spärrar med utskicket på e-post, utom studsarnas: en adress som inte
    finns har inte avregistrerat sig, den står under Studsar), med
    andelarna. Tom dict när utskicket inte har några e-postmottagare."""
    from .models import Suppression

    rows = Recipient.objects.filter(utskick=utskick, channel=CHANNEL_EMAIL)
    agg = rows.aggregate(
        total=Count("pk", filter=~Q(status=R.SKIPPED)),
        skipped=Count("pk", filter=Q(status=R.SKIPPED)),
        sent=Count("pk", filter=Q(status__in=Recipient.SENT_LIKE)),
        attempted=Count("pk", filter=Q(status__in=(*Recipient.SENT_LIKE, R.FAILED, R.BOUNCED))),
        delivered=Count("pk", filter=Q(status__in=(R.DELIVERED, R.COMPLAINED))),
        bounced=Count("pk", filter=Q(status=R.BOUNCED)),
        complained=Count("pk", filter=Q(status=R.COMPLAINED)),
        opened=Count("pk", filter=Q(opened_at__isnull=False)),
        clicked=Count("pk", filter=Q(first_clicked_at__isnull=False)),
    )
    out = {key: int(value or 0) for key, value in agg.items()}
    if not out["total"] and not out["skipped"]:
        return {}
    out["unsubscribed"] = (
        Suppression.objects.filter(utskick=utskick, channel=CHANNEL_EMAIL)
        .exclude(reason=Suppression.Reason.BOUNCE)
        .count()
    )
    base = out["delivered"] or out["sent"]
    out["delivered_pct"] = _pct(out["delivered"], out["attempted"] or out["sent"])
    out["opened_pct"] = _pct(out["opened"], base)
    out["click_pct"] = _pct(out["clicked"], base)
    out["bounced_pct"] = _pct(out["bounced"], out["attempted"] or out["sent"])
    return out


# ---------------------------------------------------------------------------
# S4 (rapport-byggaren): hela rapporten (README I.8, J S4)
#
# Varje tal bygger på samma rader som summary (bara mänskliga klick,
# förfrågningar utan svarstrådarnas egna) och varje siffra räknas med samma
# villkor som listan den leder till (view_q och recipients_for, ?visa= och
# ?kanal=), så att talet och listans längd är lika. När retentionen tagit
# mottagarna och klicken (13 månader) räknas tratten ur Utskick.stats och
# leder ingenstans; diagrammet, länkarnas listor och landningssidan saknas
# då (länkarna visar TrackedLink-summorna).
#
#   FUNNEL_STEPS                            (nyckel, rubrik, ?visa=) i trattens ordning
#   funnel(utskick, numbers=None, channel=None) -> list[dict]
#                                           Skickade, Levererade, Klickade,
#                                           Stannade 30 s+, Förfrågan:
#                                           {"key", "label", "n", "pct", "width",
#                                            "visa", "kanal"}
#   clicks_per_hour(utskick, now=None, hours=CHART_HOURS) -> list[dict]
#                                           [{"start": datetime, "n": int}] från
#                                           starten, en per timme; [] utan klick
#   chart(points) -> dict                   staplarna som tal till mallens SVG
#   per_link(utskick) -> list[dict]         {"link", "label", "destination", "kind",
#                                            "clicks", "leads", "visa", "visa_leads"}
#   lp_behaviour(utskick) -> dict           besök, mediantid, andel i mobil, ringde,
#                                           formulär ("På landningssidan: mediantid
#                                           1 min 52 s · 91 % i mobil"), {} utan besök
#   follow_up_count(utskick) -> int         kontakter som fick utskicket men inte klickade
#   full(utskick, now=None, numbers=None) -> dict
#                                           allt ovan (vyns enda anrop)
#   EXPORT_HEADER, export_rows(utskick)     CSV-exporten (app_utskick_export): varje
#                                           cell genom flamingo.exports.safe_cell, inga
#                                           adresser för borttagna kontakter
#   channel_from(raw), channel_param(channel), channels_of(utskick)
#                                           ?kanal= ("sms", "e-post") och kanalerna
# ---------------------------------------------------------------------------

#: Trattens steg (I.8): (nyckel i Utskick.stats, rubrik, ?visa= på mottagarsidan).
FUNNEL_STEPS = (
    ("attempted", "Skickade", "skickade"),
    ("delivered", "Levererade", "levererade"),
    ("clicked", "Klickade", "klickade"),
    ("engaged", "Stannade 30 s+", "stannade"),
    ("leads", "Förfrågan", "forfragan"),
)
#: Trattens steg som bara finns när utskicket har länkar.
LINK_STEPS = ("clicked", "engaged")
#: Klick per timme visas för högst så många timmar från starten, och minst
#: CHART_MIN_HOURS (tomma timmar efter det sista klicket tas bort).
CHART_HOURS = 48
CHART_MIN_HOURS = 12
#: Diagrammets rityta (viewBox). Bredden kommer från css (width:100%).
CHART_WIDTH = 480
CHART_HEIGHT = 120
#: ?kanal= på mottagarsidan; utan den visas alla kanaler (som summary räknar).
CHANNEL_PARAMS = {"sms": CHANNEL_SMS, "e-post": CHANNEL_EMAIL}
CHANNEL_LABELS = {CHANNEL_SMS: "Sms", CHANNEL_EMAIL: "E-post"}
#: Kolumnerna i CSV-exporten (I.8, H.3).
EXPORT_HEADER = (
    "Namn",
    "Kanal",
    "Adress",
    "Läge",
    "Skickat",
    "Levererat",
    "Klick",
    "Tid på sidan (s)",
    "Svarade",
    "Förfrågan",
)
DELETED_NAME = "Borttagen kontakt"
_MONTHS = ("jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec")


# --- Kanalen och vyerna ------------------------------------------------------


def channel_from(raw):
    """?kanal= som kanal ("sms", "email"), annars None: alla kanaler."""
    return CHANNEL_PARAMS.get(str(raw or "").strip())


def channel_param(channel):
    """Kanalen som ?kanal= skriver den, "" för alla."""
    for key, value in CHANNEL_PARAMS.items():
        if value == channel:
            return key
    return ""


def channels_of(utskick):
    """Kanalerna som utskicket har mottagare i (hoppade över oräknade), sms först."""
    found = set(
        Recipient.objects.filter(utskick=utskick)
        .exclude(status=R.SKIPPED)
        .values_list("channel", flat=True)
        .distinct()
    )
    return [channel for channel in (CHANNEL_SMS, CHANNEL_EMAIL) if channel in found]


def delivered_q(channel=None):
    """Levererade. För e-post räknas klagomålen med (mejlet kom fram), som
    email_numbers; för sms och alla kanaler bara delivered, som summary."""
    if channel == CHANNEL_EMAIL:
        return Q(status__in=(R.DELIVERED, R.COMPLAINED))
    return Q(status=R.DELIVERED)


def _channel_free(view):
    return view in CHANNEL_FREE or str(view).startswith(LINK_PREFIX)


def _lp_click_exists(human=True, **filters):
    """Ett klick från mottagaren på utskickets länk till en Flamingo-sida."""
    clicks = Click.objects.filter(
        recipient=OuterRef("pk"), link__kind=TrackedLink.Kind.LP, **filters
    )
    if human:
        clicks = clicks.filter(kind=Click.Kind.HUMAN)
    return Exists(clicks)


def link_view(link, leads=False):
    """?visa= för länkens klick ("lank-12") eller förfrågningar ("lank-12-forfragan")."""
    return f"{LINK_PREFIX}{link.pk}{LINK_LEADS_SUFFIX if leads else ''}"


def _view_link(utskick, view):
    """(länken, förfrågningar?) för "lank-<id>" och "lank-<id>-forfragan".
    Bara utskickets egna länkar; (None, False) annars."""
    rest = str(view)[len(LINK_PREFIX) :]
    leads = rest.endswith(LINK_LEADS_SUFFIX)
    if leads:
        rest = rest[: -len(LINK_LEADS_SUFFIX)]
    if not rest.isdigit() or len(rest) > 12:
        return None, False
    link = (
        TrackedLink.objects.filter(pk=int(rest), utskick=utskick, account_id=utskick.account_id)
        .select_related("campaign")
        .first()
    )
    return link, leads


def _link_q(utskick, view):
    link, leads = _view_link(utskick, view)
    if link is None:
        return None
    if leads:
        return Q(_lead_exists(attribution__link=link.pk))
    return Q(
        Exists(
            Click.objects.filter(recipient=OuterRef("pk"), link_id=link.pk, kind=Click.Kind.HUMAN)
        )
    )


def destination_text(link):
    """Målet kort: "/lp/vinterdack" för en Flamingo-sida, annars värd och
    sökväg ("exempelror.example/boka"), högst 60 tecken."""
    raw = link.destination or ""
    if not raw and link.campaign_id:
        raw = link.campaign.landing_url or ""
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if link.kind == TrackedLink.Kind.LP:
        text = path or "/"
    else:
        host = parts.netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        text = f"{host}{path}"
    return text[:60]


def link_name(link):
    """Länkens namn i rapporten: etiketten, annars målet."""
    return (link.label or "").strip() or destination_text(link)


# --- Tratten -----------------------------------------------------------------


def _width(n, first):
    """Stapelns bredd i procent av det första steget (minst 1 när n > 0)."""
    if not first or not n:
        return 0
    return max(1, min(100, round(100 * n / first)))


def funnel(utskick, numbers=None, channel=None):
    """Trattens steg med antal och andel av det första steget (Skickade).
    numbers är summary(utskick) när vyn redan har den; channel en kanal ur
    mottagarna (None: alla). Varje tal räknas med listans villkor (view_q),
    så att det leder till exakt de mottagarna. Utan mottagare kvar
    (retentionen) räknas stegen ur Utskick.stats och leder ingenstans.
    [] när inget har skickats."""
    numbers = numbers if numbers is not None else summary(utskick)
    if numbers.get("from_stats"):
        values = {
            "attempted": numbers.get("attempted") or numbers.get("sent"),
            "delivered": numbers.get("delivered"),
            "clicked": numbers.get("clicked"),
            "engaged": numbers.get("engaged"),
            "leads": int(numbers.get("leads") or 0) + int(numbers.get("leads_late") or 0),
        }
        linked = False
    elif not numbers.get("has_rows"):
        return []
    else:
        rows = Recipient.objects.filter(utskick=utskick)
        if channel in (CHANNEL_SMS, CHANNEL_EMAIL):
            rows = rows.filter(channel=channel)
        values = rows.aggregate(
            **{
                key: Count("pk", filter=view_q(utskick, view, channel))
                for key, _label, view in FUNNEL_STEPS
            }
        )
        linked = True
    first = int(values.get("attempted") or 0)
    if not first:
        return []
    steps = []
    for key, label, view in FUNNEL_STEPS:
        n = int(values.get(key) or 0)
        steps.append(
            {
                "key": key,
                "label": label,
                "n": n,
                "pct": _pct(n, first),
                "width": _width(n, first),
                "visa": view if linked else "",
                "kanal": channel_param(channel) if linked else "",
            }
        )
    return steps


# --- Klick per timme -----------------------------------------------------------


def _start(utskick):
    """När utskicket började gå: started_at, annars första sent_at."""
    from django.db.models import Min

    if utskick.started_at:
        return utskick.started_at
    return Recipient.objects.filter(utskick=utskick, sent_at__isnull=False).aggregate(
        first=Min("sent_at")
    )["first"]


def clicks_per_hour(utskick, now=None, hours=CHART_HOURS):
    """Mänskliga klick per timme från utskickets start (started_at, annars
    första sent_at), högst hours timmar och inte förbi now. Tomma timmar
    efter det sista klicket tas bort, men minst CHART_MIN_HOURS visas när
    så många har gått. [] utan start eller utan klick (också när
    retentionen tagit klicken)."""
    now = now or timezone.now()
    start = _start(utskick)
    if start is None or start > now:
        return []
    # Stockholms förskjutning är hela timmar, så timmen räknas lika i UTC.
    start = start.replace(minute=0, second=0, microsecond=0)
    hours = max(1, int(hours))
    counts = [0] * hours
    times = Click.objects.filter(
        utskick=utskick,
        account_id=utskick.account_id,
        kind=Click.Kind.HUMAN,
        at__gte=start,
        at__lt=start + timedelta(hours=hours),
    ).values_list("at", flat=True)
    for at in times.iterator(chunk_size=2000):
        counts[int((at - start).total_seconds() // 3600)] += 1
    if not any(counts):
        return []
    last = max(i for i, n in enumerate(counts) if n)
    begun = int((now - start).total_seconds() // 3600) + 1
    shown = min(hours, max(min(begun, CHART_MIN_HOURS), last + 1))
    return [{"start": start + timedelta(hours=i), "n": counts[i]} for i in range(shown)]


def _clock(moment, with_day=False):
    local = timezone.localtime(moment, STOCKHOLM)
    if with_day:
        return f"{local.day} {_MONTHS[local.month - 1]} {local:%H.%M}"
    return f"{local:%H.%M}"


def _svg_number(value):
    """Ett tal till ett SVG-attribut: punkt som decimaltecken, aldrig
    lokaliserat ("12.5", inte "12,5" som mallen skulle skriva ett flyttal)."""
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


def chart(points):
    """Staplarna i diagrammet (mallen ritar en SVG med viewBox och utan
    style=): {"width", "height", "bars", "first", "mid", "last", "total",
    "top", "hours", "rows"}. bars: {"x", "y", "w", "h"} som text till attributen,
    "n", "title" ("09.00: 12 klick") och "hi" (timmen eller timmarna med
    flest klick). rows: timmarna med klick i text, för skärmläsare. {} utan
    klick."""
    if not points:
        return {}
    top = max(point["n"] for point in points)
    if not top:
        return {}
    end = points[-1]["start"] + timedelta(hours=1)
    days = {timezone.localtime(p["start"], STOCKHOLM).date() for p in points}
    days.add(timezone.localtime(end - timedelta(seconds=1), STOCKHOLM).date())
    with_day = len(days) > 1
    slot = CHART_WIDTH / len(points)
    gap = min(slot * 0.25, 6)
    usable = CHART_HEIGHT - 2
    bars = []
    rows = []
    for i, point in enumerate(points):
        n = point["n"]
        h = max(2.0, usable * n / top) if n else 0.0
        title = f"{_clock(point['start'], with_day)}: {_group(n)} klick"
        bars.append(
            {
                "x": _svg_number(i * slot + gap / 2),
                "y": _svg_number(CHART_HEIGHT - h),
                "w": _svg_number(slot - gap),
                "h": _svg_number(h),
                "n": n,
                "title": title,
                "hi": n == top,
            }
        )
        if n:
            rows.append(title)
    return {
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "bars": bars,
        "first": _clock(points[0]["start"], with_day),
        # Mittmarkeringen på axeln: timmen mitt i diagrammet.
        "mid": _clock(points[len(points) // 2]["start"], with_day),
        "last": _clock(end, with_day),
        "total": sum(point["n"] for point in points),
        "top": top,
        "hours": len(points),
        "rows": rows,
    }


def _group(n):
    return f"{int(n or 0):,}".replace(",", chr(0xA0))


# --- Länkarna ------------------------------------------------------------------


def per_link(utskick):
    """Länktabellen: en rad per TrackedLink i utskicket. clicks är
    mottagarna som klickade på länken (mänskliga klick), leads mottagarna
    med en förfrågan via den; båda leder till listan bakom (visa,
    visa_leads). Utan mottagare kvar läses TrackedLink.human_clicks och
    leads (summorna ticken räknat upp) och raderna leder ingenstans."""
    links = list(
        TrackedLink.objects.filter(utskick=utskick, account_id=utskick.account_id)
        .select_related("campaign")
        .order_by("pk")
    )
    if not links:
        return []
    ids = [link.pk for link in links]
    has_rows = Recipient.objects.filter(utskick=utskick).exists()
    clicked, leads = {}, {}
    if has_rows:
        clicked = dict(
            Click.objects.filter(
                utskick=utskick,
                link_id__in=ids,
                kind=Click.Kind.HUMAN,
                recipient__utskick=utskick,
            )
            .values("link")
            .annotate(n=Count("recipient", distinct=True))
            .values_list("link", "n")
        )
        rows = (
            Lead.objects.filter(
                account_id=utskick.account_id,
                utskick_recipient__utskick=utskick,
                attribution__link__in=ids,
            )
            .exclude(source=Lead.SOURCE_REPLY)
            .values_list("attribution__link", "utskick_recipient")
        )
        for link_id, recipient_id in rows:
            if isinstance(link_id, int):
                leads.setdefault(link_id, set()).add(recipient_id)
    out = []
    for link in links:
        if has_rows:
            n_clicks, n_leads = clicked.get(link.pk, 0), len(leads.get(link.pk, ()))
        else:
            n_clicks, n_leads = link.human_clicks, link.leads
        out.append(
            {
                "link": link,
                "label": link_name(link),
                "destination": destination_text(link),
                "kind": link.get_kind_display(),
                "clicks": n_clicks,
                "leads": n_leads,
                "visa": link_view(link) if has_rows else "",
                "visa_leads": link_view(link, leads=True) if has_rows else "",
            }
        )
    return out


# --- Landningssidan ------------------------------------------------------------


def duration_text(seconds):
    """ "1 min 52 s", "45 s", "" för inget."""
    if seconds is None:
        return ""
    minutes, rest = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    return f"{rest} s"


def lp_behaviour(utskick):
    """Beteendet på landningssidan (bara klick på utskickets länkar till en
    Flamingo-sida, README E.4): visits (mänskliga klick med besök),
    visitors, called och form_leads (mottagare, samma villkor som listorna
    besokte, ringde och formular), median_seconds (tid på sidan bland
    besöken som mätte någon tid), mobile_pct (andelen besök i mobil) och
    line ("På landningssidan: mediantid 1 min 52 s · 91 % i mobil").
    linked säger om talen leder till mottagarna. {} utan besök och samtal."""
    import statistics

    clicks = Click.objects.filter(
        utskick=utskick,
        account_id=utskick.account_id,
        link__utskick=utskick,
        link__kind=TrackedLink.Kind.LP,
    )
    visits = clicks.filter(kind=Click.Kind.HUMAN, lp_visits__gt=0)
    n_visits = visits.count()
    if not n_visits and not clicks.filter(called=True).exists():
        return {}
    seconds = list(visits.filter(engaged_seconds__gt=0).values_list("engaged_seconds", flat=True))
    median = round(statistics.median(seconds)) if seconds else None
    mobile_pct = _pct(visits.filter(device="mobile").count(), n_visits)
    linked = Recipient.objects.filter(utskick=utskick).exists()
    people = {}
    if linked:
        rows = Recipient.objects.filter(utskick=utskick)
        people = rows.aggregate(
            visitors=Count("pk", filter=view_q(utskick, "besokte")),
            called=Count("pk", filter=view_q(utskick, "ringde")),
            form_leads=Count("pk", filter=view_q(utskick, "formular")),
        )
    parts = []
    if median is not None:
        parts.append(f"mediantid {duration_text(median)}")
    if mobile_pct is not None:
        parts.append(f"{_pct_text(mobile_pct)} i mobil")
    return {
        "visits": n_visits,
        "visitors": int(people.get("visitors") or 0) if linked else n_visits,
        "called": int(people.get("called") or 0) if linked else clicks.filter(called=True).count(),
        "form_leads": int(people.get("form_leads") or 0) if linked else 0,
        "median_seconds": median,
        "median_text": duration_text(median),
        "mobile_pct": mobile_pct,
        "line": ("På landningssidan: " + " · ".join(parts)) if parts else "",
        "linked": linked,
    }


def _pct_text(value):
    from .templatetags.utskick_tags import procent

    return procent(value)


# --- Följ upp och hela rapporten -------------------------------------------------


def follow_up_count(utskick):
    """Kontakter som fick utskicket (någon kanal) men inte klickade på något
    i det: de som "Följ upp de som inte klickade" gäller (segments.follow_up_rules).
    Den som anmälde utskicket som skräp är inte med (segments._c_got_utskick)."""
    clicked = Recipient.objects.filter(
        utskick=utskick, contact__isnull=False, first_clicked_at__isnull=False
    ).values("contact")
    complained = Recipient.objects.filter(
        utskick=utskick, contact__isnull=False, status=Recipient.Status.COMPLAINED
    ).values("contact")
    return (
        Recipient.objects.filter(
            utskick=utskick, contact__isnull=False, status__in=Recipient.SENT_LIKE
        )
        .exclude(contact__in=clicked)
        .exclude(contact__in=complained)
        .values("contact")
        .distinct()
        .count()
    )


def full(utskick, now=None, numbers=None):
    """Allt rapporten visar i S4, för vyns kontext: numbers (summary), en
    tratt per kanal (funnels: {"channel", "label", "kanal", "steps"}; rubriken
    bara när kanalerna är flera), clicks_per_hour och chart, links
    (per_link) och has_links, lp (lp_behaviour), follow_up (follow_up_count,
    0 utan länkar) och can_export (mottagarna finns kvar). Utan länkar har
    tratten inte Klickade och Stannade, och diagrammet är tomt."""
    now = now or timezone.now()
    numbers = numbers if numbers is not None else summary(utskick, now)
    funnels = []
    channels = []
    if numbers.get("from_stats"):
        steps = funnel(utskick, numbers)
        if steps:
            funnels.append({"channel": "", "label": "", "kanal": "", "steps": steps})
    elif numbers.get("has_rows"):
        channels = channels_of(utskick)
        for channel in channels:
            steps = funnel(utskick, numbers, channel=channel)
            if steps:
                funnels.append(
                    {
                        "channel": channel,
                        "label": CHANNEL_LABELS[channel] if len(channels) > 1 else "",
                        "kanal": channel_param(channel),
                        "steps": steps,
                    }
                )
    has_rows = bool(numbers.get("has_rows"))
    links = per_link(utskick)
    if not links:
        # Utan länkar kan ingen klicka: Klickade och Stannade bort ur
        # tratten, inget diagram och ingen uppföljning av "de som inte
        # klickade" (det vore alla).
        for item in funnels:
            item["steps"] = [s for s in item["steps"] if s["key"] not in LINK_STEPS]
    points = clicks_per_hour(utskick, now) if links else []
    return {
        "numbers": numbers,
        "channels": channels,
        "funnels": funnels,
        "clicks_per_hour": points,
        "chart": chart(points),
        "links": links,
        "has_links": bool(links),
        "lp": lp_behaviour(utskick),
        "has_lp": TrackedLink.objects.filter(utskick=utskick, kind=TrackedLink.Kind.LP).exists(),
        "follow_up": follow_up_count(utskick) if has_rows and links else 0,
        "can_export": has_rows,
    }


# --- Exporten --------------------------------------------------------------------


def _stamp(moment):
    if not moment:
        return ""
    return f"{timezone.localtime(moment, STOCKHOLM):%Y-%m-%d %H:%M}"


def _state(recipient):
    if recipient.status == R.SKIPPED:
        reason = recipient.get_skip_reason_display() or ""
        return f"Hoppades över: {reason.lower()}" if reason else "Hoppades över"
    return recipient.get_status_display()


def export_rows(utskick):
    """CSV-raderna (utan rubrikraden) för utskickets alla mottagare, också
    de som hoppades över, i pk-ordning. Varje cell går genom safe_cell. En
    borttagen kontakt står som "Borttagen kontakt" utan adress (H.4)."""
    from apps.flamingo.exports import safe_cell

    from .normalize import display_phone

    rows = (
        Recipient.objects.filter(utskick=utskick)
        .select_related("contact")
        .annotate(
            engaged=Max(
                "clicks__engaged_seconds", filter=Q(clicks__kind=Click.Kind.HUMAN), default=0
            ),
            has_lead=_lead_exists(),
        )
        .order_by("pk")
    )
    for recipient in rows.iterator(chunk_size=500):
        kontakt = recipient.contact
        if kontakt is None:
            name, address = DELETED_NAME, ""
        elif recipient.channel == CHANNEL_SMS:
            name, address = kontakt.display_name, display_phone(recipient.address)
        else:
            name, address = kontakt.display_name, recipient.address
        if recipient.stopped_at:
            replied = "STOPP"
        elif recipient.replied_at:
            replied = "Ja"
        else:
            replied = ""
        line = (
            name,
            CHANNEL_LABELS.get(recipient.channel, recipient.channel),
            address,
            _state(recipient),
            _stamp(recipient.sent_at),
            _stamp(recipient.delivered_at),
            recipient.click_count,
            recipient.engaged or "",
            replied,
            "Ja" if recipient.has_lead else "",
        )
        yield [safe_cell(cell) for cell in line]
