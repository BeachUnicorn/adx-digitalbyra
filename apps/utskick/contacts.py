"""
Kontakterna i kundens register (README B.1, H.4). Allt som skapar, ändrar,
matchar eller tar bort en kontakt går härifrån, så att reglerna gäller
lika för manuellt, import, anmälan, landningssidan och API:t.

    clean(account, data)                 Cleaned (normaliserat) eller ContactError
    create(account, data, source=..., actor=...)   ny kontakt med samtyckesrader
    update(contact, data, actor=...)     namn, typ, företag, organisationsnummer, fält
                                         (och adresserna via change_address)
    change_address(contact, channel, value, actor=...)
                                         enda skrivaren av phone och email efter skapandet
    match(account, phone, email)         Match(contact, conflict) för importen
    fill_from(contact, cleaned, actor)   importens "Uppdateras": fyller tomt, skriver
                                         aldrig över en adress
    search(qs, q)                        sökningen i listan (search_text__contains)
    record_event(contact, kind, ...)     Event plus senaste aktivitet
    add_to_list / add_tag / remove_tag   massändringar inom kontot
    room_left(account)                   hur många fler som får plats (contact_limit)
    export_contact(contact)              GDPR: allt om en person som en dict (S1 och S2)
    log_export(account, actor, kind, rows), reserve_export(account)
    delete_contact(contact, actor=..., delete_leads=True, suppress=True)
                                         GDPR: ta bort personen, behåll beviset

data är en dict med kind, first_name, last_name (eller full_name),
company_name, org_number, phone, email och fields ({nyckel: värde}).

Telefon och e-post ändras efter skapandet bara av change_address: samtycket
på kanalen blir då missing (eller unsubscribed om den nya adressen är
spärrad), en rad "Adressen ändrades" skrivs i samtyckesloggen och company
härleds om. En import skriver aldrig över en ifylld adress (match ger en
konflikt). Unika krockar fångas och blir ett formulärfel; loggarna bär bara
pk, aldrig nummer eller adresser (H.3).

Varje väg som skapar kontakter kräver access.can_collect(account) (utskick
på och biträdesavtalet godkänt); create prövar det också själv.
"""

import logging
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import consent as consents
from . import keys, limits, normalize
from . import suppression as suppressions
from .access import SYSTEM, can_collect, settings_for
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    ConsentLog,
    Contact,
    Event,
    ExportLog,
    FieldDef,
    ListMembership,
    Suppression,
)

logger = logging.getLogger(__name__)

DUPLICATE_PHONE = "Numret finns redan på en annan kontakt."
DUPLICATE_EMAIL = "E-postadressen finns redan på en annan kontakt."
NOT_MOBILE = "Numret kan inte ta emot sms."
LIMIT_TEXT = "Du har nått gränsen på {limit} kontakter. Be ADX höja den."
COLLECT_TEXT = "Lägg till kontakter: godkänn biträdesavtalet först."

#: Fältet där en fast telefon hamnar (normalize.LANDLINE_FIELD).
LANDLINE_LABEL = "Telefon"
#: Fulla exporter per konto och svenskt dygn (H.3).
EXPORTS_PER_DAY = 10


class ContactError(ValueError):
    """Ett fel att visa i formuläret. field är fältets namn, eller None."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


class ContactLimitReached(ContactError):
    pass


class CollectNotAllowed(ContactError):
    pass


@dataclass
class Cleaned:
    kind: str = Contact.Kind.PERSON
    first_name: str = ""
    last_name: str = ""
    company_name: str = ""
    org_number: str = ""
    phone: str = ""
    phone_country: str = ""
    email: str = ""
    fields: dict = field(default_factory=dict)
    #: Nycklarna i data som fanns med (fill_from skriver bara dem).
    given: set = field(default_factory=set)


@dataclass
class Match:
    contact: Contact | None
    #: "" eller en konflikt: two_contacts, phone_differs, email_differs.
    conflict: str = ""


# ---------------------------------------------------------------------------
# Normalisering
# ---------------------------------------------------------------------------


def field_defs(account):
    return {f.key: f for f in FieldDef.objects.filter(account=account)}


def _landline_field(account, defs):
    key = normalize.LANDLINE_FIELD
    if key not in defs:
        defs[key], _ = FieldDef.objects.get_or_create(
            account=account,
            key=key,
            defaults={"label": LANDLINE_LABEL, "kind": FieldDef.Kind.TEXT, "order": 999},
        )
    return key


def clean(account, data, defs=None):
    """Normalisera en kontakts uppgifter. ContactError(meddelande, fält) för
    det som inte går att spara. defs är kontots extrafält (field_defs), för
    importen som rensar många rader."""
    defs = field_defs(account) if defs is None else defs
    out = Cleaned(given=set(data))
    kind = data.get("kind") or Contact.Kind.PERSON
    if kind not in Contact.Kind.values:
        raise ContactError("Välj privatperson eller företag.", "kind")
    out.kind = kind
    if data.get("full_name") and not (data.get("first_name") or data.get("last_name")):
        out.first_name, out.last_name = normalize.split_name(data["full_name"])
        out.given |= {"first_name", "last_name"}
    else:
        out.first_name = " ".join(str(data.get("first_name") or "").split())[:60]
        out.last_name = " ".join(str(data.get("last_name") or "").split())[:80]
    out.company_name = " ".join(str(data.get("company_name") or "").split())[:120]
    for name in ("first_name", "last_name", "company_name"):
        if normalize.looks_like_personnummer(getattr(out, name)):
            raise ContactError(normalize.PERSONNUMMER_TEXT, name)

    try:
        org = normalize.org_number(data.get("org_number"))
    except normalize.InvalidValue as exc:
        raise ContactError(str(exc), "org_number") from None
    if org.personal:
        # Enskild firma: organisationsnumret är ett personnummer. Sparas
        # inte, och raden är en privatperson (MFL 19 §).
        out.org_number = ""
        out.kind = Contact.Kind.PERSON
    else:
        out.org_number = org.value

    phone = normalize.phone(data.get("phone"))
    if phone.error:
        raise ContactError(phone.error, "phone")
    out.phone, out.phone_country = phone.e164, phone.country

    try:
        out.email = normalize.email(data.get("email"))
    except normalize.InvalidValue as exc:
        raise ContactError(str(exc), "email") from None

    fields = {}
    for key, raw in (data.get("fields") or {}).items():
        definition = defs.get(key)
        if definition is None:
            raise ContactError("Fältet finns inte.", f"field:{key}")
        try:
            value = normalize.field_value(definition.kind, raw, definition.choices)
        except normalize.InvalidValue as exc:
            raise ContactError(str(exc), f"field:{key}") from None
        if value:
            fields[key] = value
    if phone.landline:
        fields[_landline_field(account, defs)] = phone.landline
    out.fields = fields
    return out


def search_text_for(contact):
    """Det sökningen letar i: namn, företag, organisationsnummer, e-post,
    telefonsiffror (både +46... och 07...) och fältvärden, i gemener."""
    parts = [
        contact.first_name,
        contact.last_name,
        contact.company_name,
        contact.org_number,
        contact.email,
    ]
    if contact.phone:
        digits = contact.phone.lstrip("+")
        parts.append(digits)
        if contact.phone.startswith("+46"):
            parts.append("0" + contact.phone[3:])
    for value in (contact.fields or {}).values():
        text = str(value)
        parts.append(text)
        compact = "".join(ch for ch in text if ch.isalnum())
        if compact != text:
            parts.append(compact)
    return " ".join(p for p in parts if p).lower()


def search(contacts, q):
    """Filtrera en Contact-queryset på sökrutan. Ett nummer söks på sina
    siffror ("070-123 45" hittar 0701234567), ett regnummer utan mellanslag."""
    text = " ".join(str(q or "").split()).lower()
    if not text:
        return contacts
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and len(digits) >= 3 and all(ch.isdigit() or ch in "+-() " for ch in text):
        return contacts.filter(search_text__contains=digits)
    condition = Q(search_text__contains=text)
    compact = "".join(ch for ch in text if ch.isalnum())
    if compact and compact != text:
        condition |= Q(search_text__contains=compact)
    return contacts.filter(condition)


# ---------------------------------------------------------------------------
# Skapa, ändra, matcha
# ---------------------------------------------------------------------------


def room_left(account):
    """Hur många kontakter till som får plats under kontots gräns."""
    limit = settings_for(account).contact_limit
    return max(0, limit - Contact.objects.filter(account=account).count())


def _duplicate_error(contact):
    """Vilken unik regel som krockade (för ett läsbart formulärfel)."""
    others = Contact.objects.filter(account_id=contact.account_id).exclude(pk=contact.pk)
    if contact.phone and others.filter(phone=contact.phone).exists():
        return ContactError(DUPLICATE_PHONE, "phone")
    return ContactError(DUPLICATE_EMAIL, "email")


def create(
    account,
    data,
    *,
    source,
    actor=None,
    source_detail="",
    check_collect=True,
    cleaned=None,
    defs=None,
    now=None,
):
    """Skapa en kontakt med samtyckesrader (missing, eller unsubscribed för
    en spärrad adress) och company härlett. data som för clean(), eller ett
    färdigt cleaned. ContactError vid fel, krock eller full gräns."""
    if source not in Contact.Source.values:
        raise ValueError(f"Okänd källa: {source!r}")
    if check_collect and not can_collect(account):
        raise CollectNotAllowed(COLLECT_TEXT)
    actor = actor or SYSTEM
    now = now or timezone.now()
    data_clean = cleaned if cleaned is not None else clean(account, data, defs)
    if not (data_clean.phone or data_clean.email):
        if not (data_clean.first_name or data_clean.last_name or data_clean.company_name):
            raise ContactError("Fyll i minst ett namn, ett mobilnummer eller en e-post.")
    consent_source = _consent_source(source)
    with transaction.atomic():
        limits.lock_contacts(account)
        limit = settings_for(account).contact_limit
        if Contact.objects.filter(account=account).count() >= limit:
            raise ContactLimitReached(LIMIT_TEXT.format(limit=_group(limit)))
        contact = Contact(
            account=account,
            kind=data_clean.kind,
            first_name=data_clean.first_name,
            last_name=data_clean.last_name,
            company_name=data_clean.company_name,
            org_number=data_clean.org_number,
            phone=data_clean.phone,
            phone_country=data_clean.phone_country,
            email=data_clean.email,
            fields=data_clean.fields,
            source=source,
            source_detail=(source_detail or "")[:200],
            created_at=now,
        )
        contact.search_text = search_text_for(contact)
        try:
            with transaction.atomic():
                contact.save()
        except IntegrityError:
            logger.info("Kontakten för konto %s krockade med en befintlig adress", account.pk)
            contact.pk = None
            raise _duplicate_error(contact) from None
        consents.ensure_rows(contact, consent_source, actor, now=now)
        consents.derive_company(contact, consent_source, actor, now=now)
    return contact


def _consent_source(contact_source):
    """Samtyckesloggens källa för en ny kontakt från contact_source."""
    return {
        Contact.Source.IMPORT: Consent.Source.IMPORT,
        Contact.Source.FORM: Consent.Source.LP_FORM,
        Contact.Source.LEAD: Consent.Source.LP_FORM,
        Contact.Source.SIGNUP: Consent.Source.SIGNUP,
        Contact.Source.API: Consent.Source.API,
        Contact.Source.REPLY: Consent.Source.REPLY,
    }.get(contact_source, Consent.Source.MANUAL)


def _group(number):
    return f"{int(number):,}".replace(",", " ")


def update(contact, data, *, actor=None, source=Consent.Source.MANUAL, defs=None, now=None):
    """Ändra namn, typ, företag, organisationsnummer och extrafält, och
    adresserna via change_address när de finns med i data och skiljer sig.
    Bara nycklarna som finns i data ändras. ContactError vid fel."""
    actor = actor or SYSTEM
    now = now or timezone.now()
    merged = {
        "kind": contact.kind,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "company_name": contact.company_name,
        "org_number": contact.org_number,
        "phone": contact.phone,
        "email": contact.email,
        "fields": dict(contact.fields or {}),
    }
    for key, value in data.items():
        if key == "fields":
            merged["fields"].update(value or {})
        else:
            merged[key] = value
    if "full_name" in data and not ({"first_name", "last_name"} & set(data)):
        merged["first_name"], merged["last_name"] = normalize.split_name(data["full_name"])
    merged.pop("full_name", None)
    defs = field_defs(contact.account) if defs is None else defs
    # Värden för fält som kunden tagit bort följer inte med (de syns inte
    # och skulle annars stoppa varje ändring av kontakten).
    merged["fields"] = {k: v for k, v in merged["fields"].items() if k in defs}
    cleaned = clean(contact.account, merged, defs)
    with transaction.atomic():
        contact.kind = cleaned.kind
        contact.first_name = cleaned.first_name
        contact.last_name = cleaned.last_name
        contact.company_name = cleaned.company_name
        contact.org_number = cleaned.org_number
        contact.fields = cleaned.fields
        contact.search_text = search_text_for(contact)
        contact.save(
            update_fields=[
                "kind",
                "first_name",
                "last_name",
                "company_name",
                "org_number",
                "fields",
                "search_text",
                "updated_at",
            ]
        )
        if cleaned.phone != contact.phone:
            change_address(contact, CHANNEL_SMS, cleaned.phone, actor=actor, now=now)
        if cleaned.email != contact.email:
            change_address(contact, CHANNEL_EMAIL, cleaned.email, actor=actor, now=now)
        consents.derive_company(contact, source, actor, now=now)
    return contact


def change_address(
    contact, channel, value, *, actor=None, source_detail="Adressen ändrades", now=None
):
    """Byt (eller töm) kontaktens mobilnummer eller e-post. Samtycket på
    kanalen blir missing, eller unsubscribed om den nya adressen är spärrad,
    med en rad i samtyckesloggen; company härleds om. True om något
    ändrades. ContactError för ett ogiltigt värde, en fast telefon eller en
    adress som en annan kontakt redan har."""
    actor = actor or SYSTEM
    now = now or timezone.now()
    if channel == CHANNEL_SMS:
        phone = normalize.phone(value)
        if phone.error:
            raise ContactError(phone.error, "phone")
        if phone.landline:
            raise ContactError(NOT_MOBILE, "phone")
        new, country, field_name = phone.e164, phone.country, "phone"
    elif channel == CHANNEL_EMAIL:
        try:
            new = normalize.email(value)
        except normalize.InvalidValue as exc:
            raise ContactError(str(exc), "email") from None
        country, field_name = "", "email"
    else:
        raise ValueError(f"Okänd kanal: {channel!r}")
    if new == getattr(contact, field_name):
        return False
    with transaction.atomic():
        setattr(contact, field_name, new)
        update_fields = [field_name, "search_text", "updated_at"]
        if channel == CHANNEL_SMS:
            contact.phone_country = country
            update_fields.append("phone_country")
        else:
            contact.email_state = Contact.EmailState.OK
            contact.email_soft_bounces = 0
            contact.email_bounced_at = None
            update_fields += ["email_state", "email_soft_bounces", "email_bounced_at"]
        contact.search_text = search_text_for(contact)
        try:
            with transaction.atomic():
                contact.save(update_fields=update_fields)
        except IntegrityError:
            logger.info("Kontakt %s: den nya adressen finns redan på en annan kontakt", contact.pk)
            contact.refresh_from_db()
            raise ContactError(
                DUPLICATE_PHONE if channel == CHANNEL_SMS else DUPLICATE_EMAIL, field_name
            ) from None
        if Consent.objects.filter(contact=contact, channel=channel).exists():
            value_hash = keys.value_hash(channel, new)
            blocked = bool(value_hash) and suppressions.is_suppressed(
                contact.account, channel, value_hash=value_hash
            )
            consents.set_status(
                contact,
                channel,
                consents.UNSUBSCRIBED if blocked else consents.MISSING,
                source=Consent.Source.ADDRESS,
                actor=actor,
                source_detail=source_detail,
                suppression_reason=Suppression.Reason.MANUAL,
                now=now,
            )
        else:
            consents.ensure_rows(contact, Consent.Source.ADDRESS, actor, source_detail, now=now)
        if channel == CHANNEL_EMAIL:
            consents.derive_company(contact, Consent.Source.ADDRESS, actor, now=now)
    return True


def match(account, phone="", email=""):
    """Kontakten som en importrad (normaliserad) hör till. Konflikt när
    numret och e-posten pekar på två olika kontakter, eller när den ena
    matchar men den andra skiljer sig från en ifylld adress: då skrivs
    ingenting över (B.1)."""
    by_phone = Contact.objects.filter(account=account, phone=phone).first() if phone else None
    by_email = Contact.objects.filter(account=account, email=email).first() if email else None
    if by_phone and by_email and by_phone.pk != by_email.pk:
        return Match(None, "two_contacts")
    contact = by_phone or by_email
    if contact is None:
        return Match(None)
    if phone and contact.phone and contact.phone != phone:
        return Match(contact, "phone_differs")
    if email and contact.email and contact.email != email:
        return Match(contact, "email_differs")
    return Match(contact)


def fill_from(contact, cleaned, *, actor=None, now=None):
    """Importens uppdatering av en befintlig kontakt: tomma namn och
    företagsuppgifter fylls i, extrafält från filen skrivs (ett värde i
    filen vinner), en tom adress fylls via change_address. En ifylld adress
    skrivs aldrig över (match ger konflikt först). True om något ändrades."""
    actor = actor or SYSTEM
    changed = []
    for name in ("first_name", "last_name", "company_name", "org_number"):
        value = getattr(cleaned, name)
        if value and not getattr(contact, name):
            setattr(contact, name, value)
            changed.append(name)
    if cleaned.kind == Contact.Kind.COMPANY and contact.kind != Contact.Kind.COMPANY:
        if "kind" in cleaned.given:
            contact.kind = Contact.Kind.COMPANY
            changed.append("kind")
    new_fields = {**(contact.fields or {}), **cleaned.fields}
    if new_fields != (contact.fields or {}):
        contact.fields = new_fields
        changed.append("fields")
    with transaction.atomic():
        if changed:
            contact.search_text = search_text_for(contact)
            contact.save(update_fields=[*changed, "search_text", "updated_at"])
        address_changed = False
        if cleaned.phone and not contact.phone:
            address_changed |= change_address(
                contact, CHANNEL_SMS, cleaned.phone, actor=actor, source_detail="Import", now=now
            )
        if cleaned.email and not contact.email:
            address_changed |= change_address(
                contact, CHANNEL_EMAIL, cleaned.email, actor=actor, source_detail="Import", now=now
            )
        if changed:
            consents.derive_company(contact, Consent.Source.IMPORT, actor, now=now)
    return bool(changed) or address_changed


# ---------------------------------------------------------------------------
# Händelser, listor och taggar
# ---------------------------------------------------------------------------


def touch(contact, kind, at=None):
    """Senaste aktivitet ("Senast" i listan), bara framåt i tiden."""
    at = at or timezone.now()
    Contact.objects.filter(pk=contact.pk).filter(
        Q(last_activity_at__isnull=True) | Q(last_activity_at__lt=at)
    ).update(last_activity_at=at, last_activity_kind=kind[:20], inactive_flagged_at=None)


def record_event(contact, kind, data=None, lead=None, at=None, activity=True):
    """En händelse på kontaktens tidslinje (Event.S1_KINDS och senare), och
    senaste aktivitet. data är litet och utan fritext från tredje part.
    activity=False när händelsen inte är något personen gjort (en ny
    import av samma fil): då räknas den inte som aktivitet, och en kontakt
    som inte hörts av på två år förblir flaggad (E.7)."""
    at = at or timezone.now()
    event = Event.objects.create(
        account_id=contact.account_id,
        contact=contact,
        kind=kind,
        at=at,
        lead=lead,
        data=data or {},
    )
    if activity:
        touch(contact, kind, at)
    return event


def _own_contact_ids(account, contacts):
    ids = [c.pk if isinstance(c, Contact) else int(c) for c in contacts]
    return list(Contact.objects.filter(account=account, pk__in=ids).values_list("pk", flat=True))


def add_to_list(contact_list, contacts, source=ListMembership.Source.MANUAL):
    """Lägg kontakter (rader eller id:n, bara kontots egna) i en lista.
    Antal nya platser."""
    ids = _own_contact_ids(contact_list.account, contacts)
    existing = set(
        ListMembership.objects.filter(list=contact_list, contact_id__in=ids).values_list(
            "contact_id", flat=True
        )
    )
    rows = [
        ListMembership(list=contact_list, contact_id=pk, source=source)
        for pk in ids
        if pk not in existing
    ]
    ListMembership.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def remove_from_list(contact_list, contacts):
    ids = _own_contact_ids(contact_list.account, contacts)
    deleted, _ = ListMembership.objects.filter(list=contact_list, contact_id__in=ids).delete()
    return deleted


def add_tag(tag, contacts):
    """Tagga kontakter (bara kontots egna). Antal nya taggningar."""
    through = Contact.tags.through
    ids = _own_contact_ids(tag.account, contacts)
    existing = set(
        through.objects.filter(tag=tag, contact_id__in=ids).values_list("contact_id", flat=True)
    )
    rows = [through(contact_id=pk, tag_id=tag.pk) for pk in ids if pk not in existing]
    through.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def remove_tag(tag, contacts):
    through = Contact.tags.through
    ids = _own_contact_ids(tag.account, contacts)
    deleted, _ = through.objects.filter(tag=tag, contact_id__in=ids).delete()
    return deleted


# ---------------------------------------------------------------------------
# GDPR: export och borttagning per person (H.4), och exportloggen (H.3)
# ---------------------------------------------------------------------------


def linked_leads(contact):
    """Kontaktens förfrågningar, alltid filtrerade på kontot (H.1)."""
    from apps.flamingo.models import Lead

    return Lead.objects.filter(account_id=contact.account_id, contact=contact)


def _iso(value):
    return timezone.localtime(value).isoformat() if value else None


def export_contact(contact):
    """Allt registret vet om personen, som en dict för en JSON-fil (S1:
    uppgifterna, samtyckena och loggen, listor, taggar, händelser och
    förfrågningar). Senare steg lägger till mottagare, klick, svar och sms."""
    consents_out = [
        {
            "kanal": c.get_channel_display(),
            "status": c.get_status_display(),
            "grund": c.get_basis_display(),
            "text": c.text_shown,
            "källa": c.get_source_display() if c.source else "",
            "insamlat": _iso(c.collected_at),
            "bekräftat": _iso(c.confirmed_at),
            "ändrat": _iso(c.changed_at),
        }
        for c in contact.consents.order_by("channel")
    ]
    log = [
        {
            "tid": _iso(row.at),
            "kanal": row.channel,
            "från": row.old_status,
            "till": row.new_status,
            "text": row.text_shown,
            "källa": row.source,
            "detalj": row.source_detail,
        }
        for row in ConsentLog.objects.filter(contact=contact).order_by("at", "pk")
    ]
    leads = [
        {
            "tid": _iso(lead.created_at),
            "namn": lead.name,
            "telefon": lead.phone,
            "e-post": lead.email,
            "meddelande": lead.message,
            "svar": lead.answers,
            "status": lead.get_status_display(),
        }
        for lead in linked_leads(contact).order_by("created_at", "pk")
    ]
    return {
        "kontakt": {
            "typ": contact.get_kind_display(),
            "förnamn": contact.first_name,
            "efternamn": contact.last_name,
            "företag": contact.company_name,
            "organisationsnummer": contact.org_number,
            "mobil": contact.phone,
            "e-post": contact.email,
            "extrafält": contact.fields,
            "källa": contact.get_source_display(),
            "skapad": _iso(contact.created_at),
            "senaste aktivitet": _iso(contact.last_activity_at),
            # S3 (integrationen): en adress som studsat får aldrig mejl igen.
            "e-postadressen studsade": _iso(contact.email_bounced_at),
        },
        "samtycken": consents_out,
        "samtyckeslogg": log,
        "listor": list(
            contact.memberships.select_related("list")
            .order_by("list__name")
            .values_list("list__name", flat=True)
        ),
        "taggar": list(contact.tags.order_by("name").values_list("name", flat=True)),
        "händelser": [
            {"tid": _iso(e.at), "händelse": e.kind} for e in contact.events.order_by("at", "pk")
        ],
        "förfrågningar": leads,
        **_export_s2(contact),
    }


def reserve_export(account, now=None):
    """Får kontot göra en full export till i dag (högst EXPORTS_PER_DAY)?
    Räknar exporten om svaret är ja."""
    window = limits.day_window(now)
    return not limits.hit("export", str(account.pk), window, EXPORTS_PER_DAY)


def log_export(account, actor, kind, rows):
    """En rad i exportloggen ("Senast exporterad av ...")."""
    actor = actor or SYSTEM
    return ExportLog.objects.create(
        account=account,
        user=actor.user,
        as_staff=bool(actor.staff),
        kind=kind,
        rows=int(rows),
    )


def delete_contact(contact, *, actor=None, delete_leads=True, suppress=True, now=None):
    """Ta bort en person (H.4), i en transaktion. Samtyckesloggen och
    spärrarna finns kvar som pseudonymt bevis (contact blir null, kundens
    anteckning töms); med suppress läggs varje känd adress på spärrlistan
    (orsak erasure) så att personen inte importeras igen; med delete_leads
    tas personens förfrågningar bort. Svarstrådarna och deras förfrågningar
    tas alltid bort, mottagarraderna nollas och sms-texterna töms (S2,
    _erase_s2, README H.4).
    Returnerar {"leads": n, "suppressed": n}."""
    actor = actor or SYSTEM
    now = now or timezone.now()
    account = contact.account
    summary = {"leads": 0, "suppressed": 0}
    with transaction.atomic():
        # S2 först: svarstrådarna och deras förfrågningar tas alltid bort.
        _erase_s2(contact, account)
        if delete_leads:
            leads = linked_leads(contact)
            summary["leads"] = leads.count()
            leads.delete()
        ConsentLog.objects.filter(contact=contact).update(evidence="")
        if suppress:
            for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
                value_hash = keys.value_hash(channel, contact.address(channel))
                if value_hash:
                    _, created = suppressions.add(
                        account, channel, value_hash, Suppression.Reason.ERASURE, now=now
                    )
                    summary["suppressed"] += int(created)
        pk = contact.pk
        contact.delete()
    logger.info(
        "Kontakt %s i konto %s togs bort (GDPR) av användare %s",
        pk,
        account.pk,
        getattr(actor.user, "pk", None),
    )
    return summary


# ---------------------------------------------------------------------------
# S2 (inkorg-byggaren): utskick, klick, svar och sms i GDPR-exporten och
# borttagningen (README H.4). Anropas av export_contact och delete_contact.
# ---------------------------------------------------------------------------


def _sms_rows_for(contact, recipient_sms=(), thread_sms=()):
    """Kontaktens sms genom Flamingo (allt utom API:t) hos kundens SmsAccount:
    till numret, eller burna av kontaktens mottagare och trådar."""
    from apps.sms.models import SmsAccount, SmsMessage

    sms_account = SmsAccount.objects.filter(customer_id=contact.account.customer_id).first()
    if sms_account is None:
        return SmsMessage.objects.none()
    match = Q(pk__in=list(recipient_sms)) | Q(pk__in=list(thread_sms))
    if contact.phone:
        match |= Q(to=contact.phone)
    return (
        SmsMessage.objects.filter(account=sms_account)
        .exclude(source=SmsMessage.Source.API)
        .filter(match)
    )


def _contact_threads(contact):
    """Kontaktens svarstrådar: kopplade till kontakten, eller till dess nummer
    eller e-post utan kontakt (avtalet saknades när svaret kom)."""
    from .models import Thread

    match = Q(contact=contact)
    addresses = [a for a in (contact.phone, contact.email) if a]
    if addresses:
        match |= Q(contact__isnull=True, address__in=addresses)
    return Thread.objects.filter(account_id=contact.account_id).filter(match)


def _export_s2(contact):
    from .models import Click, Recipient, ThreadMessage

    recipients = (
        Recipient.objects.filter(contact=contact, utskick__account_id=contact.account_id)
        .select_related("utskick")
        .order_by("created_at", "pk")
    )
    clicks = Click.objects.filter(contact=contact, account_id=contact.account_id).order_by(
        "at", "pk"
    )
    threads = list(_contact_threads(contact).select_related("utskick").order_by("created_at"))
    messages = ThreadMessage.objects.filter(thread__in=threads).order_by("at", "pk")
    by_thread = {}
    for message in messages:
        by_thread.setdefault(message.thread_id, []).append(
            {
                "tid": _iso(message.at),
                "riktning": "in" if message.direction == ThreadMessage.Direction.IN else "ut",
                "text": message.body,
                # S3 (integrationen): mejlsvarens ämnesrad.
                **({"ämne": message.subject} if message.subject else {}),
            }
        )
    recipient_sms = [r.sms_message_id for r in recipients if r.sms_message_id]
    thread_sms = [m.sms_message_id for m in messages if m.sms_message_id]
    sms_rows = _sms_rows_for(contact, recipient_sms, thread_sms).order_by("created_at", "pk")
    return {
        "utskick": [
            {
                "utskick": r.utskick.name,
                "kanal": r.get_channel_display(),
                "status": r.get_status_display(),
                "skickat": _iso(r.sent_at),
                "levererat": _iso(r.delivered_at),
                "klick": r.click_count,
                "svarade": _iso(r.replied_at),
                "avregistrerade": _iso(r.stopped_at),
                # S3 (integrationen): öppningen är en indikation (pixeln, H.5).
                "öppnade (indikation)": _iso(r.opened_at),
            }
            for r in recipients
        ],
        "klick": [{"tid": _iso(c.at), "enhet": c.device} for c in clicks],
        "svar": [
            {
                "kanal": t.get_channel_display(),
                "utskick": t.utskick.name if t.utskick_id else "",
                "meddelanden": by_thread.get(t.pk, []),
            }
            for t in threads
        ],
        "sms": [
            {"tid": _iso(m.created_at), "text": m.body, "status": m.get_status_display()}
            for m in sms_rows
        ],
    }


def _erase_s2(contact, account):
    """Det S2 vet om personen, i delete_contacts transaktion: trådarna med
    meddelanden och svarsförfrågningarna tas bort, mottagarraderna nollas
    (kontakt, adress, sammanfogning), sms genom Flamingo får tomt nummer och
    tom text (land, delar, pris och referens finns kvar för underlaget,
    K.2.6), och inkommande sms från numret töms."""
    from apps.flamingo.models import Lead

    from .models import InboundMessage, Recipient, ThreadMessage

    threads = _contact_threads(contact)
    thread_ids = list(threads.values_list("pk", flat=True))
    recipients = Recipient.objects.filter(contact=contact, utskick__account=account)
    recipient_sms = list(
        recipients.exclude(sms_message__isnull=True).values_list("sms_message_id", flat=True)
    )
    thread_sms = list(
        ThreadMessage.objects.filter(thread_id__in=thread_ids)
        .exclude(sms_message__isnull=True)
        .values_list("sms_message_id", flat=True)
    )
    _sms_rows_for(contact, recipient_sms, thread_sms).update(to="", body="")
    Lead.objects.filter(
        account=account, source=Lead.SOURCE_REPLY, reply_thread__pk__in=thread_ids
    ).delete()
    threads.model.objects.filter(pk__in=thread_ids).delete()
    recipients.update(contact=None, address="", merge={})
    inbound = Q(contact=contact)
    if contact.phone:
        inbound |= Q(account=account, from_address=contact.phone)
    if contact.email:
        inbound |= Q(account=account, from_address=contact.email)
    # S3 (integrationen): ett mejlsvar har också en ämnesrad (G.3, H.4).
    InboundMessage.objects.filter(inbound).update(from_address="", body="", subject="")
