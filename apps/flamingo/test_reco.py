"""ADX Flamingo: omdömena från Reco (reco.py, Recos del av
app_views/reviews.py och blocket "Omdömen från Reco"): länken och id:t,
profilsidan, ägaren, gränserna, demot, blocket i varje variant, 90 dagar och
Konverteringskollen. Sist Utvalda (Giovannis beslut 2026-10-04): omdömena
ur profilsidan, valet och ordningen, blocket, brytaren (inställningen och
byråns knapp), cronens takt och att inget syns för en profil som inte är
intygad, är bortkopplad eller hör till demot.

Reco anropas aldrig: reco.fetch är utbytt mot FakeReco i varje test som kan
nå Reco, och analyzer._request (själva anslutningen) fäller testet om något
ändå försöker. Profilsidan och widgeten är testdata i testdata/, byggda ur
CS Auto AB:s riktiga sidor (hämtade 2026-10-04, omdömena utbytta)."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer
from apps.tools import analyzer
from apps.tools.analyzer import AnalysError, Sida

from . import pagebuilder, reco
from .app_views.pages import _library
from .models import RATING_SOURCES, Fact, FlamingoAccount, LandingPage, is_rating_like
from .pagebuilder import koll, registry
from .test_demo import run_demo
from .test_google_ads import NOTHING

User = get_user_model()
DATA = Path(__file__).resolve().parent / "testdata"
PROFILE_HTML = (DATA / "reco_profil_cs_auto.html").read_text(encoding="utf-8")
WIDGET_HTML = (DATA / "reco_widget_cs_auto.html").read_text(encoding="utf-8")
CS_AUTO = "https://www.reco.se/cs-auto-ab"
CS_AUTO_ID = "5998572"
WIDGET_FOR_ID = (
    "https://widget.reco.se/v2/venues/5998572/vertical/small?inverted=false&border=true&lang=sv"
)
ALERTS = {"INQUIRY_NOTIFICATION_EMAIL": "larm@adx.example"}


class FakeReco:
    """En utbytt reco.fetch: svaren efter adressen (HTML eller ett fel), och
    varje anrop noterat med sina argument."""

    def __init__(self, pages=None):
        self.pages = {CS_AUTO: PROFILE_HTML, WIDGET_FOR_ID: WIDGET_HTML} if pages is None else pages
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        page = self.pages.get(url, AnalysError(f"Kunde inte hämta {url}: HTTP 404"))
        if isinstance(page, Exception):
            raise page
        return Sida(url=url, status=200, html=page, body=page.encode("utf-8"))

    @property
    def urls(self):
        return [url for url, _kwargs in self.calls]


def _no_network(*args, **kwargs):
    raise AssertionError("Ett test försökte nå nätet (Reco anropas aldrig från testerna).")


class RecoFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(name="CS Auto AB")
        cls.owner = User.objects.create_user("chabbe@csauto.se", password="x")
        cls.customer.users.add(cls.owner)
        cls.account = FlamingoAccount.objects.create(
            customer=cls.customer, is_enabled=True, website_url="https://www.csauto.se/"
        )

    def setUp(self):
        self.reco = FakeReco()
        for patcher in (
            mock.patch.object(reco, "fetch", self.reco),
            mock.patch.object(analyzer, "_request", _no_network),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client()
        self.client.force_login(self.owner)

    def post(self, data):
        return self.client.post(reverse("flamingo:app_reviews"), data)

    def new_day(self):
        """Dagens gräns för hämtningar nollställd (som ett nytt dygn)."""
        FlamingoAccount.objects.filter(pk=self.account.pk).update(daily_usage={})
        self.account.refresh_from_db()

    def messages_of(self, response):
        if response.status_code == 302:
            response = self.client.get(response.url)
        return [str(m) for m in response.context["messages"]]

    def connected(self, **fields):
        reco.connect(self.account, CS_AUTO)
        if fields:
            FlamingoAccount.objects.filter(pk=self.account.pk).update(**fields)
        self.account.refresh_from_db()
        return self.account

    def page_with(self, *blocks):
        return LandingPage.objects.create(
            account=self.account,
            name=f"Prov {LandingPage.objects.count()}",
            draft={"blocks": list(blocks)},
        )

    def block(self, variant):
        return pagebuilder.new_block("reviews_reco", variant, self.account)


# ---------------------------------------------------------------------------
# Länken och id:t
# ---------------------------------------------------------------------------


class LinkTests(TestCase):
    def test_profile_links_in_every_form(self):
        for text in (
            "https://www.reco.se/cs-auto-ab",
            "http://www.reco.se/cs-auto-ab",
            "https://reco.se/cs-auto-ab",
            "www.reco.se/cs-auto-ab",
            "reco.se/cs-auto-ab",
            "RECO.SE/CS-Auto-AB",
            " reco.se/cs-auto-ab/ ",
            "https://www.reco.se/cs-auto-ab/omdomen?sida=2&sort=nya#topp",
            "https://www.reco.se/cs-auto-ab?utm_source=mejl",
            "https://www.reco.se/share/cs-auto-ab/review/3361469",
            "https://m.reco.se/cs-auto-ab",
        ):
            with self.subTest(text=text):
                link = reco.parse_link(text)
                self.assertEqual((link.slug, link.venue_id, link.error), ("cs-auto-ab", "", ""))
                self.assertEqual(link.value, CS_AUTO)

    def test_ids_and_widget_links(self):
        for text in (
            "5998572",
            " 5998572 ",
            "https://widget.reco.se/v2/venues/5998572/horizontal/xlarge?inverted=false&border=true&lang=sv",
            "widget.reco.se/v2/venues/5998572/vertical/small",
            '<iframe src="https://widget.reco.se/v2/venues/5998572/vertical/medium">',
            "https://www.reco.se/v/venue/5998572/spontaneous?rating=5",
        ):
            with self.subTest(text=text):
                link = reco.parse_link(text)
                self.assertEqual((link.slug, link.venue_id, link.error), ("", CS_AUTO_ID, ""))
                self.assertEqual(link.value, CS_AUTO_ID)

    def test_what_is_not_a_profile_says_so(self):
        cases = {
            "": reco.EMPTY,
            "0123": reco.BAD_ID,
            "1234567890123": reco.BAD_ID,
            "https://www.reco.se/r/3361469": reco.REVIEW_LINK,
            "https://www.reco.se/v/review/3361469": reco.REVIEW_LINK,
            "https://www.reco.se/": reco.NOT_A_PROFILE,
            "https://www.reco.se/foretag/priser": reco.NOT_A_PROFILE,
            "https://www.reco.se/user/17800184": reco.NOT_A_PROFILE,
            "https://www.reco.se/k%C3%A4lla": reco.NOT_A_PROFILE,
            "https://widget.reco.se/v2/venues/": reco.WIDGET_WITHOUT_ID,
            "https://notreco.se/cs-auto-ab": reco.NOT_A_LINK,
            "https://reco.se.evil.example/cs-auto-ab": reco.NOT_A_LINK,
            "https://evil.example/reco.se/cs-auto-ab": reco.NOT_A_LINK,
            "https://www.reco.se@evil.example/cs-auto-ab": reco.NOT_A_LINK,
            "https://evil.example@www.reco.se/cs-auto-ab": reco.NOT_A_LINK,
            "javascript:alert(1)//reco.se/x": reco.NOT_A_LINK,
            "ftp://www.reco.se/cs-auto-ab": reco.NOT_A_LINK,
            "https://www.reco.se:99999/cs-auto-ab": reco.NOT_A_LINK,
            "CS Auto Bålsta": reco.NOT_A_LINK,
        }
        for text, error in cases.items():
            with self.subTest(text=text):
                link = reco.parse_link(text)
                self.assertEqual(link.error, error)
                self.assertEqual((link.slug, link.venue_id), ("", ""))

    def test_the_widget_is_built_only_from_digits(self):
        src = reco.widget_src(CS_AUTO_ID, "horizontal", "xlarge")
        self.assertEqual(
            src,
            "https://widget.reco.se/v2/venues/5998572/horizontal/xlarge"
            "?inverted=false&border=true&lang=sv",
        )
        self.assertEqual(reco.widget_src(5998572, "vertical", "small"), WIDGET_FOR_ID)
        self.assertEqual(reco.widget_src(" 5998572\n", "vertical", "small"), WIDGET_FOR_ID)
        for bad in (
            "5998572/../../evil",
            "5998572?x=1",
            "5998572#x",
            "59985 72",
            "0123",
            "-5",
            "1e5",
            "５９９８５７２",  # siffror som inte är ASCII
            "",
            None,
            True,
            "javascript:alert(1)",
            '5998572"><script>',
        ):
            with self.subTest(bad=bad):
                self.assertEqual(reco.widget_src(bad, "horizontal", "large"), "")
                self.assertEqual(reco.frames("stor", bad), [])
        for orientation, size in (
            ("horizontal", "huge"),
            ("vertical", "large"),
            ("diagonal", "small"),
            ("horizontal/../x", "small"),
        ):
            self.assertEqual(reco.widget_src(CS_AUTO_ID, orientation, size), "")

    def test_the_variants_map_giovannis_widgets(self):
        def shape(variant):
            return [
                (f.orientation, f.size, f.height, f.width, f.screen)
                for f in reco.frames(variant, CS_AUTO_ID)
            ]

        self.assertEqual(shape("stor"), [("horizontal", "xlarge", 225, None, "all")])
        self.assertEqual(
            shape("medel"),
            [("horizontal", "large", 60, None, "wide"), ("vertical", "medium", 150, 300, "narrow")],
        )
        self.assertEqual(shape("liten"), [("horizontal", "small", 27, None, "all")])
        self.assertEqual(shape("staende"), [("vertical", "medium", 150, 300, "all")])
        self.assertEqual(shape("okand"), shape(reco.DEFAULT_VARIANT))
        self.assertEqual(
            set(reco.VARIANT_WIDGETS) | set(reco.SELECTED_VARIANTS),
            set(registry.TYPES["reviews_reco"].variant_keys),
        )
        # Giovannis storlekar och höjder.
        self.assertEqual(
            reco.WIDGET_SIZES,
            {
                "horizontal": {"xlarge": 225, "large": 60, "medium": 64, "small": 27},
                "vertical": {"medium": 150, "small": 120},
            },
        )

    def test_a_stored_profile_link_is_only_a_profile_on_reco(self):
        self.assertEqual(reco.profile_link(CS_AUTO), CS_AUTO)
        for bad in (
            "http://www.reco.se/cs-auto-ab",
            "https://reco.se/cs-auto-ab",
            "https://www.reco.se/cs-auto-ab/omdomen",
            "https://www.reco.se/cs-auto-ab?x=1",
            "https://www.reco.se/foretag",
            "https://www.reco.se.evil.example/cs-auto-ab",
            "javascript:alert(1)",
            "https://www.reco.se/",
            "",
            None,
        ):
            with self.subTest(bad=bad):
                self.assertEqual(reco.profile_link(bad), "")


# ---------------------------------------------------------------------------
# Sidorna hos Reco (testdata ur CS Auto AB:s riktiga sidor)
# ---------------------------------------------------------------------------


class ParseTests(TestCase):
    def test_the_cs_auto_page_gives_5998572(self):
        profile = reco.parse_profile(PROFILE_HTML)
        self.assertEqual(profile.venue_id, CS_AUTO_ID)
        self.assertEqual(profile.slug, "cs-auto-ab")
        self.assertEqual(profile.name, "Cs Auto AB")
        self.assertEqual(profile.rating, Decimal("4.9"))
        self.assertEqual(profile.count, 145)
        self.assertEqual(profile.website, "https://www.csauto.se")
        self.assertEqual(profile.phone, "0107075992")
        self.assertEqual(profile.city, "Bålsta")

    def test_the_fallbacks_when_recos_page_changes(self):
        start = PROFILE_HTML.index("window.VenueData")
        end = PROFILE_HTML.index("})();", start) + len("})();")
        without_venue_data = PROFILE_HTML[:start] + PROFILE_HTML[end:]
        profile = reco.parse_profile(without_venue_data)
        # PaginationData har id:t, JSON-LD namnet, betyget, hemsidan och numret.
        self.assertEqual(profile.venue_id, CS_AUTO_ID)
        self.assertEqual(profile.name, "Cs Auto AB")
        self.assertEqual((profile.rating, profile.count), (Decimal("4.9"), 145))
        self.assertEqual(profile.website, "https://www.csauto.se")
        self.assertEqual(profile.phone, "0107-07 59 92")
        # Utan båda: det id som de flesta knapparna har, inte det andra
        # företaget längre ner på sidan.
        start = without_venue_data.index("window.PaginationData")
        end = without_venue_data.index(";", start) + 1
        only_buttons = without_venue_data[:start] + without_venue_data[end:]
        self.assertEqual(reco.parse_profile(only_buttons).venue_id, CS_AUTO_ID)

    def test_no_trustworthy_id_gives_nothing(self):
        conflict = PROFILE_HTML.replace(
            '"venueId":5998572,"name":"Cs Auto AB"', '"venueId":1234567,"name":"Cs Auto AB"', 1
        )
        self.assertIsNone(reco.parse_profile(conflict))
        tied = '<a data-venue-id="1234567"></a><a data-venue-id="7654321"></a>'
        self.assertIsNone(reco.parse_profile(tied))
        for html in ("", "<html><body>Hittades inte</body></html>", None, "x" * 1000):
            self.assertIsNone(reco.parse_profile(html))
        broken = PROFILE_HTML.replace(
            '"venueId":5998572,"rating"', '"venueId":"ej-siffror","rating"'
        )
        # VenueData utan id: PaginationData gäller.
        self.assertEqual(reco.parse_profile(broken).venue_id, CS_AUTO_ID)

    def test_the_widget_gives_the_profiles_address(self):
        self.assertEqual(reco.parse_widget(WIDGET_HTML, CS_AUTO_ID), "cs-auto-ab")
        self.assertEqual(reco.parse_widget(WIDGET_HTML, "1234567"), "")
        self.assertEqual(reco.parse_widget("", CS_AUTO_ID), "")
        evil = WIDGET_HTML.replace('slug:"cs-auto-ab"', 'slug:"../../evil"')
        self.assertEqual(reco.parse_widget(evil, CS_AUTO_ID), "")


# ---------------------------------------------------------------------------
# Hämtningen och ägaren
# ---------------------------------------------------------------------------


@override_settings(**ALERTS)
class ConnectTests(RecoFixture, TestCase):
    def test_the_cs_auto_link_is_looked_up_and_stored(self):
        match = reco.connect(self.account, "https://www.reco.se/cs-auto-ab?utm_source=x")
        self.assertEqual(match, reco.MATCH_WEBSITE)
        self.assertEqual(self.reco.urls, [CS_AUTO])
        _url, kwargs = self.reco.calls[0]
        # SSRF-skyddet med Recos värdar, ett tak för storleken och tiden.
        self.assertEqual(kwargs["hosts"], ("www.reco.se",))
        self.assertEqual(kwargs["max_bytes"], reco.MAX_BYTES)
        self.assertEqual(kwargs["time_limit"], reco.TIME_LIMIT)
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_venue_id, CS_AUTO_ID)
        self.assertEqual(self.account.reco_url, CS_AUTO)
        self.assertEqual(self.account.reco_name, "Cs Auto AB")
        self.assertEqual(self.account.reco_rating, Decimal("4.9"))
        self.assertEqual(self.account.reco_review_count, 145)
        self.assertIsNotNone(self.account.reco_fetched_at)
        self.assertFalse(self.account.reco_unverified)
        self.assertTrue(self.account.reco_trusted)
        self.assertEqual(mail.outbox, [])
        # Betyget blir aldrig en uppgift: annonserna och förslagen ser det inte.
        self.assertFalse(self.account.facts.exists())

    def test_an_id_looks_up_the_address_in_the_widget_first(self):
        reco.connect(self.account, CS_AUTO_ID)
        self.assertEqual(self.reco.urls, [WIDGET_FOR_ID, CS_AUTO])
        self.assertEqual(
            [kwargs["hosts"] for _url, kwargs in self.reco.calls],
            [("widget.reco.se",), ("www.reco.se",)],
        )
        self.account.refresh_from_db()
        self.assertEqual((self.account.reco_venue_id, self.account.reco_url), (CS_AUTO_ID, CS_AUTO))

    def test_an_id_that_does_not_match_the_page_is_refused(self):
        widget = WIDGET_HTML.replace('entityId:"5998572"', 'entityId:"1111111"')
        self.reco.pages[WIDGET_FOR_ID.replace("5998572", "1111111")] = widget
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, "1111111")
        self.assertEqual(caught.exception.message, reco.ID_MISMATCH)
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_venue_id, "")

    def test_the_phone_number_is_enough(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(website_url="")
        self.account.refresh_from_db()
        Fact.objects.create(
            account=self.account,
            key="telefon",
            label="Telefon",
            value="010-707 59 92",
            confirmed=True,
        )
        self.assertEqual(reco.connect(self.account, CS_AUTO), reco.MATCH_PHONE)
        self.assertTrue(self.account.reco_trusted)

    def test_a_profile_that_does_not_match_is_stored_unverified_and_adx_is_alerted(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            website_url="https://www.konkurrenten.se/"
        )
        self.account.refresh_from_db()
        # Ett obekräftat nummer och ett liknande namn räcker inte.
        Fact.objects.create(
            account=self.account, key="telefon", label="Telefon", value="010-707 59 92"
        )
        self.assertEqual(reco.connect(self.account, CS_AUTO), "")
        self.account.refresh_from_db()
        self.assertTrue(self.account.reco_unverified)
        self.assertFalse(self.account.reco_trusted)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("liknar inte", mail.outbox[0].subject)
        self.assertIn("Kunden har inte mejlats", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["larm@adx.example"])
        # Inget syns på sidorna, och blocket går inte att lägga till.
        self.assertFalse(registry.available(self.account)["reviews_reco"][0])
        with self.assertRaises(pagebuilder.BlockUnavailable):
            self.block("stor")
        # Samma profil igen: inget nytt larm.
        reco.connect(self.account, CS_AUTO)
        self.assertEqual(len(mail.outbox), 1)
        # "Profilen är vår": syns, och byrån får veta vem som intygade.
        reco.confirm_owner(self.account, self.owner)
        self.account.refresh_from_db()
        self.assertTrue(self.account.reco_trusted)
        self.assertEqual(self.account.reco_confirmed_by, self.owner)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("intygad", mail.outbox[1].subject)
        self.assertIn("chabbe@csauto.se", mail.outbox[1].body)
        # En ny hämtning av samma profil behåller intyget.
        reco.connect(self.account, CS_AUTO)
        self.account.refresh_from_db()
        self.assertTrue(self.account.reco_trusted)

    def test_a_shared_host_is_not_a_match(self):
        page = PROFILE_HTML.replace("https://www.csauto.se", "https://www.facebook.com/csauto")
        self.reco.pages[CS_AUTO] = page
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            website_url="https://www.facebook.com/konkurrenten"
        )
        self.account.refresh_from_db()
        self.assertEqual(reco.connect(self.account, CS_AUTO), "")
        self.assertTrue(self.account.reco_unverified)

    def test_a_competitors_profile_never_becomes_the_customers(self):
        """CS Auto har redan sin profil hos ADX. En konkurrent som klistrar in
        länken får nej, inget sparas, och byrån larmas."""
        reco.connect(self.account, CS_AUTO)
        rival = FlamingoAccount.objects.create(
            customer=Customer.objects.create(name="Bålsta Bil AB"),
            is_enabled=True,
            website_url="https://www.csauto.se/",  # även med samma hemsida
        )
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(rival, CS_AUTO)
        self.assertEqual(caught.exception.message, reco.TAKEN)
        rival.refresh_from_db()
        self.assertEqual(rival.reco_venue_id, "")
        self.assertFalse(rival.reco_trusted)
        self.assertIn("försökte koppla", mail.outbox[-1].subject)
        self.assertIn("CS Auto AB", mail.outbox[-1].body)
        with self.assertRaises(reco.RecoError):
            reco.confirm_owner(rival, self.staff)
        # Databasens regel håller även om kontrollen skulle missas.
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError), transaction.atomic():
            FlamingoAccount.objects.filter(pk=rival.pk).update(reco_venue_id=CS_AUTO_ID)

    def test_recos_errors_are_never_shown(self):
        self.reco.pages[CS_AUTO] = AnalysError("Kunde inte hämta https://www.reco.se/x: HTTP 503")
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, CS_AUTO)
        self.assertEqual(caught.exception.message, reco.RECO_DOWN)
        self.reco.pages[CS_AUTO] = RuntimeError("hemlig stackspårning")
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, CS_AUTO)
        self.assertEqual(caught.exception.message, reco.RECO_DOWN)
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, "https://www.reco.se/finns-inte-ab")
        self.assertEqual(caught.exception.message, reco.NOT_FOUND)
        self.new_day()
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, "7777777")
        self.assertEqual(caught.exception.message, reco.ID_NOT_FOUND)
        self.reco.pages[CS_AUTO] = "<html>Ingen profil</html>"
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, CS_AUTO)
        self.assertEqual(caught.exception.message, reco.NO_ID)

    def test_lookups_are_limited_per_account_and_day(self):
        for _ in range(reco.LOOKUP_DAILY_MAX):
            reco.connect(self.account, CS_AUTO)
        calls = len(self.reco.calls)
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, CS_AUTO)
        self.assertEqual(caught.exception.message, reco.LOOKUP_LIMIT)
        self.assertEqual(len(self.reco.calls), calls, "gränsen prövas före anropet")
        # En trasig länk kostar ingen hämtning.
        other = FlamingoAccount.objects.create(customer=Customer.objects.create(name="Annan AB"))
        with self.assertRaises(reco.RecoError):
            reco.connect(other, "https://evil.example/")
        self.assertEqual(other.daily_usage.get(reco.USAGE_LOOKUP), None)
        # I morgon går det igen.
        tomorrow = timezone.now() + timedelta(days=1)
        reco.connect(self.account, CS_AUTO, now=tomorrow)

    def test_disconnect_removes_everything(self):
        self.connected()
        reco.disconnect(self.account)
        self.account.refresh_from_db()
        self.assertEqual(
            (
                self.account.reco_venue_id,
                self.account.reco_url,
                self.account.reco_name,
                self.account.reco_rating,
                self.account.reco_review_count,
                self.account.reco_fetched_at,
                self.account.reco_unverified,
            ),
            ("", "", "", None, None, None, False),
        )


# ---------------------------------------------------------------------------
# Demot och betygen
# ---------------------------------------------------------------------------


@override_settings(**ALERTS)
class DemoTests(RecoFixture, TestCase):
    def setUp(self):
        super().setUp()
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        self.client = Client()
        self.client.force_login(self.staff)
        session = self.client.session
        session[VIEW_AS_KEY] = self.customer.pk
        session.save()

    def test_the_demo_never_calls_reco(self):
        with self.assertRaises(reco.RecoError) as caught:
            reco.connect(self.account, CS_AUTO)
        self.assertEqual(caught.exception.message, reco.DEMO_REFUSED)
        for data in (
            {"action": "reco_find", "reco": CS_AUTO},
            {"action": "reco_confirm", "reco": CS_AUTO},
            {"action": "reco_confirm", "reco": CS_AUTO_ID},
            {"action": "reco_refresh"},
        ):
            with self.subTest(data=data):
                self.assertIn(self.post(data).status_code, (200, 302))
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Reco anropas aldrig", html)
        self.assertEqual(self.reco.calls, [])
        self.assertEqual(mail.outbox, [])

    def test_the_demo_command_gives_a_fictional_profile_and_no_iframe(self):
        with override_settings(DEBUG=True):
            run_demo()
        demo = FlamingoAccount.objects.get(is_demo=True, customer__name__contains="Exempelrör")
        self.assertEqual(demo.reco_venue_id, "0000000")
        self.assertEqual(reco.clean_venue_id(demo.reco_venue_id), "")
        self.assertEqual(demo.reco_url, "")
        pages = LandingPage.objects.filter(account=demo)
        variants = {
            b["variant"] for p in pages for b in p.draft_blocks if b["type"] == "reviews_reco"
        }
        self.assertEqual(variants, {"stor", "medel", "liten", "staende", "utvalda_kort"})
        self.assertEqual((demo.reco_reviews, demo.reco_reviews_selected), ([], []))
        for page in pages:
            html = pagebuilder.render_page_html(page, demo, which="draft")
            with self.subTest(page=page.name):
                self.assertNotIn("widget.reco.se", html)
                self.assertNotIn("<iframe", html)
                self.assertNotIn("www.reco.se", html)
                if any(b["type"] == "reviews_reco" for b in page.draft_blocks):
                    self.assertIn("Demot hämtar ingenting från Reco", html)
        # Cron rensar aldrig demot.
        FlamingoAccount.objects.filter(pk=demo.pk).update(
            reco_fetched_at=timezone.now() - timedelta(days=400)
        )
        self.assertEqual(reco.expire(), 0)
        self.assertEqual(self.reco.calls, [])


class RatingTests(TestCase):
    def test_a_reco_rating_is_rating_like_and_never_used_as_a_fact(self):
        self.assertTrue(is_rating_like("reco", "Reco", "4,9"))
        self.assertTrue(is_rating_like("ovrigt", "Övrigt", "Reco 4,9 (145)"))
        account = FlamingoAccount.objects.create(customer=Customer.objects.create(name="Prov AB"))
        fact = Fact.objects.create(
            account=account,
            key="reco",
            label="Reco",
            value="4,9",
            source=Fact.SOURCE_SITE,
            confirmed=True,
        )
        self.assertFalse(fact.is_usable)
        self.assertNotIn("reco", account.confirmed_facts())
        self.assertEqual(RATING_SOURCES, ("google", "adx"))


# ---------------------------------------------------------------------------
# Blocket på sidan
# ---------------------------------------------------------------------------


class BlockTests(RecoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.connected()

    def test_every_variant_renders_recos_widget_from_the_id(self):
        expected = {
            "stor": [("horizontal/xlarge", "225", "")],
            "medel": [("horizontal/large", "60", ""), ("vertical/medium", "150", "300")],
            "liten": [("horizontal/small", "27", "")],
            "staende": [("vertical/medium", "150", "300")],
        }
        import re

        for variant, frames in expected.items():
            block = self.block(variant)
            page = self.page_with(block)
            for editing in (False, True):
                with self.subTest(variant=variant, editing=editing):
                    html = pagebuilder.render_block_html(page, block, self.account, editing=editing)
                    self.assertIn('class="rn-block rn-reco ', html)
                    found = re.findall(
                        r'<iframe class="[^"]*" src="https://widget\.reco\.se/v2/venues/5998572/'
                        r'([a-z]+/[a-z]+)\?inverted=false&amp;border=true&amp;lang=sv" '
                        r'title="Omdömen på Reco" loading="lazy" height="(\d+)"(?: width="(\d+)")? '
                        r'referrerpolicy="strict-origin-when-cross-origin" '
                        r'sandbox="allow-scripts allow-same-origin allow-popups '
                        r'allow-popups-to-escape-sandbox"></iframe>',
                        html,
                    )
                    self.assertEqual(found, frames)
                    self.assertEqual(html.count("<iframe"), len(frames))
                    self.assertNotIn("allow-top-navigation", html)
                    if variant == "liten":
                        self.assertNotIn("<h2", html)
                        self.assertNotIn("Se alla omdömen på Reco", html)
                    else:
                        self.assertIn("Omdömen från Reco", html)
                        self.assertIn(f'href="{CS_AUTO}"', html)
        medel = pagebuilder.render_block_html(
            self.page_with(self.block("medel")), self.block("medel"), self.account
        )
        self.assertIn("rn-reco__frame--wide", medel)
        self.assertIn("rn-reco__frame--narrow", medel)

    def test_the_small_one_is_a_strip_right_after_the_top(self):
        hero = pagebuilder.new_block("hero", "text", self.account)
        small = self.block("liten")
        html = pagebuilder.render_page_html(self.page_with(hero, small), self.account)
        self.assertIn("rn-reco--liten rn-s--strip", html)

    def test_without_a_trusted_profile_nothing_from_reco_is_shown(self):
        block = self.block("stor")
        page = self.page_with(block)
        for fields in (
            {"reco_unverified": True},
            {"reco_venue_id": "", "reco_url": ""},
            {"reco_venue_id": "5998572/../x"},
        ):
            with self.subTest(fields=fields):
                FlamingoAccount.objects.filter(pk=self.account.pk).update(**fields)
                self.account.refresh_from_db()
                html = pagebuilder.render_page_html(page, self.account)
                self.assertNotIn("widget.reco.se", html)
                self.assertNotIn("rn-reco", html)
                editing = pagebuilder.render_block_html(page, block, self.account, editing=True)
                self.assertIn("data-pb-empty", editing)
                self.assertNotIn("<iframe", editing)
                # Ett dolt block stoppar inte publiceringen.
                self.assertEqual(
                    [p for p in pagebuilder.page_problems(page) if "Reco" in str(p)], []
                )
                FlamingoAccount.objects.filter(pk=self.account.pk).update(
                    reco_unverified=False, reco_venue_id=CS_AUTO_ID, reco_url=CS_AUTO
                )
                self.account.refresh_from_db()

    def test_a_tampered_profile_link_is_not_drawn(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            reco_url="https://www.reco.se.evil.example/cs-auto-ab"
        )
        self.account.refresh_from_db()
        block = self.block("stor")
        html = pagebuilder.render_block_html(self.page_with(block), block, self.account)
        self.assertIn("widget.reco.se/v2/venues/5998572/", html)
        self.assertNotIn("evil", html)
        self.assertNotIn("Se alla omdömen på Reco", html)

    def test_names_and_titles_are_escaped(self):
        evil = '<img src=x onerror="alert(1)">'
        block = self.block("stor")
        version = pagebuilder.active_version(block)
        version["fields"]["title"] = evil
        html = pagebuilder.render_block_html(self.page_with(block), block, self.account)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x onerror=", html)
        # Ett namn från Reco i verktyget.
        page = PROFILE_HTML.replace(
            '"venueName":"Cs Auto AB"', '"venueName":"<script>alert(1)</script>"'
        )
        self.reco.pages[CS_AUTO] = page
        reco.connect(self.account, CS_AUTO)
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_name, "<script>alert(1)</script>")
        response = self.client.get(reverse("flamingo:app_reviews"))
        html = response.content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        response = self.post({"action": "reco_find", "reco": '"><script>alert(2)</script>'})
        self.assertNotIn("<script>alert(2)</script>", response.content.decode())


# ---------------------------------------------------------------------------
# Omdömen i verktyget
# ---------------------------------------------------------------------------


@override_settings(**ALERTS)
class ViewTests(RecoFixture, TestCase):
    def test_paste_confirm_and_see_the_profile(self):
        response = self.post({"action": "reco_find", "reco": "reco.se/cs-auto-ab/omdomen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.reco.calls, [], "inget anrop innan kunden bekräftat")
        html = response.content.decode()
        self.assertIn("Är det här ni?", html)
        self.assertIn('name="reco" value="https://www.reco.se/cs-auto-ab"', html)
        response = self.post({"action": "reco_confirm", "reco": CS_AUTO})
        self.assertRedirects(
            response, reverse("flamingo:app_reviews") + "#reco", fetch_redirect_response=False
        )
        messages = self.messages_of(response)
        self.assertTrue(any("samma hemsidan" in m for m in messages), messages)
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Cs Auto AB", html)
        self.assertIn("<b>4,9</b> av 5, 145 omdömen på Reco", html)
        self.assertIn("5998572", html)
        self.assertIn("Lägg till blocket Omdömen från Reco", html)
        self.assertIn(f"Profilen kan hämtas {reco.LOOKUP_DAILY_MAX - 1} gånger till i dag", html)

    def test_a_bad_link_says_why_without_a_call(self):
        response = self.post({"action": "reco_find", "reco": "https://www.reco.se/r/3361469"})
        self.assertContains(response, reco.REVIEW_LINK)
        response = self.post({"action": "reco_confirm", "reco": "https://evil.example/"})
        self.assertIn(reco.NOT_A_LINK, self.messages_of(response))
        self.assertEqual(self.reco.calls, [])

    def test_unverified_own_refresh_and_disconnect(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(website_url="")
        response = self.post({"action": "reco_confirm", "reco": CS_AUTO_ID})
        self.assertTrue(any("liknar inte" in m for m in self.messages_of(response)))
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Profilen liknar inte företaget", html)
        self.assertIn('value="reco_own"', html)
        response = self.post({"action": "reco_own"})
        self.account.refresh_from_db()
        self.assertTrue(self.account.reco_trusted)
        self.assertEqual(self.account.reco_confirmed_by, self.owner)
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Intygad som er av chabbe@csauto.se", html)
        calls = len(self.reco.calls)
        response = self.post({"action": "reco_refresh"})
        self.assertIn(
            "Profilen och omdömena är hämtade från Reco igen.", self.messages_of(response)
        )
        # Den sparade adressen används: ett anrop, inte widgeten först.
        self.assertEqual(self.reco.urls[calls:], [CS_AUTO])
        response = self.post({"action": "reco_disconnect"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_venue_id, "")
        self.assertIn(
            "Koppla er profil på Reco",
            self.client.get(reverse("flamingo:app_reviews")).content.decode(),
        )

    def test_the_limit_shows_and_stops(self):
        for _ in range(reco.LOOKUP_DAILY_MAX):
            self.post({"action": "reco_confirm", "reco": CS_AUTO})
        response = self.post({"action": "reco_confirm", "reco": CS_AUTO})
        self.assertIn(reco.LOOKUP_LIMIT, self.messages_of(response))
        self.assertEqual(len(self.reco.calls), reco.LOOKUP_DAILY_MAX)
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Profilen har hämtats så många gånger som går i dag.", html)

    def test_the_editor_library_links_here_and_the_block_needs_the_profile(self):
        page = self.page_with()
        item = next(
            i
            for g in _library(self.account, page)
            for i in g["items"]
            if i["type"].key == "reviews_reco"
        )
        self.assertFalse(item["ok"])
        self.assertEqual(item["link"], reverse("flamingo:app_reviews") + "#reco")
        self.assertIn("Reco", item["reason"])
        html = self.client.get(reverse("flamingo:app_page", args=[page.pk])).content.decode()
        self.assertIn(f'href="{reverse("flamingo:app_reviews")}#reco"', html)
        self.connected()
        item = next(
            i
            for g in _library(self.account, page)
            for i in g["items"]
            if i["type"].key == "reviews_reco"
        )
        self.assertEqual((item["ok"], item["link"]), (True, ""))


# ---------------------------------------------------------------------------
# 90 dagar och cron
# ---------------------------------------------------------------------------


@override_settings(**NOTHING)
class ExpiryTests(RecoFixture, TestCase):
    def test_name_rating_and_count_go_after_ninety_days_the_widget_stays(self):
        now = timezone.now()
        self.connected(reco_fetched_at=now - timedelta(days=89))
        self.assertEqual(reco.expire(now), 0)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            reco_fetched_at=now - timedelta(days=91)
        )
        out = StringIO()
        call_command("flamingo_google_sync", stdout=out)
        self.assertIn(
            "Omdömen från Reco: 0 hämtade, 0 med fel, 1 rensade (för gamla).", out.getvalue()
        )
        self.account.refresh_from_db()
        self.assertEqual(
            (self.account.reco_name, self.account.reco_rating, self.account.reco_review_count),
            ("", None, None),
        )
        self.assertEqual((self.account.reco_venue_id, self.account.reco_url), (CS_AUTO_ID, CS_AUTO))
        self.assertTrue(self.account.reco_trusted)
        # Cron hämtar ingenting från Reco.
        self.assertEqual(len(self.reco.calls), 1)
        out = StringIO()
        call_command("flamingo_google_sync", stdout=out)
        self.assertNotIn("Reco", out.getvalue())


# ---------------------------------------------------------------------------
# Konverteringskollen
# ---------------------------------------------------------------------------


class KollTests(RecoFixture, TestCase):
    def social(self, *blocks):
        page = self.page_with(*blocks)
        return next(i for i in koll.koll(page, self.account)["items"] if i["key"] == "omdomen")

    def test_the_reco_block_counts_as_social_proof(self):
        hero = pagebuilder.new_block("hero", "text", self.account)
        item = self.social(hero)
        self.assertFalse(item["ok"])
        self.assertEqual(item["title"], "Inga omdömen från Google")
        self.connected()
        item = self.social(hero)
        self.assertFalse(item["ok"])
        self.assertEqual(item["action"]["kind"], "add_block")
        self.assertEqual(item["action"]["type"], "reviews_reco")
        item = self.social(hero, self.block("medel"))
        self.assertTrue(item["ok"])
        self.assertEqual(item["title"], "Omdömen från Reco")
        # Ett block som skapades medan profilen var intygad, och profilen
        # som sedan inte längre är det.
        reco_block = self.block("stor")
        FlamingoAccount.objects.filter(pk=self.account.pk).update(reco_unverified=True)
        self.account.refresh_from_db()
        item = self.social(hero, reco_block)
        self.assertFalse(item["ok"])
        self.assertEqual(item["title"], "Recos ruta syns inte")
        self.assertEqual(item["action"]["url"], reverse("flamingo:app_reviews") + "#reco")


# ---------------------------------------------------------------------------
# "Bygg sidan åt mig"
# ---------------------------------------------------------------------------


class AiBuildTests(RecoFixture, TestCase):
    def test_an_existing_reco_block_follows_the_proposal_unchanged(self):
        from .pagebuilder import ai

        self.connected()
        kept = self.block("medel")
        page = self.page_with(pagebuilder.new_block("hero", "text", self.account), kept)
        with (
            mock.patch("apps.assistant.llm.is_configured", return_value=False),
            mock.patch("apps.assistant.llm.call", side_effect=AssertionError("ingen AI")),
        ):
            result = ai.build(page, self.account, goal="call")
            self.assertIn(kept, result["blocks"])
            # Utan en intygad profil följer det inte med, och förslaget
            # lägger aldrig till blocket själv.
            FlamingoAccount.objects.filter(pk=self.account.pk).update(reco_unverified=True)
            self.account.refresh_from_db()
            result = ai.build(page, self.account, goal="call")
            self.assertNotIn("reviews_reco", [b["type"] for b in result["blocks"]])
            FlamingoAccount.objects.filter(pk=self.account.pk).update(reco_unverified=False)
            self.account.refresh_from_db()
            bare = self.page_with(pagebuilder.new_block("hero", "text", self.account))
            result = ai.build(bare, self.account, goal="call")
            self.assertNotIn("reviews_reco", [b["type"] for b in result["blocks"]])


# ---------------------------------------------------------------------------
# Utvalda: kundens valda omdömen (Giovannis beslut 2026-10-04)
# ---------------------------------------------------------------------------

REVIEW_IDS = ["1000001", "1000002", "1000003", "1000004"]


def article(review_id, author, day, stars=5, text="Text.", invited=True):
    """Ett omdömeskort i samma form som på Recos profilsida."""
    rating = "<span></span>" * stars + "<em></em>" * (5 - stars)
    label = '<span class="venue-hide-mobile">Omdöme från inbjuden kund</span>' if invited else ""
    return (
        f'<article id="{review_id}" class="review-card-v2"><header>'
        f'<a class="venue-clean-link" href="/user/1"><b>{author}</b></a><time>{day}</time>'
        f'</header><div class="venue-ratings">{rating}</div>{label}'
        f'<p class="truncated-text">{text}</p></article>'
    )


class ReviewParseTests(TestCase):
    def test_the_reviews_on_the_cs_auto_page(self):
        reviews = reco.parse_reviews(PROFILE_HTML)
        self.assertEqual([r["id"] for r in reviews], REVIEW_IDS)
        first, second, third, fourth = reviews
        self.assertEqual(
            first,
            {
                "id": "1000001",
                "author": "Exempel A",
                "date": "2026-10-02",
                "rating": 5,
                "text": "Påhittad text i testdatan.",
                "uri": "https://www.reco.se/r/1000001",
                "invited": True,
            },
        )
        # Fyra stjärnor (<em> är tom), entiteterna avkodade, radbrytningen kvar.
        self.assertEqual(second["rating"], 4)
        self.assertEqual(second["text"], 'Bra bemötande, inte för "på".\nBytte bromsar & olja.')
        # Utan Recos märkning: inte inbjuden.
        self.assertFalse(third["invited"])
        # Betyget fanns bara i JSON-LD.
        self.assertEqual((fourth["rating"], fourth["invited"]), (5, True))
        # Företagets svar, ett kort utan namn och ett utan id är inga omdömen.
        texts = " ".join(r["text"] for r in reviews)
        self.assertNotIn("svar från företaget", texts)
        self.assertNotIn("hoppas över", texts)
        self.assertEqual(reco.parse_profile(PROFILE_HTML).reviews, reviews)

    def test_newest_first_capped_and_broken_cards_skipped(self):
        cards = [
            article(str(2000000 + n), f"Namn {n}", f"2026-0{1 + n % 9}-1{n % 10}")
            for n in range(60)
        ]
        cards.append(article("../../x", "Trasig", "2026-09-01"))
        cards.append(article("3000001", "", "2026-09-01"))
        cards.append(article("3000002", "Utan betyg", "2026-09-01", stars=0))
        reviews = reco.parse_reviews("".join(cards))
        self.assertEqual(len(reviews), reco.MAX_STORED)
        days = [r["date"] for r in reviews]
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertNotIn("3000001", [r["id"] for r in reviews])
        self.assertNotIn("3000002", [r["id"] for r in reviews])
        self.assertEqual(reco.parse_reviews(""), [])
        self.assertEqual(reco.parse_reviews(None), [])

    def test_links_are_only_reviews_on_reco(self):
        self.assertEqual(reco.review_link("3361469"), "https://www.reco.se/r/3361469")
        self.assertEqual(reco.review_link(3361469), "https://www.reco.se/r/3361469")
        for bad in ("../x", "0123", "", None, "1/../../evil", "12345678901234"):
            self.assertEqual(reco.review_link(bad), "")
        self.assertEqual(
            reco.safe_review_link("https://www.reco.se/r/1"), "https://www.reco.se/r/1"
        )
        for bad in (
            "http://www.reco.se/r/1",
            "https://reco.se/r/1",
            "https://www.reco.se/r/1?x=1",
            "https://www.reco.se.evil.example/r/1",
            "javascript:alert(1)",
            "https://www.reco.se/user/1",
        ):
            self.assertEqual(reco.safe_review_link(bad), "")
        # Den sparade länken läses aldrig: den byggs om av id:t.
        stored = reco._stored_review(
            {"id": "5", "author": "A", "rating": 5, "uri": "javascript:alert(1)", "invited": "ja"}
        )
        self.assertEqual(stored["uri"], "https://www.reco.se/r/5")
        self.assertFalse(stored["invited"])
        for broken in (
            {"id": "x", "author": "A", "rating": 5},
            {"id": "5", "author": "", "rating": 5},
            {"id": "5", "author": "A", "rating": 9},
            {"id": "5", "author": "A", "rating": True},
            "inte ett omdöme",
        ):
            self.assertIsNone(reco._stored_review(broken))


@override_settings(**ALERTS)
class SelectionTests(RecoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.connected()

    def test_a_verified_profile_stores_the_reviews_and_the_customer_chooses(self):
        self.assertEqual([r["id"] for r in self.account.reco_reviews], REVIEW_IDS)
        self.assertEqual(self.account.reco_reviews_selected, [])
        self.assertEqual(reco.selected_reviews(self.account), [])
        response = self.post(
            {
                "action": "reco_select",
                "order": REVIEW_IDS + ["9999999", "<script>"],
                "show": ["1000003", "1000001", "9999999"],
            }
        )
        self.assertIn("2 omdömen från Reco syns på sidorna.", self.messages_of(response))
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_reviews_selected, ["1000001", "1000003"])
        # Ner och Upp flyttar bland de valda.
        self.post(
            {
                "action": "reco_select",
                "order": REVIEW_IDS,
                "show": ["1000001", "1000003"],
                "move": "1000001:down",
            }
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_reviews_selected, ["1000003", "1000001"])
        self.assertEqual(
            [r["id"] for r in reco.selected_reviews(self.account)], ["1000003", "1000001"]
        )
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Utvalda omdömen från Reco", html)
        self.assertIn("Syns som nummer 1", html)
        self.assertIn("Omdöme från inbjuden kund", html)
        self.assertIn('href="https://www.reco.se/r/1000003"', html)
        self.assertIn("2 av högst 5 valda", html)
        self.assertIn("Hämtad ", html)
        self.assertIn("Hämta igen", html)

    def test_at_most_five_are_chosen(self):
        cards = "".join(
            article(str(2000000 + n), f"Namn {n}", f"2026-09-{10 + n}") for n in range(8)
        )
        page = PROFILE_HTML.replace("</body>", cards + "</body>")
        self.reco.pages[CS_AUTO] = page
        reco.connect(self.account, CS_AUTO)
        ids = [r["id"] for r in reco.stored_reviews(self.account)]
        response = self.post({"action": "reco_select", "order": ids, "show": ids})
        self.assertTrue(any("Högst 5 omdömen" in m for m in self.messages_of(response)))
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_reviews_selected, ids[: reco.MAX_SELECTED])

    def test_a_refresh_keeps_the_choice_and_skips_what_is_gone(self):
        reco.select(self.account, ["1000002", "1000001"])
        self.reco.pages[CS_AUTO] = PROFILE_HTML.replace(
            '<article id="1000002"', '<article id="1999999"'
        )
        reco.connect(self.account, CS_AUTO)
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_reviews_selected, ["1000002", "1000001"])
        self.assertEqual([r["id"] for r in reco.selected_reviews(self.account)], ["1000001"])

    def test_a_new_profile_resets_the_choice(self):
        reco.select(self.account, ["1000001"])
        other = PROFILE_HTML.replace("5998572", "7777777").replace("cs-auto-ab", "annat-ab")
        self.reco.pages["https://www.reco.se/annat-ab"] = other
        reco.connect(self.account, "https://www.reco.se/annat-ab")
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_venue_id, "7777777")
        self.assertEqual(self.account.reco_reviews_selected, [])

    def test_unverified_disconnected_and_demo_show_nothing(self):
        reco.select(self.account, ["1000001"])
        # Inte intygad: texterna sparas inte, och inget visas.
        FlamingoAccount.objects.filter(pk=self.account.pk).update(website_url="")
        self.account.refresh_from_db()
        reco.connect(self.account, CS_AUTO)
        self.account.refresh_from_db()
        self.assertTrue(self.account.reco_unverified)
        self.assertEqual(self.account.reco_reviews, [])
        self.assertEqual(reco.selected_reviews(self.account), [])
        response = self.post({"action": "reco_select", "order": REVIEW_IDS, "show": REVIEW_IDS})
        self.assertTrue(any("intygad" in m for m in self.messages_of(response)))
        self.assertNotIn(
            "Utvalda omdömen från Reco",
            self.client.get(reverse("flamingo:app_reviews")).content.decode(),
        )
        # Intygad och hämtad igen: valet står kvar och syns.
        reco.confirm_owner(self.account, self.owner)
        self.new_day()
        reco.connect(self.account, CS_AUTO)
        self.account.refresh_from_db()
        self.assertEqual([r["id"] for r in reco.selected_reviews(self.account)], ["1000001"])
        # Demot visar aldrig omdömen, inte ens sparade.
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        self.assertEqual(reco.selected_reviews(self.account), [])
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=False)
        self.account.refresh_from_db()
        # Bortkopplad: texterna och valet bort.
        reco.disconnect(self.account)
        self.account.refresh_from_db()
        self.assertEqual((self.account.reco_reviews, self.account.reco_reviews_selected), ([], []))
        self.assertEqual(reco.selected_reviews(self.account), [])


class UtvaldaBlockTests(RecoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.connected()
        reco.select(self.account, ["1000002", "1000001", "1000003", "1000004"])

    def render(self, variant, editing=False):
        block = self.block(variant)
        return pagebuilder.render_block_html(
            self.page_with(block), block, self.account, editing=editing
        )

    def test_every_utvalda_variant_in_ren(self):
        cards = self.render("utvalda_kort")
        self.assertIn('class="rn-block rn-reco rn-reco--utvalda_kort ', cards)
        self.assertNotIn("<iframe", cards)
        self.assertIn("Omdömen från Reco", cards)
        self.assertEqual(cards.count('class="rn-review"'), 3)
        # Kundens ordning: Exempel B först, och det fjärde visas inte.
        self.assertLess(cards.index("Exempel B"), cards.index("Exempel A"))
        self.assertNotIn("Exempel D", cards)
        self.assertIn("30 sep 2026", cards)
        self.assertIn('aria-label="4 av 5"', cards)
        self.assertIn('href="https://www.reco.se/r/1000002"', cards)
        self.assertEqual(cards.count("Omdöme från inbjuden kund"), 2)
        self.assertIn("CS Auto har valt vilka omdömen som visas här, och i vilken ordning.", cards)
        self.assertIn(f'href="{CS_AUTO}"', cards)
        self.assertIn('<b class="rn-reviews__big">4,9</b> av 5', cards)
        self.assertIn("145 omdömen på Reco", cards)
        quote = self.render("utvalda_citat")
        self.assertEqual(quote.count('class="rn-quote"'), 1)
        self.assertIn("Bra bemötande, inte för &quot;på&quot;.<br>Bytte bromsar &amp; olja.", quote)
        line = self.render("utvalda_rad")
        self.assertIn("<b>4,9</b> av 5", line)
        self.assertIn("145 omdömen", line)
        self.assertNotIn("rn-review__text", line)
        self.assertIn(f'href="{CS_AUTO}"', line)
        # Varje länk på blocket går till Reco.
        import re

        for html in (cards, quote, line):
            for href in re.findall(r'href="(https?://[^"]+)"', html):
                self.assertTrue(href.startswith("https://www.reco.se/"), href)

    def test_without_a_choice_the_reviews_are_hidden_but_the_line_shows_the_rating(self):
        reco.select(self.account, [])
        self.assertEqual(self.render("utvalda_kort"), "")
        self.assertIn("data-pb-empty", self.render("utvalda_kort", editing=True))
        self.assertIn("<b>4,9</b> av 5", self.render("utvalda_rad"))

    def test_unverified_disconnected_and_demo_show_no_reviews(self):
        # Blocken skapades medan profilen var intygad.
        blocks = [self.block(variant) for variant in reco.SELECTED_VARIANTS]
        for fields in ({"reco_unverified": True}, {"reco_venue_id": "", "reco_url": ""}):
            with self.subTest(fields=fields):
                FlamingoAccount.objects.filter(pk=self.account.pk).update(**fields)
                self.account.refresh_from_db()
                for block in blocks:
                    html = pagebuilder.render_block_html(self.page_with(block), block, self.account)
                    self.assertEqual(html, "")
            FlamingoAccount.objects.filter(pk=self.account.pk).update(
                reco_unverified=False, reco_venue_id=CS_AUTO_ID, reco_url=CS_AUTO
            )
            self.account.refresh_from_db()
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        html = self.render("utvalda_kort")
        self.assertIn("Demot hämtar ingenting från Reco och visar inga omdömen", html)
        self.assertNotIn("Exempel A", html)
        self.assertNotIn("www.reco.se", html)

    def test_names_and_texts_are_escaped(self):
        evil = [
            {
                "id": "1000001",
                "author": '<img src=x onerror="alert(1)">',
                "date": "2026-10-02",
                "rating": 5,
                "text": "<script>alert(2)</script>\nrad två",
                "uri": "javascript:alert(3)",
                "invited": True,
            }
        ]
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            reco_reviews=evil, reco_reviews_selected=["1000001"]
        )
        self.account.refresh_from_db()
        for variant in ("utvalda_kort", "utvalda_citat"):
            html = self.render(variant)
            self.assertNotIn("<img src=x", html)
            self.assertNotIn("<script>alert(2)", html)
            self.assertNotIn("javascript:", html)
            self.assertIn("&lt;script&gt;alert(2)&lt;/script&gt;<br>rad två", html)
            self.assertIn('href="https://www.reco.se/r/1000001"', html)
        page = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<script>alert(2)", page)
        self.assertNotIn("javascript:alert(3)", page)
        self.assertIn("&lt;img src=x onerror=", page)

    def test_the_conversion_check_counts_chosen_reviews(self):
        hero = pagebuilder.new_block("hero", "text", self.account)
        page = self.page_with(hero, self.block("utvalda_kort"))
        item = next(i for i in koll.koll(page, self.account)["items"] if i["key"] == "omdomen")
        self.assertTrue(item["ok"])
        reco.select(self.account, [])
        item = next(i for i in koll.koll(page, self.account)["items"] if i["key"] == "omdomen")
        self.assertFalse(item["ok"])
        self.assertEqual(item["title"], "Omdömena från Reco syns inte")
        self.assertEqual(item["action"]["url"], reverse("flamingo:app_reviews") + "#reco")


@override_settings(**NOTHING)
class KillSwitchTests(RecoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.connected()
        reco.select(self.account, ["1000001"])
        self.staff_client = Client()
        self.staff_client.force_login(self.staff)

    def switch(self, client, enabled):
        return client.post(
            reverse("manage:flamingo_selected_reviews"), {"enabled": "1" if enabled else "0"}
        )

    def assert_off_everywhere(self):
        self.assertFalse(reco.selected_enabled())
        # Blocket ritar Recos egen ruta (Liggande stor) i stället.
        block = self.block("utvalda_kort")
        html = pagebuilder.render_block_html(self.page_with(block), block, self.account)
        self.assertIn("widget.reco.se/v2/venues/5998572/horizontal/xlarge", html)
        self.assertIn("rn-reco--stor", html)
        self.assertNotIn("Exempel A", html)
        hero = pagebuilder.new_block("hero", "text", self.account)
        page = self.page_with(hero, dict(self.block("utvalda_rad")))
        self.assertNotIn("rn-s--strip", pagebuilder.render_page_html(page, self.account))
        self.assertEqual(reco.selected_reviews(self.account), [])
        # Valet göms med en rad, och går inte att spara.
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Utvalda omdömen från Reco är avstängda av ADX just nu.", html)
        self.assertNotIn('value="reco_select"', html)
        response = self.post({"action": "reco_select", "order": REVIEW_IDS, "show": REVIEW_IDS})
        self.assertIn(reco.SELECTED_OFF, self.messages_of(response))
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_reviews_selected, ["1000001"])
        # Inget hämtas av cron, och en ny hämtning sparar inga nya texter.
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            reco_fetched_at=timezone.now() - timedelta(days=30)
        )
        calls = len(self.reco.calls)
        summary = reco.refresh_due()
        self.assertEqual(
            (summary.fetched, summary.skipped), (0, "Utvalda omdömen från Reco är avstängda")
        )
        self.assertEqual(len(self.reco.calls), calls)
        self.reco.pages[CS_AUTO] = PROFILE_HTML.replace(
            '<article id="1000003"', '<article id="1888888"'
        )
        self.new_day()
        reco.connect(self.account, CS_AUTO)
        self.account.refresh_from_db()
        self.assertIn("1000003", [r["id"] for r in self.account.reco_reviews])
        # Konverteringskollen räknar Recos ruta.
        item = next(
            i
            for i in koll.koll(self.page_with(hero, block), self.account)["items"]
            if i["key"] == "omdomen"
        )
        self.assertTrue(item["ok"])

    def test_the_setting_turns_it_off(self):
        with override_settings(FLAMINGO_RECO_SELECTED_ENABLED=False):
            self.assert_off_everywhere()
            html = self.staff_client.get(reverse("manage:flamingo_overview")).content.decode()
            self.assertIn("Avstängt i miljön (FLAMINGO_RECO_SELECTED_ENABLED)", html)
        self.assertTrue(reco.selected_enabled())

    def test_staffs_switch_turns_it_off_for_everyone_at_once(self):
        html = self.staff_client.get(reverse("manage:flamingo_overview")).content.decode()
        self.assertIn("Utvalda omdömen från Reco: på.", html)
        self.assertIn("1 kund har valt omdömen.", html)
        # Kunden når inte knappen.
        self.switch(self.client, enabled=False)
        self.assertTrue(reco.selected_enabled())
        self.assertEqual(
            self.staff_client.get(reverse("manage:flamingo_selected_reviews")).status_code, 405
        )
        response = self.switch(self.staff_client, enabled=False)
        self.assertRedirects(
            response, reverse("manage:flamingo_overview") + "#reco", fetch_redirect_response=False
        )
        settings_row = reco.FlamingoSettings.get_solo()
        self.assertFalse(settings_row.reco_selected_enabled)
        self.assertEqual(settings_row.reco_selected_changed_by, self.staff)
        self.assert_off_everywhere()
        html = self.staff_client.get(reverse("manage:flamingo_overview")).content.decode()
        self.assertIn("Utvalda omdömen från Reco: av.", html)
        self.assertIn("Slå på utvalda omdömen från Reco igen", html)
        self.assertEqual(mail.outbox, [])
        self.switch(self.staff_client, enabled=True)
        self.assertTrue(reco.selected_enabled())
        self.assertEqual([r["id"] for r in reco.selected_reviews(self.account)], ["1000001"])


@override_settings(**NOTHING)
class RefreshCadenceTests(RecoFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.connected()
        self.calls = len(self.reco.calls)

    def age(self, days, **fields):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            reco_fetched_at=timezone.now() - timedelta(days=days), **fields
        )
        self.account.refresh_from_db()

    def fetched(self):
        return len(self.reco.calls) - self.calls

    def test_once_a_week_and_only_when_utvalda_is_used(self):
        # Ingen har valt något och inget block använder Utvalda: inget hämtas.
        self.age(8)
        self.assertEqual(reco.refresh_due().fetched, 0)
        # Ett block med Utvalda i ett utkast räcker.
        self.page_with(self.block("utvalda_citat"))
        summary = reco.refresh_due()
        self.assertEqual((summary.fetched, self.fetched()), (1, 1))
        self.assertEqual(self.reco.calls[-1][1]["hosts"], ("www.reco.se",))
        # Hämtad nyss: inget mer den här veckan.
        self.assertEqual(reco.refresh_due().fetched, 0)
        self.age(6, reco_reviews_selected=["1000001"])
        self.assertEqual(reco.refresh_due().fetched, 0)
        self.age(7, reco_reviews_selected=["1000001"], daily_usage={})
        self.assertEqual(reco.refresh_due().fetched, 1)
        self.assertEqual(self.fetched(), 2)

    def test_a_failure_is_tried_once_a_day(self):
        self.age(8, reco_reviews_selected=["1000001"])
        self.reco.pages[CS_AUTO] = AnalysError("Kunde inte hämta x: HTTP 503")
        out = StringIO()
        call_command("flamingo_google_sync", stdout=out)
        self.assertIn("Omdömen från Reco: 0 hämtade, 1 med fel", out.getvalue())
        self.assertIn(reco.RECO_DOWN, out.getvalue())
        call_command("flamingo_google_sync", stdout=StringIO())
        self.assertEqual(self.fetched(), 1)
        tomorrow = timezone.now() + timedelta(days=1)
        reco.refresh_due(now=tomorrow)
        self.assertEqual(self.fetched(), 2)

    def test_never_an_unverified_demo_or_disabled_account(self):
        for fields in ({"reco_unverified": True}, {"is_demo": True}, {"is_enabled": False}):
            with self.subTest(fields=fields):
                self.age(8, reco_reviews_selected=["1000001"], daily_usage={}, **fields)
                reco.refresh_due()
                self.assertEqual(self.fetched(), 0)
                FlamingoAccount.objects.filter(pk=self.account.pk).update(
                    reco_unverified=False, is_demo=False, is_enabled=True
                )

    def test_a_capped_number_per_run(self):
        other = FlamingoAccount.objects.create(
            customer=Customer.objects.create(name="Annan AB"),
            is_enabled=True,
            reco_venue_id="1234567",
            reco_url=CS_AUTO,
            reco_reviews_selected=["1"],
        )
        self.age(8, reco_reviews_selected=["1000001"])
        FlamingoAccount.objects.filter(pk=other.pk).update(
            reco_fetched_at=timezone.now() - timedelta(days=9)
        )
        with mock.patch.object(reco, "REFRESH_PER_RUN", 1):
            summary = reco.refresh_due()
        self.assertEqual(summary.fetched + summary.failed, 1)
        self.assertEqual(self.fetched(), 1)

    def test_hamta_igen_is_limited_per_day(self):
        self.new_day()
        for _ in range(reco.LOOKUP_DAILY_MAX):
            self.post({"action": "reco_refresh"})
        before = len(self.reco.calls)
        response = self.post({"action": "reco_refresh"})
        self.assertIn(reco.LOOKUP_LIMIT, self.messages_of(response))
        self.assertEqual(len(self.reco.calls), before)
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Profilen har hämtats så många gånger som går i dag.", html)

    def test_the_texts_go_after_ninety_days_the_choice_stays(self):
        reco.select(self.account, ["1000001"])
        self.age(91, daily_usage={})
        with mock.patch.object(reco, "REFRESH_PER_RUN", 0):
            summary = reco.refresh_due()
        self.assertEqual(summary.expired, 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.reco_reviews, [])
        self.assertEqual(self.account.reco_reviews_selected, ["1000001"])
        self.assertEqual(self.account.reco_venue_id, CS_AUTO_ID)
        self.assertEqual(reco.selected_reviews(self.account), [])
        # Nästa lyckade hämtning ger tillbaka valet.
        reco.refresh_due()
        self.account.refresh_from_db()
        self.assertEqual([r["id"] for r in reco.selected_reviews(self.account)], ["1000001"])
