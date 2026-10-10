"""
Kontaktkortets tidslinje (README I.7): källorna slås ihop när kortet läses,
inget kopieras till Event (disken).

    for_contact(contact, page=1)   Page(items, number, has_next, has_previous)

Källor i S1: samtyckesloggen, händelserna (importerad, anmälan, förfrågan
utan egen rad) och förfrågningarna i Inkorgen (med "Öppna i Inkorgen").
S2: sms ur Recipient (skickat, levererat, gick inte fram, med länk till
rapporten), mänskliga klick ur Click (med tiden på sidan), svar och STOPP
ur ThreadMessage (med "Öppna i Inkorgen") och besöken på landningssidan
(Event lp_visit). S3: mejlen ur Recipient (levererat, studsat, klagomål)
och öppningarna (Event opened, "Öppnade (indikation)"). S4: besöken på
kundens egen webbplats (Event site_visit), sms från kontaktkortet och
"Svarar oftast" (reply_habit, för kortets sammanfattning). Senare steg lägger
till sina källor i SOURCES (flöden): en funktion (contact, limit) -> lista
med Item, nyast först.

Varje källa filtrerar på kontaktens konto, också förfrågningarna (H.1).
"""

from dataclasses import dataclass, field
from datetime import datetime

from django.urls import reverse
from django.utils import timezone

from . import consent as consents
from .models import CHANNEL_SMS, Consent, ConsentLog, Event

PER_PAGE = 30

_CHANNEL = {"sms": "Sms", "email": "E-post"}
_STATUS = {
    Consent.Status.YES: "ja",
    Consent.Status.EXISTING: "befintlig kund",
    Consent.Status.COMPANY: "företag",
    Consent.Status.PENDING: "väntar på bekräftelse",
    Consent.Status.MISSING: "inget samtycke",
    Consent.Status.DECLINED: "vill inte ha erbjudanden",
    Consent.Status.UNSUBSCRIBED: "avregistrerad",
}

#: Rubriker för händelsernas slag. Senare steg lägger till sina.
EVENT_TITLES = {
    Event.IMPORTED: "Importerad",
    Event.SIGNUP: "Anmälde sig på anmälningssidan",
    Event.LEAD: "Förfrågan",
    Event.TEST_SEND: "Fick ett testutskick",
}


@dataclass
class Item:
    at: datetime
    kind: str
    title: str
    detail: str = ""
    url: str = ""
    link_label: str = ""
    #: Byrån gjorde ändringen i kundvyn.
    by_staff: bool = False


@dataclass
class Page:
    items: list = field(default_factory=list)
    number: int = 1
    has_next: bool = False

    @property
    def has_previous(self):
        return self.number > 1

    @property
    def next_page_number(self):
        return self.number + 1

    @property
    def previous_page_number(self):
        return self.number - 1


def _consent_items(contact, limit):
    rows = ConsentLog.objects.filter(account_id=contact.account_id, contact=contact).order_by(
        "-at", "-pk"
    )[:limit]
    items = []
    for row in rows:
        channel = _CHANNEL.get(row.channel, row.channel)
        if row.source == Consent.Source.ADDRESS and row.new_status in (
            Consent.Status.MISSING,
            Consent.Status.UNSUBSCRIBED,
        ):
            title = "Nytt mobilnummer" if row.channel == CHANNEL_SMS else "Ny e-postadress"
        else:
            title = f"{channel}: {_STATUS.get(row.new_status, row.new_status)}"
        try:
            source = Consent.Source(row.source).label
        except ValueError:
            source = row.source
        shown = consents.shown_detail(row.source, row.source_detail)
        detail = " · ".join(p for p in (source, shown, row.by_label) if p)
        items.append(
            Item(at=row.at, kind="consent", title=title, detail=detail, by_staff=row.by_staff)
        )
    return items


def _event_items(contact, limit):
    rows = (
        Event.objects.filter(account_id=contact.account_id, contact=contact)
        .exclude(kind=Event.LEAD, lead__isnull=False)
        .select_related("utskick")
        .order_by("-at", "-pk")[:limit]
    )
    items = []
    for row in rows:
        detail = ""
        if row.kind == Event.IMPORTED:
            # Filens namn (äldre rader har bara importens nummer).
            detail = str(row.data.get("fil") or "")
        elif row.kind == Event.OPENED and row.utskick_id:
            # S3: vilket mejl som öppnades.
            detail = row.utskick.name
        # --- S4 (rapport-byggaren): besöket på kundens egen webbplats (E.6),
        # och ett mål (adxFlamingo.track) med samma slag och "mal" i data.
        elif row.kind == Event.SITE_VISIT:
            detail = site_visit_detail(row)
        # --- slut S4
        items.append(
            Item(
                at=row.at,
                kind=row.kind,
                title=site_visit_title(row) or EVENT_TITLES.get(row.kind, row.kind),
                detail=detail,
            )
        )
    return items


def _lead_items(contact, limit):
    from apps.flamingo.models import Lead

    rows = (
        Lead.objects.filter(account_id=contact.account_id, contact=contact)
        .select_related("campaign", "service")
        .order_by("-created_at", "-pk")[:limit]
    )
    items = []
    for lead in rows:
        parts = [lead.get_source_display()]
        if lead.campaign_id:
            parts.append(lead.campaign.name)
        items.append(
            Item(
                at=lead.created_at,
                kind="lead",
                title="Förfrågan",
                detail=" · ".join(p for p in parts if p),
                url=reverse("flamingo:app_lead", args=[lead.pk]),
                link_label="Öppna i Inkorgen",
            )
        )
    return items


# --- S2 (utskick-ui-byggaren): sms, klick och svar -------------------------
# Läses ur Recipient, Click och ThreadMessage (inget kopieras till Event).
# Besöket på landningssidan är en Event (lp_visit, attribution.py).

EVENT_TITLES.update(
    {
        Event.LP_VISIT: "Besökte landningssidan",
        Event.CALL_CLICK: "Ringde från landningssidan",
        Event.REPLY: "Svarade på ett sms",
        Event.STOP: "Svarade STOPP",
        Event.START: "Svarade START",
    }
)


def _recipient_items(contact, limit):
    from .models import Recipient

    S = Recipient.Status
    rows = (
        Recipient.objects.filter(
            utskick__account_id=contact.account_id,
            contact=contact,
            status__in=(*Recipient.SENT_LIKE, S.FAILED),
        )
        .select_related("utskick")
        .order_by("-sent_at", "-pk")[:limit]
    )
    items = []
    for row in rows:
        if row.status == S.DELIVERED:
            detail = "Levererat"
        elif row.status in (S.FAILED, S.BOUNCED):
            detail = "Gick inte fram"
        else:
            detail = "Skickat"
        # --- S3 (integrationen): mejlets studs och klagomål i ord (I.7).
        if row.channel != CHANNEL_SMS:
            detail = EMAIL_DETAILS.get(row.status, detail)
        # --- slut S3
        at = row.delivered_at or row.sent_at or row.created_at
        channel = "Sms" if row.channel == CHANNEL_SMS else "E-post"
        items.append(
            Item(
                at=at,
                kind="utskick",
                title=f"{channel}: {row.utskick.name}",
                detail=detail,
                url=reverse("flamingo:app_utskick", args=[row.utskick_id]),
                link_label="Rapporten",
            )
        )
    return items


def _engaged(seconds):
    minutes, rest = divmod(int(seconds or 0), 60)
    if minutes:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    return f"{rest} s"


#: Klickets enhet (analytics.utils.parse_user_agent, DeviceType) som den
#: skrivs i tidslinjen; okänd och bot visas inte.
DEVICE_LABELS = {"mobile": "mobil", "desktop": "dator", "tablet": "surfplatta"}


def _click_items(contact, limit):
    from .models import Click

    rows = (
        Click.objects.filter(account_id=contact.account_id, contact=contact, kind=Click.Kind.HUMAN)
        .select_related("utskick")
        .order_by("-at", "-pk")[:limit]
    )
    items = []
    for row in rows:
        parts = [row.utskick.name if row.utskick_id else ""]
        if row.engaged_seconds:
            parts.append(f"stannade {_engaged(row.engaged_seconds)} på sidan")
        if row.device in DEVICE_LABELS:
            parts.append(DEVICE_LABELS[row.device])
        items.append(
            Item(
                at=row.at,
                kind="click",
                title="Klickade på länken",
                detail=" · ".join(p for p in parts if p),
            )
        )
    return items


def _reply_items(contact, limit):
    from .models import Thread, ThreadMessage

    rows = (
        ThreadMessage.objects.filter(
            thread__account_id=contact.account_id,
            thread__contact=contact,
            direction=ThreadMessage.Direction.IN,
        )
        .select_related("thread")
        .order_by("-at", "-pk")[:limit]
    )
    items = []
    for row in rows:
        thread = row.thread
        stop = thread.kind == Thread.Kind.STOP
        text = " ".join(str(row.body or "").split())
        items.append(
            Item(
                at=row.at,
                kind="reply",
                title="Svarade STOPP" if stop else "Svarade",
                detail=text[:120],
                url=reverse("flamingo:app_lead", args=[thread.lead_id]) if thread.lead_id else "",
                link_label="Öppna i Inkorgen" if thread.lead_id else "",
            )
        )
    return items


# --- S3 (integrationen): mejlen (README I.7) -------------------------------
# Levererat, studsat och klagomål läses ur Recipient (_recipient_items ovan),
# öppnat ur händelsen opened som pixeln skriver en gång per mottagare
# (link_views.open_pixel). Öppningar är en indikation (K.1.7).

EVENT_TITLES[Event.OPENED] = "Öppnade (indikation)"
#: Mejlets läge i tidslinjen, när det skiljer sig från sms:ets.
EMAIL_DETAILS = {
    "bounced": "Studsade: adressen finns inte",
    "complained": "Markerade mejlet som skräppost",
}

# --- slut S3 --------------------------------------------------------------------

# --- S4 (rapport-byggaren): besök på egen sajt, sms från kortet och "Svarar
# oftast" (README I.7, E.6) --------------------------------------------------
# Besöket på kundens egen webbplats är en Event (site_visit, skriven av
# link_views.snippet_beacon med data {"klick", "sida", "varde"}) och visas av
# _event_items med rubriken nedan och detaljen ur site_visit_detail
# ("Höstservice · exempelror.example/priser"). Sms:en som kunden (eller
# byrån åt kunden) skrivit själv, från kontaktkortet eller som svar i
# Inkorgen, läses ur ThreadMessage av contact_sms_items (en källa i
# SOURCES); svaren på dem står redan i _reply_items.
#
#   contact_sms_items(contact, limit) -> list[Item]   "Sms till kontakten"
#   reply_habit(contact, now=None) -> str             "Svarar oftast på sms, kvällstid",
#                                                     eller "" (färre än REPLY_HABIT_MIN svar)

EVENT_TITLES[Event.SITE_VISIT] = "Besökte webbplatsen"
#: "Svarar oftast" visas först efter så här många svar (README L, Product 10).
REPLY_HABIT_MIN = 3
#: Så många av de senaste svaren läses för "Svarar oftast".
REPLY_HABIT_SCAN = 50
#: Tiden på dygnet (Stockholm) i "Svarar oftast": (från timme, till timme, ord).
REPLY_BANDS = (
    (6, 10, "på morgonen"),
    (10, 17, "dagtid"),
    (17, 22, "kvällstid"),
)
REPLY_NIGHT = "nattetid"
CONTACT_SMS_TITLE = "Sms till kontakten"


def site_visit_title(event):
    """Rubriken för ett mål på kundens webbplats ("Gjorde på webbplatsen:
    bokning"), annars "" (då gäller EVENT_TITLES)."""
    if event.kind != Event.SITE_VISIT or not isinstance(event.data, dict):
        return ""
    goal = str(event.data.get("mal") or "")[:40]
    return f"Gjorde på webbplatsen: {goal}" if goal else ""


def site_visit_detail(event):
    """Detaljen för ett besök på kundens webbplats: utskicket och sidan
    ("exempelror.example/priser"), utan frågedel."""
    data = event.data if isinstance(event.data, dict) else {}
    host = str(data.get("varde") or "")[:253]
    page = str(data.get("sida") or "").split("?", 1)[0].split("#", 1)[0][:80]
    where = f"{host}{page}" if host else page
    parts = [event.utskick.name if event.utskick_id else "", where]
    return " · ".join(p for p in parts if p)


def _sent_by(message):
    """Vem som skrev sms:et: "Skickat av Anna", "Skickat av ADX (Giovanni)
    åt kunden"."""
    user = message.sent_by
    first = ""
    if user is not None:
        first = (user.first_name or user.get_username() or "").strip()
    if message.sent_as_staff:
        return f"Skickat av ADX ({first}) åt kunden" if first else "Skickat av ADX åt kunden"
    return f"Skickat av {first}" if first else ""


def contact_sms_items(contact, limit):
    """Sms som kunden själv skrivit till kontakten (från kontaktkortet eller
    som svar i Inkorgen): utgående meddelanden i kontaktens sms-trådar hos
    kontots egna trådar, inte utskickens sms och inte de automatiska svaren
    på STOPP och START. Med vem som skickade ("Skickat av ADX (Giovanni) åt
    kunden") och "Kom inte fram" när det inte gick i väg."""
    from django.db.models import Q

    from .models import ThreadMessage

    rows = (
        ThreadMessage.objects.filter(
            thread__account_id=contact.account_id,
            thread__contact=contact,
            thread__channel=CHANNEL_SMS,
            direction=ThreadMessage.Direction.OUT,
            inbound__isnull=True,
        )
        .filter(Q(sent_by__isnull=False) | Q(sent_as_staff=True) | Q(sms_message__source="reply"))
        .select_related("thread", "sent_by")
        .order_by("-at", "-pk")[:limit]
    )
    items = []
    for row in rows:
        text = " ".join(str(row.body or "").split())
        parts = [text[:120], _sent_by(row)]
        if row.status == ThreadMessage.Status.FAILED:
            parts.append("Kom inte fram")
        elif row.status == ThreadMessage.Status.SENDING:
            parts.append("Skickas")
        thread = row.thread
        items.append(
            Item(
                at=row.at,
                kind="sms_out",
                title=CONTACT_SMS_TITLE,
                detail=" · ".join(p for p in parts if p),
                url=reverse("flamingo:app_lead", args=[thread.lead_id]) if thread.lead_id else "",
                link_label="Öppna i Inkorgen" if thread.lead_id else "",
                by_staff=bool(row.sent_as_staff),
            )
        )
    return items


def _band(hour):
    for start, end, word in REPLY_BANDS:
        if start <= hour < end:
            return word
    return REPLY_NIGHT


def _most_common(values):
    """Det vanligaste värdet, eller None när två eller fler delar förstaplatsen."""
    from collections import Counter

    ranked = Counter(values).most_common(2)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


#: Svar som är ett kommando och inte ett svar: inboundens nyckelord.
REPLY_HABIT_SKIP = frozenset({"stop", "start", "unsubscribe"})


def _is_command(meta, body):
    """STOPP, START och avregistreringar räknas aldrig som svar. Nyckelordet
    står i InboundMessage.meta så länge raden finns; efter gallringen
    (INBOUND_DAYS, ThreadMessage.inbound blir NULL) avgör texten, som
    trådmeddelandet behåller."""
    from .inbound import stop

    if isinstance(meta, dict) and meta.get("keyword") in REPLY_HABIT_SKIP:
        return True
    return stop.classify(body) in (stop.STOP, stop.START)


def reply_habit(contact, now=None):
    """Vanligaste kanalen och tiden på dygnet (Stockholm) för kontaktens svar
    (ThreadMessage in i kontots trådar, inte STOPP och START), bland de
    senaste REPLY_HABIT_SCAN, när minst REPLY_HABIT_MIN finns: "Svarar
    oftast på sms, kvällstid". Delar två kanaler eller två tider
    förstaplatsen utelämnas den delen; aldrig gissat: "" annars.

    Kommandona sorteras bort i Python (_is_command), inte i SQL: ett
    exclude på inbound__meta__keyword blir NOT(NULL) för ett vanligt svar
    vars meta saknar nyckeln, och då föll varje riktigt svar bort."""
    from apps.sms.pricing import STOCKHOLM

    from .models import ThreadMessage

    scanned = (
        ThreadMessage.objects.filter(
            thread__account_id=contact.account_id,
            thread__contact=contact,
            direction=ThreadMessage.Direction.IN,
        )
        .order_by("-at", "-pk")
        .values_list("at", "thread__channel", "body", "inbound__meta")[:REPLY_HABIT_SCAN]
    )
    rows = [(at, ch) for at, ch, body, meta in scanned if not _is_command(meta, body)]
    if len(rows) < REPLY_HABIT_MIN:
        return ""
    channel = _most_common([ch for _at, ch in rows])
    band = _most_common([_band(timezone.localtime(at, STOCKHOLM).hour) for at, _ch in rows])
    words = {CHANNEL_SMS: "sms", "email": "mejl"}
    if channel in words and band:
        return f"Svarar oftast på {words[channel]}, {band}"
    if channel in words:
        return f"Svarar oftast på {words[channel]}"
    if band:
        return f"Svarar oftast {band}"
    return ""


# --- slut S4 --------------------------------------------------------------------


#: Tidslinjens källor. Varje steg lägger till sina (README I.7).
SOURCES = [
    _consent_items,
    _event_items,
    _lead_items,
    _recipient_items,
    _click_items,
    _reply_items,
    # S4 (rapport-byggaren): sms som kunden skrivit själv.
    contact_sms_items,
]


def for_contact(contact, page=1, per_page=PER_PAGE):
    """En sida av kontaktens tidslinje, nyast först."""
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    limit = page * per_page + 1
    merged = []
    for source in SOURCES:
        merged.extend(source(contact, limit))
    merged.sort(key=lambda item: item.at, reverse=True)
    start = (page - 1) * per_page
    return Page(
        items=merged[start : start + per_page],
        number=page,
        has_next=len(merged) > page * per_page,
    )
