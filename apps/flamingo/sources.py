"""
Källorna på landningssidorna: varifrån ett besök, ett klick på numret och
en förfrågan kom, besöken per dag och statistiken per sida (README, Steg 2).

    origin_for(campaign, click, tracking) -> Origin
                                källan för ett besök eller en förfrågan: utskickets
                                eller den namngivna länkens klick först, sedan
                                kampanjens adress (eller ett giltigt ?kampanj= på
                                sidans egen), sedan ett klick-id, annars Direkt
    ad_campaign_for(page, campaign_id) -> Campaign eller None
                                kampanjen ?kampanj= pekar på, när den hör till
                                sidans konto och leder till sidan
    is_counted_request(request) -> bool
                                en riktig sidvisning: GET, ingen förhämtning, ingen
                                bot eller länkförhandsvisning
    record_visit(page, origin, request, now=None) -> bool
                                ett besök i PageVisitDay, högst ett per besökare,
                                sida, källa och timme
    lead_source_expr()          källan för en förfrågan i SQL: page_source, eller för
                                en rad som den förra versionen skrev, spåret och
                                kampanjen (samma regler som origin_for)
    page_leads_q(page)          förfrågningarna från sidan (också gamla rader utan
                                landing_page, genom kampanjens sida)
    page_stats(page, days, now=None) -> PageStats
                                Statistik: summorna och en rad per källa
    list_numbers(account, now=None, days=30) -> {sidans id: ListNumbers}
                                listans siffror: besök, förfrågningar, antal källor
    lead_source(lead) -> LeadSource eller None
                                inkorgens chip: källan, sidan och kampanjen,
                                utskicket eller länken (som Statistik)

Annonserna på sidans egen adress: Flamingos kampanjer hos Google leder till
/lp/<konto>/<sida>/ med kampanjens slutadressuffix kampanj=<id>
(exports.ad_url_suffix). Ett ?kampanj= räknas bara när kampanjen finns, hör
till sidans konto och leder till just den sidan (kampanjens sida, eller
sidan annonserna publicerades med, Campaign.ads_final_url); då är besöket,
klicket på numret och förfrågan kampanjens, som på kampanjens adress. Ett
främmande, okänt eller felaktigt kampanjnummer ignoreras helt (vanliga reglerna:
ett klick-id ger Annons utan kampanj, annars Direkt).

Källorna (models.PAGE_SOURCE_*): Annons (ad, kampanjen eller ett klick-id),
Sms och E-post (sms, email, utskicket), en namngiven länk (named, länken;
QR-kod, Instagram eller Annat efter TrackedLink.medium, läst när
statistiken ritas, så att en ändrad sort gäller överallt) och Direkt
(direct, sidans egen adress utan spårad länk).

Besöken: inga kakor och ingen lagring i webbläsaren (utskickens D10).
Besökaren känns igen bara som ip_hash (limits.ip_hash) i utskickens räknare
(utskick.limits.hit med scopet lp_visit, rensas efter två dygn), och bara
för att samma besökare inte ska räknas mer än en gång i timmen per sida och
källa. Vyn räknar inte byrån, kundens egna kontakter, förhandsvisningen
eller demokontot (public_views._counted_visit).

Förfrågningarna räknas som på översikten: inte skräp och inte svar på
utskick (de är trådar i Inkorgen). Ett klick på numret är en förfrågan.

Frågorna är platta: page_stats gör en fråga för besöken, en för
förfrågningarna och en för när räkningen började; list_numbers en för
besöken och en för förfrågningarna, hur många sidor och källor det än är.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.db import connection
from django.db.models import Case, CharField, Count, F, Min, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import limits
from .models import (
    PAGE_SOURCE_AD,
    PAGE_SOURCE_DIRECT,
    PAGE_SOURCE_EMAIL,
    PAGE_SOURCE_NAMED,
    PAGE_SOURCE_SMS,
    Campaign,
    Lead,
    PageVisitDay,
)

#: Perioderna i Statistik (?dagar=), och den som gäller från början.
PERIODS = (7, 30, 90)
DEFAULT_DAYS = 30
#: Scopet i utskickens räknare (utskick.limits).
VISIT_SCOPE = "lp_visit"
#: Raderna i tabellen i den här ordningen (mockupen), störst först inom sorten.
SOURCE_ORDER = (
    PAGE_SOURCE_AD,
    PAGE_SOURCE_SMS,
    PAGE_SOURCE_EMAIL,
    PAGE_SOURCE_NAMED,
    PAGE_SOURCE_DIRECT,
)
#: Chipets text och ton (fl-src--<ton>, flamingo-app-pages.css).
SOURCE_CHIPS = {
    PAGE_SOURCE_AD: ("Annons", "ad"),
    PAGE_SOURCE_SMS: ("Sms", "sms"),
    PAGE_SOURCE_EMAIL: ("E-post", "mail"),
    PAGE_SOURCE_DIRECT: ("Direkt", "direct"),
}
#: En namngiven länks chip efter länkens sort (TrackedLink.medium).
MEDIUM_CHIPS = {
    "qr": ("QR-kod", "qr"),
    "instagram": ("Instagram", "social"),
    "other": ("Annat", "other"),
    "": ("Annat", "other"),
}
DIRECT_TEXT = "utan länk"
AD_TEXT = "Google Ads"


@dataclass(frozen=True)
class Origin:
    """Källan för ett besök eller en förfrågan."""

    source: str
    key: str
    campaign_id: int | None = None
    utskick_id: int | None = None
    link_id: int | None = None


def origin_for(campaign=None, click=None, tracking=None):
    """Källan: ett klick från ett utskick eller en namngiven länk (samma
    konto, utskick.attribution.resolve) vinner; sedan kampanjens adress
    (Annons); sedan ett klick-id från Google på sidans egen adress (Annons
    utan kampanj); annars Direkt."""
    if click is not None:
        channel = getattr(click, "channel", "")
        if channel == "named" and click.link_id:
            return Origin(PAGE_SOURCE_NAMED, f"named:l{click.link_id}", link_id=click.link_id)
        if channel in (PAGE_SOURCE_SMS, PAGE_SOURCE_EMAIL) and click.utskick_id:
            return Origin(channel, f"{channel}:u{click.utskick_id}", utskick_id=click.utskick_id)
    if campaign is not None:
        return Origin(PAGE_SOURCE_AD, f"ad:c{campaign.pk}", campaign_id=campaign.pk)
    tracking = tracking or {}
    if any(tracking.get(key) for key in ("gclid", "gbraid", "wbraid")):
        return Origin(PAGE_SOURCE_AD, PAGE_SOURCE_AD)
    return Origin(PAGE_SOURCE_DIRECT, PAGE_SOURCE_DIRECT)


def ad_campaign_for(page, campaign_id):
    """Kampanjen som ?kampanj=<id> på sidans egen adress pekar på, eller None.

    Bara en kampanj hos sidans konto som leder till sidan: kampanjens sida
    nu, eller sidan annonserna publicerades med (Campaign.ads_final_url,
    så att en kampanj som bytt sida efter publiceringen fortfarande räknas
    där annonserna landar). Ett annat kontos kampanj, en okänd eller en som
    leder till en annan sida ignoreras. En fråga, bara när id:t finns."""
    if page is None or not campaign_id:
        return None
    campaign = (
        Campaign.objects.select_related("account__customer", "service")
        .filter(pk=campaign_id, account_id=page.account_id)
        .first()
    )
    if campaign is None:
        return None
    if campaign.landing_page_id == page.pk:
        return campaign
    from urllib.parse import urlsplit

    published = urlsplit(campaign.ads_final_url or "").path
    if published and published == page.landing_url:
        return campaign
    return None


# ---------------------------------------------------------------------------
# Besöken
# ---------------------------------------------------------------------------


def is_counted_request(request):
    """En riktig sidvisning i en webbläsare: GET (inte HEAD), ingen
    förhämtning (Sec-Purpose eller Purpose: prefetch), ett Sec-Fetch-Dest
    som är document när det finns, och ingen bot eller
    länkförhandsvisning (utskick.attribution.is_preview: samma lista som
    klicken på k.adx.se; en tom User-Agent räknas som bot)."""
    from apps.utskick.attribution import is_preview

    if request.method != "GET":
        return False
    headers = request.headers
    purpose = (headers.get("Sec-Purpose", "") + headers.get("Purpose", "")).lower()
    if "prefetch" in purpose or "prerender" in purpose:
        return False
    dest = headers.get("Sec-Fetch-Dest", "")
    if dest and dest != "document":
        return False
    return not is_preview(str(request.META.get("HTTP_USER_AGENT", "") or "")[:500])


def record_visit(page, origin, request, now=None):
    """Ett besök på sidan från källan: dagens rad i PageVisitDay räknas upp
    (INSERT ... ON CONFLICT, en fråga). Samma besökare (ip_hash) räknas en
    gång per sida, källa och timme. True när besöket räknades."""
    from apps.common.net import client_ip
    from apps.utskick import limits as counters

    now = now or timezone.now()
    hashed = limits.ip_hash(client_ip(request))
    if hashed and counters.hit(
        VISIT_SCOPE, f"{page.pk}:{origin.key}:{hashed[:32]}", counters.hour_window(now), 1
    ):
        return False
    day = limits.stockholm_today(now)
    with connection.cursor() as cursor:
        cursor.execute(
            'INSERT INTO flamingo_pagevisitday ("account_id", "page_id", "day", "source", '
            '"key", "campaign_id", "utskick_id", "link_id", "visits") '
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1) "
            'ON CONFLICT ("page_id", "day", "key") '
            'DO UPDATE SET "visits" = flamingo_pagevisitday."visits" + 1',
            [
                page.account_id,
                page.pk,
                day,
                origin.source,
                origin.key[:40],
                origin.campaign_id,
                origin.utskick_id,
                origin.link_id,
            ],
        )
    return True


# ---------------------------------------------------------------------------
# Förfrågningarna
# ---------------------------------------------------------------------------


def lead_source_expr():
    """Förfrågans källa i SQL. page_source när den finns; annars (en rad
    som den förra versionen skrev under deployen) samma regler som
    origin_for ur spåret och kampanjen."""
    return Case(
        When(~Q(page_source=""), then=F("page_source")),
        When(attribution__channel="named", then=Value(PAGE_SOURCE_NAMED)),
        When(attribution__channel="sms", then=Value(PAGE_SOURCE_SMS)),
        When(attribution__channel="email", then=Value(PAGE_SOURCE_EMAIL)),
        When(campaign__isnull=False, then=Value(PAGE_SOURCE_AD)),
        default=Value(PAGE_SOURCE_DIRECT),
        output_field=CharField(),
    )


def counted_leads():
    """Förfrågningarna som räknas (som översikten): inte skräp, inte svar."""
    return Lead.objects.filter(status__in=Lead.COUNTED_STATUSES).exclude(source=Lead.SOURCE_REPLY)


def page_leads_q(page_id):
    """Förfrågningarna från sidan: landing_page, eller för en gammal rad
    utan den, sidan kampanjen visar."""
    return Q(landing_page_id=page_id) | Q(
        landing_page__isnull=True, campaign__landing_page_id=page_id
    )


def _lead_key(source, campaign_id, utskick_id, link_id):
    if source == PAGE_SOURCE_AD:
        return f"ad:c{campaign_id}" if campaign_id else PAGE_SOURCE_AD
    if source in (PAGE_SOURCE_SMS, PAGE_SOURCE_EMAIL):
        return f"{source}:u{utskick_id}" if utskick_id else source
    if source == PAGE_SOURCE_NAMED:
        return f"named:l{link_id}" if link_id else PAGE_SOURCE_NAMED
    return PAGE_SOURCE_DIRECT


# ---------------------------------------------------------------------------
# Statistiken
# ---------------------------------------------------------------------------


def clean_days(raw):
    """?dagar= som ett av PERIODS, annars DEFAULT_DAYS."""
    try:
        value = int(str(raw or "").strip())
    except ValueError:
        return DEFAULT_DAYS
    return value if value in PERIODS else DEFAULT_DAYS


def period(days, now=None):
    """(första dagen, början som tid) för de senaste days dagarna i svensk
    tid, i dag medräknad (som översikten)."""
    now = now or timezone.now()
    first = limits.stockholm_today(now) - timedelta(days=days - 1)
    return first, datetime.combine(first, time.min, tzinfo=limits.STOCKHOLM)


def share(leads, visits):
    """Andelen som frågar i procent, eller None utan besök."""
    if not visits:
        return None
    return round(leads * 100 / visits, 1)


@dataclass
class SourceRow:
    key: str
    source: str
    chip: str
    tone: str
    text: str
    visits: int = 0
    leads: int = 0

    @property
    def share(self):
        return share(self.leads, self.visits)


@dataclass
class PageStats:
    days: int
    first_day: object
    visits: int = 0
    leads: int = 0
    rows: list = field(default_factory=list)
    #: Den första dagen besök räknades (för hela ADX), eller None.
    counted_since: object = None

    @property
    def share(self):
        return share(self.leads, self.visits)

    @property
    def visits_partial(self):
        """Började perioden innan besöken räknades?"""
        return self.counted_since is None or self.first_day < self.counted_since


def _chip(source, medium):
    if source == PAGE_SOURCE_NAMED:
        return MEDIUM_CHIPS.get(medium or "", MEDIUM_CHIPS[""])
    return SOURCE_CHIPS.get(source, SOURCE_CHIPS[PAGE_SOURCE_DIRECT])


def _text(source, campaign_name, utskick_name, link_label):
    if source == PAGE_SOURCE_AD:
        return campaign_name or AD_TEXT
    if source in (PAGE_SOURCE_SMS, PAGE_SOURCE_EMAIL):
        return utskick_name or "Utskick"
    if source == PAGE_SOURCE_NAMED:
        return link_label or "Länk"
    return DIRECT_TEXT


def _row(rows, key, source, medium, campaign_name, utskick_name, link_label):
    row = rows.get(key)
    if row is None:
        chip, tone = _chip(source, medium)
        row = SourceRow(
            key=key,
            source=source,
            chip=chip,
            tone=tone,
            text=_text(source, campaign_name, utskick_name, link_label),
        )
        rows[key] = row
    return row


def page_stats(page, days=DEFAULT_DAYS, now=None):
    """Statistik för sidan de senaste days dagarna: besök, förfrågningar och
    en rad per källa (SOURCE_ORDER, flest besök först inom sorten)."""
    now = now or timezone.now()
    first_day, since = period(days, now)
    stats = PageStats(days=days, first_day=first_day)
    rows = {}
    visits = (
        PageVisitDay.objects.filter(page_id=page.pk, day__gte=first_day)
        .values(
            "key",
            "source",
            "campaign__name",
            "utskick__name",
            "link__label",
            "link__medium",
        )
        .annotate(n=Sum("visits"))
    )
    for item in visits:
        row = _row(
            rows,
            item["key"],
            item["source"],
            item["link__medium"],
            item["campaign__name"],
            item["utskick__name"],
            item["link__label"],
        )
        row.visits += int(item["n"] or 0)
    leads = (
        counted_leads()
        .filter(page_leads_q(page.pk), account_id=page.account_id, created_at__gte=since)
        .annotate(src=lead_source_expr())
        .values(
            "src",
            "campaign_id",
            "utskick_id",
            "named_link_id",
            "campaign__name",
            "utskick__name",
            "named_link__label",
            "named_link__medium",
        )
        .annotate(n=Count("pk"))
    )
    for item in leads:
        key = _lead_key(item["src"], item["campaign_id"], item["utskick_id"], item["named_link_id"])
        row = _row(
            rows,
            key,
            item["src"],
            item["named_link__medium"],
            item["campaign__name"],
            item["utskick__name"],
            item["named_link__label"],
        )
        row.leads += int(item["n"] or 0)
    order = {source: n for n, source in enumerate(SOURCE_ORDER)}
    stats.rows = sorted(
        rows.values(),
        key=lambda r: (order.get(r.source, len(order)), -r.visits, -r.leads, r.text),
    )
    stats.visits = sum(r.visits for r in stats.rows)
    stats.leads = sum(r.leads for r in stats.rows)
    stats.counted_since = PageVisitDay.objects.aggregate(first=Min("day"))["first"]
    return stats


@dataclass
class ListNumbers:
    visits: int = 0
    leads: int = 0
    keys: set = field(default_factory=set)

    @property
    def sources(self):
        return len(self.keys)


def list_numbers(account, now=None, days=DEFAULT_DAYS):
    """{sidans id: ListNumbers} för kontots sidor, de senaste days dagarna.
    En sida utan besök och förfrågningar saknas (ListNumbers() i mallen)."""
    now = now or timezone.now()
    first_day, since = period(days, now)
    out = {}
    for item in (
        PageVisitDay.objects.filter(account=account, day__gte=first_day)
        .values("page_id", "key")
        .annotate(n=Sum("visits"))
    ):
        numbers = out.setdefault(item["page_id"], ListNumbers())
        numbers.visits += int(item["n"] or 0)
        numbers.keys.add(item["key"])
    for item in (
        counted_leads()
        .filter(account=account, created_at__gte=since)
        .annotate(
            pid=Coalesce("landing_page_id", "campaign__landing_page_id"),
            src=lead_source_expr(),
        )
        .exclude(pid__isnull=True)
        .values("pid", "src", "campaign_id", "utskick_id", "named_link_id")
        .annotate(n=Count("pk"))
    ):
        numbers = out.setdefault(item["pid"], ListNumbers())
        numbers.leads += int(item["n"] or 0)
        numbers.keys.add(
            _lead_key(item["src"], item["campaign_id"], item["utskick_id"], item["named_link_id"])
        )
    return out


# ---------------------------------------------------------------------------
# Inkorgen: förfrågans källa som chip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadSource:
    """Inkorgens chip för en förfrågan: källan (Annons, Sms, E-post, QR-kod,
    Instagram, Annat, Direkt), sidan och texten (kampanjen, utskicket eller
    länken, som Statistik)."""

    source: str
    chip: str
    tone: str
    page_name: str
    text: str


def _lead_source_value(lead):
    """Som lead_source_expr, i Python för en rad som redan är läst."""
    if lead.page_source:
        return lead.page_source
    attribution = lead.attribution if isinstance(lead.attribution, dict) else {}
    channel = attribution.get("channel")
    if channel in (PAGE_SOURCE_NAMED, PAGE_SOURCE_SMS, PAGE_SOURCE_EMAIL):
        return channel
    if lead.campaign_id:
        return PAGE_SOURCE_AD
    return PAGE_SOURCE_DIRECT


def lead_source(lead):
    """Chipet för förfrågan, eller None för en förfrågan som inte kom från
    en landningssida (lagd för hand, ett samtal, ett svar på ett utskick,
    som har sina egna chips). En gammal rad utan sparad källa läses med
    samma regler som Statistik (lead_source_expr): spårets kanal, annars
    Annons för en kampanj, annars Direkt; sidan är kampanjens sida.

    Läser lead.landing_page, lead.campaign (och dess landing_page),
    lead.utskick och lead.named_link: inkorgen hämtar dem med förfrågan
    (select_related), så att listan gör lika många frågor hur många
    förfrågningar den än visar."""
    if lead.source not in (Lead.SOURCE_FORM, Lead.SOURCE_CALL_CLICK):
        return None
    campaign = lead.campaign if lead.campaign_id else None
    page = lead.landing_page if lead.landing_page_id else None
    if page is None and campaign is not None and campaign.landing_page_id:
        page = campaign.landing_page
    attribution = lead.attribution if isinstance(lead.attribution, dict) else {}
    if page is None and campaign is None and not lead.page_source and not attribution:
        return None
    source = _lead_source_value(lead)
    link = lead.named_link if lead.named_link_id else None
    chip, tone = _chip(source, link.medium if link is not None else "")
    utskick = lead.utskick if lead.utskick_id else None
    utskick_name = utskick.name if utskick is not None else str(attribution.get("name") or "")
    link_label = link.label if link is not None else str(attribution.get("label") or "")
    text = _text(source, campaign.name if campaign else "", utskick_name, link_label)
    return LeadSource(
        source=source,
        chip=chip,
        tone=tone,
        page_name=page.name if page is not None else "",
        text="" if source == PAGE_SOURCE_DIRECT else text,
    )
