"""
Demokundens kontakter (README C.2, J S1): anropas av flamingo_demo.

    reset(account)              tar bort allt utskick har för demokontot, också
                                spärrlistan och samtyckesloggen (bara demot)
    seed(account, staff, now)   bygger det från grunden: utskick på, ett påhittat
                                organisationsnummer, extrafält, taggar, listor,
                                kontakter med samtycken i varje läge, en klar
                                import, en avstängd anmälningssida och förfrågningar
                                kopplade till sina kontakter

Allt är påhittat som resten av demot: numren kommer ur PTS serie för film
och böcker (070-174 06 05 till 99), adresserna slutar på .example (och
demots egna förfrågningar på example.com), och organisationsnumret
559999-0000 har fel kontrollsiffra, så det kan inte vara ett riktigt
företags. Demokontot behöver inget biträdesavtal (access.dpa_ok) och
skickar aldrig något: bekräftelsemejlet till den som väntar går aldrig
iväg (optin.due hoppar över demokonton).

Idempotent: reset och seed tillsammans ger samma antal varje gång
(test_demo.counts). Samtyckena skrivs genom consent.set_status, så
beviset ser ut precis som hos en riktig kund.
"""

from datetime import timedelta

from apps.flamingo.models import Fact

from . import consent as consents
from . import contacts, importer
from .access import PERSON, SYSTEM, Actor, settings_for, suggest_public_slug
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Consent,
    ConsentLog,
    Contact,
    ContactList,
    DpaAcceptance,
    Event,
    ExportLog,
    FieldDef,
    ImportJob,
    ListMembership,
    SignupForm,
    Suppression,
    Tag,
    UtskickSettings,
    default_consent_text,
)

DISPLAY_NAME = "Exempelrör"
PUBLIC_SLUG = "exempelror-demo"
#: Påhittat, med fel kontrollsiffra (Luhn ger 8), så det tillhör ingen.
ORG_NUMBER = "559999-0000"
IMPORT_WHERE = "Kundregistret i kassan, kunder sedan 2023"
IMPORT_FILE = "kunder-exempelror.csv"


def reset(account):
    """Ta bort allt utskick har för demokontot. Vägrar för ett riktigt konto:
    spärrlistan och samtyckesloggen tas aldrig bort för en riktig kund."""
    if not account.is_demo:
        raise ValueError("utskick.demo.reset gäller bara demokontot.")
    importer.delete_account_jobs(account)
    SignupForm.objects.filter(account=account).delete()
    Contact.objects.filter(account=account).delete()
    ContactList.objects.filter(account=account).delete()
    Tag.objects.filter(account=account).delete()
    FieldDef.objects.filter(account=account).delete()
    # QuerySet.delete går förbi ConsentLog.delete (som vägrar): bara demot
    # får sin samtyckeslogg och spärrlista rensad.
    ConsentLog.objects.filter(account=account).delete()
    Suppression.objects.filter(account=account).delete()
    ExportLog.objects.filter(account=account).delete()
    DpaAcceptance.objects.filter(account=account).delete()


def _settings(account, staff, now):
    row = settings_for(account)
    slug = PUBLIC_SLUG
    taken = UtskickSettings.objects.filter(public_slug=slug).exclude(account=account)
    if taken.exists():
        slug = suggest_public_slug(DISPLAY_NAME + " demo", exclude_pk=row.pk)
    if not row.pk:
        row = UtskickSettings(account=account)
    row.is_enabled = True
    row.enabled_at = row.enabled_at or account.enabled_at or now - timedelta(days=30)
    row.enabled_by = row.enabled_by or staff
    row.disabled_at = None
    row.public_slug = slug
    row.display_name = DISPLAY_NAME
    row.consent_text_sms = default_consent_text(CHANNEL_SMS, DISPLAY_NAME)
    row.consent_text_email = default_consent_text(CHANNEL_EMAIL, DISPLAY_NAME)
    row.lp_consent = True
    row.privacy_url = ""
    row.pref_email_note = ""
    row.unsubscribe_text = ""
    row.sending_blocked = False
    row.blocked_reason = ""
    row.save()
    # Den genererade integritetssidan (capture.privacy_facts) behöver ett
    # bekräftat organisationsnummer; telefonen finns bland demots uppgifter.
    Fact.objects.update_or_create(
        account=account,
        key="orgnr",
        defaults={
            "label": "Organisationsnummer",
            "value": ORG_NUMBER,
            "source": Fact.SOURCE_ADX,
            "confirmed": True,
            "order": 5,
        },
    )
    return row


def _structure(account, staff, now):
    fields = {
        "fastighet": FieldDef.objects.create(
            account=account,
            key="fastighet",
            label="Fastighet",
            kind=FieldDef.Kind.CHOICE,
            choices=["Villa", "Radhus", "Lägenhet"],
            order=0,
            show_in_list=True,
        ),
        "senaste-service": FieldDef.objects.create(
            account=account,
            key="senaste-service",
            label="Senaste service",
            kind=FieldDef.Kind.DATE,
            order=1,
        ),
    }
    tags = {name: Tag.objects.create(account=account, name=name) for name in ("Nacka", "Värmdö")}
    lists = {
        "Kunder": ContactList.objects.create(
            account=account,
            name="Kunder",
            description="Alla som anlitat Exempelrör",
            created_by=staff,
            created_at=now - timedelta(days=20),
        ),
        "Servicepåminnelse": ContactList.objects.create(
            account=account,
            name="Servicepåminnelse",
            description="Påminnelse om service av varmvattenberedaren",
            created_by=staff,
            created_at=now - timedelta(days=12),
        ),
    }
    return fields, tags, lists


def _import_job(account, staff, now, rows):
    at = now - timedelta(days=20)
    return ImportJob.objects.create(
        account=account,
        created_by=staff,
        created_as_staff=True,
        original_name=IMPORT_FILE,
        kind=ImportJob.Kind.CSV,
        size=1024,
        delimiter=";",
        encoding="utf-8",
        header=["Namn", "Mobil", "E-post", "Fastighet"],
        row_count=rows,
        mapping={"0": "full_name", "1": "phone", "2": "email", "3": "field:fastighet"},
        consent={"choice": "existing", "sms": True, "email": True, "where": IMPORT_WHERE},
        status=ImportJob.Status.DONE,
        progress=rows,
        counts={
            "new": rows,
            "updated": 0,
            "suppressed": 0,
            "marked": 0,
            "errors": 0,
            "conflicts": 0,
            "limit": 0,
            "company_freemail": 0,
        },
        started_at=at,
        finished_at=at + timedelta(seconds=4),
        file_deleted_at=at + timedelta(days=1),
        created_at=at,
    )


def _lead_for(account, phone):
    from apps.flamingo.models import Lead
    from apps.flamingo.sms import normalize_phone

    for lead in Lead.objects.filter(account=account, contact__isnull=True).order_by("created_at"):
        if lead.phone and normalize_phone(lead.phone) == phone:
            return lead
    return None


def _link_lead(contact, lead):
    from apps.flamingo.models import Lead

    Lead.objects.filter(pk=lead.pk, account_id=contact.account_id).update(contact=contact)
    page = lead.campaign.landing_url if lead.campaign_id else ""
    contacts.record_event(contact, Event.LEAD, {"sida": page[:200]}, lead=lead, at=lead.created_at)


#: Kontakterna: (förnamn, efternamn, mobil, e-post, källa, dagar sedan, vad som händer).
PEOPLE = [
    ("Sara", "Holm", "+46701740610", "", "form", None, "lp_sms"),
    ("Maria", "Nilsson", "+46701740611", "maria.nilsson@example.com", "form", None, "lp_both"),
    ("Johan", "Berg", "+46701740612", "", "import", 20, "existing"),
    ("Erik", "Svensson", "+46701740613", "", "import", 20, "existing"),
    ("Lena", "Ek", "+46701740614", "lena.ek@hemma.example", "import", 20, "existing"),
    ("Per", "Lund", "+46701740615", "", "import", 20, "declined"),
    ("Kim", "Andersson", "+46701740620", "kim.andersson@hemma.example", "import", 20, "yes"),
    ("Nora", "Lind", "+46701740621", "", "import", 20, "unsubscribed"),
    ("Sofia", "Ström", "+46701740623", "sofia.strom@hemma.example", "import", 20, "existing"),
    ("Ella", "Berg", "", "ella.berg@hemma.example", "signup", 6, "signup"),
    ("Omar", "Haddad", "+46701740622", "", "manual", 900, "inactive"),
]

FIELDS = {
    "+46701740612": {"fastighet": "Villa"},
    "+46701740613": {"fastighet": "Villa", "senaste-service": "2025-11-03"},
    "+46701740614": {"fastighet": "Lägenhet"},
    "+46701740620": {"fastighet": "Radhus", "senaste-service": "2024-09-18"},
    "+46701740623": {"fastighet": "Radhus"},
}
TAGS = {
    "+46701740610": ("Nacka",),
    "+46701740612": ("Nacka",),
    "+46701740620": ("Värmdö",),
    "+46701740623": ("Värmdö",),
}
LISTS = {
    "Kunder": ("+46701740612", "+46701740613", "+46701740614", "+46701740620", "+46701740623"),
    "Servicepåminnelse": ("+46701740613", "+46701740620"),
}


def _set(contact, channel, status, *, source, at, **kwargs):
    return consents.set_status(contact, channel, status, source=source, now=at, **kwargs)


def _staff_actor(staff):
    """Byrån som gjorde importen åt kunden, som i kundvyn ("ADX (Giovanni)")."""
    if staff is None:
        return SYSTEM
    first = (staff.first_name or staff.get_username()).strip()
    return Actor(user=staff, label=f"ADX ({first})"[:120], staff=True)


def _people(account, row, staff, now, job, fields):
    staff_actor = _staff_actor(staff)
    file_detail = importer.source_detail(job)
    person = PERSON
    texts = {CHANNEL_SMS: row.consent_text_sms, CHANNEL_EMAIL: row.consent_text_email}
    made = {}
    for first, last, phone, email, source, days, story in PEOPLE:
        lead = _lead_for(account, phone) if phone else None
        if lead is not None:
            at = lead.created_at
        else:
            at = now - timedelta(days=days or 1, hours=3)
        data = {"first_name": first, "last_name": last, "phone": phone, "email": email}
        if phone in FIELDS:
            data["fields"] = FIELDS[phone]
        page = lead.campaign.landing_url if lead is not None and lead.campaign_id else "/lp/"
        detail = {"import": file_detail, "form": page, "signup": "/utskick/"}.get(source, "")
        contact = contacts.create(
            account,
            data,
            source=source,
            actor=staff_actor if source in ("import", "manual") else person,
            source_detail=detail,
            check_collect=False,
            defs=fields,
            now=at,
        )
        if source == "import":
            contacts.record_event(
                contact, Event.IMPORTED, {"import": job.pk, "fil": file_detail[:100]}, at=at
            )
        if story == "lp_sms" or story == "lp_both":
            _set(
                contact,
                CHANNEL_SMS,
                consents.YES,
                source=Consent.Source.LP_FORM,
                at=at,
                actor=person,
                source_detail=page,
                text_shown=texts[CHANNEL_SMS],
                collected_at=at,
            )
        if story == "lp_both":
            _set(
                contact,
                CHANNEL_EMAIL,
                consents.PENDING,
                source=Consent.Source.LP_FORM,
                at=at,
                actor=person,
                source_detail=page,
                text_shown=texts[CHANNEL_EMAIL],
                collected_at=at,
            )
        if story in ("existing", "declined", "unsubscribed", "yes"):
            status = consents.YES if story == "yes" else consents.EXISTING
            for channel in (CHANNEL_SMS, CHANNEL_EMAIL):
                if contact.address(channel):
                    _set(
                        contact,
                        channel,
                        status,
                        source=Consent.Source.IMPORT,
                        at=at,
                        actor=staff_actor,
                        source_detail=file_detail,
                        evidence=IMPORT_WHERE,
                    )
        if story == "declined":
            _set(
                contact,
                CHANNEL_SMS,
                consents.DECLINED,
                source=Consent.Source.PREFERENCE,
                at=now - timedelta(days=3),
                actor=person,
            )
        if story == "unsubscribed":
            _set(
                contact,
                CHANNEL_SMS,
                consents.UNSUBSCRIBED,
                source=Consent.Source.MANUAL,
                at=now - timedelta(days=5),
                actor=staff_actor,
                evidence="Bad om att slippa sms, i telefon",
            )
        if story == "signup":
            _set(
                contact,
                CHANNEL_EMAIL,
                consents.PENDING,
                source=Consent.Source.SIGNUP,
                at=at,
                actor=person,
                source_detail="/utskick/",
                text_shown=texts[CHANNEL_EMAIL],
                collected_at=at,
            )
            confirmed = at + timedelta(minutes=7)
            _set(
                contact,
                CHANNEL_EMAIL,
                consents.YES,
                source=Consent.Source.DOI,
                at=confirmed,
                actor=person,
                source_detail="/utskick/",
                text_shown=texts[CHANNEL_EMAIL],
                collected_at=at,
                confirmed_at=confirmed,
                proved=True,
            )
            contacts.record_event(contact, Event.SIGNUP, {"sida": "/utskick/"}, at=at)
        if story == "inactive":
            Contact.objects.filter(pk=contact.pk).update(
                created_at=now - timedelta(days=days), inactive_flagged_at=now - timedelta(days=1)
            )
        if lead is not None:
            _link_lead(contact, lead)
        made[phone or email] = contact
    return made


def seed(account, staff, now):
    """Bygg demots kontakter från grunden (efter reset och efter att
    flamingo_demo skapat förfrågningarna). Returnerar kontakterna."""
    row = _settings(account, staff, now)
    fields, tags, lists = _structure(account, staff, now)
    importable = sum(1 for p in PEOPLE if p[4] == "import")
    job = _import_job(account, staff, now, importable)
    made = _people(account, row, staff, now, job, fields)
    # Ett företag: e-post på egen domän ger e-post (företag) av sig självt.
    brf = contacts.create(
        account,
        {
            "kind": Contact.Kind.COMPANY,
            "company_name": "Brf Exempelgården",
            "org_number": ORG_NUMBER.replace("-", ""),
            "email": "styrelsen@brf-exempelgarden.example",
        },
        source=Contact.Source.MANUAL,
        check_collect=False,
        now=now - timedelta(days=9),
    )
    made["brf"] = brf
    for phone, names in TAGS.items():
        for name in names:
            contacts.add_tag(tags[name], [made[phone]])
    contacts.add_tag(tags["Nacka"], [brf])
    for name, phones in LISTS.items():
        contacts.add_to_list(
            lists[name], [made[p] for p in phones], source=ListMembership.Source.IMPORT
        )
    contacts.add_to_list(lists["Kunder"], [brf])
    SignupForm.objects.create(
        account=account,
        title="Få erbjudanden från Exempelrör",
        intro="Erbjudanden och tips om service för ditt hem. Du kan avregistrera dig när du vill.",
        channels=[CHANNEL_EMAIL],
        is_active=False,
    )
    return made
