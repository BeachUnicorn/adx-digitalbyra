"""
Grunden för Google Ads API: kopplingen (krypterad nyckel), inställningarna,
inloggningen, anropen och felen (google_ads.py), och modellerna som de andra
delarna bygger på.

Inget här når nätet: urlopen byts mot FakeGoogle, som svarar med det testet
köat och sparar varje anrop. Värdena nedan är påhittade testvärden.

FakeGoogle och CONFIGURED går att återanvända i andra testfiler:

    with override_settings(**CONFIGURED), mock.patch(
        "apps.flamingo.google_ads.urlopen", FakeGoogle(TOKEN_OK, (200, {...}))
    ):
        ...
"""

import base64
import importlib
import io
import json
import logging
from datetime import date, datetime
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from apps.common import sentry
from apps.projects.models import Customer

from . import google_ads
from .models import (
    Campaign,
    CampaignDayStats,
    ConversionUpload,
    FlamingoAccount,
    GoogleAdsConnection,
    Lead,
    Service,
    decrypt_secret,
    encrypt_secret,
)

REFRESH = "1//0testrefresh-abcdefghijklmnop"
ACCESS = "ya29.testaccess-abcdefghijklmnop"
ACCESS_2 = "ya29.testaccess-second-token"
CLIENT_SECRET = "GOCSPX-testhemlighet-123456"
DEV_TOKEN = "testdevtoken-ABCDEF123456"

#: Den version Google Ads API har 2026-10-03 (v25.2). v26 finns inte: varje
#: adress i testerna byggs på den här, så en version som inte finns syns.
API = "v25"

#: Utvecklartoken är valfri (Google bortser från den sedan 2026-09-09) men
#: satt här, så att testerna ser att den aldrig läcker.
CONFIGURED = {
    "GOOGLE_ADS_DEVELOPER_TOKEN": DEV_TOKEN,
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "123-456-7890",
    "GOOGLE_ADS_CLIENT_ID": "test-klient.apps.googleusercontent.com",
    "GOOGLE_ADS_CLIENT_SECRET": CLIENT_SECRET,
    "GOOGLE_ADS_REFRESH_TOKEN": REFRESH,
    "GOOGLE_ADS_API_VERSION": "",
}
NOTHING = {name: "" for name in CONFIGURED}

TOKEN_OK = (200, {"access_token": ACCESS, "expires_in": 3599, "token_type": "Bearer"})


class _Response:
    def __init__(self, status, raw):
        self.status = status
        self._raw = raw

    def read(self, size=-1):
        return self._raw if size is None or size < 0 else self._raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGoogle:
    """Står i urlopens ställe. Svaren är (status, dict eller bytes) eller ett
    undantag att kasta; requests är urllib-anropen i ordning."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"Oväntat anrop till {request.full_url}")
        status, body = self.responses.pop(0)
        if isinstance(status, BaseException):
            raise status
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        if status >= 400:
            raise HTTPError(request.full_url, status, "fel", {}, io.BytesIO(raw))
        return _Response(status, raw)

    def body(self, n):
        """Anrop n:s kropp som dict (JSON) eller {nyckel: värde} (formulär)."""
        raw = (self.requests[n].data or b"").decode()
        try:
            return json.loads(raw)
        except ValueError:
            return {key: values[0] for key, values in parse_qs(raw).items()}


def google_error(http, status, code_group, code, message="Fel.", request_id="req-1", **extra):
    """Ett fel som Google Ads skickar det (REST)."""
    item = {"errorCode": {code_group: code}, "message": message, **extra}
    return (
        http,
        {
            "error": {
                "code": http,
                "message": "The caller does not have permission",
                "status": status,
                "details": [
                    {
                        "@type": (
                            f"type.googleapis.com/google.ads.googleads.{API}.errors.GoogleAdsFailure"
                        ),
                        "errors": [item],
                        "requestId": request_id,
                    }
                ],
            }
        },
    )


def id_token(claims):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signatur"


def patch_http(fake):
    return mock.patch("apps.flamingo.google_ads.urlopen", fake)


class GoogleAdsBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


# ---------------------------------------------------------------------------
# Kopplingen och krypteringen
# ---------------------------------------------------------------------------


class ConnectionTests(GoogleAdsBase):
    def test_refresh_token_is_encrypted_and_reads_back(self):
        user = get_user_model().objects.create_user("byra", password="x")
        connection = GoogleAdsConnection.get_solo()
        connection.set_refresh_token(REFRESH, "ads@adx.se", user)
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertNotIn(REFRESH, row.refresh_token_encrypted)
        self.assertNotIn("testrefresh", row.refresh_token_encrypted)
        self.assertEqual(row.refresh_token(), REFRESH)
        self.assertEqual((row.google_email, row.connected_by), ("ads@adx.se", user))
        self.assertIsNotNone(row.connected_at)
        self.assertTrue(row.is_connected)
        self.assertFalse(row.token_unreadable)
        self.assertEqual(GoogleAdsConnection.get_solo().pk, 1)
        self.assertEqual(GoogleAdsConnection.objects.count(), 1)

    def test_a_new_secret_key_makes_the_token_unreadable(self):
        connection = GoogleAdsConnection.get_solo().set_refresh_token(REFRESH)
        with override_settings(SECRET_KEY="en-helt-annan-nyckel-for-testet"):
            with self.assertLogs("apps.flamingo.models", logging.WARNING) as logs:
                self.assertEqual(connection.refresh_token(), "")
                self.assertTrue(connection.token_unreadable)
        self.assertNotIn(REFRESH, "\n".join(logs.output))
        self.assertEqual(connection.refresh_token(), REFRESH)

    def test_flamingo_token_key_wins_over_secret_key(self):
        from cryptography.fernet import Fernet

        fernet_key = Fernet.generate_key().decode()
        with override_settings(FLAMINGO_TOKEN_KEY=fernet_key):
            secret = encrypt_secret(REFRESH)
            self.assertEqual(decrypt_secret(secret), REFRESH)
            self.assertEqual(Fernet(fernet_key.encode()).decrypt(secret.encode()).decode(), REFRESH)
        self.assertEqual(decrypt_secret(secret), "")
        with override_settings(FLAMINGO_TOKEN_KEY="inte en fernet-nyckel men lang och hemlig"):
            secret = encrypt_secret(REFRESH)
            self.assertEqual(decrypt_secret(secret), REFRESH)
        self.assertEqual(decrypt_secret("skräp"), "")
        self.assertEqual(decrypt_secret(""), "")

    def test_clear_forgets_everything(self):
        connection = GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, "ads@adx.se")
        connection.clear()
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertEqual((row.refresh_token_encrypted, row.google_email), ("", ""))
        self.assertIsNone(row.connected_at)
        self.assertFalse(row.is_connected)
        with self.assertRaises(ValueError):
            row.set_refresh_token("  ")

    def test_admin_never_shows_the_token(self):
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, "ads@adx.se")
        admin_user = get_user_model().objects.create_superuser("admin", "a@adx.se", "x")
        self.client.force_login(admin_user)
        page = self.client.get("/admin/flamingo/googleadsconnection/1/change/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("ads@adx.se", html)
        row = GoogleAdsConnection.objects.get(pk=1)
        self.assertNotIn(row.refresh_token_encrypted, html)
        self.assertNotIn(REFRESH, html)
        self.assertNotIn('name="refresh_token_encrypted"', html)


# ---------------------------------------------------------------------------
# Inställningarna
# ---------------------------------------------------------------------------


class SettingsTests(GoogleAdsBase):
    @override_settings(**NOTHING)
    def test_missing_settings_lists_names(self):
        self.assertEqual(list(google_ads.missing_settings()), list(google_ads.REQUIRED_SETTINGS))
        self.assertFalse(google_ads.is_configured())
        self.assertFalse(google_ads.oauth_configured())
        for name in google_ads.missing_settings():
            self.assertIn(name, google_ads.SETTING_HELP)

    @override_settings(**CONFIGURED)
    def test_all_set(self):
        self.assertEqual(google_ads.missing_settings(), [])
        self.assertTrue(google_ads.is_configured())

    @override_settings(**{**CONFIGURED, "GOOGLE_ADS_REFRESH_TOKEN": ""})
    def test_the_stored_connection_counts_as_a_refresh_token(self):
        self.assertEqual(google_ads.missing_settings(), ["GOOGLE_ADS_REFRESH_TOKEN"])
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH)
        self.assertEqual(google_ads.missing_settings(), [])

    @override_settings(**{**CONFIGURED, "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "123-456-789"})
    def test_a_short_mcc_id_is_missing(self):
        self.assertEqual(google_ads.missing_settings(), ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"])

    @override_settings(GOOGLE_ADS_API_VERSION="v25/../x")
    def test_a_strange_version_falls_back(self):
        self.assertEqual(google_ads.api_version(), google_ads.DEFAULT_VERSION)

    def test_the_default_version_is_one_google_has_released(self):
        # v26 finns inte (Googles versionsanteckningar 2026-10-03: v25.2 är
        # den senaste). Byts versionen: byt API ovan också.
        self.assertEqual(google_ads.DEFAULT_VERSION, API)
        with override_settings(GOOGLE_ADS_API_VERSION=""):
            self.assertEqual(google_ads.api_version(), API)

    @override_settings(**{**CONFIGURED, "GOOGLE_ADS_DEVELOPER_TOKEN": ""})
    def test_the_developer_token_is_not_required(self):
        self.assertNotIn("GOOGLE_ADS_DEVELOPER_TOKEN", google_ads.REQUIRED_SETTINGS)
        self.assertEqual(google_ads.missing_settings(), [])
        self.assertTrue(google_ads.is_configured())

    def test_helpers(self):
        self.assertEqual(google_ads.digits("123-456-7890"), "1234567890")
        self.assertEqual(google_ads.digits("customers/1234567890"), "1234567890")
        self.assertEqual(google_ads.resource_id("customers/1/campaigns/22"), "22")
        self.assertEqual(google_ads.to_micros(150), "150000000")
        self.assertEqual(google_ads.micros_to_kr("149600000"), 150)
        self.assertEqual(google_ads.micros_to_kr(None), 0)
        self.assertEqual(google_ads.gaql_string("O'Neil"), "'O\\'Neil'")
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=google_ads.STOCKHOLM)
        self.assertEqual(google_ads.google_datetime(summer), "2026-07-01 12:00:00+02:00")
        winter = datetime(2026, 1, 15, 8, 30)
        self.assertEqual(google_ads.google_datetime(winter), "2026-01-15 08:30:00+01:00")

    def test_demo_accounts_never_call_google(self):
        google_ads.ensure_not_demo(FlamingoAccount(is_demo=False))
        with self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.ensure_not_demo(FlamingoAccount(is_demo=True))
        self.assertEqual(caught.exception.status, "DEMO")


# ---------------------------------------------------------------------------
# Inloggningen
# ---------------------------------------------------------------------------


@override_settings(**CONFIGURED)
class OAuthTests(GoogleAdsBase):
    def test_access_token_is_fetched_once_and_cached(self):
        fake = FakeGoogle(TOKEN_OK)
        with patch_http(fake):
            self.assertEqual(google_ads.access_token(), ACCESS)
            self.assertEqual(google_ads.access_token(), ACCESS)
        self.assertEqual(len(fake.requests), 1)
        sent = fake.requests[0]
        self.assertEqual(sent.full_url, "https://oauth2.googleapis.com/token")
        self.assertEqual(sent.get_method(), "POST")
        self.assertNotIn(REFRESH, sent.full_url)
        self.assertEqual(
            fake.body(0),
            {
                "grant_type": "refresh_token",
                "refresh_token": REFRESH,
                "client_id": CONFIGURED["GOOGLE_ADS_CLIENT_ID"],
                "client_secret": CLIENT_SECRET,
            },
        )

    def test_force_refresh_and_short_lifetimes_are_not_cached(self):
        fake = FakeGoogle(
            (200, {"access_token": ACCESS, "expires_in": 60}),
            (200, {"access_token": ACCESS_2, "expires_in": 3599}),
        )
        with patch_http(fake):
            self.assertEqual(google_ads.access_token(), ACCESS)
            self.assertEqual(google_ads.access_token(), ACCESS_2)
        self.assertEqual(len(fake.requests), 2)

    def test_the_cache_key_is_a_hash(self):
        key = google_ads._access_cache_key(REFRESH)
        self.assertNotIn(REFRESH, key)
        self.assertNotIn("testrefresh", key)
        self.assertNotEqual(key, google_ads._access_cache_key(REFRESH + "x"))

    def test_invalid_grant_is_explained_and_stored(self):
        fake = FakeGoogle(
            (400, {"error": "invalid_grant", "error_description": f"Token {REFRESH} revoked"})
        )
        with patch_http(fake), self.assertLogs("apps.flamingo.google_ads") as logs:
            with self.assertRaises(google_ads.GoogleAdsError) as caught:
                google_ads.access_token()
        error = caught.exception
        self.assertEqual(error.message, google_ads.MSG_INVALID_GRANT)
        self.assertEqual(error.codes, ["oauth.invalid_grant"])
        self.assertTrue(error.is_auth_error)
        self.assertEqual(GoogleAdsConnection.get_solo().last_error, google_ads.MSG_INVALID_GRANT)
        for text in (str(error), *logs.output):
            self.assertNotIn(REFRESH, text)
            self.assertNotIn(CLIENT_SECRET, text)

    def test_unknown_oauth_errors_never_echo_secrets(self):
        description = f"Bad client_secret={CLIENT_SECRET} and refresh_token={REFRESH}"
        fake = FakeGoogle((400, {"error": "weird", "error_description": description}))
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.access_token()
        self.assertIn("Googles inloggning svarade med ett fel", caught.exception.message)
        self.assertNotIn(CLIENT_SECRET, caught.exception.message)
        self.assertNotIn(REFRESH, caught.exception.message)

    @override_settings(GOOGLE_ADS_REFRESH_TOKEN="")
    def test_no_connection_means_no_call(self):
        fake = FakeGoogle()
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.access_token()
        self.assertEqual(caught.exception.status, "NOT_CONFIGURED")
        self.assertEqual(fake.requests, [])

    def test_authorization_url(self):
        url = google_ads.authorization_url("slumpad-state", "https://adx.se/manage/google/klar/")
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", google_ads.AUTH_URL)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        self.assertEqual(
            query,
            {
                "client_id": CONFIGURED["GOOGLE_ADS_CLIENT_ID"],
                "redirect_uri": "https://adx.se/manage/google/klar/",
                "response_type": "code",
                # Data Manager API (konverteringarna) bredvid Google Ads, och
                # övervakningens läsbehörigheter (Search Console, Business Profile).
                "scope": "https://www.googleapis.com/auth/adwords "
                "https://www.googleapis.com/auth/datamanager "
                "https://www.googleapis.com/auth/webmasters.readonly "
                "https://www.googleapis.com/auth/business.manage openid email",
                "access_type": "offline",
                "prompt": "consent",
                "state": "slumpad-state",
            },
        )
        self.assertNotIn(CLIENT_SECRET, url)
        with override_settings(GOOGLE_ADS_CLIENT_ID=""):
            with self.assertRaises(google_ads.GoogleAdsError):
                google_ads.authorization_url("s", "https://adx.se/")

    def test_exchange_code_returns_the_token_the_email_and_the_scopes(self):
        fake = FakeGoogle(
            (
                200,
                {
                    "access_token": ACCESS,
                    "expires_in": 3599,
                    "refresh_token": REFRESH,
                    "scope": "openid https://www.googleapis.com/auth/adwords "
                    "https://www.googleapis.com/auth/userinfo.email",
                    "id_token": id_token({"email": "ads@adx.se", "email_verified": True}),
                },
            )
        )
        with patch_http(fake):
            token, email, scopes = google_ads.exchange_code("4/kod", "https://adx.se/klar/")
            self.assertEqual((token, email), (REFRESH, "ads@adx.se"))
            # Svarets "scope", sorterat: det byrån lät vara ikryssat.
            self.assertEqual(
                scopes,
                "https://www.googleapis.com/auth/adwords "
                "https://www.googleapis.com/auth/userinfo.email openid",
            )
            # Den kortlivade nyckeln ur samma svar cachas.
            self.assertEqual(google_ads.access_token(), ACCESS)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.body(0)["grant_type"], "authorization_code")
        self.assertEqual(fake.body(0)["code"], "4/kod")
        self.assertEqual(fake.body(0)["redirect_uri"], "https://adx.se/klar/")

    def test_id_token_parsing_is_forgiving(self):
        parse = google_ads._email_from_id_token
        self.assertEqual(parse(id_token({"email": "a@b.se"})), "a@b.se")
        self.assertEqual(parse(id_token({"sub": "1"})), "")
        self.assertEqual(parse(id_token({"email": "inte-en-adress"})), "")
        self.assertEqual(parse("trasig"), "")
        self.assertEqual(parse("a.!!!.c"), "")
        self.assertEqual(parse(None), "")

    def test_exchange_without_refresh_token_or_scope_fails(self):
        fake = FakeGoogle((200, {"access_token": ACCESS, "expires_in": 3599}))
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.exchange_code("4/kod", "https://adx.se/klar/")
        self.assertEqual(caught.exception.status, "NO_REFRESH_TOKEN")

        fake = FakeGoogle(
            (200, {"access_token": ACCESS, "refresh_token": REFRESH, "scope": "openid email"}),
            (200, {}),
        )
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.exchange_code("4/kod", "https://adx.se/klar/")
        self.assertEqual(caught.exception.message, google_ads.MSG_SCOPE_MISSING)
        # Nyckeln som inte kan användas återkallas direkt.
        self.assertEqual(fake.requests[1].full_url, google_ads.REVOKE_URL)
        self.assertEqual(fake.body(1), {"token": REFRESH})

    def test_a_used_code_is_explained(self):
        fake = FakeGoogle((400, {"error": "invalid_grant", "error_description": "Bad Request"}))
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.exchange_code("4/gammal", "https://adx.se/klar/")
        self.assertEqual(caught.exception.message, google_ads.MSG_CODE_REJECTED)
        self.assertEqual(GoogleAdsConnection.objects.filter(last_error__gt="").count(), 0)

    def test_revoke_sends_the_token_in_the_body(self):
        fake = FakeGoogle((200, {}))
        with patch_http(fake):
            self.assertTrue(google_ads.revoke(REFRESH))
        self.assertEqual(fake.requests[0].full_url, "https://oauth2.googleapis.com/revoke")
        self.assertEqual(fake.body(0), {"token": REFRESH})
        with patch_http(FakeGoogle((400, {"error": "invalid_token"}))):
            self.assertFalse(google_ads.revoke(REFRESH))
        with patch_http(FakeGoogle((URLError("nere"), None))):
            self.assertFalse(google_ads.revoke(REFRESH))
        self.assertFalse(google_ads.revoke(""))


# ---------------------------------------------------------------------------
# Anropen
# ---------------------------------------------------------------------------


@override_settings(**CONFIGURED)
class RequestTests(GoogleAdsBase):
    def test_headers_and_address(self):
        fake = FakeGoogle(TOKEN_OK, (200, {"results": []}))
        with patch_http(fake):
            google_ads.request("POST", "customers/1112223333/googleAds:search", {"query": "x"})
        sent = fake.requests[1]
        self.assertEqual(
            sent.full_url,
            f"https://googleads.googleapis.com/{API}/customers/1112223333/googleAds:search",
        )
        self.assertEqual(sent.get_header("Authorization"), f"Bearer {ACCESS}")
        self.assertEqual(sent.get_header("Developer-token"), DEV_TOKEN)
        self.assertEqual(sent.get_header("Login-customer-id"), "1234567890")
        self.assertEqual(sent.get_header("Content-type"), "application/json")
        self.assertEqual(fake.body(1), {"query": "x"})

    def test_login_customer_id_can_be_given_or_left_out(self):
        fake = FakeGoogle(
            TOKEN_OK,
            (200, {"resourceNames": ["customers/1234567890", "customers/9998887777"]}),
            (200, {}),
        )
        with patch_http(fake):
            self.assertEqual(google_ads.list_accessible_customers(), ["1234567890", "9998887777"])
            google_ads.request("GET", "customers/9998887777", login_customer_id="999-888-7777")
        self.assertIsNone(fake.requests[1].get_header("Login-customer-id"))
        self.assertEqual(fake.requests[1].get_method(), "GET")
        self.assertIsNone(fake.requests[1].data)
        self.assertEqual(fake.requests[2].get_header("Login-customer-id"), "9998887777")

    def test_only_googles_hosts_and_clean_paths(self):
        for path in ("../v1/x", "customers/1//x", "https://evil.example/x", "customers/1?x=1"):
            with self.subTest(path=path), self.assertRaises(google_ads.GoogleAdsError):
                google_ads._api_url(path)
        with self.assertRaises(google_ads.GoogleAdsError):
            google_ads._http("GET", "https://evil.example/token")
        with self.assertRaises(google_ads.GoogleAdsError):
            google_ads._http("GET", f"http://googleads.googleapis.com/{API}/x")

    def test_search_reads_every_page(self):
        fake = FakeGoogle(
            TOKEN_OK,
            (200, {"results": [{"campaign": {"id": "1"}}], "nextPageToken": "sida2"}),
            (200, {"results": [{"campaign": {"id": "2"}}, {"campaign": {"id": "3"}}]}),
        )
        query = "SELECT campaign.id FROM campaign"
        with patch_http(fake):
            rows = list(google_ads.search("111-222-3333", query))
        self.assertEqual([row["campaign"]["id"] for row in rows], ["1", "2", "3"])
        self.assertEqual(fake.body(1), {"query": query})
        self.assertEqual(fake.body(2), {"query": query, "pageToken": "sida2"})
        self.assertTrue(fake.requests[2].full_url.endswith("customers/1112223333/googleAds:search"))
        self.assertEqual(len(fake.requests), 3)

    def test_a_bad_customer_id_is_refused_before_any_call(self):
        fake = FakeGoogle()
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            list(google_ads.search("123", "SELECT customer.id FROM customer"))
        self.assertEqual(caught.exception.status, "INVALID_CUSTOMER_ID")
        self.assertEqual(fake.requests, [])

    def test_mutate_body(self):
        operations = [{"campaignBudgetOperation": {"create": {"amountMicros": "150000000"}}}]
        answer = {"mutateOperationResponses": [{"campaignBudgetResult": {"resourceName": "b"}}]}
        fake = FakeGoogle(TOKEN_OK, (200, answer))
        with patch_http(fake):
            result = google_ads.mutate("1112223333", operations, validate_only=True)
        self.assertEqual(result, answer)
        self.assertTrue(
            fake.requests[1].full_url.endswith("/customers/1112223333/googleAds:mutate")
        )
        self.assertEqual(
            fake.body(1),
            {"mutateOperations": operations, "partialFailure": False, "validateOnly": True},
        )

    def test_a_401_gets_one_new_token(self):
        fake = FakeGoogle(
            TOKEN_OK,
            (401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "Expired"}}),
            (200, {"access_token": ACCESS_2, "expires_in": 3599}),
            (200, {"ok": True}),
        )
        with patch_http(fake):
            self.assertEqual(google_ads.request("GET", "customers/1112223333"), {"ok": True})
        self.assertEqual(fake.requests[3].get_header("Authorization"), f"Bearer {ACCESS_2}")

    def test_success_marks_the_connection_ok_but_not_on_every_call(self):
        GoogleAdsConnection.objects.create(pk=1, last_error="Gammalt fel")
        fake = FakeGoogle(TOKEN_OK, (200, {}), (200, {}))
        with patch_http(fake):
            google_ads.request("GET", "customers/1112223333")
            first = GoogleAdsConnection.get_solo().last_ok_at
            self.assertIsNotNone(first)
            self.assertEqual(GoogleAdsConnection.get_solo().last_error, "")
            GoogleAdsConnection.objects.update(last_ok_at=None)
            google_ads.request("GET", "customers/1112223333")
        self.assertIsNone(GoogleAdsConnection.get_solo().last_ok_at)

    def test_no_answer_and_too_big_answers(self):
        fake = FakeGoogle(TOKEN_OK, (URLError("timeout"), None))
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.request("GET", "customers/1112223333")
        self.assertEqual(caught.exception.status, "UNAVAILABLE")
        self.assertEqual(caught.exception.message, google_ads.MSG_UNREACHABLE)

        with (
            patch_http(FakeGoogle((200, b'{"results": ["for mycket"]}'))),
            self.assertRaises(google_ads.GoogleAdsError) as caught,
        ):
            google_ads._http("GET", f"https://googleads.googleapis.com/{API}/x", max_bytes=10)
        self.assertEqual(caught.exception.status, "TOO_LARGE")

    @override_settings(GOOGLE_ADS_DEVELOPER_TOKEN="")
    def test_without_a_developer_token_the_header_is_left_out(self):
        # Avvecklad hos Google 2026-09-09: anropet görs ändå, utan headern.
        fake = FakeGoogle(TOKEN_OK, (200, {"id": "1"}))
        with patch_http(fake):
            google_ads.request("GET", "customers/1112223333")
        sent = fake.requests[1]
        self.assertIsNone(sent.get_header("Developer-token"))
        self.assertEqual(sent.get_header("Authorization"), f"Bearer {ACCESS}")
        self.assertEqual(urlsplit(sent.full_url).path, f"/{API}/customers/1112223333")


# ---------------------------------------------------------------------------
# Felen
# ---------------------------------------------------------------------------


@override_settings(**CONFIGURED)
class ErrorTests(GoogleAdsBase):
    def call(self, *responses):
        cache.clear()
        fake = FakeGoogle(TOKEN_OK, *responses)
        with patch_http(fake), self.assertRaises(google_ads.GoogleAdsError) as caught:
            google_ads.request("POST", "customers/1112223333/googleAds:search", {"query": "x"})
        return caught.exception

    def test_known_codes_get_swedish_explanations(self):
        cases = [
            ("authorizationError", "DEVELOPER_TOKEN_NOT_APPROVED", "Explorer", True),
            (
                "authorizationError",
                "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION",
                "Google Ads API Overview",
                True,
            ),
            ("authorizationError", "DEVELOPER_TOKEN_PROHIBITED", "behövs inte längre", True),
            ("authorizationError", "USER_PERMISSION_DENIED", "förvaltarkonto", False),
            ("authorizationError", "CUSTOMER_NOT_ENABLED", "inte aktivt", False),
            ("authenticationError", "CUSTOMER_NOT_FOUND", "tio siffror", False),
            ("authenticationError", "NOT_ADS_USER", "inget Google Ads-konto", True),
            ("authenticationError", "OAUTH_TOKEN_REVOKED", "Koppla ADX:s Google-konto", True),
            ("managerLinkError", "CUSTOMER_NOT_ACTIVE", "inte aktiv", False),
            ("quotaError", "RESOURCE_EXHAUSTED", "kvoten", False),
        ]
        for group, code, words, connection_error in cases:
            with self.subTest(code=code):
                GoogleAdsConnection.objects.all().delete()
                error = self.call(google_error(403, "PERMISSION_DENIED", group, code))
                self.assertIn(words, error.message)
                self.assertEqual(error.codes, [f"{group}.{code}"])
                self.assertEqual(error.code_names, [code])
                self.assertEqual((error.status, error.http_status), ("PERMISSION_DENIED", 403))
                self.assertEqual(error.request_id, "req-1")
                self.assertEqual(error.is_auth_error, connection_error)
                stored = GoogleAdsConnection.objects.filter(pk=1).first()
                self.assertEqual(bool(stored and stored.last_error), connection_error)

    def test_quota_by_status(self):
        error = self.call(
            (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Slut"}})
        )
        self.assertEqual(error.message, google_ads.MSG_QUOTA)
        self.assertTrue(error.is_quota_error)

    def test_api_not_enabled_in_the_cloud_project(self):
        error = self.call(
            (
                403,
                {
                    "error": {
                        "code": 403,
                        "status": "PERMISSION_DENIED",
                        "message": "Google Ads API has not been used in project 123",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                "reason": "SERVICE_DISABLED",
                            }
                        ],
                    }
                },
            )
        )
        self.assertEqual(error.message, google_ads.MSG_SERVICE_DISABLED)
        self.assertEqual(error.codes, ["errorInfo.SERVICE_DISABLED"])
        self.assertTrue(error.is_auth_error)

    def test_unknown_codes_show_googles_message_and_where(self):
        location = {
            "fieldPathElements": [
                {"fieldName": "mutate_operations", "index": 3},
                {"fieldName": "campaign_operation"},
                {"fieldName": "create"},
                {"fieldName": "name"},
            ]
        }
        response = google_error(
            400,
            "INVALID_ARGUMENT",
            "campaignError",
            "DUPLICATE_CAMPAIGN_NAME",
            message="A campaign with this name already exists.",
            location=location,
        )
        response[1]["error"]["details"][0]["errors"].append(
            {"errorCode": {"stringLengthError": "TOO_LONG"}, "message": "Too long."}
        )
        error = self.call(response)
        self.assertEqual(
            error.message, "Google: A campaign with this name already exists. (1 fel till)"
        )
        self.assertEqual(
            error.errors[0],
            {
                "code": "campaignError.DUPLICATE_CAMPAIGN_NAME",
                "message": "A campaign with this name already exists.",
                "index": 3,
                "field_path": "mutate_operations[3].campaign_operation.create.name",
            },
        )
        self.assertEqual(error.errors[1]["index"], None)
        self.assertFalse(error.is_auth_error)
        self.assertFalse(GoogleAdsConnection.objects.filter(last_error__gt="").exists())

    def test_without_details(self):
        self.assertIn("HTTP 503", self.call((503, b"<html>nere</html>")).message)
        message = self.call((404, b"")).message
        self.assertIn(API, message)
        self.assertIn("avvecklad", message)
        self.assertNotIn("för gammal", message)
        error = self.call((401, {}), (200, {"access_token": ACCESS_2}), (401, {}))
        self.assertEqual(error.message, google_ads.MSG_RECONNECT)

    def test_partial_failure_in_a_successful_answer(self):
        payload = {
            "results": [{}, {"gclid": "abc"}],
            "partialFailureError": google_error(
                400,
                "INVALID_ARGUMENT",
                "conversionUploadError",
                "CLICK_NOT_FOUND",
                message="The click was not found.",
                location={"fieldPathElements": [{"fieldName": "conversions", "index": 0}]},
            )[1]["error"],
        }
        error = google_ads.partial_failure_error(payload)
        self.assertEqual(error.codes, ["conversionUploadError.CLICK_NOT_FOUND"])
        self.assertEqual(error.errors[0]["index"], 0)
        self.assertIsNone(google_ads.partial_failure_error({"results": []}))

    def test_tokens_never_reach_the_message_or_the_log(self):
        echo = (
            f"Bad header Authorization: Bearer {ACCESS} dev {DEV_TOKEN} "
            f"secret {CLIENT_SECRET} refresh {REFRESH}"
        )
        response = google_error(401, "UNAUTHENTICATED", "authenticationError", "WHATEVER", echo)
        response[1]["error"]["message"] = echo
        fake = FakeGoogle(TOKEN_OK, response, (200, {"access_token": ACCESS_2}), response)
        logger = logging.getLogger("apps.flamingo")
        with (
            patch_http(fake),
            self.assertLogs(logger, logging.DEBUG) as logs,
            self.assertRaises(google_ads.GoogleAdsError) as caught,
        ):
            google_ads.request("GET", "customers/1112223333")
        error = caught.exception
        texts = [
            str(error),
            error.message,
            repr(error.args),
            json.dumps(error.errors),
            " ".join(error.codes),
            error.request_id,
            *logs.output,
            GoogleAdsConnection.get_solo().last_error,
        ]
        for text in texts:
            for secret in (ACCESS, ACCESS_2, DEV_TOKEN, CLIENT_SECRET, REFRESH):
                self.assertNotIn(secret, text)
        self.assertTrue(error.is_auth_error)


# ---------------------------------------------------------------------------
# Modellerna
# ---------------------------------------------------------------------------


class ModelTests(TestCase):
    def setUp(self):
        customer = Customer.objects.create(name="Lindqvist Rör AB")
        self.account = FlamingoAccount.objects.create(customer=customer, is_enabled=True)
        self.service = Service.objects.create(account=self.account, name="Badrum")
        self.campaign = Campaign.objects.create(
            account=self.account, service=self.service, name="Badrum Nacka"
        )

    def test_new_defaults(self):
        self.assertFalse(self.account.is_demo)
        self.assertEqual(self.account.google_conversion_actions, {})
        self.assertIsNone(self.account.google_auto_tagging)
        self.assertEqual(self.campaign.google_resources, {})

    def test_conversions_one_of_each_kind_and_only_deals_follow_the_status(self):
        lead = Lead.objects.create(
            account=self.account,
            campaign=self.campaign,
            gclid="Cj0abc",
            ad_consent=Lead.CONSENT_GRANTED,
        )
        ConversionUpload.objects.create(lead=lead, kind=ConversionUpload.KIND_LEAD)
        lead.set_status(Lead.STATUS_WON, value_kr=4800)
        deal = lead.conversions.get(kind=ConversionUpload.KIND_DEAL)
        self.assertEqual(deal.value_kr, 4800)
        self.assertIsNone(lead.conversions.get(kind=ConversionUpload.KIND_LEAD).value_kr)
        lead.set_status(Lead.STATUS_LOST)
        self.assertEqual(
            list(lead.conversions.values_list("kind", flat=True)), [ConversionUpload.KIND_LEAD]
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ConversionUpload.objects.create(lead=lead, kind=ConversionUpload.KIND_LEAD)
        self.assertIn("Förfrågan", str(lead.conversions.get()))

    def test_call_click_leads(self):
        lead = Lead(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual(lead.display_name, "Klick på telefonnumret")
        self.assertEqual(Lead().display_name, "Okänd")
        self.assertEqual(Lead(gbraid="g1").click_ids, {"gbraid": "g1"})
        self.assertTrue(Lead(wbraid="w1").has_click_id)
        self.assertFalse(Lead().has_click_id)
        self.assertEqual(Lead().get_ad_consent_display(), "Inte tillfrågad")

    def test_the_publish_guard_is_taken_once(self):
        other = Campaign.objects.get(pk=self.campaign.pk)
        self.assertTrue(self.campaign.claim_google_publish())
        self.assertFalse(other.claim_google_publish())
        self.assertIsNotNone(Campaign.objects.get(pk=self.campaign.pk).google_publish_started_at)
        self.campaign.release_google_publish()
        self.assertTrue(other.claim_google_publish())

    def test_day_stats(self):
        day = CampaignDayStats.objects.create(
            campaign=self.campaign, date=date(2026, 10, 2), cost_micros=149_600_000, clicks=7
        )
        self.assertEqual(day.cost_kr, 150)
        self.assertEqual(list(self.campaign.day_stats.all()), [day])
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampaignDayStats.objects.create(campaign=self.campaign, date=date(2026, 10, 2))

    def test_the_migration_moves_click_ids_out_of_utm(self):
        migration = importlib.import_module("apps.flamingo.migrations.0005_google_ads")
        lead = Lead.objects.create(
            account=self.account, utm={"utm_source": "google", "gbraid": "G1", "wbraid": "W1"}
        )
        untouched = Lead.objects.create(account=self.account, utm={"utm_source": "x"})
        migration.click_ids_out_of_utm(django_apps, None)
        lead.refresh_from_db()
        self.assertEqual((lead.gbraid, lead.wbraid), ("G1", "W1"))
        self.assertEqual(lead.utm, {"utm_source": "google"})
        untouched.refresh_from_db()
        self.assertEqual(untouched.utm, {"utm_source": "x"})
        migration.click_ids_back_to_utm(django_apps, None)
        lead.refresh_from_db()
        self.assertEqual(lead.utm, {"utm_source": "google", "gbraid": "G1", "wbraid": "W1"})


class SentryScrubTests(TestCase):
    def test_google_tokens_are_masked_in_reports(self):
        cases = {
            f"Authorization: Bearer {ACCESS}": "Authorization: Bearer [Filtered]",
            f"grant_type=refresh_token&refresh_token={REFRESH}&client_secret={CLIENT_SECRET}": (
                "grant_type=refresh_token&refresh_token=[Filtered]&client_secret=[Filtered]"
            ),
            f"nyckeln {REFRESH} gick ut": "nyckeln [Filtered] gick ut",
        }
        for raw, expected in cases.items():
            self.assertEqual(sentry.scrub_text(raw), expected, raw)
