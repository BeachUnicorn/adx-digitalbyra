"""
Rapportens siffror (README I.8, S2-delen; S4 bygger ut med tratten, klick
per timme och länktabellen).

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

from django.db.models import Count, Exists, Max, OuterRef, Q, Sum
from django.utils import timezone

from apps.flamingo.models import Lead
from apps.sms.models import UNITS_PER_KR, SmsMessage

from .models import CHANNEL_SMS, Click, Recipient

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
}
SKIPPED_PREFIX = "hoppades-over-"


def _leads(utskick):
    return Lead.objects.filter(account_id=utskick.account_id, utskick=utskick).exclude(
        source=Lead.SOURCE_REPLY
    )


def _lead_exists():
    return Exists(
        Lead.objects.filter(utskick_recipient=OuterRef("pk")).exclude(source=Lead.SOURCE_REPLY)
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


def recipients_for(utskick, view):
    """Mottagarna bakom siffran ?visa=view (VIEWS, eller
    "hoppades-over-<orsak>"), med det tabellen läser: kontakten, längsta tid
    på sidan (mänskliga klick) och om en förfrågan kom. None för en okänd vy."""
    rows = Recipient.objects.filter(utskick=utskick, channel=CHANNEL_SMS)
    if view.startswith(SKIPPED_PREFIX):
        reason = view[len(SKIPPED_PREFIX) :]
        if reason not in Recipient.SkipReason.values:
            return None
        rows = rows.filter(status=R.SKIPPED, skip_reason=reason)
    elif view == "alla":
        rows = rows.exclude(status=R.SKIPPED)
    elif view == "levererade":
        rows = rows.filter(status=R.DELIVERED)
    elif view == "misslyckade":
        rows = rows.filter(status__in=(R.FAILED, R.BOUNCED))
    elif view == "kvar":
        rows = rows.filter(status__in=(R.QUEUED, R.SENDING))
    elif view == "klickade":
        rows = rows.filter(first_clicked_at__isnull=False)
    elif view == "klickade-inte":
        rows = rows.filter(status=R.DELIVERED, first_clicked_at__isnull=True)
    elif view == "stannade":
        rows = rows.filter(_engaged_exists())
    elif view == "svarade":
        rows = rows.filter(replied_at__isnull=False)
    elif view == "stopp":
        rows = rows.filter(stopped_at__isnull=False)
    elif view == "forfragan":
        rows = rows.filter(_lead_exists())
    elif view == "hoppades-over":
        rows = rows.filter(status=R.SKIPPED)
    elif view == "avbrutna":
        rows = rows.filter(status=R.CANCELLED)
    else:
        return None
    return (
        rows.select_related("contact")
        .annotate(
            engaged=Max(
                "clicks__engaged_seconds", filter=Q(clicks__kind=Click.Kind.HUMAN), default=0
            ),
            has_lead=_lead_exists(),
        )
        .order_by("pk")
    )


def view_label(view):
    if view.startswith(SKIPPED_PREFIX):
        reason = view[len(SKIPPED_PREFIX) :]
        try:
            return "Hoppades över: " + Recipient.SkipReason(reason).label.lower()
        except ValueError:
            return ""
    return VIEWS.get(view, "")
