"""
Samtycket per kontakt och kanal (README B.1, H.5, H.6). set_status är den
enda som skriver Consent.status, och varje ändring blir en rad i
ConsentLog (beviset, bara nya rader).

    set_status(contact, channel, status, source=..., actor=..., ...) -> Outcome
    check_transition(old, new, source, ...)   "" eller en kod för varför den nekas
    ensure_rows(contact, source, actor)       en rad per kanal med adress (efter skapande)
    derive_company(contact, source, actor)    company av och på (bara e-post, bara från missing)
    eligible(contact, channel, purpose)       får kontakten reklam/information på kanalen?
    ineligible_reason(...)                    varför inte ("no_consent", "suppressed", ...)
    eligible_contacts(qs, channel, purpose)   samma sak som ett filter i databasen
    chip(contact, channel, consent)           etiketten i listan och på kortet

Övergångarna (check_transition):

    till declined eller unsubscribed   alla med åtkomst och personen själv, när som helst
                                       (unsubscribed alltid med en spärr)
    från unsubscribed                  bara med bevis från personen (proved=True), eller
                                       till pending från ett formulär (bekräftelsen bevisar)
    import, manuellt, API              missing (eller härlett company) -> yes eller existing,
                                       med evidence ("kassan, från 2024"), aldrig över pending,
                                       declined, unsubscribed eller en spärr
    formulär (lp_form, signup,         -> pending (e-post väntar på bekräftelsemejlet) från
    preference)                        missing, declined, unsubscribed, pending (yes,
                                       existing och company får redan reklam: "already");
                                       lp_form -> yes direkt för sms (K.2.5) från missing,
                                       company, existing, aldrig över en spärr
    bevis (doi, confirm, start, link,  -> yes från vad som helst, med proved=True; en spärr
    preference)                        på adressen tas bort i samma transaktion
    company                            bara härlett (derive_company): typ företag, juridiskt
                                       organisationsnummer, e-post som inte är gratis-e-post,
                                       bara från missing och aldrig över en spärr
    address                            adressen ändrades: -> missing, eller unsubscribed om
                                       den nya adressen är spärrad (contacts.change_address)

Samtycket är bundet till adressen: value_hash är hashen av adressen det
gäller. Raden finns för varje kanal där kontakten har en adress, så att
eligible_contacts kan räkna i databasen.

confirm_sent_at och confirm_count är bekräftelsens bokföring och skrivs av
optin.py direkt, inte här (de ändrar ingen status).
"""

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from . import keys
from . import suppression as suppressions
from .access import SYSTEM
from .freemail import is_freemail
from .models import CHANNEL_EMAIL, CHANNEL_SMS, Consent, ConsentLog, Contact, Suppression

YES = Consent.Status.YES
EXISTING = Consent.Status.EXISTING
COMPANY = Consent.Status.COMPANY
PENDING = Consent.Status.PENDING
MISSING = Consent.Status.MISSING
DECLINED = Consent.Status.DECLINED
UNSUBSCRIBED = Consent.Status.UNSUBSCRIBED

REKLAM = "reklam"
INFORMATION = "information"
PURPOSES = (REKLAM, INFORMATION)

#: Statusar som får reklam (med bunden adress och utan spärr).
REKLAM_OK = (YES, EXISTING, COMPANY)
#: Personens egna val, som bara personen själv ändrar.
PERSON_LOCKED = (PENDING, DECLINED, UNSUBSCRIBED)

S = Consent.Source
#: Kundens egna vägar: import, manuellt och API.
CUSTOMER_SOURCES = frozenset({S.IMPORT, S.MANUAL, S.API})
#: Formulär där personen själv lämnar sin adress (obekräftat).
FORM_SOURCES = frozenset({S.LP_FORM, S.SIGNUP, S.PREFERENCE})
#: Källor som kan bära ett bevis från personen (proved=True).
PROOF_SOURCES = frozenset({S.DOI, S.CONFIRM, S.START, S.LINK, S.PREFERENCE})

BASIS_FOR = {
    YES: Consent.Basis.CONSENT,
    EXISTING: Consent.Basis.EXISTING_CUSTOMER,
    COMPANY: Consent.Basis.COMPANY,
}

#: Spärrens orsak när samtycket blir unsubscribed från en viss källa.
SUPPRESSION_REASON = {
    S.STOP: Suppression.Reason.STOP,
    S.LINK: Suppression.Reason.LINK,
    S.LIST_UNSUB: Suppression.Reason.LIST_UNSUB,
    S.COMPLAINT: Suppression.Reason.COMPLAINT,
    S.PREFERENCE: Suppression.Reason.PREFERENCE,
    S.IMPORT: Suppression.Reason.IMPORT,
    S.REPLY: Suppression.Reason.REPLY,
}

#: Varför en övergång nekades, i klartext för gränssnittet.
REFUSAL_TEXTS = {
    "no_address": "Kontakten saknar adress för den kanalen.",
    "suppressed": "Adressen är avregistrerad. Bara personen själv kan anmäla sig igen.",
    "locked": "Personen har själv valt det här. Bara personen kan ändra det.",
    "not_allowed": "Det samtycket går inte att sätta här.",
    "already": "Personen får redan erbjudanden.",
    "evidence": "Skriv var och när personen sa ja, eller att hen är kund.",
}


@dataclass
class Outcome:
    """Svaret från set_status. refused är "" när det gick, annars en
    nyckel i REFUSAL_TEXTS."""

    consent: Consent | None
    changed: bool = False
    refused: str = ""
    lifted: bool = False

    @property
    def ok(self):
        return not self.refused

    @property
    def refusal_text(self):
        return REFUSAL_TEXTS.get(self.refused, "")


def check_transition(old, new, source, *, channel, suppressed=False, proved=False):
    """ "" när övergången old -> new från source är tillåten, annars koden
    (se REFUSAL_TEXTS). Reglerna står i modulens docstring."""
    if new == old:
        return ""
    if new in (DECLINED, UNSUBSCRIBED):
        if old == UNSUBSCRIBED and not proved:
            return "locked"
        return ""
    if source == S.ADDRESS:
        return "" if new in (MISSING, UNSUBSCRIBED) else "not_allowed"
    if old == UNSUBSCRIBED and not proved and not (new == PENDING and source in FORM_SOURCES):
        return "locked"
    if new == MISSING:
        return "not_allowed"
    if new == COMPANY:
        if channel != CHANNEL_EMAIL:
            return "not_allowed"
        if old != MISSING:
            return "locked" if old in PERSON_LOCKED else "not_allowed"
        return "suppressed" if suppressed else ""
    if new == PENDING:
        if source not in FORM_SOURCES:
            return "not_allowed"
        if old in REKLAM_OK:
            return "already"
        return ""
    if new == YES and proved:
        return ""
    if new == YES and source == S.LP_FORM:
        if old in PERSON_LOCKED:
            return "locked"
        return "suppressed" if suppressed else ""
    if new in (YES, EXISTING) and source in CUSTOMER_SOURCES:
        if old in PERSON_LOCKED:
            return "locked"
        if suppressed:
            return "suppressed"
        if old not in (MISSING, COMPANY):
            return "not_allowed"
        return ""
    return "not_allowed"


def _consent_row(contact, channel, lock=False):
    rows = Consent.objects.filter(contact=contact, channel=channel)
    if lock:
        rows = rows.select_for_update()
    return rows.first()


def set_status(
    contact,
    channel,
    status,
    *,
    source,
    actor=None,
    source_detail="",
    text_shown="",
    tracking_ok=False,
    evidence="",
    collected_at=None,
    confirmed_at=None,
    ip_hash="",
    proved=False,
    suppression_reason=None,
    now=None,
):
    """Ändra samtycket för en kanal, med en rad i samtyckesloggen.

    Nekade övergångar ändrar ingenting och returneras som Outcome(refused=
    kod); importen räknar dem, vyerna visar refusal_text. proved=True bara
    när personen själv bevisat det (DOI-klick, bekräftelselänk, Ångra med
    giltigt engångsvärde) och bara med en källa i PROOF_SOURCES; då tas en
    spärr på adressen bort. unsubscribed lägger alltid en spärr.
    KeyMismatch om processens nycklar inte stämmer (keys.py)."""
    if channel not in (CHANNEL_SMS, CHANNEL_EMAIL):
        raise ValueError(f"Okänd kanal: {channel!r}")
    if status not in Consent.Status.values:
        raise ValueError(f"Okänd status: {status!r}")
    if source not in Consent.Source.values:
        raise ValueError(f"Okänd källa: {source!r}")
    if proved and source not in PROOF_SOURCES:
        raise ValueError("Bara personens egna bekräftelser kan vara bevis.")
    actor = actor or SYSTEM
    now = now or timezone.now()
    address = contact.address(channel)
    value_hash = keys.value_hash(channel, address)

    with transaction.atomic():
        keys.require_fingerprints()
        row = _consent_row(contact, channel, lock=True)
        old = row.status if row is not None else MISSING
        if not value_hash and status not in (MISSING, DECLINED):
            return Outcome(row, refused="no_address")
        if row is None and status == MISSING:
            return Outcome(None)
        suppressed = suppressions.is_suppressed(contact.account, channel, value_hash=value_hash)
        refused = check_transition(
            old, status, source, channel=channel, suppressed=suppressed, proved=proved
        )
        if refused:
            return Outcome(row, refused=refused)
        if (
            status in (YES, EXISTING)
            and status != old
            and source in CUSTOMER_SOURCES
            and not (evidence or "").strip()
        ):
            # Kundens ja eller befintlig kund kräver beviset (B.1): var och när.
            return Outcome(row, refused="evidence")
        if status == old and row is not None and row.value_hash == value_hash:
            if proved and confirmed_at and not row.confirmed_at:
                row.confirmed_at = confirmed_at
                row.save(update_fields=["confirmed_at"])
            lifted = False
            if proved and status == YES and suppressed:
                lifted = suppressions.lift(contact.account, channel, value_hash)
            return Outcome(row, changed=False, lifted=lifted)
        row = _apply(
            contact,
            channel,
            row,
            status,
            value_hash=value_hash,
            source=source,
            actor=actor,
            source_detail=source_detail,
            text_shown=text_shown,
            tracking_ok=tracking_ok,
            evidence=evidence,
            collected_at=collected_at,
            confirmed_at=confirmed_at,
            ip_hash=ip_hash,
            suppression_reason=suppression_reason,
            now=now,
        )
        lifted = False
        if proved and status == YES and suppressed:
            lifted = suppressions.lift(contact.account, channel, value_hash)
    return Outcome(row, changed=True, lifted=lifted)


def _apply(
    contact,
    channel,
    row,
    status,
    *,
    value_hash,
    source,
    actor,
    source_detail="",
    text_shown="",
    tracking_ok=False,
    evidence="",
    collected_at=None,
    confirmed_at=None,
    ip_hash="",
    suppression_reason=None,
    now,
):
    """Skriv statusen, loggraden och (för unsubscribed) spärren. Inga regler
    här: anroparen har prövat övergången. Körs i en transaktion."""
    actor = actor or SYSTEM
    old = row.status if row is not None else MISSING
    if row is None:
        row = Consent(contact=contact, channel=channel)
    positive = status in (YES, EXISTING, COMPANY, PENDING)
    row.status = status
    row.basis = BASIS_FOR.get(status, Consent.Basis.NONE)
    row.value_hash = value_hash
    row.text_shown = text_shown or ""
    row.tracking_ok = bool(tracking_ok) and status in (YES, PENDING) and channel == CHANNEL_EMAIL
    row.evidence = (evidence or "")[:300]
    row.source = source
    row.source_detail = (source_detail or "")[:200]
    if positive:
        row.collected_at = collected_at or (now if status in (YES, PENDING) else None)
        row.confirmed_at = confirmed_at
    else:
        row.collected_at = None
        row.confirmed_at = None
    if status != PENDING:
        row.confirm_sent_at = None
        row.confirm_count = 0
    row.changed_at = now
    row.changed_by = actor.user
    row.changed_by_label = (actor.label or "")[:120]
    row.save()
    ConsentLog.objects.create(
        account_id=contact.account_id,
        contact=contact,
        channel=channel,
        value_hash=value_hash,
        old_status=old,
        new_status=status,
        basis=row.basis,
        text_shown=row.text_shown,
        evidence=row.evidence,
        source=source,
        source_detail=row.source_detail,
        by_user=actor.user,
        by_label=row.changed_by_label,
        by_staff=bool(actor.staff),
        ip_hash=ip_hash or "",
        at=now,
    )
    if status == UNSUBSCRIBED and value_hash:
        reason = suppression_reason or SUPPRESSION_REASON.get(source, Suppression.Reason.MANUAL)
        suppressions.add(contact.account, channel, value_hash, reason, now=now)
    return row


def ensure_rows(contact, source, actor=None, source_detail="", now=None):
    """Efter att en kontakt skapats eller fått en adress: en rad per kanal
    med adress, missing och bunden till adressen. En spärrad adress blir
    unsubscribed direkt (med en loggrad), så att listan visar Avregistrerad.
    source är kontaktens källa (import, manual, signup ...)."""
    now = now or timezone.now()
    actor = actor or SYSTEM
    with transaction.atomic():
        for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
            value_hash = keys.value_hash(channel, contact.address(channel))
            if not value_hash:
                continue
            row = _consent_row(contact, channel, lock=True)
            if row is not None and row.value_hash == value_hash:
                continue
            keys.require_fingerprints()
            if row is not None:
                # Adressen har bytts utan change_address: samma sak som ett byte.
                _apply(
                    contact,
                    channel,
                    row,
                    MISSING,
                    value_hash=value_hash,
                    source=Consent.Source.ADDRESS,
                    actor=actor,
                    source_detail="Adressen ändrades",
                    now=now,
                )
                row.refresh_from_db()
            if suppressions.is_suppressed(contact.account, channel, value_hash=value_hash):
                _apply(
                    contact,
                    channel,
                    row,
                    UNSUBSCRIBED,
                    value_hash=value_hash,
                    source=source,
                    actor=actor,
                    source_detail=source_detail or "Adressen finns på spärrlistan",
                    now=now,
                )
            elif row is None:
                Consent.objects.create(
                    contact=contact,
                    channel=channel,
                    status=MISSING,
                    value_hash=value_hash,
                    source=source,
                    source_detail=(source_detail or "")[:200],
                    changed_at=now,
                    changed_by=actor.user,
                    changed_by_label=(actor.label or "")[:120],
                )


def company_applies(contact):
    """Får kontaktens e-post status company (H.5, MFL 20 §)? Typ företag,
    juridiskt organisationsnummer, en e-postadress som inte är gratis-e-post."""
    return bool(
        contact.kind == Contact.Kind.COMPANY
        and len(contact.org_number or "") == 10
        and contact.org_number[2] not in "01"
        and contact.email
        and not is_freemail(contact.email)
    )


def derive_company(contact, source, actor=None, now=None):
    """Sätt eller ta bort company på e-postkanalen efter kontaktens uppgifter
    (H.5). Härlett, inte en övergång någon begär: sätts bara från missing,
    på adressen raden är bunden till och aldrig över en spärr; tas bort
    (tillbaka till missing) när villkoren inte längre gäller. source är den
    väg som ändrade uppgifterna (för loggen). Returnerar Outcome."""
    now = now or timezone.now()
    value_hash = keys.value_hash(CHANNEL_EMAIL, contact.email)
    with transaction.atomic():
        row = _consent_row(contact, CHANNEL_EMAIL, lock=True)
        if row is None or not value_hash or row.value_hash != value_hash:
            return Outcome(row)
        if company_applies(contact):
            if row.status != MISSING:
                return Outcome(row)
            keys.require_fingerprints()
            if suppressions.is_suppressed(contact.account, CHANNEL_EMAIL, value_hash=value_hash):
                return Outcome(row, refused="suppressed")
            row = _apply(
                contact,
                CHANNEL_EMAIL,
                row,
                COMPANY,
                value_hash=value_hash,
                source=source,
                actor=actor,
                source_detail="Företag med organisationsnummer",
                now=now,
            )
            return Outcome(row, changed=True)
        if row.status == COMPANY:
            keys.require_fingerprints()
            row = _apply(
                contact,
                CHANNEL_EMAIL,
                row,
                MISSING,
                value_hash=value_hash,
                source=source,
                actor=actor,
                source_detail="Inte längre företag med organisationsnummer",
                now=now,
            )
            return Outcome(row, changed=True)
    return Outcome(row)


# ---------------------------------------------------------------------------
# Vem får utskick
# ---------------------------------------------------------------------------


def ineligible_reason(contact, channel, purpose, consent=None, suppressed=None):
    """ "" när kontakten får ett utskick med syftet purpose (REKLAM eller
    INFORMATION) på kanalen, annars orsaken som I.8 visar: no_address,
    bounced, suppressed, no_consent, declined, pending_doi.

    consent och suppressed kan skickas in när de redan är hämtade (listor
    och frysningen); annars läses de här."""
    if purpose not in PURPOSES:
        raise ValueError(f"Okänt syfte: {purpose!r}")
    address = contact.address(channel)
    if not address:
        return "no_address"
    if channel == CHANNEL_EMAIL and contact.email_state != Contact.EmailState.OK:
        return "bounced"
    value_hash = keys.value_hash(channel, address)
    if suppressed is None:
        suppressed = suppressions.is_suppressed(contact.account, channel, value_hash=value_hash)
    if suppressed:
        return "suppressed"
    if consent is None:
        consent = _consent_row(contact, channel)
    status = consent.status if consent is not None else MISSING
    if status == UNSUBSCRIBED:
        return "suppressed"
    if purpose == INFORMATION:
        return ""
    if status == DECLINED:
        return "declined"
    if status == PENDING:
        return "pending_doi"
    if status not in REKLAM_OK:
        return "no_consent"
    if status == COMPANY and channel != CHANNEL_EMAIL:
        return "no_consent"
    if consent.value_hash != value_hash:
        return "no_consent"
    return ""


def eligible(contact, channel, purpose, consent=None, suppressed=None):
    """Får kontakten ett utskick med syftet på kanalen just nu?"""
    return not ineligible_reason(contact, channel, purpose, consent, suppressed)


def eligible_contacts(contacts, channel, purpose):
    """Samma regel som eligible, som ett filter på en Contact-queryset (en
    fråga, för rubrikens "1 902 kan få sms" och listans filter). Bygger på
    att samtyckesraden alltid är bunden till kontaktens nuvarande adress
    (ensure_rows och contacts.change_address)."""
    if purpose == REKLAM:
        statuses = (YES, EXISTING) if channel == CHANNEL_SMS else REKLAM_OK
    else:
        statuses = tuple(s for s in Consent.Status.values if s != UNSUBSCRIBED)
    blocked = Suppression.objects.filter(
        account=OuterRef(OuterRef("account")),
        channel=channel,
        value_hash=OuterRef("value_hash"),
    )
    ok_consent = (
        Consent.objects.filter(contact=OuterRef("pk"), channel=channel, status__in=statuses)
        .exclude(value_hash="")
        .filter(~Exists(blocked))
    )
    field = "phone" if channel == CHANNEL_SMS else "email"
    contacts = contacts.filter(Exists(ok_consent)).exclude(**{field: ""})
    if channel == CHANNEL_EMAIL:
        contacts = contacts.filter(email_state=Contact.EmailState.OK)
    return contacts


# ---------------------------------------------------------------------------
# Etiketterna
# ---------------------------------------------------------------------------

_CHANNEL_WORD = {CHANNEL_SMS: "Sms", CHANNEL_EMAIL: "E-post"}


def chip(contact, channel, consent=None):
    """Etiketten för kanalen i listan och på kortet (README B.1 och I.7):
    {"label", "tone", "channel"}. tone är ok, wait, muted, warn eller stop
    (klassen fl-kt-chip--<tone> i Kontakters css)."""
    word = _CHANNEL_WORD[channel]
    if not contact.address(channel):
        missing = "E-post saknas" if channel == CHANNEL_EMAIL else "Mobil saknas"
        return {"label": missing, "tone": "muted", "channel": word}
    if channel == CHANNEL_EMAIL and contact.email_state == Contact.EmailState.BOUNCED:
        return {"label": "E-post: studsad", "tone": "stop", "channel": word}
    status = consent.status if consent is not None else MISSING
    if status == YES:
        label, tone = f"{word}: ja", "ok"
    elif status == EXISTING:
        label, tone = f"{word}: befintlig kund", "ok"
    elif status == COMPANY:
        label, tone = "E-post (företag)", "ok"
    elif status == PENDING:
        label, tone = "Väntar på bekräftelse", "wait"
    elif status == DECLINED:
        label, tone = "Vill inte ha erbjudanden", "warn"
    elif status == UNSUBSCRIBED:
        stop = consent is not None and consent.source == S.STOP
        label, tone = ("Avregistrerad (STOPP)" if stop else "Avregistrerad"), "stop"
    else:
        label, tone = "Inget samtycke", "muted"
    return {"label": label, "tone": tone, "channel": word}
