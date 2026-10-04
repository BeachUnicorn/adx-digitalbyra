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

Landningssidorna byggs i sidbyggaren (pagebuilder/): en sida per kampanj,
utom Rörjour som delas av två kampanjer, och tillsammans har sidorna varje
blocktyp och variant. Bilderna i mediaarkivet ritas här med Pillow och är
tydligt påhittade ("Exempelbild"). Google-profilen och omdömena är också
påhittade och hämtas aldrig från Google. Profilen på Reco är påhittad (id:t
0000000, ingen länk till reco.se, inga omdömen): blocket Omdömen från Reco
ritar en exempelruta i stället för Recos ruta och i stället för utvalda
omdömen, och Reco anropas aldrig.

Idempotent: demokunden hittas på namnet OCH is_demo och uppdateras, och
innehållet (uppgifter, tjänster, kampanjer, sidor, bilder, granskningar,
förfrågningar, sms och Googles siffror) byggs om från grunden varje gång.
Inget dubbleras.
En annan kund med samma namn, som inte är demokontot, rörs aldrig:
kommandot avbryts i stället.

Ingenting skickas: inga mejl, inga sms, inget till Google eller Reco.
"""

import io
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from apps.projects.models import Customer

from ... import pagebuilder, sms
from ...models import (
    MEDIA_FORMAT,
    MEDIA_THUMB_SIDE,
    Campaign,
    CampaignDayStats,
    Fact,
    FlamingoAccount,
    LandingPage,
    Lead,
    MediaAsset,
    Review,
    Service,
    SmsLog,
)
from ...pagebuilder import BuildContext
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
#: Demots påhittade id på Reco (reco.clean_venue_id tar aldrig emot det).
RECO_ID = "0000000"

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
    ("forsakring", "Försäkring", "Ansvarsförsäkring", Fact.SOURCE_CUSTOMER, True, 63),
    ("f-skatt", "F-skatt", "Godkänd för F-skatt", Fact.SOURCE_ADX, True, 64),
    ("garanti", "Garanti", "Två års garanti på arbetet", Fact.SOURCE_CUSTOMER, True, 65),
    ("kontaktperson", "Kontaktperson", "Kim Exempel", Fact.SOURCE_CUSTOMER, True, 66),
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
    (
        _price_key("Byte av varmvattenberedare"),
        "Pris, Byte av varmvattenberedare",
        "Från 12 900 kr med montering",
        Fact.SOURCE_CUSTOMER,
        True,
        92,
    ),
    (
        _price_key("Filmning av avlopp"),
        "Pris, Filmning av avlopp",
        "Filmning från 1 900 kr",
        Fact.SOURCE_CUSTOMER,
        True,
        93,
    ),
]

#: Google-profilen och omdömena: påhittade, som allt annat i demot. De
#: hämtas aldrig från Google, och profilen har ingen länk till Google Maps.
GOOGLE_REVIEWS = [
    (
        "Anna E.",
        5,
        "Läckan under diskbänken var lagad på en kvart, och vi fick veta vad som hade hänt. "
        "Trevligt och tydligt bemötande.",
        "för 2 veckor sedan",
    ),
    (
        "Johan E.",
        5,
        "Noggranna och städade efter sig. Bra pris på utryckningen.",
        "för en månad sedan",
    ),
    (
        "Maria E.",
        4,
        "Bra jobb med varmvattenberedaren. Fick vänta på en reservdel.",
        "för 2 månader sedan",
    ),
    ("Erik E.", 5, "Snabb hjälp med stoppet i avloppet en söndag.", "för 3 månader sedan"),
]

NEGATIVES = ["jobb", "lön", "utbildning", "gör det själv", "gratis", "praktik"]


def _kw(text, match="phrase"):
    return {"text": text, "match": match}


def _change(part, label, before, after, reason, field=None):
    """En ändring i en granskning, som manage_review sparar den."""
    return {
        "field": field or part,
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
            media = self._media(account, contact, now)
            self._pages(account, campaigns, media, contact, staff, now)
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
        # Sidorna efter kampanjerna (en sida som en kampanj visar kan inte
        # tas bort), bilderna sist; filerna tas bort när raden är borta.
        account.landing_pages.all().delete()
        account.site_images.all().delete()
        account.media.all().delete()
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

        account.google_place_id = "demo-exempelror"
        account.google_place_name = "Exempelrör (demo)"
        account.google_maps_uri = ""
        account.google_rating = Decimal("4.8")
        account.google_review_count = 37
        account.google_reviews = [
            {
                "id": f"places/demo-exempelror/reviews/{n}",
                "author": author,
                "author_uri": "",
                "rating": rating,
                "text": text,
                "time": (enabled_at + timedelta(days=n)).isoformat(),
                "relative": relative,
            }
            for n, (author, rating, text, relative) in enumerate(GOOGLE_REVIEWS, start=1)
        ]
        account.google_reviews_selected = [r["id"] for r in account.google_reviews[:3]]
        account.google_reviews_fetched_at = now - timedelta(days=1)
        # Den påhittade profilen är demots egen (reviews.store_details prövar
        # riktiga profiler mot kunden; demot hämtar aldrig från Google).
        account.google_place_unverified = False

        # En påhittad profil på Reco: id:t är bara nollor, som Google Ads-id:t,
        # och klarar inte reco.VENUE_ID_RE, så ingen iframe kan byggas av det.
        # Ingen länk till reco.se. Blocket ritar en exempelruta i demot
        # (render._reco_demo), och Reco anropas aldrig (reco.refusal).
        account.reco_venue_id = RECO_ID
        account.reco_url = ""
        account.reco_name = "Exempelrör (demo)"
        account.reco_rating = Decimal("4.7")
        account.reco_review_count = 23
        account.reco_fetched_at = now - timedelta(days=1)
        account.reco_unverified = False
        account.reco_confirmed_at = None
        account.reco_confirmed_by = None
        # Inga omdömen från Reco i demot, inte ens påhittade (reco.py).
        account.reco_reviews = []
        account.reco_reviews_selected = []

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
            created_by=contact,
            created_at=now - timedelta(days=4),
        )
        self._review(
            needs,
            1,
            now - timedelta(days=3),
            snapshot={
                **needs.content_snapshot(),
                "headlines": ["Badrum Nacka dygnet runt"] + needs.headlines[1:],
                "negatives": NEGATIVES,
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
            ],
            note="Två ändringar. Godkänn om de ser rätt ut.",
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

    # ------------------------------------------------------------------
    # Mediaarkivet: påhittade bilder, ritade här
    # ------------------------------------------------------------------

    def _font(self, size, weight=500):
        """Figtree (sidornas typsnitt, static/fonts/), eller Pillows eget om
        filen inte går att läsa."""
        path = settings.BASE_DIR / "static" / "fonts" / "files" / "figtree-latin.woff2"
        try:
            font = ImageFont.truetype(str(path), size)
            font.set_variation_by_axes([weight])
        except (OSError, ValueError):
            return ImageFont.load_default(size=size)
        return font

    def _picture(self, size, top, bottom, label, place="left", scene=""):
        """En tydligt påhittad bild: en färgtoning med mjukt ljus, ett enkelt
        motiv (scene: rör, kakel, beredare, avlopp eller en person) och
        texten "Exempelbild: ..." i ett litet märke nere till vänster eller
        höger (place), så att reglaget i Före och efter inte delar den."""
        width, height = size
        image = Image.new("RGB", size, top)
        draw = ImageDraw.Draw(image)
        for y in range(height):
            t = y / max(1, height - 1)
            color = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
            draw.line([(0, y), (width, y)], fill=color)
        # Mjukt ljus från ett fönster uppe till höger.
        glow = Image.new("L", size, 0)
        ImageDraw.Draw(glow).ellipse(
            [width * 0.45, -height * 0.55, width * 1.35, height * 0.75], fill=150
        )
        glow = glow.filter(ImageFilter.GaussianBlur(width // 9))
        image = Image.composite(Image.new("RGB", size, (255, 255, 255)), image, glow)
        motif = getattr(self, f"_scene_{scene}", None)
        if motif is not None:
            image = motif(image)
        # Lite brus, så att ytan inte ser platt ut.
        noise = Image.effect_noise(size, 18).convert("RGB")
        image = Image.blend(image, noise, 0.035)
        draw = ImageDraw.Draw(image)
        font = self._font(max(22, width // 46), 600)
        box = draw.textbbox((0, 0), label, font=font)
        text_w, text_h = box[2] - box[0], box[3] - box[1]
        pad_x, pad_y = round(text_h * 0.9), round(text_h * 0.6)
        # Märket står en bit in från kanten, så att det syns också när bilden
        # beskärs (Toppen i mobilen visar bilden i 16:9).
        margin_x, margin_y = round(width * 0.09), round(height * 0.15)
        top_y = height - margin_y - text_h - 2 * pad_y
        left = margin_x if place != "right" else width - margin_x - text_w - 2 * pad_x
        draw.rounded_rectangle(
            [left, top_y, left + text_w + 2 * pad_x, top_y + text_h + 2 * pad_y],
            radius=text_h + pad_y,
            fill=(255, 255, 255),
        )
        draw.text((left + pad_x, top_y + pad_y - box[1]), label, font=font, fill=(32, 33, 36))
        return image

    def _soft_shadow(self, image, shape, offset=(0, 18), blur=28, alpha=90):
        """En mjuk skugga under en form (shape: en lista med punkter eller en
        rektangel som ImageDraw tar)."""
        mask = Image.new("L", image.size, 0)
        draw = ImageDraw.Draw(mask)
        moved = [(x + offset[0], y + offset[1]) for x, y in shape]
        draw.rounded_rectangle([moved[0], moved[1]], radius=40, fill=alpha)
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
        return Image.composite(Image.new("RGB", image.size, (40, 44, 52)), image, mask)

    def _pipe(self, draw, points, width, color, light):
        """Ett rör: en tjock linje med rundade knän och en ljus rand."""
        dark = tuple(max(0, c - 46) for c in color)
        draw.line(points, fill=dark, width=width + 6, joint="curve")
        for x, y in (points[0], points[-1]):
            draw.ellipse(
                [x - width / 2 - 3, y - width / 2 - 3, x + width / 2 + 3, y + width / 2 + 3],
                fill=dark,
            )
        draw.line(points, fill=color, width=width, joint="curve")
        for x, y in (points[0], points[-1]):
            draw.ellipse([x - width / 2, y - width / 2, x + width / 2, y + width / 2], fill=color)
        shifted = [(x - width * 0.18, y - width * 0.18) for x, y in points]
        draw.line(shifted, fill=light, width=max(3, width // 5), joint="curve")

    def _scene_pipes(self, image):
        width, height = image.size
        draw = ImageDraw.Draw(image)
        step = width // 9
        for x in range(0, width, step):
            draw.line([(x, 0), (x, height * 0.74)], fill=(255, 255, 255), width=2)
        for y in range(0, round(height * 0.74), step // 2):
            draw.line([(0, y), (width, y)], fill=(255, 255, 255), width=2)
        draw.rectangle([0, height * 0.74, width, height], fill=(206, 196, 184))
        w = round(height * 0.075)
        copper, light = (196, 112, 64), (238, 176, 128)
        self._pipe(
            draw,
            [(-40, height * 0.32), (width * 0.42, height * 0.32), (width * 0.42, height * 0.88)],
            w,
            copper,
            light,
        )
        self._pipe(
            draw,
            [
                (width * 1.05, height * 0.48),
                (width * 0.62, height * 0.48),
                (width * 0.62, height * 0.88),
            ],
            w,
            copper,
            light,
        )
        silver, shine = (168, 176, 186), (226, 230, 236)
        self._pipe(
            draw,
            [(width * 0.2, -40), (width * 0.2, height * 0.58), (width * 0.86, height * 0.58)],
            round(w * 0.8),
            silver,
            shine,
        )
        for cx in (width * 0.42, width * 0.62):
            draw.rounded_rectangle(
                [cx - w * 0.9, height * 0.6, cx + w * 0.9, height * 0.66],
                radius=8,
                fill=(120, 124, 132),
            )
        return image

    def _scene_tiles(self, image):
        width, height = image.size
        draw = ImageDraw.Draw(image)
        step = width // 10
        for x in range(0, width + step, step):
            draw.line([(x, 0), (x, height * 0.7)], fill=(250, 250, 250), width=3)
        for y in range(0, round(height * 0.7), step):
            draw.line([(0, y), (width, y)], fill=(250, 250, 250), width=3)
        floor = tuple(max(0, c - 34) for c in image.getpixel((width // 2, height - 2)))
        draw.rectangle([0, height * 0.7, width, height], fill=floor)
        image = self._soft_shadow(
            image, [(width * 0.52, height * 0.42), (width * 0.9, height * 0.78)]
        )
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            [width * 0.52, height * 0.42, width * 0.9, height * 0.78],
            radius=48,
            fill=(252, 252, 250),
        )
        draw.rounded_rectangle(
            [width * 0.56, height * 0.47, width * 0.86, height * 0.72],
            radius=36,
            fill=(236, 240, 242),
        )
        draw.rounded_rectangle(
            [width * 0.12, height * 0.12, width * 0.36, height * 0.46],
            radius=20,
            fill=(232, 238, 242),
            outline=(200, 208, 214),
            width=6,
        )
        return image

    def _scene_heater(self, image):
        width, height = image.size
        box = [(width * 0.36, height * 0.1), (width * 0.64, height * 0.86)]
        image = self._soft_shadow(image, box, offset=(18, 22), blur=36, alpha=110)
        draw = ImageDraw.Draw(image)
        (x0, y0), (x1, y1) = box
        for i in range(round(x1 - x0)):
            t = i / max(1, x1 - x0)
            shade = round(236 + 18 * (1 - abs(t - 0.38) * 2.2))
            draw.line([(x0 + i, y0 + 40), (x0 + i, y1 - 40)], fill=(min(255, shade),) * 3)
        draw.rounded_rectangle([x0, y0, x1, y0 + 90], radius=46, fill=(246, 246, 246))
        draw.rounded_rectangle([x0, y1 - 90, x1, y1], radius=46, fill=(226, 226, 226))
        cx = (x0 + x1) / 2
        draw.ellipse(
            [cx - 46, y0 + 160, cx + 46, y0 + 252],
            fill=(255, 255, 255),
            outline=(190, 196, 204),
            width=6,
        )
        draw.line([(cx, y0 + 206), (cx + 26, y0 + 186)], fill=(200, 70, 50), width=6)
        self._pipe(
            draw, [(cx - 60, y1), (cx - 60, height + 40)], 36, (196, 112, 64), (238, 176, 128)
        )
        self._pipe(
            draw, [(cx + 60, y1), (cx + 60, height + 40)], 36, (168, 176, 186), (226, 230, 236)
        )
        return image

    def _scene_drain(self, image, clean=False):
        width, height = image.size
        draw = ImageDraw.Draw(image)
        cx, cy, r = width * 0.5, height * 0.52, height * 0.34
        draw.ellipse([cx - r - 26, cy - r - 26, cx + r + 26, cy + r + 26], fill=(200, 204, 208))
        inner = (70, 74, 80) if clean else (92, 78, 60)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=inner)
        for i in range(7):
            y = cy - r * 0.75 + i * r * 0.25
            half = (r * r - (y - cy) ** 2) ** 0.5 * 0.86
            draw.rounded_rectangle(
                [cx - half, y - 7, cx + half, y + 7], radius=7, fill=(214, 218, 222)
            )
        if not clean:
            for dx, dy, rr in ((-0.3, 0.2, 0.22), (0.25, -0.1, 0.16), (0.05, 0.35, 0.12)):
                draw.ellipse(
                    [
                        cx + dx * r - rr * r,
                        cy + dy * r - rr * r,
                        cx + dx * r + rr * r,
                        cy + dy * r + rr * r,
                    ],
                    fill=(128, 104, 72),
                )
        return image

    def _scene_drain_clean(self, image):
        return self._scene_drain(image, clean=True)

    def _scene_person(self, image):
        width, height = image.size
        draw = ImageDraw.Draw(image)
        cx = width * 0.5
        draw.ellipse(
            [cx - width * 0.4, height * 0.62, cx + width * 0.4, height * 1.35], fill=(64, 86, 112)
        )
        draw.rounded_rectangle(
            [cx - width * 0.08, height * 0.5, cx + width * 0.08, height * 0.66],
            radius=30,
            fill=(218, 178, 150),
        )
        draw.ellipse(
            [cx - width * 0.17, height * 0.2, cx + width * 0.17, height * 0.56],
            fill=(226, 186, 158),
        )
        draw.chord(
            [cx - width * 0.18, height * 0.16, cx + width * 0.18, height * 0.44],
            180,
            360,
            fill=(92, 70, 56),
        )
        return image

    def _logo(self):
        image = Image.new("RGBA", (720, 180), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([8, 30, 128, 150], radius=36, fill=(27, 102, 210, 255))
        draw.ellipse([44, 58, 92, 122], fill=(255, 255, 255, 255))
        font = self._font(88, 650)
        draw.text((156, 28), "Exempelrör", font=font, fill=(32, 33, 36, 255))
        return image

    def _asset(self, account, image, alt, contact, now, *, is_logo=False):
        """Ett MediaAsset ur en Pillow-bild: WebP, med miniatyr."""

        def webp(img):
            buffer = io.BytesIO()
            img.save(buffer, MEDIA_FORMAT, quality=82)
            return ContentFile(buffer.getvalue(), name="bild.webp")

        thumb = image.copy()
        thumb.thumbnail((MEDIA_THUMB_SIDE, MEDIA_THUMB_SIDE))
        asset = MediaAsset(
            account=account,
            alt=alt,
            is_logo=is_logo,
            source=MediaAsset.SOURCE_UPLOAD,
            rights_confirmed_at=now,
            rights_confirmed_by=contact,
        )
        asset.file.save("bild.webp", webp(image), save=False)
        asset.thumb.save("tumme.webp", webp(thumb), save=False)
        asset.save()
        return asset

    def _media(self, account, contact, now):
        pictures = {
            "jour": (
                (1800, 1350),
                (214, 226, 240),
                (176, 196, 220),
                "Exempelbild: rörjour",
                "left",
                "pipes",
            ),
            "vvb": (
                (1600, 1200),
                (220, 230, 226),
                (180, 204, 196),
                "Exempelbild: ny beredare",
                "left",
                "heater",
            ),
            "bad_fore": (
                (1600, 1000),
                (204, 200, 190),
                (160, 156, 148),
                "Exempelbild: före",
                "left",
                "tiles",
            ),
            "bad_efter": (
                (1600, 1000),
                (226, 236, 244),
                (189, 211, 230),
                "Exempelbild: efter",
                "right",
                "tiles",
            ),
            "rör_fore": (
                (1600, 1200),
                (196, 190, 180),
                (150, 144, 134),
                "Exempelbild: avlopp före",
                "left",
                "drain",
            ),
            "rör_efter": (
                (1600, 1200),
                (220, 232, 226),
                (180, 206, 192),
                "Exempelbild: avlopp efter",
                "left",
                "drain_clean",
            ),
            "person": (
                (1000, 1250),
                (238, 230, 220),
                (210, 196, 180),
                "Exempelbild: Kim",
                "left",
                "person",
            ),
        }
        alts = {
            "jour": "Påhittad exempelbild av rör under en diskbänk",
            "vvb": "Påhittad exempelbild av en ny varmvattenberedare",
            "bad_fore": "Påhittad exempelbild av ett badrum före renoveringen",
            "bad_efter": "Påhittad exempelbild av ett badrum efter renoveringen",
            "rör_fore": "Påhittad exempelbild av ett avlopp före spolningen",
            "rör_efter": "Påhittad exempelbild av ett avlopp efter spolningen",
            "person": "Påhittad exempelbild av Kim Exempel",
        }
        media = {
            key: self._asset(account, self._picture(*spec), alts[key], contact, now)
            for key, spec in pictures.items()
        }
        media["logo"] = self._asset(account, self._logo(), "Exempelrör", contact, now, is_logo=True)
        return media

    # ------------------------------------------------------------------
    # Landningssidorna (sidbyggaren)
    # ------------------------------------------------------------------

    def _block(self, account, kind, variant, ctx, **fields):
        """Ett block ur mallen, med fälten ändrade där demot vill visa mer."""
        block = pagebuilder.new_block(kind, variant, account, ctx=ctx)
        if fields:
            block["versions"][0]["fields"].update(fields)
        return block

    def _pages(self, account, campaigns, media, contact, staff, now):
        """En sida per kampanj, utom Rörjour som delas av Rörjour Nacka (live)
        och Rörjour Värmdö (hos ADX). Tillsammans har sidorna varje blocktyp
        och variant. Sidorna för kampanjerna som är live eller pausade är
        publicerade; de andra är utkast."""
        call = Service.SALES_CALL
        quote = Service.SALES_QUOTE
        book = Service.SALES_BOOK
        places = ["Nacka", "Värmdö", "Tyresö"]
        b = self._block

        jour_ctx = BuildContext(service="Rörjour", places=places, mode=call)
        # Toppen som i den godkända skissen: bilden först, tjänsten och orten
        # i överrubriken och en rubrik om vad kunden får.
        hero = b(
            account,
            "hero",
            "image",
            jour_ctx,
            kicker="Rörjour i Nacka",
            title="Läcker det? Ring oss, så kommer vi och lagar det.",
            lead="Rörmokare i Nacka, Värmdö och Tyresö. Jouren är öppen dygnet runt, alla dagar.",
            points=["Utryckning från 995 kr", "Jour dygnet runt, alla dagar"],
            image=media["jour"].pk,
        )
        # En andra version av rubriken: kunden kan byta mellan dem.
        pagebuilder.add_version(
            hero,
            dict(pagebuilder.active_fields(hero), title="Vattenläcka eller stopp? Ring jouren."),
            pagebuilder.SOURCE_CUSTOMER,
            contact,
            activate=False,
            now=now - timedelta(days=2),
        )
        jour = [
            hero,
            b(account, "certificates", "badges", jour_ctx),
            b(
                account,
                "price",
                "from",
                jour_ctx,
                text=(
                    "Priset för utryckningen. Resten bestämmer vi tillsammans innan jobbet börjar."
                ),
            ),
            b(account, "reviews_google", "cards", jour_ctx),
            b(account, "reviews_reco", "utvalda_kort", jour_ctx),
            b(account, "steps", "three", jour_ctx),
            b(account, "faq", "three", jour_ctx),
            b(
                account,
                "area",
                "map",
                jour_ctx,
                text="Jouren kommer till hela Nacka, Värmdö och Tyresö.",
            ),
            b(
                account,
                "form",
                "short",
                jour_ctx,
                note_title="Bra att veta",
                note="Stäng huvudkranen medan du väntar. Den sitter oftast vid vattenmätaren.",
            ),
            b(
                account,
                "callbar",
                "call_write",
                jour_ctx,
                title="Läcker det just nu? Ring jouren.",
            ),
        ]

        spol_ctx = BuildContext(service="Avloppsspolning", places=places[:1], mode=call)
        spolning = [
            b(
                account,
                "hero",
                "text",
                spol_ctx,
                kicker="Avloppsspolning i Nacka",
                title="Stopp i avloppet? Ring, så spolar vi rent.",
                lead="Kök, badrum eller hela fastigheten. Berätta var det står still.",
                points=[],
            ),
            b(account, "reviews_google", "line", spol_ctx),
            b(account, "steps", "four", spol_ctx),
            b(account, "reviews_reco", "liten", spol_ctx),
            b(
                account,
                "guarantee",
                "terms",
                spol_ctx,
                terms=["Gäller arbetet vi har gjort", "Säg till så tittar vi på det igen"],
            ),
            b(account, "faq", "six", spol_ctx),
            b(account, "callbar", "call", spol_ctx),
        ]

        vvb_ctx = BuildContext(service="Byte av varmvattenberedare", places=places[:1], mode=quote)
        vvb = [
            b(
                account,
                "hero",
                "image",
                vvb_ctx,
                kicker="Byte av varmvattenberedare i Nacka",
                title="Varmvatten igen, med en ny beredare.",
                lead="Berätta om din varmvattenberedare så får du en offert med montering.",
                image=media["vvb"].pk,
            ),
            b(account, "reviews_google", "cards", vvb_ctx),
            b(account, "price", "examples", vvb_ctx),
            b(account, "reviews_reco", "staende", vvb_ctx),
            b(
                account,
                "person",
                "image",
                vvb_ctx,
                role="Rörmokare och ägare",
                text=(
                    "Jag kommer själv och tittar på din beredare, och du får veta vad som "
                    "behöver göras innan vi börjar."
                ),
                image=media["person"].pk,
            ),
            b(
                account,
                "form",
                "questions",
                vvb_ctx,
                title="Berätta om jobbet",
                questions=[
                    {"key": "storlek", "label": "Hur många liter rymmer den?", "kind": "text"},
                ],
            ),
        ]

        bad_ctx = BuildContext(service="Badrumsrenovering", places=places[:1], mode=quote)
        badrum = [
            b(
                account,
                "hero",
                "form",
                bad_ctx,
                kicker="Badrumsrenovering i Nacka",
                title="Ett nytt badrum, från ritning till kakel.",
                lead="Berätta om ditt badrum så återkommer vi med en offert.",
                points=["Nacka, Värmdö och Tyresö", "Två års garanti på arbetet"],
            ),
            b(
                account,
                "form",
                "questions",
                bad_ctx,
                title="Berätta om ditt badrum",
                questions=[
                    {"key": "storlek", "label": "Ungefär hur stort är badrummet?", "kind": "text"},
                    {"key": "jobbet", "label": "Vad vill du göra?", "kind": "textarea"},
                ],
            ),
            b(
                account,
                "before_after",
                "slider",
                bad_ctx,
                caption="Ett badrum i Nacka, före och efter.",
                before=media["bad_fore"].pk,
                after=media["bad_efter"].pk,
            ),
            b(account, "reviews_google", "quote", bad_ctx),
            b(account, "reviews_reco", "medel", bad_ctx),
            b(account, "certificates", "icons", bad_ctx),
            b(account, "guarantee", "short", bad_ctx),
            b(
                account,
                "person",
                "noimage",
                bad_ctx,
                role="Ägare",
                text="Jag går igenom badrummet med dig innan vi bestämmer något.",
            ),
            b(
                account,
                "area",
                "list",
                bad_ctx,
                title="Vi jobbar i Nacka, Värmdö och Tyresö",
                places=places,
            ),
            b(account, "callbar", "call_write", bad_ctx, title="Hellre att prata om badrummet?"),
        ]

        film_ctx = BuildContext(service="Filmning av avlopp", places=places[:1], mode=book)
        filmning = [
            b(
                account,
                "hero",
                "form",
                film_ctx,
                kicker="Filmning av avlopp i Nacka",
                title="Se var stoppet sitter, innan någon gräver.",
                lead="Boka en tid så filmar vi avloppet och visar var stoppet sitter.",
            ),
            b(
                account,
                "form",
                "booking",
                film_ctx,
                title="Boka en tid",
                questions=[
                    {"key": "datum", "label": "Önskat datum", "kind": "date"},
                    {"key": "tid", "label": "Förmiddag eller eftermiddag?", "kind": "text"},
                ],
            ),
            b(account, "price", "fixed", film_ctx, price="Filmning från 1 900 kr"),
            b(
                account,
                "before_after",
                "pair",
                film_ctx,
                before=media["rör_fore"].pk,
                after=media["rör_efter"].pk,
            ),
            b(account, "reviews_reco", "stor", film_ctx),
            b(account, "steps", "three", film_ctx),
            b(account, "callbar", "call_write", film_ctx, title="Frågor om filmningen? Ring oss."),
        ]

        specs = [
            (
                "Rörjour",
                jour,
                [campaigns["live"], campaigns["in_review"]],
                LandingPage.PALETTE_BLUE,
            ),
            (
                "Avloppsspolning Nacka",
                spolning,
                [campaigns["paused"]],
                LandingPage.PALETTE_GRAPHITE,
            ),
            ("Byte av varmvattenberedare", vvb, [campaigns["approved"]], LandingPage.PALETTE_GREEN),
            ("Badrumsrenovering", badrum, [campaigns["needs_customer"]], LandingPage.PALETTE_RED),
            ("Filmning av avlopp", filmning, [campaigns["draft"]], LandingPage.PALETTE_ORANGE),
        ]
        pages = {}
        for name, blocks, users, palette in specs:
            blocks = pagebuilder.validate_blocks(blocks, account=account)
            published = any(
                c.status in (Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED) for c in users
            )
            page = LandingPage.objects.create(
                account=account,
                name=name,
                palette=palette,
                draft={"blocks": blocks},
                published={"blocks": pagebuilder.blocks.copy_blocks(blocks)}
                if published
                else {"blocks": []},
                published_at=min(c.published_at for c in users if c.published_at)
                if published
                else None,
                published_by=staff if published else None,
                created_by=contact,
                created_at=min(c.created_at for c in users),
            )
            Campaign.objects.filter(pk__in=[c.pk for c in users]).update(landing_page=page)
            for campaign in users:
                campaign.landing_page = page
            pages[name] = page
        return pages

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
        w("Sidorna i sidbyggaren:")
        w("  /flamingo/app/sidor/")
        w("Landningssidorna (bara byrån ser dem, alla andra får 404):")
        for state, campaign in campaigns.items():
            w(f"  {campaign.landing_url}   ({state})")
        w("Byråns granskning:")
        w("  /manage/flamingo/granska/")
        w(f"  /manage/flamingo/granska/{campaigns['in_review'].pk}/")
        w("")
        if contact is None:
            w("Ingen kontakt: ingen kan logga in som demokunden.")
        else:
            w(f"Kontakten {contact.get_username()} har inget lösenord.")
        w("Inget har mejlats, sms:ats eller skickats till Google.")
