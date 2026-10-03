"""ADX Flamingo: omdömena från Reco (reco.py, Recos del av
app_views/reviews.py och blocket "Omdömen från Reco"): länken och id:t,
profilsidan, ägaren, gränserna, demot, blocket i varje variant, 90 dagar och
Konverteringskollen.

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
            set(reco.VARIANT_WIDGETS), set(registry.TYPES["reviews_reco"].variant_keys)
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
        self.assertEqual(kwargs["hosts"], ("www.reco.se", "widget.reco.se"))
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
        self.assertEqual(variants, {"stor", "medel", "liten", "staende"})
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
        self.assertIn("Profilen är hämtad från Reco igen.", self.messages_of(response))
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
        self.assertIn("Omdömen från Reco: 1 rensade (för gamla).", out.getvalue())
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
