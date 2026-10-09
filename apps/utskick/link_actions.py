"""
Det sidorna på k.adx.se gör (README E.5, H.6): avregistrera med
personkoden (/s/), Ångra, personens val (/p/) och bekräftelsen (/b/).
Vyerna (link_views.py) sköter HTTP, nonce och mallar; här är reglerna.

    person_code(code), confirm_code(code)      raden för koden, eller None
    contact_for(account, channel, value_hash)  kontakten vars adress har hashen
    unsubscribe_sms(code, *, ip_hash, now) -> (Suppression, ny?)
    unsubscribe_email(code, contact, *, ip_hash, now) -> bool
    undo(code, suppression_id, nonce, *, ip_hash, now) -> bool
    preference_rows(account, row, contact) -> (rader, spärrade)
    save_preferences(account, row, contact, rows, data, *, botcheck_ok, ip_hash) -> str
    unsubscribe_all(account, contact, value_hash, *, ip_hash)
    confirm(code, *, ip_hash, now) -> str

Semantik (H.6): "Avregistrera mig" på /s/, och "Avregistrera mig från
allt", lägger en spärr (allt på kanalen stoppas, också information). Att
stänga av erbjudanden på /p/ sätter declined (information fortsätter).
Att slå på kräver alltid en bekräftelse: sms ett bekräftelse-sms med en
länk till /b/ (optin.send_due_sms), e-post ett bekräftelsemejl. Ångra
gäller 30 minuter och bara den spärr som avregistreringen lade.

Allt här fungerar också när utskick är avstängt för kontot (D.8): en
avregistrering ska alltid gå igenom. Att slå på kräver can_collect.
Kontot tas bara ur koden, aldrig ur en parameter (H.1). Loggar bär pk,
aldrig nummer, adresser eller koder (H.3).
"""

import logging

from django.db import transaction
from django.utils import timezone

from . import capture, keys, normalize, optin, tokens
from . import consent as consents
from . import suppression as suppressions
from .access import PERSON, can_collect
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNELS,
    Consent,
    ConsentLog,
    Contact,
    LinkCode,
    Recipient,
    Suppression,
)

logger = logging.getLogger(__name__)

#: source_detail i samtyckesloggen (aldrig koden: den är en behörighet).
UNSUBSCRIBE_DETAIL = "Avregistreringslänk i sms"
UNDO_DETAIL = "Ångra inom 30 minuter"
PREFERENCES_DETAIL = "Dina val (länk i sms)"
CONFIRM_DETAIL = "Bekräftelselänk i sms"

#: Raderna på /p/ (E.5).
ROW_TITLES = {CHANNEL_SMS: "Sms med erbjudanden", CHANNEL_EMAIL: "E-post med erbjudanden"}
_ON = consents.REKLAM_OK


def person_code(code):
    """Personkoden (/s/ och /p/) med konto, kund och mottagare, eller None."""
    if not code or len(code) != 6:
        return None
    return (
        LinkCode.objects.select_related("account__customer", "recipient__utskick")
        .filter(code=code, kind=LinkCode.Kind.PERSON)
        .first()
    )


def confirm_code(code):
    """Bekräftelsekoden (/b/) med konto och kontakt, eller None."""
    if not code or len(code) != 6:
        return None
    return (
        LinkCode.objects.select_related("account__customer", "contact")
        .filter(code=code, kind=LinkCode.Kind.CONFIRM)
        .first()
    )


def contact_for(account, channel, value_hash):
    """Kontakten vars nuvarande adress på kanalen har hashen, eller None
    (borttagen, eller adressen har bytts sedan sms:et skickades)."""
    if not value_hash:
        return None
    row = (
        Consent.objects.select_related("contact")
        .filter(contact__account=account, channel=channel, value_hash=value_hash)
        .first()
    )
    if row is None:
        return None
    contact = row.contact
    if keys.value_hash(channel, contact.address(channel)) != value_hash:
        return None
    return contact


def masked_number(code, contact=None):
    """070-*** ** 67 för numret koden skickades till, eller ""."""
    if contact is not None and contact.phone:
        return normalize.mask_phone(contact.phone)
    recipient = code.recipient
    if recipient is not None and recipient.address and recipient.channel == CHANNEL_SMS:
        return normalize.mask_phone(recipient.address)
    return ""


def is_suppressed(account, channel, value_hash):
    return suppressions.is_suppressed(account, channel, value_hash=value_hash)


def _log_without_contact(account, channel, value_hash, old, new, source, detail, ip_hash, now):
    """Beviset när ingen kontakt har adressen längre (borttagen): en rad i
    samtyckesloggen utan kontakt, så att spärren och Ångra syns ändå."""
    ConsentLog.objects.create(
        account=account,
        contact=None,
        channel=channel,
        value_hash=value_hash,
        old_status=old,
        new_status=new,
        basis=Consent.Basis.NONE,
        source=source,
        source_detail=detail,
        by_label=PERSON.label,
        ip_hash=ip_hash or "",
        at=now,
    )


def unsubscribe_sms(code, *, ip_hash="", now=None):
    """/s/ "Avregistrera mig": spärr på numret (orsak link) hos kontot, och
    kontaktens sms-samtycke blir unsubscribed med en rad i loggen. Spärren
    får utskicket den kom efter; mottagaren får stopped_at (räknas i
    utskickets hälsa, D.9). Returnerar (spärren, ny): Ångra erbjuds bara
    för en spärr som den här avregistreringen lade (inte efter ett STOPP)."""
    now = now or timezone.now()
    account = code.account
    recipient = code.recipient
    utskick_id = recipient.utskick_id if recipient is not None else None
    with transaction.atomic():
        contact = contact_for(account, CHANNEL_SMS, code.value_hash)
        existed = Suppression.objects.filter(
            account=account, channel=CHANNEL_SMS, value_hash=code.value_hash
        ).exists()
        if contact is not None:
            consents.set_status(
                contact,
                CHANNEL_SMS,
                consents.UNSUBSCRIBED,
                source=Consent.Source.LINK,
                actor=PERSON,
                source_detail=UNSUBSCRIBE_DETAIL,
                suppression_reason=Suppression.Reason.LINK,
                ip_hash=ip_hash,
                now=now,
            )
        row, created = suppressions.add(
            account, CHANNEL_SMS, code.value_hash, Suppression.Reason.LINK, now=now
        )
        if contact is None and not existed:
            _log_without_contact(
                account,
                CHANNEL_SMS,
                code.value_hash,
                "",
                consents.UNSUBSCRIBED,
                Consent.Source.LINK,
                UNSUBSCRIBE_DETAIL,
                ip_hash,
                now,
            )
        if not existed and utskick_id and row.utskick_id is None:
            Suppression.objects.filter(pk=row.pk, utskick__isnull=True).update(
                utskick_id=utskick_id
            )
        if recipient is not None:
            Recipient.objects.filter(pk=recipient.pk, stopped_at__isnull=True).update(
                stopped_at=now
            )
    logger.info("Utskick: avregistrering via sms-länk (konto %s, spärr %s)", account.pk, row.pk)
    return row, not existed


def unsubscribe_email(code, contact, *, ip_hash="", now=None):
    """/s/ "Vill du sluta få e-post också?": spärr på kontaktens e-post
    (orsak link). True när det fanns en adress att spärra."""
    if contact is None or not contact.email or contact.account_id != code.account_id:
        return False
    suppressions.suppress(
        code.account,
        CHANNEL_EMAIL,
        contact.email,
        Suppression.Reason.LINK,
        Consent.Source.LINK,
        source_detail=UNSUBSCRIBE_DETAIL,
        actor=PERSON,
        ip_hash=ip_hash,
        now=now,
    )
    return True


def email_question(code, contact):
    """Ska /s/ fråga om e-posten också? Den maskerade adressen, eller "" när
    kontakten saknar e-post eller den redan är spärrad."""
    if contact is None or not contact.email:
        return ""
    value_hash = keys.value_hash(CHANNEL_EMAIL, contact.email)
    if is_suppressed(code.account, CHANNEL_EMAIL, value_hash):
        return ""
    return normalize.mask_email(contact.email)


def undo(code, suppression_id, nonce, *, ip_hash="", now=None):
    """Ångra avregistreringen (E.5): bara med engångsvärdet från sidan
    (tokens.read_undo, 30 minuter) och bara den spärr som lades via länken.
    Spärren tas bort och samtycket blir som före avregistreringen
    (consent.restore). När spärren redan är borta gäller värdet inte längre
    (en gång). True när det gick."""
    now = now or timezone.now()
    try:
        suppression_id = int(suppression_id)
    except (TypeError, ValueError):
        return False
    if not tokens.read_undo(suppression_id, nonce, now):
        return False
    account = code.account
    with transaction.atomic():
        row = (
            Suppression.objects.select_for_update()
            .filter(
                pk=suppression_id,
                account=account,
                channel=CHANNEL_SMS,
                value_hash=code.value_hash,
                reason=Suppression.Reason.LINK,
            )
            .first()
        )
        if row is None:
            return False
        contact = contact_for(account, CHANNEL_SMS, code.value_hash)
        if contact is None:
            keys.require_fingerprints()
            suppressions.lift(account, CHANNEL_SMS, code.value_hash)
            _log_without_contact(
                account,
                CHANNEL_SMS,
                code.value_hash,
                consents.UNSUBSCRIBED,
                consents.MISSING,
                Consent.Source.LINK,
                UNDO_DETAIL,
                ip_hash,
                now,
            )
            return True
        unsub = (
            ConsentLog.objects.filter(
                contact=contact,
                channel=CHANNEL_SMS,
                value_hash=code.value_hash,
                new_status=consents.UNSUBSCRIBED,
                source=Consent.Source.LINK,
            )
            .order_by("-at", "-pk")
            .first()
        )
        if unsub is None:
            return False
        outcome = consents.restore(
            contact,
            CHANNEL_SMS,
            unsubscribe_log=unsub,
            source=Consent.Source.LINK,
            actor=PERSON,
            source_detail=UNDO_DETAIL,
            ip_hash=ip_hash,
            now=now,
        )
        if not outcome.ok:
            return False
        if code.recipient_id:
            Recipient.objects.filter(pk=code.recipient_id, stopped_at__isnull=False).update(
                stopped_at=None
            )
    logger.info("Utskick: avregistrering ångrad (konto %s)", account.pk)
    return True


# ---------------------------------------------------------------------------
# Dina val (/p/)
# ---------------------------------------------------------------------------


def preference_rows(account, row, contact):
    """Raderna på /p/ (E.5): en per kanal. state är on, pending eller off;
    can_change om reglaget går att ändra; sign_up när kanalen saknar adress
    och personen kan anmäla en (bara e-post: sms-koden har ett nummer)."""
    consent_by = {c.channel: c for c in contact.consents.all()}
    may_collect = can_collect(account)
    offers = {
        CHANNEL_SMS: may_collect and optin.offers_sms(),
        CHANNEL_EMAIL: may_collect and optin.offers_email(),
    }
    rows = []
    blocked = set()
    for channel in CHANNELS:
        address = contact.address(channel)
        if not address:
            if channel == CHANNEL_EMAIL and offers[CHANNEL_EMAIL]:
                rows.append(
                    {
                        "kanal": channel,
                        "rubrik": ROW_TITLES[channel],
                        "not": row.pref_email_note,
                        "tillstand": "off",
                        "kan_andras": False,
                        "anmal": True,
                        "sparrad": False,
                    }
                )
            continue
        consent = consent_by.get(channel)
        value_hash = keys.value_hash(channel, address)
        suppressed = is_suppressed(account, channel, value_hash)
        if suppressed:
            blocked.add(channel)
        status = consent.status if consent is not None else consents.MISSING
        if status in _ON and not suppressed:
            state = "on"
        elif status == consents.PENDING:
            state = "pending"
        else:
            state = "off"
        rows.append(
            {
                "kanal": channel,
                "rubrik": ROW_TITLES[channel],
                "not": row.pref_email_note if channel == CHANNEL_EMAIL else "",
                "tillstand": state,
                "kan_andras": state != "off" or offers[channel],
                "anmal": False,
                "sparrad": suppressed,
            }
        )
    return rows, blocked


def _turn_on(contact, channel, row, ip_hash):
    """Slå på en kanal: pending och en bekräftelse i kön. True när en
    bekräftelse väntar."""
    text = row.consent_text(channel)
    outcome = consents.set_status(
        contact,
        channel,
        consents.PENDING,
        source=Consent.Source.PREFERENCE,
        actor=PERSON,
        source_detail=PREFERENCES_DETAIL,
        text_shown=text,
        tracking_ok=capture.tracking_ok(text),
        ip_hash=ip_hash,
    )
    consent = outcome.consent
    if consent is None or consent.status != consents.PENDING:
        return False
    if not outcome.changed:
        if channel == CHANNEL_SMS:
            optin.requeue_sms(consent)
        else:
            optin.requeue(consent)
    return True


def _sign_up_email(account, row, contact, raw, ip_hash):
    """ "Anmäl dig" för e-post på /p/: som anmälningssidan. Adressen blir
    kontaktens om kontakten saknar e-post och ingen annan kontakt har den;
    har en annan kontakt adressen väntar dess e-post på bekräftelsen
    (mejlet går till adressen, och bara ägaren kan bekräfta). Sidan säger
    samma sak vad som än händer. True när ett mejl väntar."""
    from . import contacts

    try:
        email = normalize.email(raw)
    except normalize.InvalidValue:
        return None
    if not email:
        return None
    target = Contact.objects.filter(account=account, email=email).first()
    if target is None:
        if contact.email:
            return False
        try:
            changed = contacts.change_address(
                contact, CHANNEL_EMAIL, email, actor=PERSON, source_detail=PREFERENCES_DETAIL
            )
        except contacts.ContactError:
            return False
        if not changed:
            return False
        contact.refresh_from_db()
        target = contact
    return _turn_on(target, CHANNEL_EMAIL, row, ip_hash)


def save_preferences(account, row, contact, rows, data, *, botcheck_ok, ip_hash=""):
    """Spara reglagen på /p/. Att stänga av går alltid (declined); att slå
    på kräver botskyddet och sätter pending med en bekräftelse i kön.
    Returnerar "sparat", "sms" (ett bekräftelse-sms väntar), "mejl" (ett
    bekräftelsemejl väntar), "sms-mejl" (båda) eller "fel-epost" (en
    adress som inte gick att läsa)."""
    waiting = set()
    bad_email = False
    with transaction.atomic():
        for item in rows:
            channel = item["kanal"]
            wants = data.get(channel) == "1"
            if item["anmal"]:
                raw = str(data.get("epost", "") or "").strip()
                if wants or raw:
                    if not botcheck_ok:
                        continue
                    result = _sign_up_email(account, row, contact, raw, ip_hash)
                    if result is None:
                        bad_email = True
                    elif result:
                        waiting.add(channel)
                continue
            if item["tillstand"] in ("on", "pending") and not wants:
                consents.set_status(
                    contact,
                    channel,
                    consents.DECLINED,
                    source=Consent.Source.PREFERENCE,
                    actor=PERSON,
                    source_detail=PREFERENCES_DETAIL,
                    ip_hash=ip_hash,
                )
            elif item["tillstand"] == "off" and wants and item["kan_andras"] and botcheck_ok:
                if _turn_on(contact, channel, row, ip_hash):
                    waiting.add(channel)
    if bad_email:
        return "fel-epost"
    if waiting == {CHANNEL_SMS, CHANNEL_EMAIL}:
        return "sms-mejl"
    if CHANNEL_SMS in waiting:
        return "sms"
    if CHANNEL_EMAIL in waiting:
        return "mejl"
    return "sparat"


def unsubscribe_all(account, contact, value_hash, *, ip_hash=""):
    """ "Avregistrera mig från allt": en spärr per kanal med adress (H.6),
    eller bara numrets hash när ingen kontakt har det längre."""
    with transaction.atomic():
        if contact is None:
            existed = Suppression.objects.filter(
                account=account, channel=CHANNEL_SMS, value_hash=value_hash
            ).exists()
            suppressions.add(account, CHANNEL_SMS, value_hash, Suppression.Reason.PREFERENCE)
            if not existed:
                _log_without_contact(
                    account,
                    CHANNEL_SMS,
                    value_hash,
                    "",
                    consents.UNSUBSCRIBED,
                    Consent.Source.PREFERENCE,
                    PREFERENCES_DETAIL,
                    ip_hash,
                    timezone.now(),
                )
            return
        for channel in CHANNELS:
            address = contact.address(channel)
            if not address:
                continue
            suppressions.suppress(
                account,
                channel,
                address,
                Suppression.Reason.PREFERENCE,
                Consent.Source.PREFERENCE,
                source_detail=PREFERENCES_DETAIL,
                actor=PERSON,
                ip_hash=ip_hash,
            )


# ---------------------------------------------------------------------------
# Bekräftelsen (/b/)
# ---------------------------------------------------------------------------

#: Tillstånden för /b/ (mallen utskick/links/confirm.html).
CONFIRM_ASK = "bekrafta"
CONFIRM_DONE = "klar"
CONFIRM_EXPIRED = "utgangen"
CONFIRM_INVALID = "ogiltig"
CONFIRM_CLOSED = "stangd"


def confirm_contact(code):
    """Kontakten koden gäller, bara om dess nummer fortfarande är det
    koden skickades till; annars None."""
    contact = code.contact
    if contact is None or contact.account_id != code.account_id:
        contact = contact_for(code.account, CHANNEL_SMS, code.value_hash)
    if contact is None:
        return None
    if keys.value_hash(CHANNEL_SMS, contact.phone) != code.value_hash:
        return None
    return contact


def confirm_state(code, now=None):
    """Vad /b/ ska visa för koden: bekrafta, klar, utgangen, ogiltig eller
    stangd (kontot tar inte emot nya anmälningar; inte för START)."""
    now = now or timezone.now()
    contact = confirm_contact(code)
    if code.used_at is not None:
        if contact is None:
            return CONFIRM_DONE
        consent = contact.consents.filter(channel=CHANNEL_SMS).first()
        return (
            CONFIRM_DONE
            if consent is not None and consent.status == consents.YES
            else (CONFIRM_INVALID)
        )
    if code.expires_at is not None and code.expires_at < now:
        return CONFIRM_EXPIRED
    if contact is None and code.purpose != LinkCode.Purpose.START:
        return CONFIRM_INVALID
    if code.purpose != LinkCode.Purpose.START and not can_collect(code.account):
        return CONFIRM_CLOSED
    return CONFIRM_ASK


def confirm(code, *, text_shown, ip_hash="", now=None):
    """POST på /b/: sms blir ja, bevisat av klicket (källa confirm, eller
    start efter ett START-svar), med confirmed_at; en spärr på numret tas
    bort (consent.set_status med proved). Koden används en gång. Efter en
    anmälan läggs anmälningssidans lista och taggar på. Returnerar det
    tillstånd sidan ska visa."""
    now = now or timezone.now()
    state = confirm_state(code, now)
    if state != CONFIRM_ASK:
        return state
    account = code.account
    source = (
        Consent.Source.START if code.purpose == LinkCode.Purpose.START else Consent.Source.CONFIRM
    )
    with transaction.atomic():
        taken = LinkCode.objects.filter(pk=code.pk, used_at__isnull=True).update(used_at=now)
        if not taken:
            return CONFIRM_DONE
        contact = confirm_contact(code)
        if contact is None:
            # START för ett nummer som ingen kontakt har: bara spärren.
            keys.require_fingerprints()
            if suppressions.lift(account, CHANNEL_SMS, code.value_hash):
                _log_without_contact(
                    account,
                    CHANNEL_SMS,
                    code.value_hash,
                    consents.UNSUBSCRIBED,
                    consents.MISSING,
                    source,
                    CONFIRM_DETAIL,
                    ip_hash,
                    now,
                )
            return CONFIRM_DONE
        consent = contact.consents.filter(channel=CHANNEL_SMS).first()
        pending_text = (
            consent.text_shown
            if consent is not None and consent.status == consents.PENDING and consent.text_shown
            else ""
        )
        outcome = consents.set_status(
            contact,
            CHANNEL_SMS,
            consents.YES,
            source=source,
            actor=PERSON,
            source_detail=(consent.source_detail if pending_text else "") or CONFIRM_DETAIL,
            text_shown=pending_text or text_shown,
            collected_at=consent.collected_at if pending_text else None,
            confirmed_at=now,
            ip_hash=ip_hash,
            proved=True,
            now=now,
        )
        if not outcome.ok:
            transaction.set_rollback(True)
            return CONFIRM_INVALID
    if code.purpose == LinkCode.Purpose.SIGNUP and outcome.changed:
        from .public_views import _signup_lists

        _signup_lists(account, contact)
    logger.info("Utskick: sms bekräftat (konto %s, kod %s)", account.pk, code.pk)
    return CONFIRM_DONE
