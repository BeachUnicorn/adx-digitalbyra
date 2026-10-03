"""
Demokunden för ADX Flamingo: ett påhittat företag med data på varje sida i
verktyget och i byråns granskning.

    uv run python manage.py flamingo_demo           # lokalt, med DEBUG
    uv run python manage.py flamingo_demo --prod    # i produktion

Utan DEBUG vägrar kommandot om inte --prod anges, så att ingen kör det mot
en riktig databas av misstag.

Företaget "Exempelrör AB (demo)" finns inte. Hemsidan och e-posten ligger
under den reserverade toppdomänen .example, och telefonnumren kommer ur de
serier PTS reserverat för film och böcker (08-465 004 00 till 99 och
070-174 06 05 till 99), så ingen riktig person kan nås av misstag.

Kontot är ett demokonto (FlamingoAccount.is_demo):

- Landningssidorna (/lp/) är 404 för alla utom byrån, som förhandsvisar dem.
- Hemsidan läses aldrig av (scan.py) och inga sms skickas (sms.py).
- Inget anrop till Google (google_ads.ensure_not_demo), och konverteringarna
  exporteras eller laddas aldrig upp (manage_review.queued_uploads).

I produktion (--prod) skapas ingen användare som kan logga in: byrån tittar
med "Visa Flamingo som kunden" på kundkortet. Kontakter som kopplats till
demokunden kopplas bort (användarna rörs inte, utom demokontakten nedan som
stängs av). Lokalt finns kontakten DEMO_CONTACT, utan lösenord.

Idempotent: demokunden hittas på namnet OCH is_demo och uppdateras, och
innehållet (uppgifter, tjänster, kampanjer, granskningar, förfrågningar,
sms och Googles siffror) byggs om från grunden varje gång. Inget dubbleras.
En annan kund med samma namn, som inte är demokontot, rörs aldrig:
kommandot avbryts i stället.

Ingenting skickas: inga mejl, inga sms, inget till Google.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.projects.models import Customer

from ... import sms
from ...models import (
    Campaign,
    CampaignDayStats,
    Fact,
    FlamingoAccount,
    Lead,
    Review,
    Service,
    SmsLog,
)
from ...scan import PRICE_PREFIX

User = get_user_model()
STOCKHOLM = ZoneInfo("Europe/Stockholm")

CUSTOMER_NAME = "Exempelrör AB (demo)"
WEBSITE = "https://exempelror.example"
COMPANY_EMAIL = "info@exempelror.example"
#: Bara lokalt (DEBUG): en kontakt utan lösenord.
DEMO_CONTACT = "demo@exempelror.example"
#: PTS serier för fiktiva nummer: fast 08-465 004 00-99, mobil 070-174 06 05-99.
PHONE = "08-465 004 00"
OWNER_MOBILE = "070-174 06 05"
CUSTOMER_NOTES = (
    "Demokund för ADX Flamingo (flamingo_demo). Företaget är påhittat: mejla, "
    "ring eller fakturera aldrig. Sidorna under /lp/ syns bara för byrån."
)

#: Kundens Google Ads-konto: påhittat, tio nollor. Demokontot anropar aldrig
#: Google (google_ads.ensure_not_demo).
GOOGLE_ID = "000-000-0000"
GOOGLE_DIGITS = GOOGLE_ID.replace("-", "")

AUTOREPLY = (
    "Hej {namn}! Tack för din förfrågan till Exempelrör. Vi har tagit emot den "
    "och hör av oss. Brådskar det går det bra att ringa oss på 08-465 004 00."
)

#: Tjänsterna: namn, sätt att sälja, vald (en är bara ett förslag från läsningen).
SERVICES = [
    ("Rörjour", Service.SALES_CALL, True),
    ("Badrumsrenovering", Service.SALES_QUOTE, True),
    ("Byte av varmvattenberedare", Service.SALES_QUOTE, True),
    ("Avloppsspolning", Service.SALES_CALL, True),
    ("Filmning av avlopp", Service.SALES_BOOK, True),
    ("Golvvärme", Service.SALES_QUOTE, False),
]


def _price_key(name):
    return (PRICE_PREFIX + slugify(name))[:64].rstrip("-")


#: Uppgifterna: nyckel, rubrik, värde, källa, bekräftad, ordning (samma
#: ordning som scan.FACT_ORDER: kontakt först, priserna sist). Tre är
#: obekräftade, så att Företaget visar bekräfta, rätta och stryk, och ett
#: betyg från hemsidan visar varför det aldrig används.
FACTS = [
    ("telefon", "Telefon", PHONE, Fact.SOURCE_SITE, True, 10),
    ("adress", "Adress", "Exempelvägen 4, Nacka", Fact.SOURCE_GOOGLE, True, 20),
    ("oppettider", "Öppettider", "Vardagar 7-16", Fact.SOURCE_SITE, True, 30),
    ("epost", "E-post", COMPANY_EMAIL, Fact.SOURCE_SITE, True, 40),
    ("betyg", "Betyg på Google", "4,8 (37 omdömen)", Fact.SOURCE_GOOGLE, True, 50),
    ("jour", "Jour", "Dygnet runt, alla dagar", Fact.SOURCE_SITE, True, 60),
    ("grundat", "Grundat", "2009", Fact.SOURCE_ADX, True, 61),
    ("behorighet", "Behörighet", "Säker Vatten-auktoriserade", Fact.SOURCE_SITE, False, 62),
    ("omdomen-hemsidan", "Omdömen på hemsidan", "Trustpilot: 4,9", Fact.SOURCE_SITE, False, 63),
    ("omrade", "Område", "Nacka, Värmdö och Tyresö", Fact.SOURCE_CUSTOMER, True, 70),
    (
        _price_key("Rörjour"),
        "Pris, Rörjour",
        "Utryckning från 995 kr",
        Fact.SOURCE_CUSTOMER,
        True,
        90,
    ),
    (
        _price_key("Badrumsrenovering"),
        "Pris, Badrumsrenovering",
        "",
        Fact.SOURCE_CUSTOMER,
        False,
        91,
    ),
]

NEGATIVES = ["jobb", "lön", "utbildning", "gör det själv", "gratis", "praktik"]


def _kw(text, match="phrase"):
    return {"text": text, "match": match}


def _change(part, label, before, after, reason, field=None):
    """En ändring i en granskning, som manage_review sparar den."""
    return {
        "field": field or ("page" if part.startswith("page_") else part),
        "part": part,
        "label": label,
        "before": before,
        "after": after,
        "reason": reason,
    }


class Command(BaseCommand):
    help = (
        "Skapar eller uppdaterar demokunden för ADX Flamingo (ett påhittat företag). "
        "Lokalt med DEBUG, i produktion bara med --prod."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--prod",
            action="store_true",
            help=(
                "Kör i produktion. Ingen användare som kan logga in skapas; byrån "
                "använder Visa Flamingo som kunden på kundkortet."
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["prod"]:
            raise CommandError(
                "DEBUG är av. Kör med --prod för att skapa eller uppdatera demokunden "
                "här. Inget ändrades."
            )
        # Kontakten finns bara lokalt; --prod lokalt ger samma data som i drift.
        local = settings.DEBUG and not options["prod"]
        now = timezone.now()
        with transaction.atomic():
            customer, account = self._customer_and_account(now)
            contact = self._contact(customer, local)
            staff = User.objects.filter(is_staff=True, is_active=True).order_by("pk").first()
            self._reset(account)
            self._account_settings(account, staff, now)
            services = self._facts_and_services(account)
            campaigns = self._campaigns(account, services, contact, staff, now)
            self._day_stats(campaigns, now)
            self._leads(account, services, campaigns, now)
        self._print_urls(customer, campaigns, contact)

    # ------------------------------------------------------------------
    # Kunden, kontot och kontakten
    # ------------------------------------------------------------------

    def _customer_and_account(self, now):
        account = (
            FlamingoAccount.objects.select_related("customer")
            .filter(is_demo=True, customer__name=CUSTOMER_NAME)
            .order_by("pk")
            .first()
        )
        if account is None:
            if Customer.objects.filter(name=CUSTOMER_NAME).exists():
                raise CommandError(
                    f'Det finns redan en kund som heter "{CUSTOMER_NAME}" och som inte är '
                    "ett demokonto. Den rörs inte. Byt namn på den, eller markera dess "
                    "Flamingo-konto som demokonto i admin, och kör igen. Inget ändrades."
                )
            customer = Customer.objects.create(name=CUSTOMER_NAME)
            account = FlamingoAccount.objects.create(
                customer=customer, is_demo=True, enabled_at=now - timedelta(days=60)
            )
        customer = account.customer
        customer.website = WEBSITE
        customer.phone = PHONE
        # Ingen e-post på kunden: inget kundmejl kan nå demot (customer_recipients).
        customer.email = ""
        customer.org_number = ""
        customer.notes = CUSTOMER_NOTES
        customer.is_active = True
        customer.save()
        return customer, account

    def _contact(self, customer, local):
        """Lokalt: demokontakten, utan lösenord. I produktion: ingen alls."""
        if not local:
            # Ingen kontakt får kunna logga in på demokunden (portalens kod
            # per mejl räcker för en kontakt, lösenord eller inte).
            customer.users.clear()
            leftover = User.objects.filter(username=DEMO_CONTACT, is_staff=False).first()
            if leftover is not None:
                leftover.set_unusable_password()
                leftover.is_active = False
                leftover.save()
            return None
        contact, _ = User.objects.get_or_create(username=DEMO_CONTACT)
        contact.email = DEMO_CONTACT
        contact.first_name = "Johan"
        contact.last_name = "Exempel"
        contact.is_active = True
        contact.is_staff = False
        contact.is_superuser = False
        contact.set_unusable_password()
        contact.save()
        customer.users.add(contact)
        return contact

    def _reset(self, account):
        """Bygg om innehållet från grunden. Kampanjerna före tjänsterna: en
        tjänst är skyddad så länge en kampanj pekar på den. Granskningarna,
        Googles siffror och konverteringarna följer med sina rader."""
        account.sms_log.all().delete()
        account.leads.all().delete()
        account.campaigns.all().delete()
        account.services.all().delete()
        account.facts.all().delete()

    def _account_settings(self, account, staff, now):
        enabled_at = account.enabled_at or now - timedelta(days=60)
        account.is_demo = True
        account.is_enabled = True
        account.enabled_at = enabled_at
        account.enabled_by = account.enabled_by or staff

        account.website_url = WEBSITE
        account.scan_status = FlamingoAccount.SCAN_DONE
        account.scanned_at = enabled_at + timedelta(hours=2)
        account.scan_started_at = account.scanned_at
        account.scan_error = ""
        account.scan_day = None
        account.scan_count = 0
        account.ai_day = None
        account.ai_count = 0

        account.google_ads_customer_id = GOOGLE_ID
        account.google_status = FlamingoAccount.GOOGLE_BILLING_OK
        account.google_note = "Demokonto: Google-kontot och siffrorna är påhittade."
        account.google_billing_status = "APPROVED"
        account.google_auto_tagging = True
        account.google_synced_at = now - timedelta(hours=2)
        account.google_sync_error = ""
        account.google_link_requested_at = enabled_at + timedelta(days=1)
        base = f"customers/{GOOGLE_DIGITS}/conversionActions"
        account.google_conversion_actions = {
            "lead": f"{base}/1",
            "call": f"{base}/2",
            "deal": f"{base}/3",
        }

        account.notify_phone = OWNER_MOBILE
        account.notify_sms = True
        account.autoreply_enabled = True
        account.autoreply_text = AUTOREPLY
        account.save()

    def _facts_and_services(self, account):
        for key, label, value, source, confirmed, order in FACTS:
            Fact.objects.create(
                account=account,
                key=key,
                label=label,
                value=value,
                source=source,
                confirmed=confirmed,
                order=order,
            )
        services = {}
        for order, (name, mode, active) in enumerate(SERVICES):
            services[name] = Service.objects.create(
                account=account, name=name, sales_mode=mode, is_active=active, order=order
            )
        return services

    # ------------------------------------------------------------------
    # Kampanjerna, en i varje läge
    # ------------------------------------------------------------------

    def _google_resources(self, n):
        base = f"customers/{GOOGLE_DIGITS}"
        return {
            "budget": f"{base}/campaignBudgets/{n}",
            "campaign": f"{base}/campaigns/{n}",
            "ad_group": f"{base}/adGroups/{n}",
            "ad": f"{base}/adGroupAds/{n}~{n}",
            "criteria": [f"{base}/campaignCriteria/{n}~1", f"{base}/campaignCriteria/{n}~2"],
        }

    def _review(self, campaign, round_no, submitted, *, snapshot=None, **fields):
        return Review.objects.create(
            campaign=campaign,
            round=round_no,
            submitted_at=submitted,
            submitted_by=fields.pop("submitted_by", None),
            snapshot=snapshot if snapshot is not None else campaign.content_snapshot(),
            **fields,
        )

    def _campaigns(self, account, services, contact, staff, now):
        jour = services["Rörjour"]
        badrum = services["Badrumsrenovering"]
        vvb = services["Byte av varmvattenberedare"]
        spolning = services["Avloppsspolning"]
        filmning = services["Filmning av avlopp"]

        # -- Live --------------------------------------------------------
        live = Campaign.objects.create(
            account=account,
            service=jour,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            area="Nacka + 15 km",
            radius_km=15,
            daily_budget_kr=150,
            headlines=[
                "Rörjour i Nacka",
                "Ring Exempelrör",
                "Jour dygnet runt",
                "Vattenläcka? Ring oss",
                "Stopp i avloppet?",
                "Rörmokare i Nacka",
                "Utryckning från 995 kr",
                "Nacka, Värmdö och Tyresö",
            ],
            descriptions=[
                "Vattenläcka eller stopp? Jouren är öppen dygnet runt, alla dagar.",
                "Rörjour i Nacka, Värmdö och Tyresö. Ring oss direkt.",
                "Utryckning från 995 kr. Ring Exempelrör så hjälper vi dig.",
            ],
            keywords=[
                _kw("rörjour nacka"),
                _kw("akut rörjour"),
                _kw("vattenläcka nacka"),
                _kw("rörmokare nacka", "exact"),
                _kw("stopp i avlopp"),
            ],
            negatives=NEGATIVES + ["lediga jobb"],
            page={
                "title": "Rörjour i Nacka",
                "lead": "Vattenläcka eller stopp? Ring oss, jouren är öppen dygnet runt.",
                "points": [
                    "Jour dygnet runt, alla dagar",
                    "Nacka, Värmdö och Tyresö",
                    "Utryckning från 995 kr",
                ],
                "phone": PHONE,
                "form_title": "Hellre att vi ringer dig?",
                "questions": [],
                "note": "Stäng huvudkranen medan du väntar. Den sitter oftast vid vattenmätaren.",
            },
            google_campaign_id="9000000001",
            google_resources=self._google_resources(9000000001),
            google_synced_at=now - timedelta(hours=2),
            approved_at=now - timedelta(days=30),
            approved_by=contact,
            published_at=now - timedelta(days=29),
            created_by=contact,
            created_at=now - timedelta(days=35),
        )
        self._review(
            live,
            1,
            now - timedelta(days=33),
            snapshot={
                **live.content_snapshot(),
                "headlines": ["Exempelrör rörjour"] + live.headlines[:1] + live.headlines[2:],
                "negatives": NEGATIVES,
            },
            submitted_by=contact,
            state=Review.STATE_DONE,
            reviewer=staff,
            reviewed_at=now - timedelta(days=32),
            changes=[
                _change(
                    "headlines",
                    "Rubrik",
                    "Exempelrör rörjour",
                    "Ring Exempelrör",
                    "Kortare, och den säger vad den som söker ska göra.",
                ),
                _change(
                    "negatives",
                    "Negativt sökord",
                    "",
                    "lediga jobb",
                    "De som söker lediga jobb som rörmokare ringer inte efter jour.",
                ),
            ],
            note="Två små ändringar. Resten ser bra ut.",
        )

        # -- Pausad ------------------------------------------------------
        paused = Campaign.objects.create(
            account=account,
            service=spolning,
            name="Avloppsspolning Nacka",
            status=Campaign.STATUS_PAUSED,
            area="Nacka + 10 km",
            radius_km=10,
            daily_budget_kr=100,
            headlines=["Avloppsspolning i Nacka", "Stopp i avloppet?", "Ring Exempelrör"],
            descriptions=[
                "Stopp i avloppet? Vi spolar rent i Nacka, Värmdö och Tyresö.",
                "Ring Exempelrör, jouren är öppen dygnet runt.",
            ],
            keywords=[_kw("avloppsspolning nacka"), _kw("spola avlopp"), _kw("stopp i avlopp")],
            negatives=NEGATIVES,
            page={
                "title": "Avloppsspolning i Nacka",
                "lead": "Stopp i avloppet? Ring oss, så spolar vi rent.",
                "points": ["Nacka, Värmdö och Tyresö", "Jour dygnet runt, alla dagar"],
                "phone": PHONE,
                "form_title": "Hellre att vi ringer dig?",
                "questions": [],
                "note": "",
            },
            google_campaign_id="9000000002",
            google_resources=self._google_resources(9000000002),
            google_synced_at=now - timedelta(hours=2),
            approved_at=now - timedelta(days=46),
            approved_by=contact,
            published_at=now - timedelta(days=45),
            created_by=contact,
            created_at=now - timedelta(days=50),
        )
        self._review(
            paused,
            1,
            now - timedelta(days=49),
            submitted_by=contact,
            state=Review.STATE_DONE,
            reviewer=staff,
            reviewed_at=now - timedelta(days=48),
            changes=[],
            note="Inget att ändra.",
        )

        # -- Godkänd av kunden, inte publicerad --------------------------
        approved = Campaign.objects.create(
            account=account,
            service=vvb,
            name="Byte av varmvattenberedare",
            status=Campaign.STATUS_NEEDS_CUSTOMER,
            area="Nacka + 15 km",
            radius_km=15,
            daily_budget_kr=120,
            headlines=[
                "Ny varmvattenberedare",
                "Byte av varmvattenberedare",
                "Begär offert i dag",
            ],
            descriptions=[
                "Dags att byta varmvattenberedare? Berätta om din så får du en offert.",
                "Byte av varmvattenberedare i Nacka, Värmdö och Tyresö.",
            ],
            keywords=[_kw("byta varmvattenberedare"), _kw("ny varmvattenberedare")],
            negatives=NEGATIVES + ["begagnad"],
            page={
                "title": "Byte av varmvattenberedare i Nacka",
                "lead": "Berätta om din varmvattenberedare så får du en offert.",
                "points": ["Nacka, Värmdö och Tyresö"],
                "phone": PHONE,
                "form_title": "Berätta om jobbet",
                "questions": [
                    {"key": "storlek", "label": "Hur många liter rymmer den?", "kind": "text"},
                ],
                "note": "",
            },
            approved_at=now - timedelta(hours=22),
            approved_by=contact,
            created_by=contact,
            created_at=now - timedelta(days=5),
        )
        # Skickad utan granskning (granskningen är kundens val): inskicket var
        # kundens godkännande och ingen runda finns. Demot publiceras aldrig
        # hos Google, så den väntar i byråns kö "Godkända, ej publicerade".

        # -- Väntar på kunden (granskad, inte godkänd) --------------------
        needs = Campaign.objects.create(
            account=account,
            service=badrum,
            name="Badrumsrenovering",
            status=Campaign.STATUS_NEEDS_CUSTOMER,
            area="Nacka + 15 km",
            radius_km=15,
            daily_budget_kr=200,
            headlines=["Badrumsrenovering i Nacka", "Nytt badrum i Nacka", "Begär offert i dag"],
            descriptions=[
                "Berätta om ditt badrum så återkommer vi med en offert.",
                "Badrumsrenovering i Nacka, Värmdö och Tyresö.",
            ],
            keywords=[_kw("badrumsrenovering nacka"), _kw("renovera badrum")],
            negatives=NEGATIVES + ["badrumsmatta"],
            page={
                "title": "Badrumsrenovering i Nacka",
                "lead": "Berätta om ditt badrum så återkommer vi med en offert.",
                "points": ["Nacka, Värmdö och Tyresö"],
                "phone": PHONE,
                "form_title": "Berätta om ditt badrum",
                "questions": [
                    {"key": "storlek", "label": "Ungefär hur stort är badrummet?", "kind": "text"},
                    {"key": "jobbet", "label": "Vad vill du göra?", "kind": "textarea"},
                ],
                "note": "",
            },
            created_by=contact,
            created_at=now - timedelta(days=4),
        )
        before_lead = "Vi renoverar ditt badrum snabbt och billigt."
        self._review(
            needs,
            1,
            now - timedelta(days=3),
            snapshot={
                **needs.content_snapshot(),
                "headlines": ["Badrum Nacka dygnet runt"] + needs.headlines[1:],
                "negatives": NEGATIVES,
                "page": {**needs.page, "lead": before_lead},
            },
            submitted_by=contact,
            state=Review.STATE_DONE,
            reviewer=staff,
            reviewed_at=now - timedelta(hours=26),
            changes=[
                _change(
                    "headlines",
                    "Rubrik",
                    "Badrum Nacka dygnet runt",
                    "Badrumsrenovering i Nacka",
                    "Jouren gäller rörjour, inte renoveringar.",
                ),
                _change(
                    "negatives",
                    "Negativt sökord",
                    "",
                    "badrumsmatta",
                    "De som söker på badrumsmattor vill inte renovera.",
                ),
                _change(
                    "page_lead",
                    "Sidans ingress",
                    before_lead,
                    needs.page["lead"],
                    "Inga påståenden om pris eller tid som inte finns bland uppgifterna.",
                ),
            ],
            note="Tre ändringar. Godkänn om de ser rätt ut.",
        )

        # -- Hos ADX, andra rundan ---------------------------------------
        in_review = Campaign.objects.create(
            account=account,
            service=jour,
            name="Rörjour Värmdö",
            status=Campaign.STATUS_IN_REVIEW,
            area="Värmdö + 10 km",
            radius_km=10,
            daily_budget_kr=100,
            headlines=["Rörjour på Värmdö", "Ring Exempelrör", "Jour dygnet runt"],
            descriptions=[
                "Vattenläcka eller stopp på Värmdö? Jouren är öppen dygnet runt.",
                "Rörjour på Värmdö, i Nacka och Tyresö. Ring oss direkt.",
            ],
            keywords=[_kw("rörjour värmdö"), _kw("rörmokare värmdö")],
            negatives=NEGATIVES,
            page={
                "title": "Rörjour på Värmdö",
                "lead": "Vattenläcka eller stopp? Ring oss, jouren är öppen dygnet runt.",
                "points": ["Jour dygnet runt, alla dagar"],
                "phone": PHONE,
                "form_title": "Hellre att vi ringer dig?",
                "questions": [],
                "note": "",
            },
            created_by=contact,
            created_at=now - timedelta(days=6),
        )
        promise = "Rörjour på Värmdö. Vi kommer direkt."
        self._review(
            in_review,
            1,
            now - timedelta(days=5),
            snapshot={
                **in_review.content_snapshot(),
                "descriptions": [in_review.descriptions[0], promise],
                "keywords": [_kw("rörjour värmdö")],
            },
            submitted_by=contact,
            state=Review.STATE_DONE,
            reviewer=staff,
            reviewed_at=now - timedelta(days=4),
            changes=[
                _change(
                    "descriptions",
                    "Beskrivning",
                    promise,
                    "Rörjour på Värmdö, i Nacka och Tyresö. Ring oss direkt.",
                    "Vi kommer direkt är ett löfte om tid som vi inte kan hålla åt er.",
                ),
            ],
            note="En ändring.",
        )
        # Kunden lade till ett sökord efter granskningen och skickade in igen.
        self._review(
            in_review,
            2,
            now - timedelta(hours=3),
            submitted_by=contact,
            state=Review.STATE_PENDING,
        )

        # -- Utkast ------------------------------------------------------
        draft = Campaign.objects.create(
            account=account,
            service=filmning,
            name="Filmning av avlopp",
            status=Campaign.STATUS_DRAFT,
            area="Nacka + 10 km",
            radius_km=10,
            daily_budget_kr=80,
            # Två rubriker: kontrollerna visar att det behövs minst tre.
            headlines=["Filmning av avlopp", "Boka filmning i Nacka"],
            descriptions=[
                "Vi filmar avloppet och visar var stoppet sitter. Boka en tid.",
                "Filmning av avlopp i Nacka, Värmdö och Tyresö.",
            ],
            keywords=[_kw("filma avlopp"), _kw("filmning av avlopp")],
            negatives=NEGATIVES,
            page={
                "title": "Filmning av avlopp i Nacka",
                "lead": "Boka en tid så filmar vi avloppet och visar var stoppet sitter.",
                "points": ["Nacka, Värmdö och Tyresö"],
                "phone": PHONE,
                "form_title": "Boka en tid",
                "questions": [{"key": "datum", "label": "Önskat datum", "kind": "date"}],
                "note": "",
            },
            created_by=contact,
            created_at=now - timedelta(hours=20),
        )
        # De med en granskningsrunda: kunden bad om den (Campaign.review_requested,
        # granskningen är kundens val). Utkastet och den som skickades utan
        # granskning (approved) har ingen.
        Campaign.objects.filter(account=account, reviews__isnull=False).update(
            review_requested=True
        )
        for campaign in (live, paused, needs, in_review):
            campaign.review_requested = True
        approved.review_requested = False
        return {
            "live": live,
            "paused": paused,
            "approved": approved,
            "needs_customer": needs,
            "in_review": in_review,
            "draft": draft,
        }

    def _day_stats(self, campaigns, now):
        """Googles siffror per dag för de publicerade kampanjerna: påhittade
        men rimliga, och desamma varje körning (ingen slump)."""
        today = timezone.localtime(now, STOCKHOLM).date()
        spans = [
            # kampanj, första dagen, sista dagen (pausen)
            (campaigns["live"], 29, 1),
            (campaigns["paused"], 45, 7),
        ]
        rows = []
        for campaign, first, last in spans:
            budget = campaign.daily_budget_kr
            for i, days_ago in enumerate(range(first, last - 1, -1)):
                weekend = (today - timedelta(days=days_ago)).weekday() >= 5
                impressions = 95 + (i * 37) % 70 - (25 if weekend else 0)
                clicks = 5 + (i * 5) % 8 - (2 if weekend else 0)
                cost_kr = min(budget, round(budget * 0.55) + (i * 13) % round(budget * 0.45))
                rows.append(
                    CampaignDayStats(
                        campaign=campaign,
                        date=today - timedelta(days=days_ago),
                        cost_micros=cost_kr * 1_000_000 + ((i * 7919) % 100) * 10_000,
                        impressions=impressions,
                        clicks=clicks,
                        conversions=1.0 if i % 3 == 0 else 0.0,
                    )
                )
        CampaignDayStats.objects.bulk_create(rows)

    # ------------------------------------------------------------------
    # Förfrågningarna och sms:en
    # ------------------------------------------------------------------

    def _leads(self, account, services, campaigns, now):
        live = campaigns["live"]
        paused = campaigns["paused"]
        jour = services["Rörjour"]
        badrum = services["Badrumsrenovering"]
        vvb = services["Byte av varmvattenberedare"]
        spolning = services["Avloppsspolning"]
        utm = {"utm_source": "google", "utm_medium": "cpc", "utm_campaign": "rorjour-nacka"}

        def lead(when, **fields):
            fields.setdefault("source", Lead.SOURCE_FORM)
            created = now - when if isinstance(when, timedelta) else when
            return Lead.objects.create(account=account, created_at=created, **fields)

        sara = lead(
            timedelta(hours=5),
            campaign=live,
            service=jour,
            name="Sara Holm",
            phone="070-174 06 10",
            message="Det läcker under diskbänken. Vattnet är avstängt.",
            gclid="demo-gclid-sara",
            keyword="rörjour nacka",
            utm=utm,
        )
        self._sms_sent(account, sara)

        lead(
            timedelta(hours=26),
            campaign=live,
            service=jour,
            source=Lead.SOURCE_CALL_CLICK,
            gclid="demo-gclid-klick",
            keyword="akut rörjour",
            utm=utm,
        )

        maria = lead(
            timedelta(days=2),
            campaign=live,
            service=jour,
            name="Maria Nilsson",
            phone="070-174 06 11",
            email="maria.nilsson@example.com",
            message="Stopp i avloppet i källaren.",
            gbraid="demo-gbraid-maria",
            keyword="stopp i avlopp",
            utm=utm,
            status=Lead.STATUS_CONTACTED,
        )
        self._sms_sent(account, maria)

        junk = lead(
            timedelta(days=3),
            campaign=live,
            service=jour,
            name="Webbyrå Exempel",
            email="info@example.com",
            message="Vill ni synas bättre på Google? Vi erbjuder sökmotoroptimering.",
            status=Lead.STATUS_JUNK,
        )
        self._sms_owner_sent(account, junk)
        self._sms_row(
            account,
            junk,
            SmsLog.KIND_AUTOREPLY,
            "",
            sms.autoreply_text(account, junk),
            SmsLog.STATUS_DISABLED,
            sms.NOTE_NO_MOBILE,
        )

        lead(
            timedelta(days=4),
            service=vvb,
            source=Lead.SOURCE_MANUAL,
            name="Per Lund",
            phone="070-174 06 15",
            message="Vill ha pris på en ny beredare.",
            status=Lead.STATUS_LOST,
        )
        lead(
            timedelta(days=6),
            service=badrum,
            source=Lead.SOURCE_MANUAL,
            name="Johan Berg",
            phone="070-174 06 12",
            message="Vill byta allt inklusive golvbrunn.",
            answers={"Ungefär hur stort är badrummet?": "6 kvadratmeter"},
            status=Lead.STATUS_QUOTE,
        )
        lead(
            timedelta(days=9),
            campaign=live,
            service=jour,
            source=Lead.SOURCE_CALL,
            phone="08-465 004 10",
            message="Samtal, 2 minuter. Droppande kran i köket.",
            status=Lead.STATUS_CONTACTED,
        )

        # En förfrågan sent på kvällen: autosvaret väntar inte till morgonen,
        # det skickas inte alls (tyst tid 21-07).
        evening = datetime.combine(
            timezone.localtime(now, STOCKHOLM).date() - timedelta(days=10),
            time(23, 10),
            tzinfo=STOCKHOLM,
        )
        ali = lead(
            evening,
            campaign=paused,
            service=spolning,
            name="Ali Karimi",
            phone="070-174 06 16",
            message="Stopp i köksavloppet, vattnet står kvar.",
            gclid="demo-gclid-ali",
            keyword="stopp i avlopp",
            status=Lead.STATUS_CONTACTED,
        )
        self._sms_owner_sent(account, ali)
        self._sms_row(
            account,
            ali,
            SmsLog.KIND_AUTOREPLY,
            sms.normalize_phone(ali.phone),
            sms.autoreply_text(account, ali),
            SmsLog.STATUS_DISABLED,
            sms.NOTE_QUIET,
        )

        # Vunnen med klick-id: affären står i kö till Google. Demokontots
        # konverteringar exporteras och laddas aldrig upp.
        erik = lead(
            timedelta(days=12),
            campaign=live,
            service=jour,
            name="Erik Svensson",
            phone="070-174 06 13",
            message="Läckande blandare i badrummet.",
            gclid="demo-gclid-erik",
            keyword="akut rörjour",
            utm=utm,
        )
        self._sms_sent(account, erik)
        erik.set_status(Lead.STATUS_WON, value_kr=4800, now=now - timedelta(days=10))

        # Vunnen utan klick-id (kunden lade in den själv): ingen konvertering.
        lena = lead(
            timedelta(days=25),
            service=badrum,
            source=Lead.SOURCE_MANUAL,
            name="Lena Ek",
            phone="070-174 06 14",
            message="Badrum på 5 kvadratmeter, helrenovering.",
        )
        lena.set_status(Lead.STATUS_WON, value_kr=168000, now=now - timedelta(days=15))

    def _sms_row(self, account, lead, kind, to, body, status, error=""):
        return SmsLog.objects.create(
            account=account,
            lead=lead,
            kind=kind,
            to=(to or "")[:20],
            body=body,
            status=status,
            error=error,
            created_at=lead.created_at,
        )

    def _sms_owner_sent(self, account, lead):
        """Raden som sms:et till ägaren hade gett. Inget skickas härifrån."""
        self._sms_row(
            account,
            lead,
            SmsLog.KIND_OWNER,
            sms.normalize_phone(account.notify_phone),
            sms.owner_text(lead),
            SmsLog.STATUS_SENT,
        )

    def _sms_sent(self, account, lead):
        """Ägarens sms och autosvaret, som raderna ser ut när de gått iväg."""
        self._sms_owner_sent(account, lead)
        self._sms_row(
            account,
            lead,
            SmsLog.KIND_AUTOREPLY,
            sms.normalize_phone(lead.phone),
            sms.autoreply_text(account, lead),
            SmsLog.STATUS_SENT,
        )

    # ------------------------------------------------------------------

    def _print_urls(self, customer, campaigns, contact):
        w = self.stdout.write
        w(self.style.SUCCESS(f"Demodata klar: {customer.name} (kund {customer.pk}, demokonto)."))
        w("")
        w("Som kunden: logga in som byrån, sedan Visa Flamingo som kunden på kundkortet:")
        w(f"  /manage/kunder/{customer.pk}/#flamingo")
        w("Verktyget:")
        w("  /flamingo/app/")
        w("  /flamingo/app/inkorg/")
        w("  /flamingo/app/kampanjer/")
        for state, campaign in campaigns.items():
            w(f"  /flamingo/app/kampanjer/{campaign.pk}/   ({state})")
        w("Landningssidan (bara byrån ser den, alla andra får 404):")
        w(f"  {campaigns['live'].landing_url}")
        w("Byråns granskning:")
        w("  /manage/flamingo/granska/")
        w(f"  /manage/flamingo/granska/{campaigns['in_review'].pk}/")
        w("")
        if contact is None:
            w("Ingen kontakt: ingen kan logga in som demokunden.")
        else:
            w(f"Kontakten {contact.get_username()} har inget lösenord.")
        w("Inget har mejlats, sms:ats eller skickats till Google.")
