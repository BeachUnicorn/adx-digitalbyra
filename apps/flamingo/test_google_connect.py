"""
ADX:s inloggning hos Google (OAuth) på /manage/flamingo/google/ och kundens
Google Ads-konto: kopplingsförfrågan, nytt konto och läget från Google
(manage_google.py, google_accounts.py), kundkortets knappar och kundens sida
app/google/.

Inget här når nätet: urlopen byts mot FakeGoogle (test_google_ads.py), som
svarar med det testet köat och sparar varje anrop. Värdena är påhittade.
"""

import re
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import google_accounts, google_ads, manage_google
from .models import Campaign, FlamingoAccount, GoogleAdsConnection, Service
from .test_google_ads import (
    ACCESS,
    API,
    CLIENT_SECRET,
    CONFIGURED,
    DEV_TOKEN,
    NOTHING,
    REFRESH,
    TOKEN_OK,
    FakeGoogle,
    google_error,
    id_token,
)

User = get_user_model()

#: Inställt, men utan nyckeln i miljön: inloggningen härifrån gäller.
NO_ENV_TOKEN = {**CONFIGURED, "GOOGLE_ADS_REFRESH_TOKEN": ""}
MCC = "1234567890"
CLIENT = "9876543210"
CALLBACK = "http://testserver/manage/flamingo/google/tillbaka/"


def patch_http(fake):
    return mock.patch("apps.flamingo.google_ads.urlopen", fake)


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def path_of(request):
    return urlsplit(request.full_url).path


def search_rows(*rows, key):
    return (200, {"results": [{key: row} for row in rows]})


def link_rows(*links):
    return search_rows(*links, key="customerClientLink")


def customer_row(currency="SEK", tagging=True, status="ENABLED"):
    return search_rows(
        {
            "resourceName": f"customers/{CLIENT}",
            "id": CLIENT,
            "currencyCode": currency,
            "autoTaggingEnabled": tagging,
            "status": status,
        },
        key="customer",
    )


def billing_rows(*statuses):
    return search_rows(*({"status": status} for status in statuses), key="billingSetup")


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            "elin", password="x12345678", is_staff=True, first_name="Elin"
        )
        cls.acme = Customer.objects.create(name="Lindqvist Rör AB", email="info@ror.se")
        cls.anna = User.objects.create_user(
            "anna@ror.se", email="anna@ror.se", password="x", first_name="Anna", last_name="L"
        )
        cls.acme.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(
            customer=cls.acme,
            is_enabled=True,
            google_ads_customer_id="987-654-3210",
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN,
        )
        cls.fresh = Customer.objects.create(name="Nytt Konto AB")
        cls.fresh_account = FlamingoAccount.objects.create(
            customer=cls.fresh,
            is_enabled=True,
            google_status=FlamingoAccount.GOOGLE_REQUESTED_NEW,
        )
        cls.demo = Customer.objects.create(name="Demo Rör AB")
        cls.demo_account = FlamingoAccount.objects.create(
            customer=cls.demo,
            is_enabled=True,
            is_demo=True,
            google_ads_customer_id="555-666-7777",
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN,
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def staff_client(self):
        client = Client()
        client.force_login(self.staff)
        return client

    def card(self, customer=None):
        customer = customer or self.acme
        return self.staff_client().get(reverse("manage:customer_detail", args=[customer.pk]))


# ---------------------------------------------------------------------------
# Behörighet
# ---------------------------------------------------------------------------


class AccessTests(Base):
    def test_every_route_needs_staff(self):
        gets = [reverse("manage:flamingo_google"), reverse("manage:flamingo_google_callback")]
        posts = [
            reverse("manage:flamingo_google_connect"),
            reverse("manage:flamingo_google_test"),
            reverse("manage:flamingo_google_disconnect"),
            reverse("manage:flamingo_google_account", args=[self.acme.pk]),
        ]
        anna = Client()
        anna.force_login(self.anna)
        fake = FakeGoogle()
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, "ads@adx.se")
        with override_settings(**NO_ENV_TOKEN), patch_http(fake):
            for url in gets + posts:
                send = Client().get if url in gets else Client().post
                with self.subTest(url=url, who="anonym"):
                    response = send(url, {"action": "link", "state": "x", "code": "y"})
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("/manage/login/", response["Location"])
                with self.subTest(url=url, who="kontakt"):
                    send = anna.get if url in gets else anna.post
                    response = send(url, {"action": "link", "state": "x", "code": "y"})
                    self.assertEqual(response.status_code, 302)
                    self.assertNotIn("/manage/", response["Location"])
                    self.assertNotIn("accounts.google.com", response["Location"])
        self.assertEqual(fake.requests, [])
        connection = GoogleAdsConnection.get_solo()
        self.assertEqual(connection.refresh_token(), REFRESH)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.google_link_requested_at)

    def test_every_view_is_wrapped_in_staff_required(self):
        source = Path(manage_google.__file__).read_text()
        for name in (
            "google_page",
            "google_connect",
            "google_callback",
            "google_test",
            "google_disconnect",
            "google_account",
        ):
            with self.subTest(view=name):
                self.assertRegex(source, rf"@staff_required\n(@[^\n]+\n)*def {name}\(")

    def test_the_actions_only_take_post(self):
        client = self.staff_client()
        for url in (
            reverse("manage:flamingo_google_connect"),
            reverse("manage:flamingo_google_test"),
            reverse("manage:flamingo_google_disconnect"),
            reverse("manage:flamingo_google_account", args=[self.acme.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 405)


# ---------------------------------------------------------------------------
# Google-sidan
# ---------------------------------------------------------------------------


class PageTests(Base):
    @override_settings(**NOTHING)
    def test_missing_settings_are_listed_by_name(self):
        response = self.staff_client().get(reverse("manage:flamingo_google"))
        self.assertEqual(response.status_code, 200)
        for name in google_ads.REQUIRED_SETTINGS:
            self.assertContains(response, name)
        self.assertContains(response, "Inte inkopplat")
        self.assertContains(response, CALLBACK)
        # Ingen OAuth-klient: ingen knapp till Google.
        self.assertNotContains(response, reverse("manage:flamingo_google_connect"))

    @override_settings(**CONFIGURED)
    def test_values_are_never_shown_and_the_env_token_needs_no_button(self):
        response = self.staff_client().get(reverse("manage:flamingo_google"))
        html = response.content.decode()
        for secret in (DEV_TOKEN, CLIENT_SECRET, REFRESH, CONFIGURED["GOOGLE_ADS_CLIENT_ID"]):
            self.assertNotIn(secret, html)
        self.assertIn("Inkopplat", html)
        self.assertIn("Nyckeln i miljön (GOOGLE_ADS_REFRESH_TOKEN) används", html)
        self.assertNotIn(reverse("manage:flamingo_google_connect"), html)
        self.assertIn("123-456-7890", html)
        self.assertIn(CALLBACK, html)

    @override_settings(**NO_ENV_TOKEN)
    def test_a_stored_connection_shows_the_email_never_the_token(self):
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, "ads@adx.se", self.staff)
        response = self.staff_client().get(reverse("manage:flamingo_google"))
        html = response.content.decode()
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertIn("ads@adx.se", html)
        self.assertIn("Elin", html)
        self.assertNotIn(REFRESH, html)
        self.assertNotIn(row.refresh_token_encrypted, html)
        self.assertIn(reverse("manage:flamingo_google_connect"), html)
        self.assertIn(reverse("manage:flamingo_google_disconnect"), html)
        self.assertIn("Lindqvist Rör AB", html)

    @override_settings(**NOTHING)
    def test_the_overview_links_to_the_page(self):
        response = self.staff_client().get(reverse("manage:flamingo_overview"))
        self.assertContains(response, reverse("manage:flamingo_google"))
        self.assertContains(response, "Google Ads API: inte inkopplat, 4 inställningar saknas.")


# ---------------------------------------------------------------------------
# Inloggningen (OAuth)
# ---------------------------------------------------------------------------


GRANT = {
    "access_token": ACCESS,
    "expires_in": 3599,
    "refresh_token": REFRESH,
    "scope": "https://www.googleapis.com/auth/adwords openid email",
    "token_type": "Bearer",
    "id_token": id_token({"email": "ads@adx.se"}),
}


@override_settings(**NO_ENV_TOKEN)
class OAuthTests(Base):
    def start(self, client):
        response = client.post(reverse("manage:flamingo_google_connect"))
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response["Location"]).query)["state"][0]

    def callback(self, client, **params):
        return client.get(reverse("manage:flamingo_google_callback"), params)

    def test_start_sends_the_browser_to_google_with_a_random_state(self):
        client = self.staff_client()
        response = client.post(reverse("manage:flamingo_google_connect"))
        location = urlsplit(response["Location"])
        self.assertEqual(f"{location.scheme}://{location.netloc}", "https://accounts.google.com")
        query = parse_qs(location.query)
        self.assertEqual(query["redirect_uri"], [CALLBACK])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertIn("https://www.googleapis.com/auth/adwords", query["scope"][0])
        state = query["state"][0]
        self.assertGreaterEqual(len(state), 40)
        self.assertEqual(client.session[manage_google.STATE_KEY]["state"], state)
        self.assertNotEqual(self.start(client), state)

    @override_settings(GOOGLE_ADS_CLIENT_ID="", GOOGLE_ADS_CLIENT_SECRET="")
    def test_start_without_a_client_stays_here(self):
        response = self.staff_client().post(reverse("manage:flamingo_google_connect"))
        self.assertRedirects(
            response, reverse("manage:flamingo_google"), fetch_redirect_response=False
        )
        self.assertIn(google_ads.MSG_NO_CLIENT, texts(response))

    def test_success_stores_the_token_encrypted_and_never_shows_it(self):
        client = self.staff_client()
        state = self.start(client)
        fake = FakeGoogle((200, dict(GRANT)))
        with patch_http(fake):
            response = self.callback(client, state=state, code="4/kod-fran-google")
        self.assertRedirects(
            response, reverse("manage:flamingo_google"), fetch_redirect_response=False
        )
        self.assertEqual(len(fake.requests), 1)
        sent = fake.body(0)
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code"], "4/kod-fran-google")
        self.assertEqual(sent["redirect_uri"], CALLBACK)
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertTrue(row.is_connected)
        self.assertNotIn(REFRESH, row.refresh_token_encrypted)
        self.assertEqual(row.refresh_token(), REFRESH)
        self.assertEqual((row.google_email, row.connected_by), ("ads@adx.se", self.staff))
        messages_text = " ".join(texts(response))
        self.assertIn("ads@adx.se", messages_text)
        self.assertNotIn(REFRESH, messages_text)
        page = client.get(reverse("manage:flamingo_google")).content.decode()
        self.assertNotIn(REFRESH, page)
        self.assertNotIn(ACCESS, page)
        self.assertNotIn(row.refresh_token_encrypted, page)
        self.assertNotIn(manage_google.STATE_KEY, client.session)

    def test_a_state_can_only_be_used_once(self):
        client = self.staff_client()
        state = self.start(client)
        fake = FakeGoogle((200, dict(GRANT)))
        with patch_http(fake):
            self.callback(client, state=state, code="4/kod")
            GoogleAdsConnection.get_solo().clear()
            response = self.callback(client, state=state, code="4/kod")
        self.assertEqual(len(fake.requests), 1)
        self.assertFalse(GoogleAdsConnection.get_solo().is_connected)
        self.assertIn("redan använd", " ".join(texts(response)))

    def test_a_wrong_state_exchanges_nothing_and_burns_the_right_one(self):
        client = self.staff_client()
        state = self.start(client)
        fake = FakeGoogle()
        with patch_http(fake):
            response = self.callback(client, state=state[:-1] + "x", code="4/kod")
            self.assertIn("Inget ändrades", " ".join(texts(response)))
            self.callback(client, state=state, code="4/kod")
            # Utan start alls.
            self.callback(self.staff_client(), state=state, code="4/kod")
        self.assertEqual(fake.requests, [])
        self.assertFalse(
            GoogleAdsConnection.objects.filter(pk=1, refresh_token_encrypted__gt="").exists()
        )

    def test_an_old_state_is_refused(self):
        client = self.staff_client()
        state = self.start(client)
        session = client.session
        session[manage_google.STATE_KEY]["at"] -= manage_google.STATE_MAX_AGE + 5
        session.save()
        fake = FakeGoogle()
        with patch_http(fake):
            self.callback(client, state=state, code="4/kod")
        self.assertEqual(fake.requests, [])

    def test_access_denied_changes_nothing(self):
        GoogleAdsConnection.get_solo().set_refresh_token(
            "1//0gammal-nyckel-abcdefghij", "gammal@adx.se"
        )
        client = self.staff_client()
        state = self.start(client)
        fake = FakeGoogle()
        with patch_http(fake):
            response = self.callback(client, state=state, error="access_denied")
        self.assertEqual(fake.requests, [])
        self.assertIn("avbröts", " ".join(texts(response)))
        self.assertEqual(GoogleAdsConnection.get_solo().google_email, "gammal@adx.se")

    def test_a_rejected_code_shows_a_message_without_secrets(self):
        client = self.staff_client()
        state = self.start(client)
        fake = FakeGoogle((400, {"error": "invalid_grant", "error_description": "Bad Request"}))
        with patch_http(fake):
            response = self.callback(client, state=state, code="4/kod")
        self.assertIn(google_ads.MSG_CODE_REJECTED, texts(response))
        self.assertFalse(GoogleAdsConnection.get_solo().is_connected)

    def test_missing_adwords_scope_is_refused_and_revoked(self):
        client = self.staff_client()
        state = self.start(client)
        fake = FakeGoogle((200, {**GRANT, "scope": "openid email"}), (200, {}))
        with patch_http(fake):
            response = self.callback(client, state=state, code="4/kod")
        self.assertIn(google_ads.MSG_SCOPE_MISSING, texts(response))
        self.assertEqual(fake.requests[1].full_url, google_ads.REVOKE_URL)
        self.assertFalse(GoogleAdsConnection.get_solo().is_connected)


# ---------------------------------------------------------------------------
# Testa och koppla från
# ---------------------------------------------------------------------------


class TestAndDisconnectTests(Base):
    @override_settings(**CONFIGURED)
    def test_test_counts_accounts_and_finds_the_mcc(self):
        fake = FakeGoogle(
            TOKEN_OK, (200, {"resourceNames": [f"customers/{MCC}", f"customers/{CLIENT}"]})
        )
        with patch_http(fake):
            response = self.staff_client().post(reverse("manage:flamingo_google_test"))
        self.assertEqual(path_of(fake.requests[1]), f"/{API}/customers:listAccessibleCustomers")
        self.assertIsNone(fake.requests[1].get_header("Login-customer-id"))
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertIsNotNone(row.last_ok_at)
        self.assertEqual(row.last_error, "")
        text = " ".join(texts(response))
        self.assertIn("2 konton", text)
        self.assertIn("123-456-7890 är ett av dem", text)

    @override_settings(**CONFIGURED)
    def test_test_warns_when_the_mcc_is_not_reached(self):
        fake = FakeGoogle(TOKEN_OK, (200, {"resourceNames": [f"customers/{CLIENT}"]}))
        with patch_http(fake):
            self.staff_client().post(reverse("manage:flamingo_google_test"))
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertIsNotNone(row.last_ok_at)
        self.assertIn("Förvaltarkontot 123-456-7890 är inte bland dem", row.last_error)

    @override_settings(**CONFIGURED)
    def test_test_stores_the_error(self):
        fake = FakeGoogle((400, {"error": "invalid_grant", "error_description": "expired"}))
        with patch_http(fake):
            response = self.staff_client().post(reverse("manage:flamingo_google_test"))
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertEqual(row.last_error, google_ads.MSG_INVALID_GRANT)
        self.assertIsNone(row.last_ok_at)
        self.assertNotIn(REFRESH, " ".join(texts(response)))

    @override_settings(**NO_ENV_TOKEN)
    def test_disconnect_revokes_in_the_body_and_forgets(self):
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, "ads@adx.se")
        fake = FakeGoogle((200, {}))
        with patch_http(fake):
            response = self.staff_client().post(reverse("manage:flamingo_google_disconnect"))
        self.assertEqual(fake.requests[0].full_url, google_ads.REVOKE_URL)
        self.assertEqual(fake.body(0), {"token": REFRESH})
        self.assertNotIn(REFRESH, fake.requests[0].full_url)
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertFalse(row.is_connected)
        self.assertEqual(row.google_email, "")
        self.assertIn("återkallad hos Google", " ".join(texts(response)))

    @override_settings(**NO_ENV_TOKEN)
    def test_disconnect_forgets_even_when_google_says_no(self):
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, "ads@adx.se")
        fake = FakeGoogle((400, {"error": "invalid_token"}))
        with patch_http(fake):
            response = self.staff_client().post(reverse("manage:flamingo_google_disconnect"))
        self.assertFalse(GoogleAdsConnection.get_solo().is_connected)
        self.assertIn("bekräftade inte", " ".join(texts(response)))


# ---------------------------------------------------------------------------
# Kopplingsförfrågan, nytt konto och läget (google_accounts.py)
# ---------------------------------------------------------------------------


@override_settings(**CONFIGURED)
class RequestLinkTests(Base):
    def test_payload_and_pending_state(self):
        fake = FakeGoogle(
            TOKEN_OK,
            (200, {"result": {"resourceName": f"customers/{MCC}/customerClientLinks/{CLIENT}~1"}}),
        )
        with patch_http(fake):
            self.assertEqual(google_accounts.request_link(self.account), "pending")
        sent = fake.requests[1]
        self.assertEqual(path_of(sent), f"/{API}/customers/{MCC}/customerClientLinks:mutate")
        self.assertEqual(sent.get_header("Login-customer-id"), MCC)
        self.assertEqual(
            fake.body(1),
            {
                "operation": {
                    "create": {"clientCustomer": f"customers/{CLIENT}", "status": "PENDING"}
                }
            },
        )
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
        self.assertIsNotNone(self.account.google_link_requested_at)
        # Förfrågan sparas med id:t den gällde: bara då blir kontot kopplat.
        self.assertEqual(self.account.google_link_requested_for, "987-654-3210")
        self.assertIn("Åtkomst och säkerhet > Förvaltare", self.account.google_note)

    def test_already_invited_counts_as_pending(self):
        fake = FakeGoogle(
            TOKEN_OK,
            google_error(
                400, "INVALID_ARGUMENT", "managerLinkError", "ALREADY_INVITED_BY_THIS_MANAGER"
            ),
        )
        with patch_http(fake):
            self.assertEqual(google_accounts.request_link(self.account), "pending")
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.google_link_requested_at)

    def test_already_managed_is_not_linked_without_staff(self):
        # Att kontot redan ligger under ADX förvaltarkonto visar inte att det
        # är den här kundens: byrån kontrollerar och bockar av för hand.
        for code in ("ALREADY_MANAGED_BY_THIS_MANAGER", "ALREADY_MANAGED_IN_HIERARCHY"):
            with self.subTest(code=code):
                cache.clear()
                fake = FakeGoogle(
                    TOKEN_OK, google_error(400, "INVALID_ARGUMENT", "managerLinkError", code)
                )
                with patch_http(fake):
                    self.assertEqual(google_accounts.request_link(self.account), "managed")
                self.account.refresh_from_db()
                self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
                self.assertIsNone(self.account.google_link_requested_at)
                self.assertEqual(self.account.google_link_requested_for, "")

    def test_already_managed_on_the_card_asks_staff_to_check(self):
        fake = FakeGoogle(
            TOKEN_OK,
            google_error(
                400, "INVALID_ARGUMENT", "managerLinkError", "ALREADY_MANAGED_BY_THIS_MANAGER"
            ),
        )
        with patch_http(fake):
            response = self.staff_client().post(
                reverse("manage:flamingo_google_account", args=[self.acme.pk]), {"action": "link"}
            )
        text = " ".join(texts(response))
        self.assertIn("Kontrollera att kontot är kundens", text)
        self.assertEqual(len(fake.requests), 2)
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)

    def test_an_id_another_account_shares_is_never_sent(self):
        fake = FakeGoogle()
        with (
            mock.patch.object(
                FlamingoAccount, "google_id_shared", new_callable=mock.PropertyMock
            ) as shared,
            patch_http(fake),
            self.assertRaises(google_ads.GoogleAdsError) as caught,
        ):
            shared.return_value = True
            google_accounts.request_link(self.account)
        self.assertEqual(caught.exception.status, "ID_TAKEN")
        self.assertEqual(fake.requests, [])

    def test_an_id_changed_during_the_call_saves_nothing(self):
        def respond(request, timeout=None):
            if "customerClientLinks" in request.full_url:
                FlamingoAccount.objects.filter(pk=self.account.pk).update(
                    google_ads_customer_id="555-444-3333"
                )
            return fake_inner(request, timeout)

        fake_inner = FakeGoogle(TOKEN_OK, (200, {"result": {"resourceName": "x"}}))
        with patch_http(respond), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_accounts.request_link(self.account)
        self.assertEqual(caught.exception.status, "ID_CHANGED")
        row = FlamingoAccount.objects.get(pk=self.account.pk)
        self.assertEqual(row.google_ads_customer_id, "555-444-3333")
        self.assertIsNone(row.google_link_requested_at)
        self.assertEqual(row.google_link_requested_for, "")

    def test_other_errors_are_raised_and_change_nothing(self):
        fake = FakeGoogle(
            TOKEN_OK, google_error(400, "INVALID_ARGUMENT", "managerLinkError", "TOO_MANY_MANAGERS")
        )
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError):
            google_accounts.request_link(self.account)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.google_link_requested_at)

    def test_without_an_id_nothing_is_sent(self):
        fake = FakeGoogle()
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError):
            google_accounts.request_link(self.fresh_account)
        self.assertEqual(fake.requests, [])

    def test_the_button_on_the_card(self):
        fake = FakeGoogle(TOKEN_OK, (200, {"result": {"resourceName": "x"}}))
        with patch_http(fake):
            response = self.staff_client().post(
                reverse("manage:flamingo_google_account", args=[self.acme.pk]), {"action": "link"}
            )
        self.assertRedirects(
            response,
            reverse("manage:customer_detail", args=[self.acme.pk]) + "#flamingo-google",
            fetch_redirect_response=False,
        )
        self.assertIn("Kopplingsförfrågan är skickad", " ".join(texts(response)))
        self.assertEqual(mail.outbox, [])


@override_settings(**CONFIGURED)
class CreateAccountTests(Base):
    def post(self, **data):
        return self.staff_client().post(
            reverse("manage:flamingo_google_account", args=[self.fresh.pk]),
            {"action": "create", **data},
        )

    def test_payload_without_invite(self):
        fake = FakeGoogle(TOKEN_OK, (200, {"resourceName": "customers/5556667777"}))
        with patch_http(fake):
            new_id = google_accounts.create_client_account(self.fresh_account)
        self.assertEqual(new_id, "555-666-7777")
        sent = fake.requests[1]
        self.assertEqual(path_of(sent), f"/{API}/customers/{MCC}:createCustomerClient")
        self.assertEqual(sent.get_header("Login-customer-id"), MCC)
        self.assertEqual(
            fake.body(1),
            {
                "customerClient": {
                    "descriptiveName": "Nytt Konto AB (ADX Flamingo)",
                    "currencyCode": "SEK",
                    "timeZone": "Europe/Stockholm",
                }
            },
        )
        self.fresh_account.refresh_from_db()
        self.assertEqual(self.fresh_account.google_ads_customer_id, "555-666-7777")
        self.assertEqual(self.fresh_account.google_status, FlamingoAccount.GOOGLE_LINKED)

    def test_an_account_with_an_id_gets_no_new_one(self):
        fake = FakeGoogle()
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_accounts.create_client_account(self.account)
        self.assertEqual(caught.exception.status, "ALREADY_HAS_ACCOUNT")
        self.assertEqual(fake.requests, [])

    def test_a_linked_account_without_an_id_gets_no_new_one(self):
        FlamingoAccount.objects.filter(pk=self.fresh_account.pk).update(
            google_status=FlamingoAccount.GOOGLE_LINKED
        )
        self.fresh_account.refresh_from_db()
        fake = FakeGoogle()
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError):
            google_accounts.create_client_account(self.fresh_account)
        self.assertEqual(fake.requests, [])
        html = self.card(self.fresh).content.decode()
        self.assertNotIn("Skapa ett konto åt kunden", html)
        self.assertIn("Bockat som kopplat men utan konto-id", html)

    def test_a_second_click_creates_nothing(self):
        fake = FakeGoogle(TOKEN_OK, (200, {"resourceName": "customers/5556667777"}))
        with patch_http(fake):
            self.post()
            self.post()
        self.assertEqual(len(fake.requests), 2)

    def test_the_email_is_not_sent_unless_the_box_is_ticked(self):
        fake = FakeGoogle(TOKEN_OK, (200, {"resourceName": "customers/5556667777"}))
        with patch_http(fake):
            response = self.post(invite_email="anna@ror.se")
        body = fake.body(1)
        self.assertNotIn("emailAddress", body)
        self.assertNotIn("accessRole", body)
        self.assertIn("Ingen inbjudan skickades", " ".join(texts(response)))

    @override_settings(GOOGLE_ADS_INVITE_ON_CREATE=True)
    def test_a_ticked_box_invites_a_contact(self):
        self.fresh.users.add(self.anna)
        fake = FakeGoogle(TOKEN_OK, (200, {"resourceName": "customers/5556667777"}))
        with patch_http(fake):
            response = self.post(invite="1", invite_email="anna@ror.se")
        body = fake.body(1)
        self.assertEqual(body["emailAddress"], "anna@ror.se")
        self.assertEqual(body["accessRole"], "ADMIN")
        self.assertIn("Google tog emot inbjudan till anna@ror.se", " ".join(texts(response)))
        self.fresh_account.refresh_from_db()
        self.assertIn("anna@ror.se", self.fresh_account.google_note)
        self.assertEqual(mail.outbox, [])

    def test_without_the_allow_list_no_invite_is_sent(self):
        # emailAddress och accessRole är bara för Googles tillåtelselista
        # (v25 customer_service.proto): utan den skickas ingen inbjudan.
        self.fresh.users.add(self.anna)
        fake = FakeGoogle(TOKEN_OK, (200, {"resourceName": "customers/5556667777"}))
        with patch_http(fake):
            response = self.post(invite="1", invite_email="anna@ror.se")
        body = fake.body(1)
        self.assertNotIn("emailAddress", body)
        self.assertNotIn("accessRole", body)
        self.assertIn("Ingen inbjudan skickades", " ".join(texts(response)))
        self.fresh_account.refresh_from_db()
        self.assertEqual(self.fresh_account.google_note, google_accounts.NOTE_CREATED)
        with patch_http(FakeGoogle()), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_accounts.create_client_account(self.account, invite_email="anna@ror.se")
        self.assertEqual(caught.exception.status, "INVITE_OFF")

    def test_the_card_has_no_invite_box_without_the_allow_list(self):
        self.fresh.users.add(self.anna)
        html = self.card(self.fresh).content.decode()
        self.assertIn("Skapa ett konto åt kunden", html)
        self.assertNotIn('name="invite"', html)
        self.assertIn("Kontot skapas utan inbjudan", html)
        self.assertIn("Google mejlar kunden när du gör det", html)

    def test_explorer_access_cannot_create_accounts(self):
        fake = FakeGoogle(
            TOKEN_OK,
            google_error(403, "PERMISSION_DENIED", "authorizationError", "ACTION_NOT_PERMITTED"),
        )
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_accounts.create_client_account(self.fresh_account)
        self.assertIn("Basic", caught.exception.message)
        self.assertIn("Google Ads API Overview", caught.exception.message)
        self.fresh_account.refresh_from_db()
        self.assertEqual(self.fresh_account.google_ads_customer_id, "")

    @override_settings(GOOGLE_ADS_INVITE_ON_CREATE=True)
    def test_only_the_customers_own_addresses_can_be_invited(self):
        fake = FakeGoogle()
        with patch_http(fake):
            response = self.post(invite="1", invite_email="nagon@annan.se")
        self.assertEqual(fake.requests, [])
        self.assertIn("Inget konto skapades", " ".join(texts(response)))
        self.fresh_account.refresh_from_db()
        self.assertEqual(self.fresh_account.google_ads_customer_id, "")

    @override_settings(GOOGLE_ADS_INVITE_ON_CREATE=True)
    def test_the_card_has_the_invite_box_unticked(self):
        self.fresh.users.add(self.anna)
        html = self.card(self.fresh).content.decode()
        self.assertIn("Skapa ett konto åt kunden", html)
        box = re.search(r'<input type="checkbox" name="invite"[^>]*>', html).group(0)
        self.assertNotIn("checked", box)
        self.assertIn("Google mejlar en inbjudan till adressen", html)
        self.assertIn('<option value="anna@ror.se">Anna L (anna@ror.se)</option>', html)


@override_settings(**CONFIGURED)
class SyncTests(Base):
    def sync(self, *responses, account=None, failed=False):
        account = account or self.account
        fake = FakeGoogle(TOKEN_OK, *responses)
        with patch_http(fake):
            result = google_accounts.sync_account_status(account)
        if failed:
            self.assertIsInstance(result, google_ads.GoogleAdsError)
        else:
            self.assertIsNone(result)
        account.refresh_from_db()
        return fake

    def requested(self, google_id="987-654-3210"):
        """ADX skickade kopplingsförfrågan från kontot till google_id."""
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_link_requested_at=timezone.now() - timedelta(hours=2),
            google_link_requested_for=google_id,
        )
        self.account.refresh_from_db()

    def link(self, status, client=CLIENT, link_id="11"):
        return {
            "resourceName": f"customers/{MCC}/customerClientLinks/{client}~{link_id}",
            "clientCustomer": f"customers/{client}",
            "status": status,
            "managerLinkId": link_id,
        }

    def test_active_with_approved_billing_is_ready(self):
        self.requested()
        fake = self.sync(
            link_rows(self.link("ACTIVE"), self.link("PENDING", client="1111111111")),
            customer_row(),
            billing_rows("CANCELLED", "APPROVED"),
        )
        self.assertEqual(path_of(fake.requests[1]), f"/{API}/customers/{MCC}/googleAds:search")
        self.assertIn("FROM customer_client_link", fake.body(1)["query"])
        self.assertEqual(path_of(fake.requests[2]), f"/{API}/customers/{CLIENT}/googleAds:search")
        self.assertEqual(fake.requests[2].get_header("Login-customer-id"), MCC)
        self.assertIn("FROM billing_setup", fake.body(3)["query"])
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_BILLING_OK)
        self.assertEqual(self.account.google_billing_status, "APPROVED")
        self.assertTrue(self.account.google_auto_tagging)
        self.assertIsNotNone(self.account.google_synced_at)
        self.assertEqual(self.account.google_sync_error, "")
        # Förfrågan är godkänd: den väntar inte längre på kunden.
        self.assertIsNone(self.account.google_link_requested_at)
        self.assertEqual(self.account.google_link_requested_for, "")
        self.assertFalse(self.account.google_waiting_on_customer)

    def test_active_without_a_request_from_this_account_is_not_linked(self):
        # Kunden skrev ett id som redan ligger under ADX förvaltarkonto (en
        # annan kunds konto, eller ett som ADX förvaltar utanför Flamingo).
        fake = self.sync(link_rows(self.link("ACTIVE")))
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
        self.assertEqual(self.account.google_sync_error, google_accounts.MSG_ACTIVE_UNVERIFIED)
        self.assertEqual(self.account.google_billing_status, "")
        # Kundens konto lästes aldrig (betalning, taggning).
        self.assertEqual(len(fake.requests), 2)

    def test_active_after_a_request_for_another_id_is_not_linked(self):
        self.requested(google_id="111-222-3333")
        self.sync(link_rows(self.link("ACTIVE")))
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
        self.assertIn("markera det som kopplat för hand", self.account.google_sync_error)

    def test_an_id_shared_with_another_account_is_never_linked(self):
        self.requested()
        with mock.patch.object(
            FlamingoAccount, "google_id_shared", new_callable=mock.PropertyMock
        ) as shared:
            shared.return_value = True
            self.sync(link_rows(self.link("ACTIVE")))
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)

    def test_an_id_changed_during_the_read_gets_nothing(self):
        self.requested()

        def change_then_active(client):
            FlamingoAccount.objects.filter(pk=self.account.pk).update(
                google_ads_customer_id="555-444-3333"
            )
            return google_accounts.LINK_ACTIVE

        with (
            mock.patch.object(google_accounts, "link_status", change_then_active),
            mock.patch.object(
                google_accounts,
                "read_client",
                return_value={
                    "auto_tagging": True,
                    "currency": "SEK",
                    "status": "ENABLED",
                    "billing": "APPROVED",
                },
            ),
            override_settings(**CONFIGURED),
        ):
            self.assertIsNone(google_accounts.sync_account_status(self.account))
        row = FlamingoAccount.objects.get(pk=self.account.pk)
        self.assertEqual(row.google_ads_customer_id, "555-444-3333")
        self.assertEqual(row.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
        self.assertEqual(row.google_billing_status, "")

    def test_a_pending_link_sent_from_google_ads_is_the_customers_turn(self):
        self.assertIsNone(self.account.google_link_requested_at)
        self.sync(link_rows(self.link("PENDING")))
        self.assertIsNotNone(self.account.google_link_requested_at)
        self.assertTrue(self.account.google_waiting_on_customer)
        self.assertFalse(self.account.google_waiting_on_adx)
        # Det räknas inte som ADX förfrågan för det här id:t.
        self.assertEqual(self.account.google_link_requested_for, "")

    def test_an_answered_request_is_no_longer_waiting(self):
        for status in ("REFUSED", "CANCELED", "INACTIVE"):
            with self.subTest(status=status):
                self.requested()
                cache.clear()
                self.sync(link_rows(self.link(status)))
                self.assertIsNone(self.account.google_link_requested_at)
                self.assertEqual(self.account.google_link_requested_for, "")
                self.assertFalse(self.account.google_waiting_on_customer)

    def test_an_auth_error_is_raised_after_it_is_saved(self):
        fake = FakeGoogle(
            TOKEN_OK,
            google_error(401, "UNAUTHENTICATED", "authenticationError", "OAUTH_TOKEN_REVOKED"),
            TOKEN_OK,
            google_error(401, "UNAUTHENTICATED", "authenticationError", "OAUTH_TOKEN_REVOKED"),
        )
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_accounts.sync_account_status(self.account)
        self.assertTrue(caught.exception.is_auth_error)
        self.account.refresh_from_db()
        self.assertIn("Koppla ADX:s Google-konto igen", self.account.google_sync_error)

    def test_active_without_billing_is_linked(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_BILLING_OK, google_note=google_accounts.NOTE_LINK
        )
        self.account.refresh_from_db()
        self.sync(link_rows(self.link("ACTIVE")), customer_row(tagging=False), billing_rows())
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_LINKED)
        self.assertEqual(self.account.google_billing_status, google_accounts.BILLING_NONE)
        self.assertFalse(self.account.google_auto_tagging)
        self.assertEqual(self.account.google_note, "")

    def test_pending_stays_id_given(self):
        self.sync(link_rows(self.link("PENDING")))
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
        self.assertEqual(self.account.google_note, google_accounts.NOTE_LINK)
        self.assertIsNotNone(self.account.google_synced_at)

    def test_refused_and_canceled_leave_a_note(self):
        for status, note in (
            ("REFUSED", google_accounts.NOTE_REFUSED),
            ("CANCELED", google_accounts.NOTE_CANCELED),
            ("INACTIVE", google_accounts.NOTE_INACTIVE),
        ):
            with self.subTest(status=status):
                FlamingoAccount.objects.filter(pk=self.account.pk).update(
                    google_status=FlamingoAccount.GOOGLE_LINKED, google_note=""
                )
                self.account.refresh_from_db()
                cache.clear()
                self.sync(link_rows(self.link(status)))
                self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
                self.assertEqual(self.account.google_note, note)

    def test_a_note_from_adx_is_kept(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_note="Ringde Anna i dag.")
        self.account.refresh_from_db()
        self.sync(link_rows(self.link("REFUSED")))
        self.assertEqual(self.account.google_note, "Ringde Anna i dag.")

    def test_the_newest_ended_link_wins_and_active_beats_all(self):
        self.sync(link_rows(self.link("REFUSED", link_id="5"), self.link("CANCELED", link_id="9")))
        self.assertEqual(self.account.google_note, google_accounts.NOTE_CANCELED)

    def test_another_currency_is_a_warning(self):
        self.requested()
        self.sync(link_rows(self.link("ACTIVE")), customer_row(currency="EUR"), billing_rows())
        self.assertIn("valuta är EUR", self.account.google_sync_error)
        self.assertIsNotNone(self.account.google_synced_at)

    def test_an_error_is_stored_and_the_status_stays(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_BILLING_OK
        )
        self.account.refresh_from_db()
        self.sync(
            google_error(403, "PERMISSION_DENIED", "authorizationError", "USER_PERMISSION_DENIED"),
            failed=True,
        )
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_BILLING_OK)
        self.assertIn("når inte det här Google Ads-kontot", self.account.google_sync_error)
        self.assertIsNone(self.account.google_synced_at)

    @override_settings(**NOTHING)
    def test_without_the_api_the_manual_status_is_kept(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_BILLING_OK
        )
        self.account.refresh_from_db()
        fake = FakeGoogle()
        with patch_http(fake):
            google_accounts.sync_account_status(self.account)
        self.assertEqual(fake.requests, [])
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_status, FlamingoAccount.GOOGLE_BILLING_OK)
        self.assertIsNone(self.account.google_synced_at)

    def test_the_button_on_the_card(self):
        fake = FakeGoogle(TOKEN_OK, link_rows(self.link("PENDING")))
        with patch_http(fake):
            response = self.staff_client().post(
                reverse("manage:flamingo_google_account", args=[self.acme.pk]), {"action": "sync"}
            )
        self.assertIn("Läst från Google: Konto-id angivet.", texts(response))


@override_settings(**CONFIGURED)
class DemoTests(Base):
    def test_demo_accounts_never_call_google(self):
        fake = FakeGoogle()
        with patch_http(fake):
            self.assertIsNone(google_accounts.request_link(self.demo_account))
            self.assertIsNone(google_accounts.create_client_account(self.demo_account))
            self.assertIsNone(google_accounts.sync_account_status(self.demo_account))
            for action in ("link", "create", "sync"):
                response = self.staff_client().post(
                    reverse("manage:flamingo_google_account", args=[self.demo.pk]),
                    {"action": action, "invite": "1", "invite_email": "info@ror.se"},
                )
                self.assertIn(google_ads.MSG_DEMO, texts(response))
        self.assertEqual(fake.requests, [])
        self.demo_account.refresh_from_db()
        self.assertEqual(self.demo_account.google_status, FlamingoAccount.GOOGLE_ID_GIVEN)
        self.assertIsNone(self.demo_account.google_link_requested_at)

    def test_the_demo_card_shows_only_the_manual_form(self):
        html = self.card(self.demo).content.decode()
        self.assertIn("Demokontot pratar aldrig med Google", html)
        self.assertNotIn("Skicka kopplingsförfrågan", html)
        self.assertIn(reverse("manage:flamingo_google_update", args=[self.demo.pk]), html)


# ---------------------------------------------------------------------------
# Kundkortet
# ---------------------------------------------------------------------------


class CardTests(Base):
    @override_settings(**CONFIGURED)
    def test_with_the_api_the_card_has_the_buttons_and_the_manual_form(self):
        html = self.card().content.decode()
        self.assertIn("Skicka kopplingsförfrågan", html)
        self.assertIn("Google kan mejla kontots administratörer", html)
        self.assertIn("Hämta läget från Google", html)
        self.assertIn("Ändra för hand", html)
        self.assertIn(reverse("manage:flamingo_google_update", args=[self.acme.pk]), html)
        self.assertNotIn("Skapa ett konto åt kunden", html)

    @override_settings(**NOTHING)
    def test_without_the_api_only_the_manual_form(self):
        html = self.card().content.decode()
        self.assertNotIn("Skicka kopplingsförfrågan", html)
        self.assertNotIn("Ändra för hand", html)
        self.assertIn(reverse("manage:flamingo_google_update", args=[self.acme.pk]), html)
        self.assertIn(reverse("manage:flamingo_google"), html)

    @override_settings(**NOTHING)
    def test_the_api_buttons_refuse_without_the_api(self):
        fake = FakeGoogle()
        with patch_http(fake):
            response = self.staff_client().post(
                reverse("manage:flamingo_google_account", args=[self.acme.pk]), {"action": "link"}
            )
        self.assertEqual(fake.requests, [])
        self.assertIn("inte inkopplat", " ".join(texts(response)))

    def test_a_new_id_by_hand_forgets_the_old_account(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_billing_status="APPROVED",
            google_auto_tagging=True,
            google_synced_at=timezone.now(),
            google_link_requested_at=timezone.now(),
            google_conversion_actions={"deal": f"customers/{CLIENT}/conversionActions/1"},
        )
        url = reverse("manage:flamingo_google_update", args=[self.acme.pk])
        client = self.staff_client()
        client.post(url, {"google_status": "id_given", "google_ads_customer_id": "987-654-3210"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_billing_status, "APPROVED")
        client.post(url, {"google_status": "id_given", "google_ads_customer_id": "111-222-3333"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_billing_status, "")
        self.assertIsNone(self.account.google_auto_tagging)
        self.assertIsNone(self.account.google_synced_at)
        self.assertIsNone(self.account.google_link_requested_at)
        self.assertEqual(self.account.google_conversion_actions, {})


# ---------------------------------------------------------------------------
# Kundens sida, app/google/
# ---------------------------------------------------------------------------


class CustomerPageTests(Base):
    url = reverse("flamingo:app_google")

    def customer(self):
        client = Client()
        client.force_login(self.anna)
        return client

    def test_a_sent_invitation_says_where_to_accept(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_link_requested_at=timezone.now() - timedelta(hours=1),
            google_note=google_accounts.NOTE_LINK,
        )
        html = self.customer().get(self.url).content.decode()
        self.assertIn("Förfrågan skickad: godkänn ADX:s förfrågan i Google Ads under", html)
        self.assertNotIn("Inbjudan skickad", html)
        self.assertIn("Administratör &gt; Åtkomst och säkerhet &gt; Förvaltare", html)
        self.assertIn("Godkänn ADX:s förfrågan i Google Ads", html)
        # Förfrågans notering upprepas inte som "Från ADX".
        self.assertNotIn("Från ADX:", html)

    def test_linked_without_billing_does_not_block(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_LINKED, google_billing_status="NONE"
        )
        html = self.customer().get(self.url).content.decode()
        self.assertIn("Betalning saknas: annonserna visas först när betalningen är inlagd", html)
        self.assertIn("kan gå live ändå", html)
        self.assertIn("Skapa första kampanjen", html)
        self.assertIn("ads.google.com", html)

    def test_ready(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_BILLING_OK
        )
        html = self.customer().get(self.url).content.decode()
        self.assertIn("Kopplat, och betalningen är klar.", html)
        self.assertNotIn("Öppna Googles betalning", html)

    def test_staff_in_view_as_has_the_customers_form(self):
        """Giovanni 2026-10-03: "visa som kund" ska visa det kunden ser. Byrån
        i kundvyn har kundens formulär, och det byrån sparar gäller."""
        client = self.staff_client()
        session = client.session
        session[VIEW_AS_KEY] = self.acme.pk
        session.save()
        response = client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "gäller på riktigt")

    def test_a_refused_request_does_not_ask_the_customer_to_approve(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_link_requested_at=timezone.now() - timedelta(days=1),
            google_link_requested_for="987-654-3210",
        )
        fake = FakeGoogle(
            TOKEN_OK,
            link_rows(
                {
                    "clientCustomer": f"customers/{CLIENT}",
                    "status": "REFUSED",
                    "managerLinkId": "4",
                }
            ),
        )
        with override_settings(**CONFIGURED), patch_http(fake):
            google_accounts.sync_account_status(self.account)
        html = self.customer().get(self.url).content.decode()
        self.assertIn("nekades", html)
        self.assertNotIn("Förfrågan skickad", html)
        self.assertNotIn("Godkänn ADX:s förfrågan i Google Ads", html)

    def test_another_customers_id_is_refused(self):
        # Kund B är kopplad med 999-888-7777. Anna (kund A) skriver samma id:
        # det tas aldrig emot, så ingen synk kan göra A kopplad till B:s konto.
        other = Customer.objects.create(name="Annan Kund AB")
        FlamingoAccount.objects.create(
            customer=other,
            is_enabled=True,
            google_ads_customer_id="999-888-7777",
            google_status=FlamingoAccount.GOOGLE_BILLING_OK,
        )
        response = self.customer().post(
            self.url, {"action": "id", "google_ads_customer_id": "999 888 7777"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Det id:t går inte att använda här")
        self.assertNotContains(response, "Annan Kund AB")
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_ads_customer_id, "987-654-3210")

    def test_the_database_refuses_a_shared_id_too(self):
        from django.db import IntegrityError, transaction

        other = Customer.objects.create(name="Annan Kund AB")
        with self.assertRaises(IntegrityError), transaction.atomic():
            FlamingoAccount.objects.create(customer=other, google_ads_customer_id="987-654-3210")
        # Demokontots påhittade id räknas inte.
        demo = Customer.objects.create(name="Demo två")
        FlamingoAccount.objects.create(
            customer=demo, is_demo=True, google_ads_customer_id="987-654-3210"
        )

    def test_a_race_for_the_same_id_is_caught(self):
        with mock.patch("apps.flamingo.app_views.onboarding.google_id_taken", return_value=False):
            other = Customer.objects.create(name="Annan Kund AB")
            FlamingoAccount.objects.create(customer=other, google_ads_customer_id="999-888-7777")
            response = self.customer().post(
                self.url, {"action": "id", "google_ads_customer_id": "999-888-7777"}
            )
        self.assertContains(response, "Det id:t går inte att använda här")
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_ads_customer_id, "987-654-3210")

    def test_a_new_id_clears_old_google_errors(self):
        service = Service.objects.create(account=self.account, name="Rörjour")
        campaign = Campaign.objects.create(
            account=self.account,
            service=service,
            name="Rörjour Nacka",
            status=Campaign.STATUS_NEEDS_CUSTOMER,
            approved_at=timezone.now(),
            google_error="Kundens Google Ads-konto har valutan EUR, inte SEK.",
        )
        self.customer().post(self.url, {"action": "id", "google_ads_customer_id": "111 222 3333"})
        campaign.refresh_from_db()
        self.assertEqual(campaign.google_error, "")

    def test_a_new_id_from_the_customer_forgets_the_old_account(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_link_requested_at=timezone.now(), google_billing_status="NONE"
        )
        self.customer().post(self.url, {"action": "id", "google_ads_customer_id": "111 222 3333"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_ads_customer_id, "111-222-3333")
        self.assertIsNone(self.account.google_link_requested_at)
        self.assertEqual(self.account.google_billing_status, "")
