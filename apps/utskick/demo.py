"""
Demokundens kontakter och utskick (README C.2, J S1 och S2): anropas av
flamingo_demo.

    reset(account)              tar bort allt utskick har för demokontot, också
                                spärrlistan, samtyckesloggen, svarstrådarna och
                                de inkommande sms:en (bara demot)
    seed(account, staff, now)   bygger det från grunden: utskick på, ett påhittat
                                organisationsnummer, extrafält, taggar, listor,
                                kontakter med samtycken i varje läge, en klar
                                import, en avstängd anmälningssida och förfrågningar
                                kopplade till sina kontakter; och utskicken (S2):
                                ett skickat (simulerat) med klick, förfrågningar,
                                två svar och en STOPP, ett schemalagt och ett utkast;
                                och e-posten (S3): ett skickat (simulerat) mejl i
                                Brev med ett klick till Flamingo-sidan

Utskicken går genom samma kod som en riktig kunds: bekräftelsen
(sending.state), frysningen (sending.freeze), demots simulering
(sending.sms.simulate: levererat utan sms, D.4) och avslutet
(sending.tick.finish). Svaren och STOPP går genom inbound.routing och
inbound.stop med påhittade inkommande sms, så att Inkorgen visar
"Sms-svar", "STOPP" och "Avregistrerad automatiskt" som för en riktig
kund. Bekräftelsen av STOPP skickas aldrig: den står som "Demokontot
skickar aldrig." i tråden, som ticken skulle ha lämnat den. Inget sms och
ingen SmsMessage skapas, och 46elks anropas aldrig.

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

Det schemalagda utskicket går i väg av sig självt när tiden kommer (ticken
simulerar det, demot skickar aldrig); nästa körning av flamingo_demo
bygger om det.

E-posten (S3) går samma väg: blocken genom email.blocks (rensade och
signerade som redigerarens), bekräftelsen, frysningen med mejlets
ögonblicksbild och länkar (sending.email.freeze_email) och demots
simulering (sending.email.simulate: levererat utan SES). Inget mejl
skickas och transporten anropas aldrig.
"""

from datetime import datetime, time, timedelta

from django.db.models import F, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.flamingo.models import Fact
from apps.sms.pricing import STOCKHOLM

from . import consent as consents
from . import contacts, importer
from .access import PERSON, SYSTEM, Actor, settings_for, suggest_public_slug
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    REKLAM,
    AllowedHost,
    Click,
    Consent,
    ConsentLog,
    Contact,
    ContactList,
    DpaAcceptance,
    Event,
    ExportLog,
    FieldDef,
    ImportJob,
    InboundMessage,
    LinkCode,
    ListMembership,
    Recipient,
    SignupForm,
    Suppression,
    Tag,
    Thread,
    ThreadMessage,
    TrackedLink,
    Utskick,
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
    _reset_s2(account)
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
    _utskick(account, staff, now, made, lists, tags)
    return made


# ---------------------------------------------------------------------------
# Utskicken (S2)
# ---------------------------------------------------------------------------

#: Det skickade utskicket gick för så här många dagar sedan, klockan 10.00
#: (inom tidsfönstret både vardag och helg); det schemalagda går om så här
#: många dagar.
SENT_DAYS_AGO = 8
SCHEDULED_IN_DAYS = 5
SENT_NAME = "Spolning inför vintern"
SENT_BODY = (
    "Hej {förnamn|du}, inför vintern spolar Exempelrör avloppet så att det inte "
    "fryser eller stoppar. Boka en tid: {länk:spolning}"
)
SCHEDULED_NAME = "Service av varmvattenberedaren"
SCHEDULED_BODY = (
    "Hej {förnamn|du}, det är dags för service av varmvattenberedaren. Svara på "
    "sms:et så ringer Exempelrör upp och bokar en tid."
)
DRAFT_NAME = "Blandare till fast pris"
DRAFT_BODY = (
    "Hej {förnamn|du}, i november byter Exempelrör blandare till fast pris. "
    "Svara JA så ringer vi upp."
)
#: Svaren på det skickade utskicket: (nummer, minuter efter, text).
REPLIES = [
    (
        "+46701740612",
        50,
        "Passar det tisdag nästa vecka? Vi hade stopp i källaren i våras.",
    ),
    ("+46701740614", 180, "Tack, vi spolade i våras så vi väntar till nästa år."),
]
#: Kundens svar från Inkorgen på det andra svaret (som det står när det gått).
ANSWER = (200, "Tack Lena, hör av dig när det är dags. /Exempelrör")
STOP_FROM = ("+46701740613", 25, "Stopp")
#: Klicken: (nummer, minuter efter, sekunder på sidan, förfrågan).
CLICKS = [
    ("+46701740620", 12, 95, "form"),
    ("+46701740612", 45, 20, ""),
    ("+46701740623", 120, 48, "call"),
]
KIM_MESSAGE = "Vill boka spolning av avloppet, helst en förmiddag."


def _reset_s2(account):
    """Utskickens rader för demot: utskicken (mottagarna och länkarna följer
    med), koderna, klicken, svarstrådarna med sina förfrågningar, de
    inkommande sms:en och länkvärdarna. S3: mejlens bilder och
    avsändardomänerna (efter utskicken, som pekar på domänen med RESTRICT)."""
    from apps.flamingo.models import Lead

    from .models import EmailImage, SenderDomain

    Utskick.objects.filter(account=account).delete()
    EmailImage.objects.filter(account=account).delete()
    SenderDomain.objects.filter(account=account).delete()
    LinkCode.objects.filter(account=account).delete()
    Click.objects.filter(account=account).delete()
    Lead.objects.filter(account=account, source=Lead.SOURCE_REPLY).delete()
    Thread.objects.filter(account=account).delete()
    InboundMessage.objects.filter(account=account).delete()
    InboundMessage.objects.filter(provider_id__startswith=_inbound_prefix(account)).delete()
    AllowedHost.objects.filter(account=account).delete()


def _inbound_prefix(account):
    return f"demo-{account.pk}-"


def _at_ten(day):
    return datetime.combine(day, time(10, 0), tzinfo=STOCKHOLM)


def _campaign(account):
    """Flamingo-sidan utskicket länkar till: avloppsspolningens (pausad hos
    Google; ett klick från ett utskick räknas ändå, E.4), annars den som är live."""
    from apps.flamingo.models import Campaign

    campaigns = Campaign.objects.filter(account=account).select_related("service")
    return (
        campaigns.filter(service__name="Avloppsspolning").order_by("pk").first()
        or campaigns.filter(status=Campaign.STATUS_LIVE).order_by("pk").first()
    )


def _confirm(utskick, staff, now):
    """Bekräftelsen som Granska gör den (I.6), av byrån i kundvyn."""
    from . import audience
    from .sending import state

    nonce = state.issue_nonce(utskick)
    counted = audience.count(utskick, now)
    summary = {
        "sms": counted["sms"],
        "email": counted["email"],
        "skipped": counted["skipped"],
        "skipped_by_reason": counted["skipped_by_reason"],
        "total": counted["total"],
        "purpose": utskick.purpose,
    }
    result = state.confirm(
        utskick, actor=_staff_actor(staff), nonce=nonce, summary=summary, now=now
    )
    if not result.ok:
        raise RuntimeError(f"Demots utskick kunde inte bekräftas: {result.error}")
    return utskick


def _new_utskick(account, staff, name, body, audience, created_at, scheduled_at=None):
    from . import audience as audiences

    data = audiences.empty()
    data.update(audience)
    return Utskick.objects.create(
        account=account,
        name=name,
        purpose=REKLAM,
        channel_mode=Utskick.ChannelMode.SMS_ONLY,
        audience=data,
        sms_body=body,
        sms_sender_kind=Utskick.SenderKind.REPLY,
        send_mode=Utskick.SendMode.AT,
        scheduled_at=scheduled_at,
        created_by=staff,
        created_at=created_at,
        status_changed_at=created_at,
    )


def _send(utskick, at):
    """Frysningen, demots simulering och nu sending (D.3, D.4). Övergången
    till freezing direkt (inte freeze.start_due): demot ska byggas också
    när disken är nästan full, och utan byråns larm om det."""
    from .sending import freeze, state
    from .sending import sms as loop

    state.transition(utskick, Utskick.Status.FREEZING, expected=(Utskick.Status.SCHEDULED,), now=at)
    for _ in range(50):
        result = freeze.freeze_chunk(utskick, at)
        if result is None or result["done"]:
            break
    loop.simulate(utskick.account, at + timedelta(minutes=1), only=utskick.pk)
    # S3: mejlen simuleras på samma sätt (levererat, simulated, aldrig SES).
    from .sending import email as email_loop

    email_loop.simulate(utskick.account, at + timedelta(minutes=1), only=utskick.pk)
    utskick.refresh_from_db()
    if utskick.status != Utskick.Status.SENDING:
        raise RuntimeError(f"Demots utskick fastnade i läget {utskick.status}.")


def _click(recipient, link, at, seconds, called=False):
    """Ett mänskligt klick från en telefon, besöket på sidan och tiden där
    (som klicket, landningssidan och besöksanropet skriver dem, E.3, E.4).
    Kanalen följer mottagaren (sms eller e-post, S3)."""
    from . import attribution

    click = Click.objects.create(
        account_id=recipient.utskick.account_id,
        utskick=recipient.utskick,
        recipient=recipient,
        link=link,
        contact_id=recipient.contact_id,
        channel=Click.Channel.EMAIL if recipient.channel == CHANNEL_EMAIL else Click.Channel.SMS,
        kind=Click.Kind.HUMAN,
        at=at,
        device="mobile",
        os="iOS",
        browser="Safari",
        engaged_seconds=seconds,
        beacon_at=at + timedelta(seconds=seconds),
        called=called,
    )
    Recipient.objects.filter(pk=recipient.pk).update(
        click_count=F("click_count") + 1,
        first_clicked_at=Coalesce(F("first_clicked_at"), Value(at)),
    )
    attribution.record_lp_visit(click, link.campaign, at)
    return click


def _lead(click, campaign, kind, at, kontakt):
    """Förfrågan som klicket gav (formuläret eller numret), med spåret (E.4)."""
    from apps.flamingo.models import Lead

    from . import attribution

    fields = {
        "account": campaign.account,
        "campaign": campaign,
        "service": campaign.service,
        "created_at": at,
        "activity_at": at,
    }
    if kind == "form":
        lead = Lead.objects.create(
            source=Lead.SOURCE_FORM,
            name=kontakt.full_name,
            phone=_display(kontakt.phone),
            message=KIM_MESSAGE,
            status=Lead.STATUS_QUOTE,
            **fields,
        )
    else:
        lead = Lead.objects.create(
            source=Lead.SOURCE_CALL_CLICK, status=Lead.STATUS_CONTACTED, **fields
        )
    return attribution.attach(lead, click, now=at)


def _display(e164):
    from . import normalize

    return normalize.display_phone(e164)


def _inbound(account, n, e164, text, at):
    from .inbound import routing

    return InboundMessage.objects.create(
        channel=CHANNEL_SMS,
        provider_id=f"{_inbound_prefix(account)}{n}",
        from_address=e164,
        to_address=routing.reply_number(),
        body=text,
        received_at=at,
        created_at=at,
    )


def _candidate(account, recipient):
    from .inbound import routing

    return routing.Candidate(account, None, True, recipient, recipient.utskick)


def _context(thread, recipient):
    """Utskickets sms först i tråden, som ensure_context lägger det för en
    riktig kund (demot har ingen SmsMessage att peka på)."""
    from . import composer

    body = composer.render_sms(recipient.utskick, recipient, recipient.sms_sender)
    return ThreadMessage.objects.create(
        thread=thread,
        direction=ThreadMessage.Direction.OUT,
        body=body,
        at=recipient.sent_at,
        status=ThreadMessage.Status.SENT,
    )


def _replies(account, utskick, recipients, sent_at):
    """Två svar (ett nytt, ett besvarat och klart) och en STOPP, genom samma
    routning som 46elks inkommande (G.1)."""
    from apps.flamingo.models import Lead

    from . import threads
    from .inbound import routing, stop

    n = 0
    for e164, minutes, text in REPLIES:
        n += 1
        at = sent_at + timedelta(minutes=minutes)
        recipient = recipients[e164]
        inbound = _inbound(account, n, e164, text, at)
        thread = routing.route_to(inbound, _candidate(account, recipient), at, via="demo")
        routing._done(inbound)
        _context(thread, recipient)
        if e164 == REPLIES[-1][0]:
            answered = sent_at + timedelta(minutes=ANSWER[0])
            ThreadMessage.objects.create(
                thread=thread,
                direction=ThreadMessage.Direction.OUT,
                body=ANSWER[1],
                at=answered,
                status=ThreadMessage.Status.SENT,
            )
            Thread.objects.filter(pk=thread.pk).update(last_out_at=answered, unread=False)
            Lead.objects.filter(pk=thread.lead_id).update(status=Lead.STATUS_CONTACTED)

    e164, minutes, text = STOP_FROM
    n += 1
    at = sent_at + timedelta(minutes=minutes)
    recipient = recipients[e164]
    inbound = _inbound(account, n, e164, text, at)
    stop.apply_stop(inbound, [_candidate(account, recipient)], at)
    routing._done(inbound)
    thread = Thread.objects.get(account=account, address=e164)
    _context(thread, recipient)
    for answer in threads.answers_queued().filter(thread=thread):
        # Som ticken lämnar det för demot: "Demokontot skickar aldrig."
        threads._fail_answer(answer, "demo")


def _utskick(account, staff, now, made, lists, tags):
    """Tre utskick: ett skickat (simulerat) med klick, förfrågningar, svar
    och en STOPP, ett schemalagt och ett utkast."""
    from .sending import tick

    today = timezone.localtime(now, STOCKHOLM).date()
    sent_at = _at_ten(today - timedelta(days=SENT_DAYS_AGO))
    campaign = _campaign(account)

    sent = _new_utskick(
        account,
        staff,
        SENT_NAME,
        SENT_BODY,
        {"lists": [lists["Kunder"].pk]},
        sent_at - timedelta(days=2),
        scheduled_at=sent_at,
    )
    link = None
    if campaign is not None:
        from . import links

        link = links.add_link(sent, key="spolning", campaign=campaign, label="Boka spolning")
    else:
        sent.sms_body = SENT_BODY.replace(" Boka en tid: {länk:spolning}", "")
        sent.save(update_fields=["sms_body"])
    _confirm(sent, staff, sent_at - timedelta(days=1))
    _send(sent, sent_at)
    recipients = {
        r.address: r
        for r in Recipient.objects.filter(utskick=sent, status=Recipient.Status.DELIVERED)
    }
    if link is not None:
        for e164, minutes, seconds, kind in CLICKS:
            recipient = recipients.get(e164)
            if recipient is None:
                continue
            at = sent_at + timedelta(minutes=minutes)
            click = _click(recipient, link, at, seconds, called=kind == "call")
            if kind:
                _lead(click, campaign, kind, at + timedelta(minutes=3), made[e164])
        TrackedLink.objects.filter(pk=link.pk).update(
            human_clicks=Click.objects.filter(link=link, kind=Click.Kind.HUMAN).count(),
            leads=sum(1 for e164, *_rest, kind in CLICKS if kind and e164 in recipients),
        )
    _replies(account, sent, recipients, sent_at)
    tick.finish(sent_at + timedelta(minutes=20), only=sent.pk)

    scheduled = _new_utskick(
        account,
        staff,
        SCHEDULED_NAME,
        SCHEDULED_BODY,
        {"lists": [lists["Servicepåminnelse"].pk]},
        now - timedelta(days=1),
        scheduled_at=_at_ten(today + timedelta(days=SCHEDULED_IN_DAYS)),
    )
    _confirm(scheduled, staff, now)

    _new_utskick(
        account,
        staff,
        DRAFT_NAME,
        DRAFT_BODY,
        {"tags": [tags["Nacka"].pk]},
        now - timedelta(hours=3),
    )
    _email_utskick(account, staff, now, lists, campaign)


# ---------------------------------------------------------------------------
# E-posten (S3, integrationen)
# ---------------------------------------------------------------------------

#: Mejlet gick för så här många dagar sedan, klockan 10.00.
EMAIL_DAYS_AGO = 3
EMAIL_NAME = "Höstbrevet"
EMAIL_SUBJECT = "Hej {förnamn|du}, så klarar huset vintern"
EMAIL_PREHEADER = "Tre saker att göra innan det blir minusgrader"
#: Mejlets block i Brev (F.1): (typ, fält). Knappen till Flamingo-sidan
#: läggs till när demot har en.
EMAIL_BLOCKS = (
    (
        "hero",
        {
            "kicker": "Höstservice",
            "title": "Så klarar huset vintern",
            "lead": "Tre saker som är bra att göra innan det blir minusgrader, "
            "och hur Exempelrör kan hjälpa till.",
            "button_text": "Boka spolning",
        },
    ),
    (
        "text",
        {
            "body": "Hej {förnamn|du},\n\nnär det blir kallt kan rör i kalla utrymmen frysa. "
            "Det här är bra att göra nu:\n\n"
            "- Stäng av och töm utekranen.\n"
            "- Se över rören i garage och källare.\n"
            "- Spola avloppet innan löven fastnar."
        },
    ),
    (
        "callout",
        {
            "text": "**PS.** Har du en värmepump? Fråga om service när vi ändå är hos dig.",
        },
    ),
    (
        "signature",
        {"greeting": "Vänliga hälsningar,", "name": "Johan Lind", "line": "Exempelrör"},
    ),
)
#: Klicket på knappen: (e-post, minuter efter, sekunder på sidan).
EMAIL_CLICK = ("kim.andersson@hemma.example", 35, 40)


def _email_utskick(account, staff, now, lists, campaign):
    """Ett skickat (simulerat) mejl i Brev till listan Kunder: de med e-post
    och samtycke (eller som befintliga kunder eller företag) får det, de
    utan adress hoppas över. Blocken går genom email.blocks som
    redigerarens, frysningen gör länkarna och ögonblicksbilden, och demots
    simulering levererar utan SES. Ett klick på knappen till Flamingo-sidan."""
    from apps.flamingo.exports import landing_page_url

    from .email import blocks as email_blocks
    from .email import registry as email_registry
    from .sending import tick

    today = timezone.localtime(now, STOCKHOLM).date()
    sent_at = _at_ten(today - timedelta(days=EMAIL_DAYS_AGO))
    has_logo, _why = email_registry.logo_state(account)
    utskick = Utskick.objects.create(
        account=account,
        name=EMAIL_NAME,
        purpose=REKLAM,
        channel_mode=Utskick.ChannelMode.EMAIL_ONLY,
        audience=_audience({"lists": [lists["Kunder"].pk]}),
        subject=EMAIL_SUBJECT,
        preheader=EMAIL_PREHEADER,
        logo_position=Utskick.LogoPosition.LEFT if has_logo else Utskick.LogoPosition.NONE,
        send_mode=Utskick.SendMode.AT,
        scheduled_at=sent_at,
        created_by=staff,
        created_at=sent_at - timedelta(days=1),
        status_changed_at=sent_at - timedelta(days=1),
    )
    made = []
    for type_key, fields in EMAIL_BLOCKS:
        fields = dict(fields)
        if type_key == "hero":
            if campaign is not None:
                fields["button_url"] = landing_page_url(campaign)
            else:
                fields.pop("button_text")
        block = email_blocks.new_block(type_key, account, utskick, user=staff, now=now)
        email_blocks.add_version(
            block, fields, "adx", staff, account=account, utskick=utskick, now=now
        )
        made.append(block)
    email_blocks.save(utskick, made, rev=0, user=staff, account=account, now=now)
    utskick.refresh_from_db()
    _confirm(utskick, staff, sent_at - timedelta(hours=20))
    _send(utskick, sent_at)
    if campaign is not None:
        email, minutes, seconds = EMAIL_CLICK
        recipient = Recipient.objects.filter(
            utskick=utskick, address=email, status=Recipient.Status.DELIVERED
        ).first()
        link = TrackedLink.objects.filter(utskick=utskick, kind=TrackedLink.Kind.LP).first()
        if recipient is not None and link is not None:
            _click(recipient, link, sent_at + timedelta(minutes=minutes), seconds)
            TrackedLink.objects.filter(pk=link.pk).update(human_clicks=1)
    tick.finish(sent_at + timedelta(minutes=20), only=utskick.pk)
    return utskick


def _audience(values):
    from . import audience as audiences

    data = audiences.empty()
    data.update(values)
    return data
