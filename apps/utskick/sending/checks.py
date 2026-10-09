"""
Kontrollerna före varje sms (README D.4 "Send-time checks", D.8, H.5, H.6,
G.2, I.6). Läser alltid färskt ur databasen: en paus, en STOPP eller en
avstängning mitt i en omgång gäller från nästa mottagare.

    Check(defer, skip, reason, not_before, text, sender, collided)   .ok
    send_time_checks(recipient, now=None) -> Check
        uppskjutande (raden går tillbaka i kön, inget försök räknas):
          utskicket är inte sending, tidsfönstret är stängt, Switchboard
          sms_enabled av eller nödbromsen på, kontot får inte skicka
          (utskick av, Flamingo av, kunden inaktiv, sending_blocked)
        per person (raden hoppas över med orsaken):
          spärrad, borttagen kontakt, nytt nummer, samtycket (reklam),
          veckotaket (reklam), kollision på svarsnumret utan namnavsändare
    reply_checks(account, contact, address, now=None) -> Check
        svar från Inkorgen och kontaktkortets sms (G.2): utan fönster,
        samtycke, veckotak och kollision; spärrad ger "Personen har svarat STOPP."
    sender_for(recipient, now=None) -> (avsändare eller None, collided)
    sender_identified(utskick, body=None) -> bool     företagsnamnet i texten (H.5)
    information_problems(utskick) -> list[str]        H.5 för information
    content_fingerprint(utskick) -> str               det byråns undantag gäller
    override_valid(utskick) -> bool                   undantaget gäller texten som står
    collision_count(utskick, now=None) -> int         I.6 varningen
    breaker_active(now=None) -> bool                  Switchboard.sms_paused_until > nu
    sms_ready() -> bool                               Switchboard.sms_enabled (D.8)
    sendable(account) -> str                          "" eller varför kontot inte får skicka
    sendable_q(prefix) -> Q                           samma regel som filter (D.2)

Nödbromsen (D.4) bor också här: provider_trouble(now) räknar oklara svar
och provider_error hos 46elks för hela ADX de senaste två minuterna och
drar i bromsen vid tre (Switchboard.sms_paused_until = nu + 10 minuter,
ett larm till byrån).
"""

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.sms.models import SmsMessage

from .. import consent as consents
from .. import keys, timing
from ..models import (
    CHANNEL_SMS,
    INFORMATION,
    REKLAM,
    Contact,
    Recipient,
    Suppression,
    Switchboard,
    TrackedLink,
    Utskick,
    UtskickSettings,
)

logger = logging.getLogger(__name__)

#: Kollisionen på svarsnumret (D4): en annan kund skickade från numret till
#: samma person så här nyligen.
COLLISION_DAYS = 14
#: Nödbromsen (D.4): så många oklara svar eller provider_error inom fönstret
#: stoppar all sms-sändning från utskicken så här länge.
BREAKER_COUNT = 3
BREAKER_WINDOW = timedelta(minutes=2)
BREAKER_PAUSE = timedelta(minutes=10)

SMS_OFF_TEXT = "Sms-utskick är inte påslagna än."
BREAKER_TEXT = "Sms kan inte skickas just nu. Försök igen om en stund."
SUPPRESSED_TEXT = "Personen har svarat STOPP."
BLOCKED_TEXT = "ADX har stoppat sändningen för kontot. Kontakta ADX."
DISABLED_TEXT = "Utskick är inte aktiverat för dig. Be ADX slå på det."
NO_ADDRESS_TEXT = "Numret saknas."
SENDER_TEXT = "Mottagaren ser bara ett nummer. Skriv {name} i texten."
LOOKS_LIKE_AD_TEXT = "Det här ser ut som reklam. Välj Reklam eller ta bort erbjudandet."
INFO_REASON_TEXT = "Välj varför du skickar information."
INFO_OTHER_TEXT = "Skriv varför du skickar information."
INFO_LP_TEXT = "Information kan inte länka till en Flamingo-sida. Välj Reklam eller ta bort länken."
INFO_HOST_TEXT = "Information kan bara länka till din egen webbplats. Ta bort länken till {host}."
INFO_FIELD_TEXT = (
    "Fältet {{fält:{key}}} har värden som ser ut som reklam för några mottagare. "
    "Välj Reklam eller ta bort fältet ur texten."
)
#: Så många mottagare eller kontakter läses när fältvärdena prövas (H.5).
FIELD_SCAN = 25_000


@dataclass
class Check:
    """Utfallet av en kontroll. defer: tillbaka i kön (not_before när den
    får försökas igen, None = när utskicket eller kontot får skicka igen).
    skip: personen hoppas över (reason är Recipient.SkipReason). text är
    meningen för en vy (reply_checks). sender och collided är avsändaren
    send_time_checks valde (sender_for), så att slingan slipper fråga igen."""

    defer: bool = False
    skip: bool = False
    reason: str = ""
    not_before: object = None
    text: str = ""
    sender: str = ""
    collided: bool = False

    @property
    def ok(self):
        return not (self.defer or self.skip)


def _defer(reason, not_before=None, text=""):
    return Check(defer=True, reason=reason, not_before=not_before, text=text)


def _skip(reason, text=""):
    return Check(skip=True, reason=reason, text=text)


# ---------------------------------------------------------------------------
# Brytarna och kontot
# ---------------------------------------------------------------------------


def switch():
    """Switchboard färskt ur databasen (raden skapas vid första behovet)."""
    return Switchboard.get_solo()


def sms_ready():
    """Byrån har slagit på sms-utskick (D.8)."""
    return bool(switch().sms_enabled)


def breaker_active(now=None, row=None):
    """Nödbromsen: sms_paused_until ligger framåt. Prövas mot klockans tid
    när now saknas (bromsen sätts med klockans tid)."""
    now = now or timezone.now()
    row = row or switch()
    return bool(row.sms_paused_until and row.sms_paused_until > now)


def sendable_q(prefix=""):
    """Kontot får skicka (D.2): Flamingo på, kunden aktiv, utskick på och
    inte stoppat av byrån. prefix är vägen till FlamingoAccount
    ("utskick__account__" för mottagare)."""
    return Q(
        **{
            f"{prefix}is_enabled": True,
            f"{prefix}customer__is_active": True,
            f"{prefix}utskick__is_enabled": True,
            f"{prefix}utskick__sending_blocked": False,
        }
    )


def sendable(account, settings_row=None):
    """ "" när kontot får skicka, annars "blocked" (byrån har stoppat
    sändningen) eller "account_disabled" (utskick av, Flamingo av, kunden
    inaktiv). Läser färskt."""
    from ..access import is_enabled

    row = settings_row
    if row is None:
        row = UtskickSettings.objects.filter(account_id=account.pk).first()
    if row is None or not is_enabled(account, row):
        return Utskick.PauseReason.ACCOUNT_DISABLED
    if row.sending_blocked:
        return Utskick.PauseReason.BLOCKED
    return ""


# ---------------------------------------------------------------------------
# Avsändaren och kollisionen på svarsnumret (D4)
# ---------------------------------------------------------------------------


def reply_number():
    return str(getattr(settings, "UTSKICK_REPLY_NUMBER", "") or "").strip()


def _sms_account(account):
    from .sms_wrapper import sms_account_for

    return sms_account_for(account)


def name_sender(account, sms_account=None):
    """Kontots första godkända namnavsändare, eller ""."""
    sms_account = sms_account if sms_account is not None else _sms_account(account)
    if sms_account is None:
        return ""
    senders = sms_account.senders
    return senders[0] if senders else ""


def _other_reply_number_sms(address, sms_account_id, now):
    """Sms från svarsnumret till address från en annan kund de senaste 14
    dagarna (partiellt index sms_msg_reply_number)."""
    rows = SmsMessage.objects.filter(
        to=address,
        sender=reply_number(),
        created_at__gte=now - timedelta(days=COLLISION_DAYS),
    ).exclude(status__in=SmsMessage.STOPPED)
    if sms_account_id:
        rows = rows.exclude(account_id=sms_account_id)
    return rows


def sender_for(recipient, now=None, utskick=None, sms_account=None):
    """(avsändaren, collided). Namnavsändaren när utskicket har en; annars
    svarsnumret, utom när en annan kund skickat från numret till samma
    nummer de senaste 14 dagarna: då kontots första namnavsändare (och
    texten får /s/-länken i stället för "Svara STOPP"), eller (None, True)
    när kontot inte har någon."""
    now = now or timezone.now()
    utskick = utskick or recipient.utskick
    account = utskick.account
    sms_account = sms_account if sms_account is not None else _sms_account(account)
    if utskick.sms_sender_kind == Utskick.SenderKind.NAME:
        return (utskick.sms_sender_name or name_sender(account, sms_account)), False
    number = reply_number()
    if not number:
        return name_sender(account, sms_account) or None, False
    if _other_reply_number_sms(recipient.address, getattr(sms_account, "pk", None), now).exists():
        return name_sender(account, sms_account) or None, True
    return number, False


def collision_count(utskick, now=None):
    """Hur många av utskickets sms-mottagare (efter frysningen) eller
    kontakter (före) som nyss fick sms från en annan kund via svarsnumret
    (I.6 varnar när utskicket ber om svar)."""
    from .. import audience

    now = now or timezone.now()
    if utskick.sms_sender_kind == Utskick.SenderKind.NAME or not reply_number():
        return 0
    if utskick.frozen_at or utskick.recipients.exists():
        phones = utskick.recipients.filter(
            channel=CHANNEL_SMS, status=Recipient.Status.QUEUED
        ).values_list("address", flat=True)
    else:
        phones = audience.contacts(utskick, now).exclude(phone="").values_list("phone", flat=True)
    sms_account = _sms_account(utskick.account)
    own = getattr(sms_account, "pk", None)
    hits = 0
    batch = []

    def flush(batch):
        if not batch:
            return 0
        rows = SmsMessage.objects.filter(
            to__in=batch,
            sender=reply_number(),
            created_at__gte=now - timedelta(days=COLLISION_DAYS),
        ).exclude(status__in=SmsMessage.STOPPED)
        if own:
            rows = rows.exclude(account_id=own)
        return rows.values("to").distinct().count()

    for phone in phones.iterator(chunk_size=2000):
        if phone:
            batch.append(phone)
        if len(batch) >= 1000:
            hits += flush(batch)
            batch = []
    return hits + flush(batch)


def sender_identified(utskick, body=None):
    """Står företagets namn (display_name) i texten? Krävs för sms från
    svarsnumret (H.5); en namnavsändare visar sig själv."""
    if utskick.sms_sender_kind == Utskick.SenderKind.NAME:
        return True
    from ..access import settings_for

    name = (settings_for(utskick.account).display_name or "").strip()
    if not name:
        return True
    text = utskick.sms_body if body is None else body
    return name.casefold() in str(text or "").casefold()


# ---------------------------------------------------------------------------
# Kontrollerna före sändningen
# ---------------------------------------------------------------------------


def _week_count(recipient, utskick, now):
    from .. import audience

    counts = audience.week_counts(
        utskick.account_id,
        [recipient.contact_id],
        recipient.channel,
        now,
        exclude_pk=recipient.pk,
    )
    return counts.get(recipient.contact_id, 0)


def send_time_checks(recipient, now=None):
    """Kontrollerna för en mottagare i ett utskick, precis före sändningen
    (D.4). Färska läsningar av utskicket, kontot, Switchboard, kontakten,
    samtycket och spärren."""
    now = now or timezone.now()
    utskick = (
        Utskick.objects.select_related("account", "account__customer")
        .filter(pk=recipient.utskick_id)
        .first()
    )
    if utskick is None or utskick.status != Utskick.Status.SENDING:
        return _defer("not_sending")
    account = utskick.account
    settings_row = UtskickSettings.objects.filter(account_id=account.pk).first()
    why = sendable(account, settings_row)
    if why:
        return _defer(why)
    row = switch()
    if not row.sms_enabled:
        return _defer("sms_off", text=SMS_OFF_TEXT)
    if breaker_active(row=row):
        return _defer("breaker", not_before=row.sms_paused_until, text=BREAKER_TEXT)
    if not timing.sms_window_open(settings_row, now):
        return _defer("window", not_before=timing.next_window_start(settings_row, now))

    # Per person.
    if recipient.contact_id is None:
        return _skip(Recipient.SkipReason.DELETED)
    contact = Contact.objects.filter(pk=recipient.contact_id, account_id=account.pk).first()
    if contact is None:
        return _skip(Recipient.SkipReason.DELETED)
    channel = recipient.channel
    address = recipient.address
    if not address:
        return _skip(Recipient.SkipReason.NO_ADDRESS)
    value_hash = keys.value_hash(channel, address)
    suppressed = Suppression.objects.filter(
        account_id=account.pk, channel=channel, value_hash=value_hash
    ).exists()
    if suppressed:
        return _skip(Recipient.SkipReason.SUPPRESSED)
    if keys.clean_value(channel, contact.address(channel)) != keys.clean_value(channel, address):
        return _skip(Recipient.SkipReason.ADDRESS_CHANGED)
    purpose = utskick.purpose if utskick.purpose in (REKLAM, INFORMATION) else REKLAM
    reason = consents.ineligible_reason(contact, channel, purpose, suppressed=False)
    if reason:
        return _skip(reason)
    if purpose == REKLAM:
        from .. import audience

        cap = audience.weekly_cap(settings_row, channel)
        if _week_count(recipient, utskick, now) >= cap:
            return _skip(Recipient.SkipReason.WEEKLY_CAP)
    if channel != CHANNEL_SMS:
        return Check()
    sender, collided = sender_for(recipient, now, utskick=utskick)
    if not sender:
        return _skip(Recipient.SkipReason.REPLY_COLLISION)
    return Check(sender=sender, collided=collided)


def reply_checks(account, contact, address, now=None):
    """Svar från Inkorgen och kontaktkortets sms (G.2, H.6): Switchboard,
    nödbromsen, kontot och spärren. Inget fönster, inget samtycke (personen
    skrev till oss), inget veckotak och ingen kollision (vårt svar blir
    rätteligen det senaste från numret). contact får vara None (en tråd utan
    kontakt); spärren prövas då på adressen. text är meningen för vyn."""
    now = now or timezone.now()
    if account is None or getattr(account, "is_demo", False):
        from .sms_wrapper import DEMO_TEXT

        return _defer("demo", text=DEMO_TEXT)
    row = switch()
    if not row.sms_enabled:
        return _defer("sms_off", text=SMS_OFF_TEXT)
    if breaker_active(row=row):
        return _defer("breaker", not_before=row.sms_paused_until, text=BREAKER_TEXT)
    why = sendable(account)
    if why == Utskick.PauseReason.BLOCKED:
        return _defer(why, text=BLOCKED_TEXT)
    if why:
        return _defer(why, text=DISABLED_TEXT)
    address = keys.clean_value(CHANNEL_SMS, address)
    if not address:
        return _skip(Recipient.SkipReason.NO_ADDRESS, text=NO_ADDRESS_TEXT)
    if Suppression.objects.filter(
        account_id=account.pk,
        channel=CHANNEL_SMS,
        value_hash=keys.value_hash(CHANNEL_SMS, address),
    ).exists():
        return _skip(Recipient.SkipReason.SUPPRESSED, text=SUPPRESSED_TEXT)
    if contact is not None and contact.account_id != account.pk:
        return _skip(Recipient.SkipReason.DELETED, text=NO_ADDRESS_TEXT)
    return Check(sender=reply_number())


# ---------------------------------------------------------------------------
# Information (H.5)
# ---------------------------------------------------------------------------

#: Ord, priser, procent och koder som gör ett utskick till reklam (H.5).
OFFER_RE = re.compile(
    r"(?<![0-9a-zåäöéü])"
    r"(?:erbjudande\w*|rabatt\w*|kampanj\w*|rea|rean|fynd\w*|gratis|spara|sparar|sparade|spartips|"
    r"kr|kronor|sek|kod|koden|koder)"
    r"(?![0-9a-zåäöéü])"
    r"|%|\d+\s*(?::-|kr\b|sek\b)|\bprocent\b",
    re.IGNORECASE,
)


def looks_like_ad(text):
    return bool(OFFER_RE.search(str(text or "")))


def own_hosts(account):
    """Värdarna information får länka till: kundens egna domäner som
    länkarna ser dem (links.own_domains: webbplatsen i ADX kundregister,
    verifierade avsändardomäner från S3 och skriptets domäner från S4),
    aldrig adressen kunden själv skrev i Flamingo."""
    from .. import links

    return links.own_domains(account)


def _own(host, hosts):
    host = (host or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return any(host == h or host.endswith("." + h) for h in hosts)


def content_fingerprint(utskick):
    """Det byråns undantag för information gäller (H.5): skälet, texten och
    reservtexterna, som en kort hash. Ändras något av dem gäller undantaget
    inte längre (override_valid), också när en mall läggs in."""
    data = json.dumps(
        [
            utskick.info_reason or "",
            utskick.info_reason_text or "",
            utskick.sms_body or "",
            utskick.merge_fallbacks if isinstance(utskick.merge_fallbacks, dict) else {},
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:32]


def override_valid(utskick):
    """Gäller byråns undantag (content_override) texten som står nu?"""
    override = utskick.content_override if isinstance(utskick.content_override, dict) else {}
    if not override.get("reason"):
        return False
    return hmac.compare_digest(str(override.get("fingerprint") or ""), content_fingerprint(utskick))


def _ad_text(utskick):
    """Texten som prövas mot reklamorden: sms-texten med reservtexterna i
    klamrarna kvar ({förnamn|halva priset} prövas också) och utskickets
    reservtexter (merge_fallbacks)."""
    from .. import composer

    def keep_fallback(match):
        _name, bar, fallback = match.group(1).partition("|")
        return f" {fallback} " if bar else " "

    parts = [composer.TOKEN_RE.sub(keep_fallback, utskick.sms_body or "")]
    fallbacks = utskick.merge_fallbacks if isinstance(utskick.merge_fallbacks, dict) else {}
    parts += [str(value) for value in fallbacks.values() if value]
    return " ".join(parts)


def _field_values(utskick, tags):
    """(platshållare, värde) för fälten texten använder: mottagarnas frysta
    värden när utskicket är fryst (Recipient.merge), annars kontakternas i
    urvalet. Högst FIELD_SCAN rader."""
    from .. import audience

    keys_used = [t[len("fält:") :] for t in tags]
    if utskick.frozen_at or utskick.freeze_cursor:
        rows = (
            Recipient.objects.filter(
                utskick=utskick, channel=CHANNEL_SMS, status=Recipient.Status.QUEUED
            )
            .values_list("merge", flat=True)
            .iterator(chunk_size=2000)
        )
        for count, merge in enumerate(rows):
            if count >= FIELD_SCAN:
                return
            for tag in tags:
                value = (merge or {}).get(tag)
                if value:
                    yield tag, value
        return
    rows = audience.contacts(utskick).values_list("fields", flat=True)[:FIELD_SCAN]
    for fields in rows.iterator(chunk_size=2000):
        for key in keys_used:
            value = (fields or {}).get(key)
            if value not in (None, ""):
                yield "fält:" + key, value


def field_problems(utskick):
    """Fältvärden ({fält:...}) i ett informationsutskick som ser ut som
    reklam (H.5). Byråns undantag släpper dem inte: värdena kan ändras
    efteråt av en import. En text per fält."""
    from .. import composer

    tags = [t for t in composer.placeholders(utskick.sms_body or "").tags if t.startswith("fält:")]
    if not tags:
        return []
    flagged = []
    for tag, value in _field_values(utskick, tags):
        if tag not in flagged and looks_like_ad(str(value)):
            flagged.append(tag)
            if len(flagged) == len(tags):
                break
    return [INFO_FIELD_TEXT.format(key=tag[len("fält:") :]) for tag in flagged]


def information_problems(utskick):
    """Det som stoppar ett informationsutskick i Granska och i
    förkontrollerna (H.5): skälet, Flamingo-sidor, länkar till andra
    webbplatser än kundens egen, ord, priser och koder som ser ut som
    reklam i texten och reservtexterna, och fältvärden som gör det. Byråns
    undantag (content_override med skäl) släpper bara reklamorden i texten
    det gavs för (override_valid), aldrig länkarna eller fältvärdena. Tom
    lista för reklam."""
    if utskick.purpose != INFORMATION:
        return []
    problems = []
    if not utskick.info_reason:
        problems.append(INFO_REASON_TEXT)
    elif utskick.info_reason == Utskick.InfoReason.ANNAT and not utskick.info_reason_text.strip():
        problems.append(INFO_OTHER_TEXT)
    from .. import composer

    body = utskick.sms_body or ""
    used = list(
        TrackedLink.objects.filter(utskick=utskick, key__in=composer.placeholders(body).links)
    )
    if any(link.kind == TrackedLink.Kind.LP for link in used):
        problems.append(INFO_LP_TEXT)
    hosts = own_hosts(utskick.account)
    for link in used:
        if link.kind != TrackedLink.Kind.EXTERNAL:
            continue
        host = urlsplit(link.destination).hostname or ""
        if not _own(host, hosts):
            problems.append(INFO_HOST_TEXT.format(host=host or "en annan webbplats"))
    if looks_like_ad(_ad_text(utskick)) and not override_valid(utskick):
        problems.append(LOOKS_LIKE_AD_TEXT)
    problems += field_problems(utskick)
    return problems


# ---------------------------------------------------------------------------
# Nödbromsen (D.4)
# ---------------------------------------------------------------------------


def provider_trouble(now=None):
    """Efter ett oklart svar eller provider_error: tre sådana hos 46elks för
    hela ADX inom två minuter drar i nödbromsen. Returnerar True när bromsen
    drogs nu (och byrån larmades en gång)."""
    from .. import alerts

    now = now or timezone.now()
    since = now - BREAKER_WINDOW
    trouble = SmsMessage.objects.filter(created_at__gte=since).filter(
        Q(error_code="provider_error")
        | Q(error_code="provider_unknown", status=SmsMessage.Status.RESERVED)
    )
    if trouble.count() < BREAKER_COUNT:
        return False
    until = now + BREAKER_PAUSE
    Switchboard.get_solo()
    pulled = (
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK)
        .filter(Q(sms_paused_until__isnull=True) | Q(sms_paused_until__lt=now))
        .update(sms_paused_until=until)
    )
    if not pulled:
        return False
    logger.error("Utskick: nödbromsen för sms drogs till %s", until.isoformat())
    alerts.breaker(until, now=now)
    return True
