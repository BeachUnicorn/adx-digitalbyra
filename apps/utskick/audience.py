"""
Urvalet för ett utskick (README D.3 steg 1 och 2, H.1, I.6, I.8).

    clean(account, data) -> dict        Utskick.audience ur formuläret eller JSON;
                                        varje id genom access.owned_ids (ForeignIds -> 400)
    contacts(utskick, now=None) -> QuerySet
                                        kontakterna, alltid från
                                        Contact.objects.filter(account=utskick.account),
                                        listor och taggar med list__account= / tag__account=,
                                        minus undantagen, sorterade på pk
    count(utskick, now=None) -> dict    app_utskick_count och Granska (I.8 JSON)
    describe(utskick) -> str            "Lista Kunder · 388" (listans rad)

Frysningen (sending/freeze.py) och räkningen använder samma bedömning
(classify): samtycket för syftet (consent.ineligible_reason), spärrlistan
(en fråga per kanal och bit), veckotaket för reklam (en summering per kanal
och bit) och kontots tillåtna länder. Därför stämmer Granskas siffror med
det som frysts, så länge ingen hunnit ändra något däremellan.

Formen på Utskick.audience:

    {"lists": [id], "tags": [id], "segments": [], "contacts": [id],
     "exclude": {"lists": [id], "tags": [id], "segments": [], "recent_days": 14}}

recent_days 0 betyder inget undantag ("Fick ett utskick senaste 14 dagarna"
är recent_days 14: kontakter som fick ett utskick, i någon kanal, de
senaste 14 dagarna). Segment kommer med S4; till dess är varje segment-id
främmande. Ett id som ändå hamnat i databasen utan att höra till kontot
(manipulerat) ger inga mottagare: listor och taggar matchas alltid med
kontot i villkoret (H.1).
"""

import operator
from collections import Counter as Tally
from datetime import datetime, time, timedelta
from functools import reduce

from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from apps.sms.pricing import STOCKHOLM

from . import consent as consents
from . import keys
from .access import ForeignIds, owned_ids
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    REKLAM,
    Consent,
    Contact,
    ContactList,
    ListMembership,
    Recipient,
    Suppression,
    Tag,
    Utskick,
)

#: Kontakter per bit i frysningen och räkningen (D.3).
CHUNK = 2000
#: "Fick ett utskick senaste 14 dagarna" (I.8), och högsta tillåtna värde.
RECENT_DAYS = 14
MAX_RECENT_DAYS = 90
#: Högsta antal listor och taggar, och enskilda kontakter, i ett urval.
MAX_GROUPS = 200
MAX_CONTACTS = 1000

MODES = tuple(Utskick.ChannelMode.values)
_TRUE = ("on", "1", "true", "ja", "yes")


def empty():
    return {
        "lists": [],
        "tags": [],
        "segments": [],
        "contacts": [],
        "exclude": {"lists": [], "tags": [], "segments": [], "recent_days": 0},
    }


# ---------------------------------------------------------------------------
# Formuläret
# ---------------------------------------------------------------------------


def _values(data, key, nested=None):
    """Värdena för key ur en QueryDict (getlist) eller en dict (lista)."""
    if hasattr(data, "getlist"):
        name = f"{nested}_{key}" if nested else key
        return [v for v in data.getlist(name) if str(v).strip() != ""]
    source = data.get(nested) if nested else data
    if source is None:
        return []
    if not isinstance(source, dict):
        raise ForeignIds
    value = source.get(key)
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _recent_days(data):
    if hasattr(data, "getlist"):
        raw = data.get("exclude_recent_days", data.get("exclude_recent", ""))
    else:
        exclude = data.get("exclude") or {}
        if not isinstance(exclude, dict):
            raise ForeignIds
        raw = exclude.get("recent_days", 0)
    if raw is True:
        return RECENT_DAYS
    if raw in (None, False, "", 0, "0"):
        return 0
    text = str(raw).strip().lower()
    if text in _TRUE:
        return RECENT_DAYS
    if not text.isdigit():
        raise ForeignIds
    return max(0, min(MAX_RECENT_DAYS, int(text)))


def clean(account, data):
    """Urvalet ur en förfrågan, med varje id prövat mot kontot (H.1).

    data är request.POST (fälten lists, tags, contacts, segments,
    exclude_lists, exclude_tags, exclude_segments och exclude_recent, en
    kryssruta som ger 14 dagar, eller exclude_recent_days) eller en dict i
    Utskick.audience-form. Ett enda främmande eller felaktigt id ger
    ForeignIds (utskick_view svarar 400)."""
    if data is None:
        return empty()
    if not hasattr(data, "getlist") and not isinstance(data, dict):
        raise ForeignIds
    if _values(data, "segments") or _values(data, "segments", "exclude"):
        # Segment finns från S4; inget id kan höra till kontot före dess.
        raise ForeignIds
    return {
        "lists": owned_ids(ContactList, account, _values(data, "lists"), limit=MAX_GROUPS),
        "tags": owned_ids(Tag, account, _values(data, "tags"), limit=MAX_GROUPS),
        "segments": [],
        "contacts": owned_ids(Contact, account, _values(data, "contacts"), limit=MAX_CONTACTS),
        "exclude": {
            "lists": owned_ids(
                ContactList, account, _values(data, "lists", "exclude"), limit=MAX_GROUPS
            ),
            "tags": owned_ids(Tag, account, _values(data, "tags", "exclude"), limit=MAX_GROUPS),
            "segments": [],
            "recent_days": _recent_days(data),
        },
    }


def _ints(values):
    """Heltalen i en lista ur databasen; allt annat ignoreras (ett
    manipulerat värde ska ge färre mottagare, aldrig ett fel i ticken)."""
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            out.append(value)
        elif isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
            out.append(int(value))
    return out


def stored(utskick):
    """Utskick.audience som den ska läsas: bara heltal, alltid alla nycklar."""
    raw = utskick.audience if isinstance(utskick.audience, dict) else {}
    exclude = raw.get("exclude") if isinstance(raw.get("exclude"), dict) else {}
    try:
        recent = max(0, min(MAX_RECENT_DAYS, int(exclude.get("recent_days") or 0)))
    except (TypeError, ValueError):
        recent = 0
    return {
        "lists": _ints(raw.get("lists")),
        "tags": _ints(raw.get("tags")),
        "segments": [],
        "contacts": _ints(raw.get("contacts")),
        "exclude": {
            "lists": _ints(exclude.get("lists")),
            "tags": _ints(exclude.get("tags")),
            "segments": [],
            "recent_days": recent,
        },
    }


def is_empty(utskick):
    aud = stored(utskick)
    return not (aud["lists"] or aud["tags"] or aud["contacts"])


# ---------------------------------------------------------------------------
# Kontakterna
# ---------------------------------------------------------------------------


def _in_lists(account_id, list_ids):
    return Exists(
        ListMembership.objects.filter(
            contact=OuterRef("pk"), list_id__in=list_ids, list__account_id=account_id
        )
    )


def _in_tags(account_id, tag_ids):
    through = Contact.tags.through
    return Exists(
        through.objects.filter(
            contact_id=OuterRef("pk"), tag_id__in=tag_ids, tag__account_id=account_id
        )
    )


def recently_sent(account_id, days, now, exclude_utskick=None):
    """Exists för "fick ett utskick, i någon kanal, de senaste days dagarna"."""
    rows = Recipient.objects.filter(
        contact=OuterRef("pk"),
        utskick__account_id=account_id,
        status__in=Recipient.SENT_LIKE,
        sent_at__gte=now - timedelta(days=days),
    )
    if exclude_utskick is not None:
        rows = rows.exclude(utskick_id=exclude_utskick)
    return Exists(rows)


def contacts(utskick, now=None):
    """Urvalets kontakter, sorterade på pk. Börjar alltid från kontots egna
    kontakter, och listor och taggar matchas med kontot i villkoret, så ett
    främmande id i Utskick.audience ger inget (H.1). Ett urval utan listor,
    taggar och kontakter är tomt (aldrig "alla")."""
    now = now or timezone.now()
    account_id = utskick.account_id
    aud = stored(utskick)
    qs = Contact.objects.filter(account_id=account_id)
    include = []
    if aud["lists"]:
        include.append(Q(_in_lists(account_id, aud["lists"])))
    if aud["tags"]:
        include.append(Q(_in_tags(account_id, aud["tags"])))
    if aud["contacts"]:
        include.append(Q(pk__in=aud["contacts"]))
    if not include:
        return qs.none()
    qs = qs.filter(reduce(operator.or_, include))
    exclude = aud["exclude"]
    if exclude["lists"]:
        qs = qs.exclude(_in_lists(account_id, exclude["lists"]))
    if exclude["tags"]:
        qs = qs.exclude(_in_tags(account_id, exclude["tags"]))
    if exclude["recent_days"]:
        qs = qs.exclude(recently_sent(account_id, exclude["recent_days"], now, utskick.pk))
    return qs.order_by("pk")


def chunks(utskick, after=0, size=CHUNK, now=None):
    """Kontakterna i bitar om size, med nyckeln pk (frysningens markör)."""
    base = contacts(utskick, now)
    cursor = after
    while True:
        chunk = list(base.filter(pk__gt=cursor)[:size])
        if not chunk:
            return
        yield chunk
        cursor = chunk[-1].pk


# ---------------------------------------------------------------------------
# Bedömningen per kontakt
# ---------------------------------------------------------------------------


def week_bounds(at):
    """Måndag 00.00 till nästa måndag 00.00 (svensk ISO-vecka) runt at."""
    local = timezone.localtime(at, STOCKHOLM)
    monday = local.date() - timedelta(days=local.weekday())
    start = datetime.combine(monday, time(0, 0), tzinfo=STOCKHOLM)
    end = datetime.combine(monday + timedelta(days=7), time(0, 0), tzinfo=STOCKHOLM)
    return start, end


def week_counts(account_id, contact_ids, channel, at, exclude_utskick=None, exclude_pk=None):
    """{kontakt: antal reklamutskick} på kanalen under veckan runt at. Det
    som räknas är skickat (eller på väg) från ett reklamutskick; information
    räknas inte och följer inte taket (H.5)."""
    if not contact_ids:
        return {}
    start, end = week_bounds(at)
    rows = Recipient.objects.filter(
        contact_id__in=list(contact_ids),
        channel=channel,
        utskick__account_id=account_id,
        utskick__purpose=REKLAM,
        status__in=(*Recipient.SENT_LIKE, Recipient.Status.SENDING),
        sent_at__gte=start,
        sent_at__lt=end,
    )
    if exclude_utskick is not None:
        rows = rows.exclude(utskick_id=exclude_utskick)
    if exclude_pk is not None:
        rows = rows.exclude(pk=exclude_pk)
    return dict(rows.values("contact_id").annotate(n=Count("pk")).values_list("contact_id", "n"))


def weekly_cap(settings_row, channel):
    if settings_row is None:
        return 2 if channel == CHANNEL_SMS else 4
    return settings_row.weekly_cap_sms if channel == CHANNEL_SMS else settings_row.weekly_cap_email


def allowed_countries(account):
    """Länderna kundens SmsAccount får skicka till, eller None (inget att pröva)."""
    from .sending.sms_wrapper import sms_account_for

    sms_account = sms_account_for(account)
    return set(sms_account.countries) if sms_account is not None else None


class Judge:
    """Bedömningen av en bit kontakter för ett utskick: samtycket, spärren,
    veckotaket och landet, med fyra frågor per bit (samtycken, spärrar per
    kanal, veckotaket per kanal). reasons(contact) ger {kanal: orsak} där ""
    betyder att kontakten får utskicket på kanalen."""

    def __init__(self, utskick, chunk, *, at, settings_row, countries=None, channels=None):
        self.utskick = utskick
        self.purpose = utskick.purpose if utskick.purpose in consents.PURPOSES else REKLAM
        self.channels = channels or (CHANNEL_SMS, CHANNEL_EMAIL)
        self.countries = countries
        ids = [c.pk for c in chunk]
        self.consents = {
            (row.contact_id, row.channel): row
            for row in Consent.objects.filter(contact_id__in=ids, channel__in=self.channels)
        }
        self.hashes = {}
        self.suppressed = {}
        self.weekly = {}
        self.caps = {}
        for channel in self.channels:
            hashes = {}
            for contact in chunk:
                address = contact.address(channel)
                if address:
                    hashes[contact.pk] = keys.value_hash(channel, address)
            self.hashes[channel] = hashes
            wanted = {h for h in hashes.values() if h}
            self.suppressed[channel] = (
                set(
                    Suppression.objects.filter(
                        account_id=utskick.account_id, channel=channel, value_hash__in=wanted
                    ).values_list("value_hash", flat=True)
                )
                if wanted
                else set()
            )
            if self.purpose == REKLAM:
                self.caps[channel] = weekly_cap(settings_row, channel)
                self.weekly[channel] = week_counts(
                    utskick.account_id, list(hashes), channel, at, exclude_utskick=utskick.pk
                )

    def reason(self, contact, channel):
        address = contact.address(channel)
        if not address:
            return Recipient.SkipReason.NO_ADDRESS
        value_hash = self.hashes[channel].get(contact.pk, "")
        consent = self.consents.get((contact.pk, channel))
        reason = consents.ineligible_reason(
            contact,
            channel,
            self.purpose,
            consent=consent,
            suppressed=value_hash in self.suppressed[channel],
        )
        if reason:
            return reason
        if channel == CHANNEL_SMS and self.countries is not None:
            country = (contact.phone_country or "").upper()
            if country and country not in self.countries:
                return Recipient.SkipReason.COUNTRY
        if self.purpose == REKLAM:
            cap = self.caps[channel]
            if self.weekly[channel].get(contact.pk, 0) >= cap:
                return Recipient.SkipReason.WEEKLY_CAP
        return ""

    def reasons(self, contact):
        return {channel: self.reason(contact, channel) for channel in self.channels}


def plan(mode, reasons):
    """Raderna för kontakten i kanalläget mode: [(kanal, orsak)], där orsak
    "" är en mottagare i kö och annars hoppas kontakten över.

    sms_only och email_only: den kanalen. sms_then_email: sms om det går,
    annars e-post om det går, annars överhoppad med sms-orsaken (e-postens
    när kontakten saknar mobil). both: varje kanal som går; går ingen, en
    överhoppad rad som för sms_then_email."""
    sms = reasons.get(CHANNEL_SMS, Recipient.SkipReason.NO_ADDRESS)
    email = reasons.get(CHANNEL_EMAIL, Recipient.SkipReason.NO_ADDRESS)
    if mode == Utskick.ChannelMode.EMAIL_ONLY:
        return [(CHANNEL_EMAIL, email)]
    if mode == Utskick.ChannelMode.SMS_ONLY:
        return [(CHANNEL_SMS, sms)]
    if mode == Utskick.ChannelMode.BOTH:
        rows = [(ch, "") for ch, why in ((CHANNEL_SMS, sms), (CHANNEL_EMAIL, email)) if not why]
        if rows:
            return rows
    elif not sms:
        return [(CHANNEL_SMS, "")]
    elif not email:
        return [(CHANNEL_EMAIL, "")]
    if sms == Recipient.SkipReason.NO_ADDRESS and email != Recipient.SkipReason.NO_ADDRESS:
        return [(CHANNEL_EMAIL, email)]
    return [(CHANNEL_SMS, sms)]


def channels_for(mode):
    if mode == Utskick.ChannelMode.SMS_ONLY:
        return (CHANNEL_SMS,)
    if mode == Utskick.ChannelMode.EMAIL_ONLY:
        return (CHANNEL_EMAIL,)
    return (CHANNEL_SMS, CHANNEL_EMAIL)


# ---------------------------------------------------------------------------
# Räkningen (Mottagare, Kanal och Granska)
# ---------------------------------------------------------------------------


def send_moment(utskick, now):
    """Tiden veckotaket räknas för: den schemalagda tiden när den ligger
    framåt, annars nu."""
    at = utskick.scheduled_at
    if utskick.send_mode == Utskick.SendMode.AT and at and at > now:
        return at
    return now


def count(utskick, now=None):
    """Siffrorna för urvalet (I.8 app_utskick_count, I.6 Granska):

        {"total": kontakter i urvalet,
         "modes": {"sms_only": {"sms", "email", "skipped"}, "email_only": {...},
                   "sms_then_email": {...}, "both": {...}},
         "channel_mode": utskickets läge,
         "sms": n, "email": n, "skipped": n          (för utskickets läge),
         "skipped_by_reason": {orsak: n}             (för utskickets läge)}

    En överhoppad kontakt räknas en gång per läge. Samma bedömning som
    frysningen, i bitar om CHUNK."""
    from .access import settings_for

    now = now or timezone.now()
    at = send_moment(utskick, now)
    settings_row = settings_for(utskick.account)
    countries = allowed_countries(utskick.account)
    modes = {mode: {"sms": 0, "email": 0, "skipped": 0} for mode in MODES}
    by_reason = {mode: Tally() for mode in MODES}
    total = 0
    for chunk in chunks(utskick, now=now):
        total += len(chunk)
        judge = Judge(utskick, chunk, at=at, settings_row=settings_row, countries=countries)
        for contact in chunk:
            reasons = judge.reasons(contact)
            for mode in MODES:
                rows = plan(mode, reasons)
                queued = [ch for ch, why in rows if not why]
                for channel in queued:
                    modes[mode][channel] += 1
                if not queued:
                    modes[mode]["skipped"] += 1
                    by_reason[mode][rows[0][1]] += 1
    mode = utskick.channel_mode if utskick.channel_mode in MODES else MODES[0]
    return {
        "total": total,
        "modes": modes,
        "channel_mode": mode,
        "sms": modes[mode]["sms"],
        "email": modes[mode]["email"],
        "skipped": modes[mode]["skipped"],
        "skipped_by_reason": dict(by_reason[mode].most_common()),
    }


# ---------------------------------------------------------------------------
# Listans rad
# ---------------------------------------------------------------------------


def _group(n):
    return f"{int(n):,}".replace(",", " ")


def recipient_total(utskick):
    """Mottagarna i kö eller skickade: efter frysningen ur frozen_counts,
    annars ur bekräftelsen, annars None."""
    frozen = utskick.frozen_counts or {}
    if utskick.frozen_at and frozen:
        return int(frozen.get("sms", 0)) + int(frozen.get("email", 0))
    summary = utskick.confirm_summary or {}
    if summary:
        return confirmed_total(summary, utskick.channel_mode)
    return None


def confirmed_total(summary, mode):
    """Antalet mottagare i bekräftelsen (confirm_summary): toppens sms och
    email, eller count()-formen med modes. None när siffrorna saknas."""
    if not isinstance(summary, dict):
        return None
    if "sms" in summary or "email" in summary:
        try:
            return int(summary.get("sms") or 0) + int(summary.get("email") or 0)
        except (TypeError, ValueError):
            return None
    row = (summary.get("modes") or {}).get(mode) if isinstance(summary.get("modes"), dict) else None
    if isinstance(row, dict):
        try:
            return int(row.get("sms") or 0) + int(row.get("email") or 0)
        except (TypeError, ValueError):
            return None
    return None


def describe(utskick):
    """Listans rad under namnet: "Lista Kunder · 388", "Taggar VIP, Nya ·
    40", "3 kontakter". Namnen hämtas med kontot i villkoret."""
    aud = stored(utskick)
    parts = []
    names = list(
        ContactList.objects.filter(account_id=utskick.account_id, pk__in=aud["lists"])
        .order_by("name")
        .values_list("name", flat=True)
    )
    if names:
        parts.append(("Lista " if len(names) == 1 else "Listor ") + ", ".join(names))
    tags = list(
        Tag.objects.filter(account_id=utskick.account_id, pk__in=aud["tags"])
        .order_by("name")
        .values_list("name", flat=True)
    )
    if tags:
        parts.append(("Tagg " if len(tags) == 1 else "Taggar ") + ", ".join(tags))
    if aud["contacts"]:
        n = len(aud["contacts"])
        parts.append(f"{_group(n)} {'kontakt' if n == 1 else 'kontakter'}")
    text = ", ".join(parts) or "Inga mottagare valda"
    total = recipient_total(utskick)
    if total is not None and parts:
        text += f" · {_group(total)}"
    return text
