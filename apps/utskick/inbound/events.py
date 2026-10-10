"""
SES-händelserna (README D.7, D.5, D.9): en händelse ur kön
adx-utskick-events (rå leverans från SNS, så SQS-kroppen är händelsens JSON).

    EVENT_TYPES                    det konfigurationssetet publicerar
    receipt_key(event) -> str      EventReceipt.key: f"{mail.messageId}:{eventType}"
    recipient_tags(event) -> (a, u, r)
    apply(event, now=None) -> str  "applied", "ignored" eller "unknown_recipient"

apply körs i queues.poll:s transaktion efter att EventReceipt skrivits.
Mottagaren hittas med mail.tags.r, prövad mot mail.tags.a (kontot), u
(utskicket) och, när den finns, Recipient.ses_message_id:

    Send              adopterar unknown (ses_message_id, sent)
    Delivery          delivered, kontaktens studsräknare nollas
    Bounce Permanent  bounced, Contact.email_state=bounced,
                      Suppression(reason="bounce"), räknas i hälsan;
                      underslaget OnAccountSuppressionList: bara failed med
                      skip_reason ses_suppressed, ingen kontakt, ingen spärr,
                      inte i hälsan (adressen spärrades hos SES för en annan
                      avsändare)
    Bounce Transient (och Undetermined)
                      failed med "Tillfällig studs", email_soft_bounces + 1;
                      fem i rad (Delivery nollar) -> som en permanent studs
    Complaint         complained, samtycket unsubscribed (källa complaint) och
                      Suppression(reason="complaint") genom suppression.suppress
    Reject, Rendering Failure   failed
    DeliveryDelay, Open, Click  ingenting (öppningar räknas av vår egen pixel,
                      S3-HANDOFF.md: konfigurationssetet publicerar inte OPEN)

Status flyttas bara framåt (Recipient.RANK). Undantaget är en studs efter
levererat: mottagarens server tog emot mejlet och skickade sedan en
studs, och studsen är det som gäller. Efter en studs eller ett klagomål
prövas hälsan (sending.health.check_utskick och check_account); en
leverans kan aldrig få en gräns att passeras.

Mejl utan taggen r (bekräftelsemejlen, testmejlen, svaren från Inkorgen,
taggen k): bara studs och klagomål på kontaktens adress via (a, adressens
hash). Byråns provmejl och bekräftelselänken rör inga kontakter.

Inga adresser i loggen, bara pk.
"""

import logging
from datetime import UTC, datetime

from django.db.models import F
from django.utils import timezone

from .. import keys
from .. import suppression as suppressions
from ..consent import COMPLAINT_DETAIL
from ..models import (
    CHANNEL_EMAIL,
    Consent,
    Contact,
    Recipient,
    Suppression,
)

logger = logging.getLogger(__name__)

#: Händelserna konfigurationssetet publicerar (server/aws-utskick-s3.sh).
#: OPEN ingår inte: SES lägger då in sin egen pixel i varje HTML-mejl, också
#: hos mottagare utan tracking_ok (H.5).
EVENT_TYPES = (
    "SEND",
    "REJECT",
    "BOUNCE",
    "COMPLAINT",
    "DELIVERY",
    "DELIVERY_DELAY",
    "RENDERING_FAILURE",
)

RS = Recipient.Status
APPLIED = "applied"
IGNORED = "ignored"
UNKNOWN_RECIPIENT = "unknown_recipient"

SOFT_TEXT = "Tillfällig studs"
REJECT_TEXT = "E-posttjänsten stoppade mejlet innan det skickades."
RENDERING_TEXT = "E-posttjänsten kunde inte bygga mejlet."
SUPPRESSED_TEXT = "Adressen är spärrad hos e-posttjänsten."
#: Slagen av mejl utan mottagare vars studsar och klagomål gäller kontakten.
CONTACT_KINDS = ("doi", "test", "reply")
#: Underslaget när SES inte ens försökte: adressen står på kontots spärrlista.
ON_ACCOUNT_LIST = "OnAccountSuppressionList"
#: Lägen som en studs får ersätta (allt utom klagomål och de som inte skickats).
BOUNCEABLE = (RS.SENDING, RS.SENT, RS.UNKNOWN, RS.DELIVERED, RS.FAILED)


def receipt_key(event):
    """EventReceipt.key för en händelse: f"{mail.messageId}:{eventType}".
    "" när händelsen saknar något av dem (den kvitteras inte och kastas)."""
    mail = event.get("mail") if isinstance(event, dict) else None
    message_id = str((mail or {}).get("messageId") or "").strip()
    event_type = str(event.get("eventType") or event.get("notificationType") or "").strip()
    if not message_id or not event_type:
        return ""
    return f"{message_id}:{event_type}"[:140]


def recipient_tags(event):
    """(konto, utskick, mottagare) ur mail.tags som heltal eller None.
    SES ger taggarna som listor av strängar: {"a": ["12"], "r": ["345"]}."""
    tags = ((event or {}).get("mail") or {}).get("tags") or {}

    def one(name):
        values = tags.get(name) or []
        value = values[0] if isinstance(values, list) and values else values
        try:
            number = int(str(value))
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    return one("a"), one("u"), one("r")


def _kind_tag(event):
    tags = ((event or {}).get("mail") or {}).get("tags") or {}
    values = tags.get("k") or []
    value = values[0] if isinstance(values, list) and values else values
    return str(value or "")


def event_type(event):
    return str(event.get("eventType") or event.get("notificationType") or "").strip()


def _when(text, now):
    """En tidpunkt ur händelsen (ISO 8601), annars now."""
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return now
    if timezone.is_naive(when):
        when = when.replace(tzinfo=UTC)
    return when


def _ranked_below(status):
    """Lägena med lägre rang än status (D.5: bara framåt)."""
    rank = Recipient.RANK[status]
    return [s for s, r in Recipient.RANK.items() if r < rank]


# ---------------------------------------------------------------------------
# Kontakten och spärren
# ---------------------------------------------------------------------------


def _contact_for(account_id, address):
    clean = keys.clean_value(CHANNEL_EMAIL, address)
    if not account_id or not clean:
        return None
    return Contact.objects.filter(account_id=account_id, email=clean).first()


def _hard_bounce(account_id, address, utskick_id, now):
    """En adress som inte finns (H.6): kontakten blir studsad och adressen
    hamnar på kontots spärrlista (reason bounce). Samtycket rörs inte."""
    clean = keys.clean_value(CHANNEL_EMAIL, address)
    if not account_id or not clean:
        return
    Contact.objects.filter(account_id=account_id, email=clean).update(
        email_state=Contact.EmailState.BOUNCED, email_bounced_at=now
    )
    _touch(account_id, clean, "bounce", now)
    from apps.flamingo.models import FlamingoAccount

    account = FlamingoAccount.objects.filter(pk=account_id).first()
    if account is None:
        return
    row, created = suppressions.add(
        account, CHANNEL_EMAIL, keys.value_hash(CHANNEL_EMAIL, clean), Suppression.Reason.BOUNCE
    )
    if created and utskick_id:
        Suppression.objects.filter(pk=row.pk, utskick__isnull=True).update(utskick_id=utskick_id)


def _soft_bounce(account_id, address, now):
    """En tillfällig studs: kontaktens räknare plus ett. True när det är
    den femte i rad (då behandlas den som en permanent studs)."""
    contact = _contact_for(account_id, address)
    if contact is None:
        return False
    Contact.objects.filter(pk=contact.pk).update(email_soft_bounces=F("email_soft_bounces") + 1)
    count = Contact.objects.filter(pk=contact.pk).values_list("email_soft_bounces", flat=True)
    return (count.first() or 0) >= Contact.SOFT_BOUNCE_LIMIT


def _complaint(account_id, address, utskick_id, now):
    """Klagomål (H.6): avregistrerad från e-post med en spärr, som en
    avregistrering (suppression.suppress, källa complaint)."""
    clean = keys.clean_value(CHANNEL_EMAIL, address)
    if not account_id or not clean:
        return
    from apps.flamingo.models import FlamingoAccount

    account = FlamingoAccount.objects.filter(pk=account_id).first()
    if account is None:
        return
    row, _contact = suppressions.suppress(
        account,
        CHANNEL_EMAIL,
        clean,
        reason=Suppression.Reason.COMPLAINT,
        source=Consent.Source.COMPLAINT,
        source_detail=COMPLAINT_DETAIL,
        now=now,
    )
    if utskick_id:
        Suppression.objects.filter(pk=row.pk, utskick__isnull=True).update(utskick_id=utskick_id)
    _touch(account_id, clean, "complaint", now)


def _touch(account_id, clean, kind, now):
    """ "Senast" i kontaktlistan (I.7): "Adressen finns inte, 12 sep" efter
    en studs, "Markerade som skräppost" efter ett klagomål (S3, integrationen)."""
    from .. import contacts

    for contact in Contact.objects.filter(account_id=account_id, email=clean):
        contacts.touch(contact, kind, now)


def _addresses(event, section, key):
    """Adresserna i händelsen: bounce.bouncedRecipients och
    complaint.complainedRecipients är objekt med emailAddress,
    delivery.recipients är strängar."""
    rows = ((event.get(section) or {}).get(key)) or []
    found = []
    for row in rows:
        value = row.get("emailAddress") if isinstance(row, dict) else row
        address = str(value or "").strip()
        if address:
            found.append(address)
    return found


# ---------------------------------------------------------------------------
# Händelserna
# ---------------------------------------------------------------------------


def _find(event):
    """(mottagaren, utfallet): mottagaren när taggarna stämmer, annars None
    och "unknown_recipient"."""
    account_id, utskick_id, recipient_id = recipient_tags(event)
    recipient = (
        Recipient.objects.select_related("utskick")
        .filter(pk=recipient_id, channel=CHANNEL_EMAIL)
        .first()
    )
    if recipient is None or recipient.utskick.account_id != account_id:
        return None
    if utskick_id and recipient.utskick_id != utskick_id:
        return None
    message_id = str(((event.get("mail") or {}).get("messageId")) or "")
    if recipient.ses_message_id and message_id and recipient.ses_message_id != message_id:
        return None
    return recipient


def _adopt_id(recipient, event):
    message_id = str(((event.get("mail") or {}).get("messageId")) or "")[:100]
    if message_id and not recipient.ses_message_id:
        Recipient.objects.filter(pk=recipient.pk, ses_message_id="").update(
            ses_message_id=message_id
        )


def _on_send(recipient, event, now):
    _adopt_id(recipient, event)
    Recipient.objects.filter(pk=recipient.pk, status__in=(RS.SENDING, RS.UNKNOWN)).update(
        status=RS.SENT, sent_at=_when((event.get("mail") or {}).get("timestamp"), now)
    )
    return APPLIED


def _on_delivery(recipient, event, now):
    _adopt_id(recipient, event)
    when = _when((event.get("delivery") or {}).get("timestamp"), now)
    Recipient.objects.filter(pk=recipient.pk, status__in=_ranked_below(RS.DELIVERED)).update(
        status=RS.DELIVERED, delivered_at=when
    )
    Recipient.objects.filter(pk=recipient.pk, sent_at__isnull=True).update(sent_at=when)
    if recipient.contact_id:
        Contact.objects.filter(pk=recipient.contact_id, email_soft_bounces__gt=0).update(
            email_soft_bounces=0
        )
    return APPLIED


def _suppressed_by_ses(recipient):
    Recipient.objects.filter(pk=recipient.pk, status__in=_ranked_below(RS.FAILED)).update(
        status=RS.FAILED,
        skip_reason=Recipient.SkipReason.SES_SUPPRESSED,
        error=SUPPRESSED_TEXT,
    )


def _health(recipient, now):
    from ..sending import health

    utskick = recipient.utskick
    health.check_utskick(utskick, now)
    health.check_account(utskick.account, now)


def _bounced(recipient, now):
    Recipient.objects.filter(pk=recipient.pk, status__in=BOUNCEABLE).update(status=RS.BOUNCED)
    if recipient.address:
        _hard_bounce(recipient.utskick.account_id, recipient.address, recipient.utskick_id, now)


def _on_bounce(recipient, event, now):
    _adopt_id(recipient, event)
    bounce = event.get("bounce") or {}
    if bounce.get("bounceSubType") == ON_ACCOUNT_LIST:
        _suppressed_by_ses(recipient)
        return APPLIED
    if bounce.get("bounceType") == "Permanent":
        _bounced(recipient, now)
        _health(recipient, now)
        return APPLIED
    # Transient eller Undetermined: en tillfällig studs.
    fifth = bool(recipient.address) and _soft_bounce(
        recipient.utskick.account_id, recipient.address, now
    )
    if fifth:
        _bounced(recipient, now)
        _health(recipient, now)
        return APPLIED
    Recipient.objects.filter(pk=recipient.pk, status__in=_ranked_below(RS.FAILED)).update(
        status=RS.FAILED, error=SOFT_TEXT
    )
    return APPLIED


def _on_complaint(recipient, event, now):
    _adopt_id(recipient, event)
    complaint = event.get("complaint") or {}
    if complaint.get("complaintSubType") == ON_ACCOUNT_LIST:
        _suppressed_by_ses(recipient)
        return APPLIED
    Recipient.objects.filter(pk=recipient.pk).exclude(
        status__in=(RS.COMPLAINED, RS.QUEUED, RS.SKIPPED, RS.CANCELLED)
    ).update(status=RS.COMPLAINED)
    if recipient.address:
        _complaint(recipient.utskick.account_id, recipient.address, recipient.utskick_id, now)
    _health(recipient, now)
    return APPLIED


def _on_failure(text):
    def handler(recipient, event, now):
        _adopt_id(recipient, event)
        Recipient.objects.filter(pk=recipient.pk, status__in=_ranked_below(RS.FAILED)).update(
            status=RS.FAILED, error=text
        )
        return APPLIED

    return handler


HANDLERS = {
    "Send": _on_send,
    "Delivery": _on_delivery,
    "Bounce": _on_bounce,
    "Complaint": _on_complaint,
    "Reject": _on_failure(REJECT_TEXT),
    "Rendering Failure": _on_failure(RENDERING_TEXT),
    "RenderingFailure": _on_failure(RENDERING_TEXT),
}


def _untagged(kind, event, account_id, now):
    """Ett mejl utan mottagare: bara studs och klagomål på kontaktens
    adress, och bara för slagen i CONTACT_KINDS."""
    if _kind_tag(event) not in CONTACT_KINDS or not account_id:
        return IGNORED
    if kind == "Bounce":
        bounce = event.get("bounce") or {}
        if bounce.get("bounceSubType") == ON_ACCOUNT_LIST:
            return IGNORED
        for address in _addresses(event, "bounce", "bouncedRecipients"):
            if bounce.get("bounceType") == "Permanent" or _soft_bounce(account_id, address, now):
                _hard_bounce(account_id, address, None, now)
        return APPLIED
    if kind == "Complaint":
        complaint = event.get("complaint") or {}
        if complaint.get("complaintSubType") == ON_ACCOUNT_LIST:
            return IGNORED
        for address in _addresses(event, "complaint", "complainedRecipients"):
            _complaint(account_id, address, None, now)
        return APPLIED
    if kind == "Delivery":
        for address in _addresses(event, "delivery", "recipients"):
            contact = _contact_for(account_id, address)
            if contact is not None and contact.email_soft_bounces:
                Contact.objects.filter(pk=contact.pk).update(email_soft_bounces=0)
        return APPLIED
    return IGNORED


def apply(event, now=None):
    """En SES-händelse (modulens text). Körs inne i köns transaktion; ett
    undantag lämnar meddelandet i kön (efter fem mottagningar DLQ:n)."""
    now = now or timezone.now()
    if not isinstance(event, dict):
        return IGNORED
    kind = event_type(event)
    if kind not in HANDLERS:
        return IGNORED
    account_id, _utskick_id, recipient_id = recipient_tags(event)
    if recipient_id is None:
        return _untagged(kind, event, account_id, now)
    recipient = _find(event)
    if recipient is None:
        logger.info("Utskick: SES-händelse %s för en okänd mottagare", kind)
        return UNKNOWN_RECIPIENT
    return HANDLERS[kind](recipient, event, now)
