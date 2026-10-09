"""
Kontaktkortets tidslinje (README I.7): källorna slås ihop när kortet läses,
inget kopieras till Event (disken).

    for_contact(contact, page=1)   Page(items, number, has_next, has_previous)

Källor i S1: samtyckesloggen, händelserna (importerad, anmälan, förfrågan
utan egen rad) och förfrågningarna i Inkorgen (med "Öppna i Inkorgen").
S2: sms ur Recipient (skickat, levererat, gick inte fram, med länk till
rapporten), mänskliga klick ur Click (med tiden på sidan), svar och STOPP
ur ThreadMessage (med "Öppna i Inkorgen") och besöken på landningssidan
(Event lp_visit). Senare steg lägger till sina källor i SOURCES (mejl,
flöden): en funktion (contact, limit) -> lista med Item, nyast först.

Varje källa filtrerar på kontaktens konto, också förfrågningarna (H.1).
"""

from dataclasses import dataclass, field
from datetime import datetime

from django.urls import reverse

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
        detail = " · ".join(p for p in (source, row.source_detail, row.by_label) if p)
        items.append(
            Item(at=row.at, kind="consent", title=title, detail=detail, by_staff=row.by_staff)
        )
    return items


def _event_items(contact, limit):
    rows = (
        Event.objects.filter(account_id=contact.account_id, contact=contact)
        .exclude(kind=Event.LEAD, lead__isnull=False)
        .order_by("-at", "-pk")[:limit]
    )
    items = []
    for row in rows:
        detail = ""
        if row.kind == Event.IMPORTED:
            # Filens namn (äldre rader har bara importens nummer).
            detail = str(row.data.get("fil") or "")
        items.append(
            Item(
                at=row.at,
                kind=row.kind,
                title=EVENT_TITLES.get(row.kind, row.kind),
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


#: Tidslinjens källor. Varje steg lägger till sina (README I.7).
SOURCES = [
    _consent_items,
    _event_items,
    _lead_items,
    _recipient_items,
    _click_items,
    _reply_items,
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
