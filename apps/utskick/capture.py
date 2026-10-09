"""
Kontakter från landningssidornas formulär (README C.2, E.4, H.5), och
integritetstexten som anmälan, landningssidan och mejlen länkar till.

    privacy_facts(account) -> PrivacyFacts      företagets namn, organisationsnummer,
                                                kontaktuppgifter (bekräftade uppgifter)
    privacy_available(account, row)             finns en integritetstext att länka till?
    privacy_url(account, row, absolute=False)   kundens egen policy (https), annars
                                                /utskick/<slug>/integritet/
    consent_texts(account, row) -> {kanal: text}   kryssrutornas exakta texter
    tracking_ok(text)                           texten nämner att mejlen mäter öppningar
    lp_consent_channels(account, spec, row)     kanalerna som får en kryssruta på /lp/
    consent_errors(channels, data) -> {fält: fel}  formulärets fel för kryssrutorna
    from_lead_form(lead, cleaned, texts, page_path, ip_hash, click=None) -> Captured

Integrationen kopplar in det här i flamingo.public_views (LeadForm och
landing) och lp/ren/blocks/form.html; modulen importerar inget därifrån.

Regler:

- Ingenting händer utan access.can_collect(account) (utskick på och
  biträdesavtalet godkänt): ingen kontakt skapas och Lead.contact förblir
  tomt. Förfrågningar från före aktiveringen kopplas aldrig i efterhand.
- Kryssrutorna erbjuds bara med lp_consent på och en integritetstext att
  länka till (H.5): kundens egen policy (https) eller den genererade sidan,
  som kräver organisationsnummer och kontaktuppgifter under Företaget.
  Sms när formuläret har ett telefonfält, e-post när det har ett e-postfält
  och byrån klarmarkerat bekräftelsemejlen (Switchboard.doi_ready_at).
- En ikryssad ruta skapar en ny kontakt och sparar beviset: exakt text,
  sidan, tiden och besökarens ip_hash. Sms blir ja direkt (frågan K.2.5),
  e-post väntar på bekräftelsemejlet (optin.py skickar det från ticken).
- En befintlig kontakt ändras aldrig från formuläret: inget namn och ingen
  adress fylls i (vem som helst kan skriva någon annans nummer och sin egen
  e-post). Samtycket sparas bara för en kanal vars adress redan är
  kontaktens; övriga ikryssade kanaler hoppas över.
- Utan ikryssad ruta (och utan ut i S2) skapas ingen kontakt: förfrågan
  kopplas bara till en befintlig kontakt när varje adress i förfrågan som
  kontakten har stämmer exakt (contacts.match utan konflikt).
- Ett samtycke binds alltid till adressen personen skrev. Pekar numret och
  e-posten på två olika kontakter får var och en sin kanal (förfrågan
  kopplas inte); skiljer sig den andra adressen från kontaktens sparas inget
  samtycke för den kanalen och förfrågan kopplas inte.
"""

import logging
import re
from dataclasses import dataclass

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import consent as consents
from . import contacts, normalize, optin
from .access import PERSON, can_collect, settings_for
from .keys import KeyMismatch
from .models import CHANNEL_EMAIL, CHANNEL_SMS, CHANNELS, Consent, Contact, Event

logger = logging.getLogger(__name__)

#: Formulärets fel när en ruta är ikryssad men adressen saknas eller inte duger.
EMAIL_NEEDED_TEXT = "Fyll i din e-post för att få erbjudanden via e-post."
NOT_MOBILE_TEXT = (
    "Det här numret kan inte få sms. Skriv ett mobilnummer, eller kryssa ur rutan för sms."
)
#: Varför kryssrutorna och anmälan är av (inställningarna visar texten).
PRIVACY_MISSING_TEXT = (
    "Fyll i organisationsnummer och kontaktuppgifter under Företaget, "
    "eller ange en länk till din integritetspolicy."
)
#: Meningen som står i e-postens samtyckestext när öppningar mäts (H.5).
TRACKING_SENTENCE = "Mejlen innehåller en bild som visar om de öppnas."

_ORG_WORDS = re.compile(r"\borg|organisationsnummer", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Integritetstexten (H.5)
# ---------------------------------------------------------------------------


@dataclass
class PrivacyFacts:
    company: str = ""
    org_number: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    website: str = ""

    @property
    def complete(self):
        """Räcker för den genererade sidan: organisationsnummer och ett sätt
        att nå företaget."""
        return bool(self.org_number and (self.phone or self.email))


def _org_number(value):
    """Ett organisationsnummer som det skrivs (NNNNNN-NNNN), eller ""."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 10:
        return ""
    return f"{digits[:6]}-{digits[6:]}"


def privacy_facts(account, row=None):
    """Företagets uppgifter för den genererade integritetssidan, bara ur
    kontots bekräftade uppgifter under Företaget (aldrig gissat)."""
    from apps.flamingo.generator import fact_kind

    row = row or settings_for(account)
    facts = PrivacyFacts(company=row.display_name)
    for fact in account.usable_fact_rows():
        kind = fact_kind(fact)
        value = " ".join(str(fact.value or "").split())[:200]
        if not value:
            continue
        if kind == "id" and _ORG_WORDS.search(f"{fact.key} {fact.label}"):
            facts.org_number = facts.org_number or _org_number(value)
        elif kind == "phone" and not facts.phone:
            facts.phone = value
        elif kind == "email" and not facts.email:
            facts.email = value
        elif kind == "address" and not facts.address:
            facts.address = value
        elif kind == "web" and not facts.website:
            facts.website = value
    return facts


def _own_policy(row):
    url = (row.privacy_url or "").strip()
    return url if url.lower().startswith("https://") else ""


def privacy_available(account, row=None):
    """Finns en integritetstext: kundens egen policy eller tillräckliga
    uppgifter för den genererade sidan (och en adress för anmälan)?"""
    row = row or settings_for(account)
    if _own_policy(row):
        return True
    return bool(row.public_slug) and privacy_facts(account, row).complete


def privacy_url(account, row=None, absolute=False):
    """Länken "Så hanterar <företaget> dina uppgifter", eller "" när ingen
    integritetstext finns. absolute ger en adress som fungerar i ett mejl."""
    row = row or settings_for(account)
    own = _own_policy(row)
    if own:
        return own
    if not privacy_available(account, row):
        return ""
    path = reverse("utskick_public:privacy", args=[row.public_slug])
    return optin.absolute(path) if absolute else path


# ---------------------------------------------------------------------------
# Kryssrutorna på landningssidan
# ---------------------------------------------------------------------------


def consent_texts(account, row=None):
    """Kryssrutornas exakta texter, med företagets namn ifyllt. Det som
    visas är det som sparas som bevis (Consent.text_shown)."""
    row = row or settings_for(account)
    return {channel: row.consent_text(channel) for channel in CHANNELS}


def tracking_ok(text):
    """Sa personen ja till en text som nämner att öppningar mäts (H.5)?"""
    return TRACKING_SENTENCE in str(text or "")


def lp_consent_channels(account, spec, row=None):
    """Kanalerna som får en kryssruta i formuläret, i ordning sms, e-post.
    Tom lista när kryssrutorna inte ska visas alls."""
    row = row or settings_for(account)
    if not (row.lp_consent and can_collect(account) and privacy_available(account, row)):
        return []
    channels = [CHANNEL_SMS]
    if getattr(spec, "asks_email", False) and optin.doi_ready():
        channels.append(CHANNEL_EMAIL)
    return channels


#: Fältet som bär felet för en ikryssad ruta (consent_errors).
ERROR_FIELDS = {CHANNEL_SMS: "phone", CHANNEL_EMAIL: "email"}


def consent_errors(channels, data):
    """Fel för formuläret när en ruta är ikryssad men adressen inte duger.
    data är formulärets cleaned_data (phone, email, consent_sms, consent_email).
    Felet står vid adressfältet (ERROR_FIELDS), och rutan märks också.
    Inget samtycke sparas för en kanal med fel."""
    errors = {}
    if CHANNEL_EMAIL in channels and data.get("consent_email"):
        try:
            if not normalize.email(data.get("email")):
                errors["email"] = EMAIL_NEEDED_TEXT
        except normalize.InvalidValue:
            errors["email"] = EMAIL_NEEDED_TEXT
    if CHANNEL_SMS in channels and data.get("consent_sms"):
        if not normalize.phone(data.get("phone")).e164:
            errors["phone"] = NOT_MOBILE_TEXT
    return errors


# ---------------------------------------------------------------------------
# Förfrågan blir (eller kopplas till) en kontakt
# ---------------------------------------------------------------------------


@dataclass
class Captured:
    """Vad from_lead_form gjorde. contact är kontakten förfrågan kopplades
    till (eller None). email_pending: ett bekräftelsemejl väntar, så att
    tack-sidan kan säga det."""

    contact: Contact | None = None
    email_pending: bool = False


def _email_of(raw):
    try:
        return normalize.email(raw)
    except normalize.InvalidValue:
        return ""


def _link(lead, contact, page_path, now):
    """Koppla förfrågan till kontakten (samma konto) och lägg en händelse
    för senaste aktivitet (tidslinjen visar själva förfrågan)."""
    from apps.flamingo.models import Lead

    Lead.objects.filter(pk=lead.pk, account_id=contact.account_id).update(contact=contact)
    lead.contact = contact
    contacts.record_event(
        contact, Event.LEAD, data={"sida": str(page_path or "")[:200]}, lead=lead, at=now
    )


def _new_contact(account, lead, phone, email, page_path, now):
    """En ny kontakt ur förfrågan, eller None (gränsen nådd, eller en krock
    som en samtidig förfrågan hann före). Ett namn som inte går att spara
    utelämnas hellre än att kontakten inte skapas."""
    data = {"full_name": lead.name or "", "email": email}
    if phone.ok:
        data["phone"] = lead.phone
    for attempt in (data, {**data, "full_name": ""}):
        try:
            return contacts.create(
                account,
                attempt,
                source=Contact.Source.FORM,
                actor=PERSON,
                source_detail=page_path,
                check_collect=False,
                now=now,
            )
        except contacts.ContactLimitReached:
            from . import alerts

            logger.warning("Utskick: kontaktgränsen nådd för konto %s (landningssidan)", account.pk)
            alerts.agency(
                "Utskick: kontaktgränsen nådd",
                [
                    f"Konto {account.pk} har nått sin gräns för kontakter.",
                    "Kryssrutor på landningssidan sparas inte förrän gränsen höjs.",
                ],
                once=f"contact_limit:{account.pk}",
                window="day",
            )
            return None
        except contacts.ContactError as exc:
            if exc.field in ("first_name", "last_name") and attempt["full_name"]:
                continue
            logger.info("Utskick: kontakt från förfrågan %s sparades inte (%s)", lead.pk, exc.field)
            return None
    return None


def _owners(account, lead, phone, email, found, page_path, now):
    """(kontakten per kanal, kontakten förfrågan kopplas till eller None).
    En kanal saknas när adressen inte redan är kontaktens. En befintlig
    kontakt ändras aldrig härifrån (se modulens regler)."""
    if found.conflict == "two_contacts":
        by_phone = Contact.objects.filter(account=account, phone=phone.e164).first()
        by_email = Contact.objects.filter(account=account, email=email).first()
        return {CHANNEL_SMS: by_phone, CHANNEL_EMAIL: by_email}, None
    contact = found.contact
    exact = not found.conflict
    if contact is None:
        contact = _new_contact(account, lead, phone, email, page_path, now)
        if contact is None:
            again = contacts.match(account, phone=phone.e164, email=email)
            contact = again.contact if again.conflict != "two_contacts" else None
            exact = not again.conflict
    if contact is None:
        return {}, None
    owners = {
        CHANNEL_SMS: contact if phone.e164 and contact.phone == phone.e164 else None,
        CHANNEL_EMAIL: contact if email and contact.email == email else None,
    }
    return owners, contact if exact else None


def from_lead_form(lead, cleaned, texts, page_path, ip_hash="", click=None, now=None):
    """Efter att landningssidans formulär skapat förfrågan (lead): spara
    kryssrutornas samtycken och koppla förfrågan till kontakten.

    cleaned är formulärets cleaned_data (consent_sms, consent_email), texts
    kryssrutornas texter som de visades ({kanal: text}, bara de kanaler som
    hade en ruta), page_path sidans adress ("/lp/varmepump/") och ip_hash
    flamingo.limits.ip_hash. click är utskickets klick (S2), annars None.
    Kastar aldrig: ett fel här får inte fälla förfrågan."""
    account = lead.account
    now = now or timezone.now()
    if not can_collect(account):
        return Captured()
    texts = texts or {}
    ticked = [ch for ch in CHANNELS if cleaned.get(f"consent_{ch}") and texts.get(ch)]
    phone = normalize.phone(lead.phone)
    email = _email_of(lead.email)
    try:
        found = contacts.match(account, phone=phone.e164, email=email)
        if not ticked and click is None:
            contact = found.contact if not found.conflict else None
            if contact is not None:
                _link(lead, contact, page_path, now)
            return Captured(contact)
        with transaction.atomic():
            owners, primary = _owners(account, lead, phone, email, found, page_path, now)
            email_pending = False
            for channel in ticked:
                owner = owners.get(channel)
                if owner is None:
                    continue
                if channel == CHANNEL_SMS:
                    consents.set_status(
                        owner,
                        CHANNEL_SMS,
                        consents.YES,
                        source=Consent.Source.LP_FORM,
                        actor=PERSON,
                        source_detail=page_path,
                        text_shown=texts[channel],
                        collected_at=now,
                        ip_hash=ip_hash,
                        now=now,
                    )
                    continue
                outcome = consents.set_status(
                    owner,
                    CHANNEL_EMAIL,
                    consents.PENDING,
                    source=Consent.Source.LP_FORM,
                    actor=PERSON,
                    source_detail=page_path,
                    text_shown=texts[channel],
                    tracking_ok=tracking_ok(texts[channel]),
                    collected_at=now,
                    ip_hash=ip_hash,
                    now=now,
                )
                row = outcome.consent
                if row is not None and row.status == consents.PENDING:
                    if not outcome.changed:
                        optin.requeue(row, now=now)
                    email_pending = True
            if primary is not None:
                _link(lead, primary, page_path, now)
        return Captured(primary, email_pending)
    except KeyMismatch:
        logger.error("Utskick: samtycket från förfrågan %s sparades inte (nyckeln)", lead.pk)
    except Exception:  # noqa: BLE001 - förfrågan ska alltid fram ändå
        logger.exception("Utskick: kontakten från förfrågan %s sparades inte", lead.pk)
    return Captured()
