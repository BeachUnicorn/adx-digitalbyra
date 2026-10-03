"""ADX Flamingo: omdömena från Google (reviews.py, app_views/reviews.py och
steget i flamingo_google_sync): profilen pekas ut (sök, länk, Place ID),
Place Details sparas, kunden väljer och ordnar, märkningen på /lp/, cron,
gränserna, demot och läget utan nyckel.

Google anropas aldrig: urlopen i reviews.py är utbytt i varje test som
kunde nå Google, och de andra testerna har ingen nyckel."""

import io
import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from urllib.error import HTTPError

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import pagebuilder, reviews
from .models import Campaign, Fact, FlamingoAccount, Service
from .test_google_ads import NOTHING
from .testing import pages_from_campaigns

User = get_user_model()
KEY = "test-nyckel-som-aldrig-visas"
WITH_KEY = {"GOOGLE_PLACES_API_KEY": KEY}
NO_KEY = {"GOOGLE_PLACES_API_KEY": ""}
#: Google Ads API av, så att synkkommandot bara kör omdömena.
NO_ADS = NOTHING
PLACE = "ChIJexempelPlats1234"

SEARCH = {
    "places": [
        {
            "id": PLACE,
            "displayName": {"text": "Lindqvist Rör AB", "languageCode": "sv"},
            "formattedAddress": "Exempelvägen 4, 131 50 Nacka, Sverige",
            "rating": 4.8,
            "userRatingCount": 37,
            "googleMapsUri": "https://maps.google.com/?cid=123",
        },
        {"id": "ChIJannanPlats98765", "displayName": {"text": "Lindqvist Bygg"}},
        {"id": "../../hack", "displayName": {"text": "Trasig"}},
    ]
}


def details(rating=4.8, count=37, reviews_list=None):
    return {
        "id": PLACE,
        "displayName": {"text": "Lindqvist Rör AB", "languageCode": "sv"},
        "formattedAddress": "Exempelvägen 4, 131 50 Nacka, Sverige",
        "rating": rating,
        "userRatingCount": count,
        "googleMapsUri": "https://maps.google.com/?cid=123",
        "reviews": REVIEWS if reviews_list is None else reviews_list,
    }


REVIEWS = [
    {
        "name": f"places/{PLACE}/reviews/AAA",
        "relativePublishTimeDescription": "för 3 veckor sedan",
        "rating": 5,
        "text": {"text": "Kom samma kväll.", "languageCode": "sv"},
        "originalText": {"text": "Came the same evening.", "languageCode": "en"},
        "authorAttribution": {
            "displayName": "Anna L.",
            "uri": "https://www.google.com/maps/contrib/111",
            "photoUri": "https://lh3.googleusercontent.com/a/anna",
        },
        "publishTime": "2026-09-12T10:00:00Z",
        "googleMapsUri": "https://www.google.com/maps/reviews/data=anna",
    },
    {
        "name": f"places/{PLACE}/reviews/BBB",
        "relativePublishTimeDescription": "för 2 veckor sedan",
        "rating": 4,
        "text": {"text": "Trevliga och noggranna.", "languageCode": "sv"},
        "authorAttribution": {"displayName": "Johan S.", "uri": "http://osaker.example/johan"},
        "publishTime": "2026-09-20T10:00:00Z",
        "googleMapsUri": "https://www.google.com/maps/reviews/data=johan",
    },
    {"name": "inte-ett-omdome", "rating": 5, "authorAttribution": {"displayName": "X"}},
    {"name": f"places/{PLACE}/reviews/CCC", "rating": 5, "authorAttribution": {}},
]


class FakeGoogle:
    """En utbytt urlopen för Places API: svaren efter adressen, och varje
    anrop noterat (Request-objekten)."""

    def __init__(self, search=None, place=None, error=None):
        self.search = SEARCH if search is None else search
        self.place = details() if place is None else place
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if request.full_url == reviews.SEARCH_URL:
            payload = self.search
        else:
            payload = self.place
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    @property
    def urls(self):
        return [r.full_url for r in self.requests]


def http_error(code, status):
    body = json.dumps({"error": {"code": code, "status": status, "message": KEY}}).encode()
    return HTTPError(reviews.SEARCH_URL, code, status, {}, io.BytesIO(body))


class ReviewsFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(name="Lindqvist Rör AB")
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.customer.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(customer=cls.customer, is_enabled=True)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.anna)
        self.google = FakeGoogle()
        patcher = mock.patch.object(reviews, "urlopen", self.google)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, data):
        return self.client.post(reverse("flamingo:app_reviews"), data)

    def messages_of(self, response):
        if response.status_code == 302:
            response = self.client.get(response.url)
        return [str(m) for m in response.context["messages"]]

    def connected(self, **fields):
        reviews.store_details(self.account, details(), PLACE)
        if fields:
            FlamingoAccount.objects.filter(pk=self.account.pk).update(**fields)
        self.account.refresh_from_db()
        return self.account


# ---------------------------------------------------------------------------
# Länken från Google Maps och Place ID
# ---------------------------------------------------------------------------


class ParseTests(TestCase):
    def test_place_ids_in_maps_links(self):
        cases = {
            f"https://www.google.com/maps/place/?q=place_id:{PLACE}": PLACE,
            f"https://www.google.com/maps/search/?api=1&query=Lindqvist&query_place_id={PLACE}": (
                PLACE
            ),
            (
                "https://www.google.se/maps/place/Lindqvist+R%C3%B6r/@59.31,18.16,17z/data="
                f"!3m1!4b1!4m6!3m5!1s{PLACE}!8m2!3d59.31!4d18.16"
            ): PLACE,
            f"https://www.google.com/maps/place/X/data=!4m2!3m1!19s{PLACE}": PLACE,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reviews.parse_maps_link(text).place_id, expected)

    def test_a_name_in_the_link_becomes_a_search(self):
        link = reviews.parse_maps_link(
            "https://www.google.com/maps/place/Lindqvist+R%C3%B6r+AB/@59.31,18.16,17z/data=!3m1"
            "!1s0x465f8b:0x1a2b3c"
        )
        self.assertEqual((link.place_id, link.query), ("", "Lindqvist Rör AB"))
        link = reviews.parse_maps_link("maps.google.com/?q=Lindqvist+R%C3%B6r+Nacka")
        self.assertEqual(link.query, "Lindqvist Rör Nacka")

    def test_short_links_other_sites_and_plain_text(self):
        self.assertEqual(
            reviews.parse_maps_link("https://maps.app.goo.gl/AbCdEf").error, reviews.SHORT_LINK
        )
        self.assertEqual(
            reviews.parse_maps_link(
                "https://evil.example/maps/place/?q=place_id:ChIJxxxxxxxxxx"
            ).error,
            reviews.NOT_A_MAPS_LINK,
        )
        self.assertEqual(
            reviews.parse_maps_link("https://google.com.evil.example/maps").error,
            reviews.NOT_A_MAPS_LINK,
        )
        self.assertIsNone(reviews.parse_maps_link("Lindqvist Rör Nacka"))

    def test_place_id_shape(self):
        self.assertEqual(reviews.clean_place_id(f" {PLACE} "), PLACE)
        self.assertEqual(reviews.clean_place_id(f"place_id:{PLACE}"), PLACE)
        for bad in ("", "kort", "ChIJ../../etc", "ChIJ abc def ghi", "x" * 201, "ChIJ?key=1&x=2"):
            with self.subTest(bad=bad):
                self.assertEqual(reviews.clean_place_id(bad), "")


# ---------------------------------------------------------------------------
# Sökningen och Place Details
# ---------------------------------------------------------------------------


@override_settings(**WITH_KEY)
class SearchTests(ReviewsFixture, TestCase):
    def test_text_search_asks_for_the_agreed_fields_only(self):
        hits = reviews.search(self.account, "Lindqvist Rör Nacka")
        self.assertEqual([h.place_id for h in hits], [PLACE, "ChIJannanPlats98765"])
        self.assertEqual(hits[0].rating, "4,8")
        self.assertEqual(hits[0].count, 37)
        request = self.google.requests[0]
        self.assertEqual(request.full_url, reviews.SEARCH_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.get_header("X-goog-fieldmask"),
            "places.id,places.displayName,places.formattedAddress,places.rating,"
            "places.userRatingCount,places.googleMapsUri",
        )
        self.assertEqual(request.get_header("X-goog-api-key"), KEY)
        body = json.loads(request.data)
        self.assertEqual(body["textQuery"], "Lindqvist Rör Nacka")
        self.assertEqual((body["languageCode"], body["regionCode"]), ("sv", "SE"))

    def test_searches_are_limited_per_account_and_day(self):
        for _ in range(reviews.SEARCH_DAILY_MAX):
            reviews.search(self.account, "Lindqvist")
        with self.assertRaises(reviews.ReviewsError) as caught:
            reviews.search(self.account, "Lindqvist")
        self.assertEqual(caught.exception.message, reviews.SEARCH_LIMIT)
        self.assertEqual(len(self.google.requests), reviews.SEARCH_DAILY_MAX)

    def test_googles_error_and_the_key_are_never_shown(self):
        self.google.error = http_error(403, "PERMISSION_DENIED")
        response = self.post({"action": "find", "q": "Lindqvist"})
        html = response.content.decode()
        self.assertNotIn(KEY, html)
        self.assertNotIn("PERMISSION_DENIED", html)
        self.assertIn(reviews.GOOGLE_DOWN, self.messages_of(response))

    def test_the_view_shows_hits_with_attribution_and_confirm_buttons(self):
        response = self.post({"action": "find", "q": "Lindqvist Rör"})
        html = response.content.decode()
        self.assertIn("Träffar hos Google", html)
        self.assertIn("Exempelvägen 4", html)
        self.assertIn('translate="no">Google Maps<', html)
        self.assertIn(f'value="{PLACE}"', html)
        self.assertIn("Det här är vi", html)
        self.assertNotIn("../../hack", html)
        self.assertEqual(self.google.urls, [reviews.SEARCH_URL])
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_place_id, "")  # inget sparat vid en sökning

    def test_a_maps_link_with_a_place_id_asks_for_confirmation_without_a_call(self):
        link = f"https://www.google.com/maps/place/?q=place_id:{PLACE}"
        html = self.post({"action": "find", "q": link}).content.decode()
        self.assertIn("Är det här ni?", html)
        self.assertIn(PLACE, html)
        self.assertEqual(self.google.requests, [])
        self.post({"action": "place_id", "place_id": PLACE})
        self.assertEqual(self.google.requests, [])

    def test_a_maps_link_with_a_name_searches_on_the_name(self):
        self.post(
            {"action": "find", "q": "https://www.google.com/maps/place/Lindqvist+R%C3%B6r/@59,18"}
        )
        self.assertEqual(json.loads(self.google.requests[0].data)["textQuery"], "Lindqvist Rör")


@override_settings(**WITH_KEY)
class ConnectTests(ReviewsFixture, TestCase):
    def test_details_are_stored_in_the_documented_shape(self):
        response = self.post({"action": "confirm", "place_id": PLACE})
        self.assertEqual(response.status_code, 302)
        request = self.google.requests[0]
        self.assertTrue(
            request.full_url.startswith(f"{reviews.DETAILS_URL.format(place_id=PLACE)}?")
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.get_header("X-goog-fieldmask"),
            "id,displayName,formattedAddress,rating,userRatingCount,googleMapsUri,reviews,"
            "websiteUri,nationalPhoneNumber",
        )
        account = FlamingoAccount.objects.get(pk=self.account.pk)
        self.assertEqual(account.google_place_id, PLACE)
        self.assertEqual(account.google_place_name, "Lindqvist Rör AB")
        self.assertEqual(account.google_maps_uri, "https://maps.google.com/?cid=123")
        self.assertEqual(account.google_rating, Decimal("4.8"))
        self.assertEqual(account.google_review_count, 37)
        self.assertIsNotNone(account.google_reviews_fetched_at)
        self.assertEqual(
            account.google_reviews,
            [
                {
                    "id": f"places/{PLACE}/reviews/BBB",
                    "author": "Johan S.",
                    "author_uri": "",
                    "rating": 4,
                    "text": "Trevliga och noggranna.",
                    "time": "2026-09-20T10:00:00Z",
                    "relative": "för 2 veckor sedan",
                    "uri": "https://www.google.com/maps/reviews/data=johan",
                },
                {
                    "id": f"places/{PLACE}/reviews/AAA",
                    "author": "Anna L.",
                    "author_uri": "https://www.google.com/maps/contrib/111",
                    "rating": 5,
                    "text": "Came the same evening.",  # originalet, inte Googles översättning
                    "time": "2026-09-12T10:00:00Z",
                    "relative": "för 3 veckor sedan",
                    "uri": "https://www.google.com/maps/reviews/data=anna",
                },
            ],
        )
        self.assertEqual(account.google_reviews_selected, [])
        self.assertNotIn("googleusercontent", json.dumps(account.google_reviews))
        shown = self.messages_of(response)
        self.assertTrue(any("Välj vilka omdömen" in m for m in shown), shown)

    def test_the_rating_becomes_a_confirmed_fact_from_google(self):
        Fact.objects.create(
            account=self.account, key="betyg", label="Betyg", value="4,9", source="site"
        )
        reviews.connect(self.account, PLACE)
        fact = self.account.facts.get(key="betyg")
        self.assertEqual(
            (fact.value, fact.source, fact.confirmed, fact.label),
            ("4,8 av 37 omdömen", Fact.SOURCE_GOOGLE, True, "Betyg på Google"),
        )
        self.assertTrue(fact.is_usable)
        self.assertEqual(self.account.confirmed_facts()["betyg"], "4,8 av 37 omdömen")
        # Samma form som places.py skriver.
        from . import places

        self.assertEqual(fact.value, places._rating(details()))

    def test_a_rating_from_adx_is_never_overwritten(self):
        Fact.objects.create(
            account=self.account,
            key="betyg",
            label="Betyg",
            value="4,7 av 120 omdömen",
            source=Fact.SOURCE_ADX,
            confirmed=True,
        )
        reviews.connect(self.account, PLACE)
        reviews.disconnect(self.account)
        fact = self.account.facts.get(key="betyg")
        self.assertEqual((fact.value, fact.source), ("4,7 av 120 omdömen", Fact.SOURCE_ADX))

    def test_details_are_limited_per_account_and_day(self):
        for _ in range(reviews.DETAILS_DAILY_MAX):
            reviews.connect(self.account, PLACE)
        before = len(self.google.requests)
        self.assertEqual(before, reviews.DETAILS_DAILY_MAX)
        response = self.post({"action": "refresh"})
        self.assertEqual(len(self.google.requests), before)
        self.assertIn(reviews.DETAILS_LIMIT, self.messages_of(response))

    def test_an_unknown_place_says_so(self):
        self.google.error = HTTPError(
            "u", 404, "NOT_FOUND", {}, io.BytesIO(b'{"error":{"status":"NOT_FOUND"}}')
        )
        response = self.post({"action": "confirm", "place_id": PLACE})
        self.assertIn(reviews.NOT_FOUND, self.messages_of(response))
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_place_id, "")

    def test_a_bad_place_id_never_reaches_google(self):
        for bad in ("../../v1/places:searchText", "ChIJ%2F..%2Fx", "kort"):
            with self.subTest(bad=bad):
                self.post({"action": "confirm", "place_id": bad})
                self.post({"action": "place_id", "place_id": bad})
        self.assertEqual(self.google.requests, [])

    def test_a_new_profile_resets_the_selection(self):
        self.connected(google_reviews_selected=[f"places/{PLACE}/reviews/AAA"])
        other = details()
        other["id"] = "ChIJannanPlats98765"
        self.google.place = other
        reviews.connect(self.account, "ChIJannanPlats98765")
        self.assertEqual(self.account.google_reviews_selected, [])


# ---------------------------------------------------------------------------
# Kundens val, ordningen och att koppla bort
# ---------------------------------------------------------------------------


@override_settings(**WITH_KEY)
class SelectionTests(ReviewsFixture, TestCase):
    AAA = f"places/{PLACE}/reviews/AAA"
    BBB = f"places/{PLACE}/reviews/BBB"

    def test_choose_order_and_move(self):
        self.connected()
        self.post(
            {
                "action": "select",
                "order": [self.AAA, self.BBB],
                "show": [self.AAA, self.BBB, "places/annan/reviews/X"],
            }
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_reviews_selected, [self.AAA, self.BBB])
        self.assertEqual(
            [r["author"] for r in self.account.selected_google_reviews()], ["Anna L.", "Johan S."]
        )
        self.post(
            {
                "action": "select",
                "order": [self.AAA, self.BBB],
                "show": [self.AAA, self.BBB],
                "move": f"{self.BBB}:up",
            }
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_reviews_selected, [self.BBB, self.AAA])
        self.post({"action": "select", "order": [self.BBB, self.AAA], "show": [self.AAA]})
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_reviews_selected, [self.AAA])
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Syns som nummer 1", html)
        self.assertIn("Visa på sidorna", html)
        self.assertIn('href="https://www.google.com/maps/contrib/111"', html)
        self.assertIn('href="https://www.google.com/maps/reviews/data=anna"', html)
        self.assertEqual(self.google.requests, [])

    def test_disconnect_removes_the_profile_and_the_rating(self):
        self.connected(google_reviews_selected=[self.AAA])
        reviews.store_rating_fact(self.account, details())
        response = self.post({"action": "disconnect"})
        self.assertEqual(response.status_code, 302)
        account = FlamingoAccount.objects.get(pk=self.account.pk)
        self.assertEqual(
            (
                account.google_place_id,
                account.google_place_name,
                account.google_maps_uri,
                account.google_rating,
                account.google_review_count,
                account.google_reviews,
                account.google_reviews_selected,
                account.google_reviews_fetched_at,
            ),
            ("", "", "", None, None, [], [], None),
        )
        self.assertFalse(account.facts.filter(key="betyg").exists())

    def test_the_business_page_links_here(self):
        html = self.client.get(reverse("flamingo:app_business")).content.decode()
        self.assertIn(reverse("flamingo:app_reviews"), html)
        self.assertIn("Koppla Google-profilen", html)


# ---------------------------------------------------------------------------
# Märkningen på /lp/
# ---------------------------------------------------------------------------


@override_settings(**NO_KEY)
class AttributionTests(ReviewsFixture, TestCase):
    def test_the_landing_page_shows_googles_attribution(self):
        reviews.store_details(self.account, details(), PLACE)
        reviews.select(self.account, [f"places/{PLACE}/reviews/AAA"])
        service = Service.objects.create(
            account=self.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        campaign = Campaign.objects.create(
            account=self.account,
            service=service,
            name="Rörjour Nacka",
            area="Nacka + 15 km",
            status=Campaign.STATUS_LIVE,
            page={"title": "Rörjour i Nacka", "phone": "08-000 00 00"},
        )
        pages_from_campaigns(campaign)
        page = campaign.landing_page
        block = pagebuilder.new_block("reviews_google", "cards", self.account)
        page.published = {"blocks": [*page.published_blocks, block]}
        page.save(update_fields=["published"])
        html = Client().get(campaign.landing_url).content.decode()
        self.assertIn("Omdömen från Google", html)
        self.assertIn("Anna L.", html)
        self.assertIn("Came the same evening.", html)
        self.assertIn('href="https://www.google.com/maps/contrib/111"', html)  # författaren
        self.assertIn('href="https://www.google.com/maps/reviews/data=anna"', html)  # omdömet
        self.assertIn('href="https://maps.google.com/?cid=123"', html)  # profilen
        self.assertIn('<span translate="no">Google&nbsp;Maps</span>', html)
        self.assertIn("har valt vilka omdömen som visas här, och i vilken ordning", html)
        self.assertNotIn("Johan S.", html)  # inte valt
        self.assertNotIn("googleusercontent", html)  # ingen författarbild från Google
        self.assertEqual(self.google.requests, [])  # sidan anropar aldrig Google


# ---------------------------------------------------------------------------
# Utan nyckel och demot
# ---------------------------------------------------------------------------


@override_settings(**NO_KEY)
class NoKeyTests(ReviewsFixture, TestCase):
    def test_the_page_says_it_is_not_switched_on_and_a_place_id_can_be_saved(self):
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("Kopplingen till Google är inte påslagen än", html)
        response = self.post({"action": "find", "q": "Lindqvist Rör"})
        self.assertTrue(any("inte är påslagen" in m for m in self.messages_of(response)))
        response = self.post({"action": "place_id", "place_id": PLACE})
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_place_id, PLACE)
        self.assertIsNone(self.account.google_reviews_fetched_at)
        self.post({"action": "refresh"})
        self.post({"action": "confirm", "place_id": PLACE})
        with self.assertRaises(reviews.ReviewsError):
            reviews.search(self.account, "Lindqvist")
        with self.assertRaises(reviews.ReviewsError):
            reviews.connect(self.account, PLACE)
        self.assertEqual(self.google.requests, [])


@override_settings(**WITH_KEY)
class DemoTests(ReviewsFixture, TestCase):
    def setUp(self):
        super().setUp()
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        self.client = Client()
        self.client.force_login(self.staff)
        session = self.client.session
        session[VIEW_AS_KEY] = self.customer.pk
        session.save()

    def test_the_demo_never_calls_google(self):
        self.connected(google_reviews_fetched_at=timezone.now() - timedelta(days=40))
        html = self.client.get(reverse("flamingo:app_reviews")).content.decode()
        self.assertIn("demokonto", html)
        for data in (
            {"action": "find", "q": "Lindqvist Rör"},
            {"action": "place_id", "place_id": PLACE},
            {"action": "confirm", "place_id": PLACE},
            {"action": "refresh"},
        ):
            with self.subTest(data=data):
                self.assertIn(self.post(data).status_code, (200, 302))
        for call in (
            lambda: reviews.search(self.account, "x y"),
            lambda: reviews.connect(self.account, PLACE),
        ):
            with self.assertRaises(reviews.ReviewsError):
                call()
        out = StringIO()
        with override_settings(**NO_ADS):
            call_command("flamingo_google_sync", stdout=out)
        self.assertEqual(self.google.requests, [])
        self.account.refresh_from_db()
        # Demots påhittade omdömen rensas aldrig av cron.
        self.assertTrue(self.account.google_reviews)


# ---------------------------------------------------------------------------
# Cron: flamingo_google_sync hämtar igen och rensar det som är för gammalt
# ---------------------------------------------------------------------------


@override_settings(**WITH_KEY, **NO_ADS)
class RefreshTests(ReviewsFixture, TestCase):
    def sync(self, *args):
        out = StringIO()
        call_command("flamingo_google_sync", *args, stdout=out)
        return out.getvalue()

    def test_profiles_older_than_a_week_are_fetched_again(self):
        old = timezone.now() - timedelta(days=8)
        self.connected(google_reviews_fetched_at=old, google_rating=Decimal("3.0"))
        fresh_customer = Customer.objects.create(name="Färsk AB")
        fresh = FlamingoAccount.objects.create(
            customer=fresh_customer,
            is_enabled=True,
            google_place_id="ChIJfarskPlats12345",
            google_reviews_fetched_at=timezone.now() - timedelta(days=2),
        )
        never_customer = Customer.objects.create(name="Aldrig AB")
        never = FlamingoAccount.objects.create(
            customer=never_customer, is_enabled=True, google_place_id="ChIJaldrigPlats1234"
        )
        out = self.sync()
        fetched = sorted(url.split("/places/")[1].split("?")[0] for url in self.google.urls)
        self.assertEqual(fetched, sorted([PLACE, "ChIJaldrigPlats1234"]))
        self.assertIn("Omdömen från Google: 2 hämtade, 0 med fel", out)
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_rating, Decimal("4.8"))
        self.assertGreater(self.account.google_reviews_fetched_at, old)
        fresh.refresh_from_db()
        self.assertEqual(fresh.google_reviews, [])
        never.refresh_from_db()
        self.assertIsNotNone(never.google_reviews_fetched_at)

    def test_content_too_old_is_removed_when_the_refresh_fails(self):
        aaa = f"places/{PLACE}/reviews/AAA"
        self.connected(
            google_reviews_fetched_at=timezone.now() - timedelta(days=91),
            google_reviews_selected=[aaa],
        )
        reviews.store_rating_fact(self.account, details())
        self.google.error = http_error(503, "UNAVAILABLE")
        out = self.sync()
        self.assertIn("1 med fel, 1 rensade", out)
        account = FlamingoAccount.objects.get(pk=self.account.pk)
        self.assertEqual(account.google_reviews, [])
        self.assertIsNone(account.google_rating)
        self.assertIsNone(account.google_review_count)
        self.assertEqual(account.google_place_name, "")
        # Place ID och valet får sparas: allt kommer tillbaka när hämtningen går.
        self.assertEqual(account.google_place_id, PLACE)
        self.assertEqual(account.google_reviews_selected, [aaa])
        self.assertFalse(account.facts.filter(key="betyg").exists())
        self.assertNotIn(KEY, out)

    def test_a_failure_within_ninety_days_keeps_the_reviews(self):
        """Giovanni 2026-10-03: 90 dagar, inte 30."""
        self.connected(google_reviews_fetched_at=timezone.now() - timedelta(days=60))
        self.google.error = http_error(503, "UNAVAILABLE")
        out = self.sync()
        self.assertIn("1 med fel, 0 rensade", out)
        self.account.refresh_from_db()
        self.assertEqual(len(self.account.google_reviews), 2)

    def test_without_the_key_nothing_is_fetched_but_old_content_is_still_removed(self):
        self.connected(google_reviews_fetched_at=timezone.now() - timedelta(days=91))
        with override_settings(**NO_KEY):
            out = self.sync()
        self.assertEqual(self.google.requests, [])
        self.assertIn("0 hämtade, 0 med fel, 1 rensade", out)
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_reviews, [])

    def test_nothing_to_do_prints_nothing_and_the_trial_run_skips_the_step(self):
        with override_settings(**NO_KEY):
            out = self.sync()
        self.assertNotIn("Omdömen", out)
        self.connected(google_reviews_fetched_at=timezone.now() - timedelta(days=91))
        out = self.sync("--prova")
        self.assertNotIn("Omdömen", out)
        self.assertEqual(self.google.requests, [])
        self.account.refresh_from_db()
        self.assertTrue(self.account.google_reviews)

    def test_a_disabled_account_is_not_fetched(self):
        self.connected(google_reviews_fetched_at=timezone.now() - timedelta(days=8))
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=False)
        self.sync()
        self.assertEqual(self.google.requests, [])
