"""
Konverteringarna genom Data Manager API (google_conversions.py), standardvägen
sedan Google slutade ta emot nya användare av uploadClickConversions
2026-06-15: anropet som Google dokumenterar det, behörigheten och
"Koppla om", valet av väg, nej för en rad, ett konto eller hela vägen,
backoff, transactionId, CSV-filen, Googles besked, demot och nycklarna.

Inget här når nätet: urlopen byts mot FakeGoogle (test_google_ads.py), som
svarar med det testet köat och fäller testet vid varje anrop som inte köats.
Värdena är påhittade testvärden.
"""

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.models import Customer

from . import exports, google_ads, google_conversions, google_reports, manage_review
from .google_ads import GoogleAdsError
from .models import (
    ConversionUpload,
    FlamingoAccount,
    GoogleAdsConnection,
    Lead,
    normalize_scopes,
    token_fingerprint,
)
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
    id_token,
)
from .test_google_measure import ACTIONS, CUSTOMER_ID, MeasureFixture

MCC = "1234567890"
INGEST_URL = "https://datamanager.googleapis.com/v1/events:ingest"
STATUS_URL = "https://datamanager.googleapis.com/v1/requestStatus:retrieve"
ADWORDS = "https://www.googleapis.com/auth/adwords"
DATAMANAGER = "https://www.googleapis.com/auth/datamanager"
FULL_SCOPE = f"{ADWORDS} {DATAMANAGER} openid https://www.googleapis.com/auth/userinfo.email"
WITHOUT_DM = f"{ADWORDS} openid https://www.googleapis.com/auth/userinfo.email"

#: Standardvägen, uttryckligen: testerna ska inte bero på en lokal .env.
DM = {**CONFIGURED, "FLAMINGO_CONVERSIONS_UPLOAD": "datamanager"}
#: Inloggningen härifrån (sparad i databasen), utan nyckeln i miljön.
DM_STORED = {**DM, "GOOGLE_ADS_REFRESH_TOKEN": ""}
RECONNECT = "Koppla om med Google för att skicka konverteringar"


def patch_http(fake):
    return mock.patch("apps.flamingo.google_ads.urlopen", fake)


def grant(scope=FULL_SCOPE, token=REFRESH):
    """Google har gett nyckeln behörigheterna (som när den förnyats)."""
    GoogleAdsConnection.objects.update_or_create(
        pk=GoogleAdsConnection.SOLO_PK,
        defaults={
            "granted_scopes": normalize_scopes(scope),
            "scopes_for": token_fingerprint(token),
        },
    )


def accepted(request_id="dm-req-1", **extra):
    """events:ingest tog emot anropet."""
    return (200, {"requestId": request_id, **extra})


def dm_error(http, status, reason="", violations=(), message="Fel.", request_id="dm-err-1"):
    """Ett fel som Data Manager API skickar det (gRPC Status i JSON):
    devguides/concepts/understand-errors."""
    details = []
    if reason:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": reason,
                "domain": "datamanager.googleapis.com",
                "metadata": {"requestId": request_id},
            }
        )
    details.append({"@type": "type.googleapis.com/google.rpc.RequestInfo", "requestId": request_id})
    if violations:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.BadRequest",
                "fieldViolations": [
                    {"field": field, "description": description, "reason": why}
                    for field, description, why in violations
                ],
            }
        )
    return (
        http,
        {"error": {"code": http, "message": message, "status": status, "details": details}},
    )


def service_disabled():
    """Google Clouds svar när Data Manager API inte är påslaget i projektet."""
    return (
        403,
        {
            "error": {
                "code": 403,
                "message": "Data Manager API has not been used in project 123 before or it is "
                "disabled.",
                "status": "PERMISSION_DENIED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "SERVICE_DISABLED",
                        "domain": "googleapis.com",
                        "metadata": {"service": "datamanager.googleapis.com"},
                    }
                ],
            }
        },
    )


def verdict(status, *reasons, count="1"):
    """requestStatus:retrieve för en sändning med en destination."""
    item = {
        "destination": {
            "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": CUSTOMER_ID},
            "loginAccount": {"accountType": "GOOGLE_ADS", "accountId": MCC},
            "productDestinationId": "11",
        },
        "requestStatus": status,
        "eventsIngestionStatus": {"recordCount": count},
    }
    if reasons:
        item["errorInfo"] = {
            "errorCounts": [
                {"recordCount": count, "reason": f"PROCESSING_ERROR_REASON_{reason}"}
                for reason in reasons
            ]
        }
    return (200, {"requestStatusPerDestination": [item]})


def texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


@override_settings(**DM)
class DataManagerBase(MeasureFixture, TestCase):
    def setUp(self):
        super().setUp()
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_conversion_actions=ACTIONS)
        self.account.refresh_from_db()
        grant()

    def queued(self, kind, **fields):
        """En rad i kö för en ny förfrågan med gclid (affären vunnen, 186 000 kr)."""
        fields.setdefault("gclid", f"G-{kind}-{Lead.objects.count()}")
        if kind == ConversionUpload.KIND_CALL:
            fields.setdefault("source", Lead.SOURCE_CALL_CLICK)
        lead = self.lead(**fields)
        value = None
        if kind == ConversionUpload.KIND_DEAL:
            value = 186000
            Lead.objects.filter(pk=lead.pk).update(
                status=Lead.STATUS_WON, value_kr=value, won_at=lead.won_at or timezone.now()
            )
            lead.refresh_from_db()
        return ConversionUpload.objects.create(lead=lead, kind=kind, value_kr=value)

    def upload(self, *responses, account=None, now=None, **kwargs):
        # Varje anrop börjar med en ny kortlivad nyckel (TOKEN_OK först).
        cache.clear()
        fake = FakeGoogle(TOKEN_OK, *responses) if responses else FakeGoogle()
        with patch_http(fake):
            result = google_conversions.upload_queued(account or self.account, now=now, **kwargs)
        return result, fake

    def staff_client(self):
        client = Client()
        client.force_login(self.staff)
        return client

    def fresh(self, row):
        row.refresh_from_db()
        return row


# ---------------------------------------------------------------------------
# Anropet, så som Google dokumenterar det
# ---------------------------------------------------------------------------


class PayloadTests(DataManagerBase):
    def test_the_request_is_exactly_what_the_docs_describe(self):
        row = self.queued(
            "lead", gclid="Cj0KCQ-lead", created_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
        )
        result, fake = self.upload(accepted("dm-req-1"))
        self.assertEqual(result, {"sent": 1, "failed": 0, "waiting": 0})
        self.assertEqual(len(fake.requests), 2)
        request = fake.requests[1]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, INGEST_URL)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {ACCESS}")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        # Google bortser från headers i ett ingest-anrop: kontona står i
        # destinationen, och utvecklartoken skickas aldrig hit.
        self.assertIsNone(request.get_header("Developer-token"))
        self.assertIsNone(request.get_header("Login-customer-id"))
        self.assertEqual(
            fake.body(1),
            {
                "destinations": [
                    {
                        "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": CUSTOMER_ID},
                        "loginAccount": {"accountType": "GOOGLE_ADS", "accountId": MCC},
                        "productDestinationId": "11",
                    }
                ],
                "events": [
                    {
                        "transactionId": f"adx-flamingo-{row.lead_id}-lead",
                        # RFC 3339 i svensk tid med offset, samma sekund som filen.
                        "eventTimestamp": "2026-09-01T12:00:00+02:00",
                        "eventSource": "WEB",
                        "adIdentifiers": {"gclid": "Cj0KCQ-lead"},
                    }
                ],
                "validateOnly": False,
            },
        )
        self.assertEqual(exports.conversion_time(row), "2026-09-01 12:00:00")
        row = self.fresh(row)
        self.assertEqual(row.status, ConversionUpload.STATUS_SENT)
        self.assertIsNotNone(row.sent_at)
        self.assertEqual(row.request_id, "dm-req-1")
        self.assertIsNone(row.checked_at)
        self.assertEqual(row.response["api"], "datamanager")
        self.assertEqual(row.response["transaction_id"], row.transaction_id)
        self.assertNotIn(row, manage_review.queued_uploads())

    def test_value_currency_and_consent_only_where_they_belong(self):
        deal = self.queued("deal", ad_consent=Lead.CONSENT_GRANTED)
        Lead.objects.filter(pk=deal.lead_id).update(won_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC))
        denied = self.queued("lead", ad_consent=Lead.CONSENT_DENIED)
        unknown = self.queued("call")
        result, fake = self.upload(accepted("a"), accepted("b"), accepted("c"))
        self.assertEqual(result["sent"], 3)
        events = {}
        destinations = {}
        for n in (1, 2, 3):
            body = fake.body(n)
            self.assertEqual(len(body["events"]), 1)
            self.assertEqual(len(body["destinations"]), 1)
            event = body["events"][0]
            events[event["transactionId"]] = event
            destinations[event["transactionId"]] = body["destinations"][0]
        deal_event = events[deal.transaction_id]
        self.assertEqual(deal_event["conversionValue"], 186000)
        self.assertEqual(deal_event["currency"], "SEK")
        self.assertEqual(deal_event["consent"], {"adUserData": "CONSENT_GRANTED"})
        # Vintertid: +01:00. Affärens tid är när den blev vunnen.
        self.assertEqual(deal_event["eventTimestamp"], "2026-01-15T09:30:00+01:00")
        self.assertEqual(destinations[deal.transaction_id]["productDestinationId"], "13")
        self.assertEqual(events[denied.transaction_id]["consent"], {"adUserData": "CONSENT_DENIED"})
        self.assertNotIn("conversionValue", events[denied.transaction_id])
        self.assertNotIn("currency", events[denied.transaction_id])
        # Sidan frågar inte (beslut 2026-10-03): inget svar, inget consent.
        self.assertNotIn("consent", events[unknown.transaction_id])
        self.assertEqual(destinations[unknown.transaction_id]["productDestinationId"], "12")

    def test_only_the_gclid_is_sent_never_a_braid(self):
        both = self.queued("lead", gclid="G-both", gbraid="B-1", wbraid="W-1")
        _, fake = self.upload(accepted())
        self.assertEqual(fake.body(1)["events"][0]["adIdentifiers"], {"gclid": "G-both"})
        self.assertEqual(self.fresh(both).status, ConversionUpload.STATUS_SENT)
        # Bara ett braid-id: köas aldrig, och en sådan rad skickas aldrig.
        braid = self.lead(wbraid="W-only")
        self.assertIsNone(braid.queue_conversion(ConversionUpload.KIND_LEAD))
        stray = ConversionUpload.objects.create(lead=braid, kind=ConversionUpload.KIND_LEAD)
        result, _ = self.upload()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.fresh(stray).status, ConversionUpload.STATUS_FAILED)


# ---------------------------------------------------------------------------
# Behörigheten och "Koppla om"
# ---------------------------------------------------------------------------


GRANT = {
    "access_token": ACCESS,
    "expires_in": 3599,
    "refresh_token": REFRESH,
    "token_type": "Bearer",
    "id_token": id_token({"email": "ads@adx.se"}),
}


@override_settings(**DM_STORED)
class ScopeTests(DataManagerBase):
    def setUp(self):
        super().setUp()
        GoogleAdsConnection.objects.all().delete()

    def connect(self, scope):
        client = self.staff_client()
        response = client.post(reverse("manage:flamingo_google_connect"))
        state = parse_qs(urlsplit(response["Location"]).query)["state"][0]
        with patch_http(FakeGoogle((200, {**GRANT, "scope": scope}))):
            response = client.get(
                reverse("manage:flamingo_google_callback"), {"state": state, "code": "4/kod"}
            )
        return client, response

    def test_the_data_manager_scope_is_requested_next_to_adwords(self):
        response = self.staff_client().post(reverse("manage:flamingo_google_connect"))
        scope = parse_qs(urlsplit(response["Location"]).query)["scope"][0]
        self.assertEqual(
            set(scope.split()),
            {
                ADWORDS,
                DATAMANAGER,
                google_ads.WEBMASTERS_SCOPE,
                google_ads.BUSINESS_SCOPE,
                "openid",
                "email",
            },
        )

    def test_connecting_records_the_granted_scopes(self):
        client, response = self.connect(FULL_SCOPE)
        row = GoogleAdsConnection.objects.get(pk=GoogleAdsConnection.SOLO_PK)
        self.assertIn(DATAMANAGER, row.granted_scopes.split())
        self.assertIn(ADWORDS, row.granted_scopes.split())
        self.assertEqual(row.scopes_for, token_fingerprint(REFRESH))
        self.assertNotIn(REFRESH, row.scopes_for)
        self.assertEqual(google_ads.datamanager_scope_state(), google_ads.SCOPE_GRANTED)
        self.assertTrue(google_conversions.upload_enabled())
        self.assertNotIn(RECONNECT, " ".join(texts(response)))
        page = client.get(reverse("manage:flamingo_google")).content.decode()
        self.assertIn("Inloggningen får skicka konverteringar med Data Manager API", page)
        self.assertIn("Google Ads, Data Manager", page)
        self.assertNotIn(RECONNECT, page)

    def test_a_grant_without_the_scope_asks_to_reconnect(self):
        client, response = self.connect(WITHOUT_DM)
        # Kopplingen sparas ändå: Google Ads fungerar, konverteringarna går som CSV.
        self.assertTrue(GoogleAdsConnection.get_solo().is_connected)
        self.assertIn(RECONNECT, " ".join(texts(response)))
        self.assertEqual(google_ads.datamanager_scope_state(), google_ads.SCOPE_MISSING)
        self.assertFalse(google_conversions.upload_enabled())
        page = client.get(reverse("manage:flamingo_google")).content.decode()
        self.assertIn(RECONNECT, page)
        self.assertIn("Koppla om med Google</button>", page)
        self.assertIn("Saknas för Data Manager API", page)
        queue = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn(RECONNECT, queue)
        row = self.queued("lead")
        result, _ = self.upload()  # inget anrop alls
        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.fresh(row).status, ConversionUpload.STATUS_QUEUED)
        # Koppla om med behörigheten: konverteringarna går med API:t.
        client, _ = self.connect(FULL_SCOPE)
        page = client.get(reverse("manage:flamingo_google")).content.decode()
        self.assertNotIn(RECONNECT, page)
        self.assertTrue(google_conversions.upload_enabled())

    def test_a_connection_from_before_the_scope_was_recorded_asks_to_reconnect(self):
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, "ads@adx.se")
        self.assertEqual(google_ads.datamanager_scope_state(), google_ads.SCOPE_UNKNOWN)
        status = google_conversions.upload_status()
        self.assertTrue(status["needs_reconnect"])
        self.assertEqual(status["reason"], google_conversions.MSG_RECONNECT)
        page = self.staff_client().get(reverse("manage:flamingo_google")).content.decode()
        self.assertIn(RECONNECT, page)

    @override_settings(GOOGLE_ADS_REFRESH_TOKEN=REFRESH)
    def test_a_token_refresh_records_the_scopes_for_that_token_only(self):
        self.assertEqual(google_ads.datamanager_scope_state(), google_ads.SCOPE_UNKNOWN)
        self.assertIn("GOOGLE_ADS_REFRESH_TOKEN", google_conversions.upload_status()["reason"])
        with patch_http(FakeGoogle((200, {**TOKEN_OK[1], "scope": FULL_SCOPE}))):
            google_ads.access_token()
        self.assertEqual(google_ads.datamanager_scope_state(), google_ads.SCOPE_GRANTED)
        # En annan nyckel ärver aldrig behörigheterna.
        with override_settings(GOOGLE_ADS_REFRESH_TOKEN="1//0en-annan-nyckel-abcdefghij"):
            self.assertEqual(google_ads.datamanager_scope_state(), google_ads.SCOPE_UNKNOWN)

    def test_google_saying_the_scope_is_missing_asks_to_reconnect(self):
        GoogleAdsConnection.get_solo().set_refresh_token(REFRESH, scopes=FULL_SCOPE)
        row = self.queued("lead")
        insufficient = dm_error(403, "PERMISSION_DENIED", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
        with self.assertRaises(GoogleAdsError) as caught:
            self.upload(insufficient)
        self.assertEqual(caught.exception.status, "UPLOAD_NOT_ALLOWED")
        self.assertEqual(caught.exception.message, google_conversions.MSG_RECONNECT)
        self.assertFalse(caught.exception.is_auth_error)
        row = self.fresh(row)
        self.assertEqual(
            (row.status, row.attempts, row.error), (ConversionUpload.STATUS_QUEUED, 0, "")
        )
        self.assertEqual(google_ads.datamanager_scope_state(), google_ads.SCOPE_MISSING)
        self.assertFalse(google_conversions.upload_enabled())
        self.assertEqual(google_conversions.upload_blocked(), "")
        page = self.staff_client().get(reverse("manage:flamingo_google")).content.decode()
        self.assertIn(RECONNECT, page)


# ---------------------------------------------------------------------------
# Vägen
# ---------------------------------------------------------------------------


class PathTests(DataManagerBase):
    def test_the_setting_picks_the_path(self):
        for value, path in (
            ("", "datamanager"),
            ("datamanager", "datamanager"),
            (" DataManager ", "datamanager"),
            ("googleads", "googleads"),
            ("off", "off"),
            ("csv", "off"),
        ):
            with self.subTest(value=value), override_settings(FLAMINGO_CONVERSIONS_UPLOAD=value):
                self.assertEqual(google_conversions.upload_path(), path)
        with override_settings(FLAMINGO_CONVERSIONS_UPLOAD="csv"):
            status = google_conversions.upload_status()
            self.assertFalse(status["active"])
            self.assertEqual(status["reason"], google_conversions.MSG_UNKNOWN_PATH)

    @override_settings(FLAMINGO_CONVERSIONS_UPLOAD="googleads")
    def test_googleads_uses_upload_click_conversions_with_the_same_id(self):
        row = self.queued("lead")
        result, fake = self.upload((200, {"results": [{"gclid": row.lead.gclid}]}))
        self.assertEqual(result["sent"], 1)
        self.assertEqual(
            fake.requests[1].full_url,
            f"https://googleads.googleapis.com/{API}/customers/{CUSTOMER_ID}:uploadClickConversions",
        )
        self.assertEqual(fake.body(1)["conversions"][0]["orderId"], row.transaction_id)
        self.assertNotIn(INGEST_URL, [r.full_url for r in fake.requests])

    @override_settings(FLAMINGO_CONVERSIONS_UPLOAD="off")
    def test_off_sends_nothing_and_the_csv_has_the_row(self):
        row = self.queued("lead")
        result, fake = self.upload()
        self.assertEqual(result, {"sent": 0, "failed": 0, "waiting": 0})
        self.assertEqual(fake.requests, [])
        self.assertIn(row, manage_review.queued_uploads())
        page = self.staff_client().get(reverse("manage:flamingo_google")).content.decode()
        self.assertIn(google_conversions.MSG_OFF, page)

    def test_data_manager_needs_every_condition(self):
        row = self.queued("lead")
        cases = {
            "API:t inte inkopplat": ({**NOTHING}, {}),
            "behörigheten saknas": ({}, {"scope": WITHOUT_DM}),
            "inte under förvaltarkontot": ({}, {"google_status": FlamingoAccount.GOOGLE_ID_GIVEN}),
            "demo": ({}, {"is_demo": True}),
        }
        for label, (overrides, change) in cases.items():
            with self.subTest(label), override_settings(**overrides):
                grant(change.get("scope", FULL_SCOPE))
                fields = {k: v for k, v in change.items() if k != "scope"}
                FlamingoAccount.objects.filter(pk=self.account.pk).update(**fields)
                account = FlamingoAccount.objects.get(pk=self.account.pk)
                result, fake = self.upload(account=account)
                self.assertEqual(result["sent"], 0)
                self.assertEqual(fake.requests, [])
                FlamingoAccount.objects.filter(pk=self.account.pk).update(
                    is_demo=False, google_status=FlamingoAccount.GOOGLE_BILLING_OK
                )
        grant()
        self.assertEqual(self.fresh(row).status, ConversionUpload.STATUS_QUEUED)

    def test_a_missing_conversion_action_makes_its_rows_wait(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_conversion_actions={"lead": ACTIONS["lead"]}
        )
        self.account.refresh_from_db()
        lead_row, deal_row = self.queued("lead"), self.queued("deal")
        refused = GoogleAdsError("Fel sort.", status="CONVERSION_ACTION_TYPE")
        with mock.patch.object(
            google_conversions, "ensure_conversion_actions", side_effect=refused
        ):
            result, fake = self.upload(accepted())
        self.assertEqual(result, {"sent": 1, "failed": 0, "waiting": 1})
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(self.fresh(lead_row).status, ConversionUpload.STATUS_SENT)
        self.assertEqual(self.fresh(deal_row).status, ConversionUpload.STATUS_QUEUED)


# ---------------------------------------------------------------------------
# Nej för en rad, ett konto eller en konvertering
# ---------------------------------------------------------------------------


class PartialFailureTests(DataManagerBase):
    def test_one_bad_event_leaves_only_that_row_in_the_queue(self):
        good, bad, other = self.queued("lead"), self.queued("call"), self.queued("deal")
        bad_gclid = dm_error(
            400,
            "INVALID_ARGUMENT",
            "INVALID_ARGUMENT",
            violations=[
                ("events.events[0].ad_identifiers.gclid", "Invalid gclid.", "INVALID_FORMAT")
            ],
        )
        before = timezone.now()
        result, fake = self.upload(accepted("r-good"), bad_gclid, accepted("r-other"))
        self.assertEqual(result, {"sent": 2, "failed": 1, "waiting": 0})
        self.assertEqual(len(fake.requests), 4)
        self.assertEqual(self.fresh(good).request_id, "r-good")
        self.assertEqual(self.fresh(other).request_id, "r-other")
        bad = self.fresh(bad)
        self.assertEqual(bad.status, ConversionUpload.STATUS_QUEUED)
        self.assertEqual(bad.error, "Google: Invalid gclid.")
        self.assertEqual(bad.attempts, 1)
        self.assertGreaterEqual(bad.next_attempt_at, before + timedelta(minutes=59))
        self.assertLessEqual(bad.next_attempt_at, timezone.now() + timedelta(hours=1))
        # Den står kvar i CSV-filen, de skickade gör det inte.
        self.assertEqual(list(manage_review.queued_uploads()), [bad])
        csv = exports.offline_conversions_csv(manage_review.queued_uploads())
        self.assertIn(bad.lead.gclid, csv)
        self.assertNotIn(good.lead.gclid, csv)
        html = self.staff_client().get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn("Google: Invalid gclid.", html)
        self.assertIn(f"Försök 1 av {ConversionUpload.MAX_ATTEMPTS}", html)

    def test_an_event_google_will_never_take_is_left_to_the_file(self):
        row = self.queued("lead")
        foreign = dm_error(400, "INVALID_ARGUMENT", "INVALID_AD_IDENTIFIER_FOR_ACCOUNT")
        result, _ = self.upload(foreign)
        self.assertEqual(result["failed"], 1)
        row = self.fresh(row)
        self.assertTrue(row.api_gave_up)
        self.assertIsNone(row.next_attempt_at)
        self.assertIn("annat Google Ads-konto", row.error)
        self.assertIn(row, manage_review.queued_uploads())

    def test_an_error_for_the_customers_account_stops_it_and_shows_on_every_row(self):
        first, second = self.queued("lead"), self.queued("call")
        denied = dm_error(403, "PERMISSION_DENIED", "PERMISSION_DENIED")
        with self.assertRaises(GoogleAdsError) as caught:
            self.upload(denied)  # ett anrop, sedan inga fler
        message = google_conversions.DM_MESSAGES["PERMISSION_DENIED"]
        self.assertEqual(caught.exception.message, message)
        self.assertFalse(caught.exception.is_auth_error)
        first, second = self.fresh(first), self.fresh(second)
        self.assertEqual((first.attempts, first.error), (1, message))
        # Den andra provades inte: felet syns, men inget försök räknas.
        self.assertEqual((second.attempts, second.error), (0, message))
        self.assertEqual(second.status, ConversionUpload.STATUS_QUEUED)

    def test_a_server_error_stops_the_run_and_tries_again_later(self):
        first, second = self.queued("lead"), self.queued("call")
        with self.assertRaises(GoogleAdsError) as caught:
            self.upload(dm_error(503, "UNAVAILABLE", message="Service unavailable."))
        self.assertEqual(caught.exception.http_status, 503)
        first, second = self.fresh(first), self.fresh(second)
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.status, ConversionUpload.STATUS_QUEUED)
        self.assertEqual((second.attempts, second.error), (0, ""))

    def test_no_answer_at_all_is_retried_with_the_same_transaction_id(self):
        from urllib.error import URLError

        row = self.queued("lead")
        with self.assertRaises(GoogleAdsError):
            self.upload((URLError("timeout"), None))
        row = self.fresh(row)
        self.assertEqual((row.status, row.attempts), (ConversionUpload.STATUS_QUEUED, 1))
        later = row.next_attempt_at + timedelta(seconds=1)
        result, fake = self.upload(accepted("r-2"), now=later)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(fake.body(1)["events"][0]["transactionId"], row.transaction_id)

    def test_a_vanished_conversion_action_is_forgotten_and_its_kind_waits(self):
        lead_a, lead_b, deal = self.queued("lead"), self.queued("lead"), self.queued("deal")
        gone = dm_error(400, "INVALID_ARGUMENT", "INVALID_CONVERSION_ACTION_ID")
        result, fake = self.upload(gone, accepted("r-deal"))
        # Förfrågan väntar (åtgärden letas upp eller skapas nästa körning),
        # affären skickas, och den andra förfrågan provas inte alls.
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(result, {"sent": 1, "failed": 0, "waiting": 1})
        self.account.refresh_from_db()
        self.assertNotIn("lead", self.account.google_conversion_actions)
        self.assertIn("deal", self.account.google_conversion_actions)
        lead_a, lead_b = self.fresh(lead_a), self.fresh(lead_b)
        self.assertEqual((lead_a.status, lead_a.attempts), (ConversionUpload.STATUS_QUEUED, 0))
        self.assertIn("finns inte längre", lead_a.error)
        self.assertEqual((lead_b.attempts, lead_b.error), (0, ""))
        self.assertEqual(self.fresh(deal).status, ConversionUpload.STATUS_SENT)

    def test_quota_keeps_the_rows_without_counting_a_try(self):
        row = self.queued("lead")
        with self.assertRaises(GoogleAdsError) as caught:
            self.upload(dm_error(429, "RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED"))
        self.assertTrue(caught.exception.is_quota_error)
        row = self.fresh(row)
        self.assertEqual((row.status, row.attempts), (ConversionUpload.STATUS_QUEUED, 0))
        self.assertIn("kvoten", row.error)


# ---------------------------------------------------------------------------
# Nej till hela vägen
# ---------------------------------------------------------------------------


class RefusalTests(DataManagerBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        other = Customer.objects.create(name="Hemlig Bygg AB")
        cls.other_account = FlamingoAccount.objects.create(
            customer=other,
            is_enabled=True,
            google_ads_customer_id="222-333-4444",
            google_status=FlamingoAccount.GOOGLE_BILLING_OK,
            google_conversion_actions={
                kind: f"customers/2223334444/conversionActions/{n}"
                for n, kind in enumerate(("lead", "call", "deal"), start=21)
            },
        )

    def run_sync(self, fake):
        status_module = types.SimpleNamespace(sync_account_status=mock.Mock(return_value=None))
        out = StringIO()
        with (
            patch_http(fake),
            mock.patch.dict(sys.modules, {"apps.flamingo.google_accounts": status_module}),
            mock.patch.object(
                google_conversions,
                "ensure_conversion_actions",
                side_effect=lambda account: account.google_conversion_actions,
            ),
            mock.patch.object(google_reports, "sync_stats", return_value=0),
        ):
            call_command("flamingo_google_sync", stdout=out)
        return out.getvalue()

    def test_service_disabled_stops_every_account_with_one_explanation(self):
        mine = self.queued("lead")
        theirs = ConversionUpload.objects.create(
            lead=Lead.objects.create(
                account=self.other_account,
                gclid="G-other",
                created_at=timezone.now() - timedelta(hours=8),
            ),
            kind=ConversionUpload.KIND_LEAD,
        )
        fake = FakeGoogle(TOKEN_OK, service_disabled())
        out = self.run_sync(fake)
        # Ett anrop: det andra kontot försöker inte.
        self.assertEqual([r.full_url for r in fake.requests][1:], [INGEST_URL])
        self.assertEqual(out.count("Konverteringarna stoppade:"), 1)
        self.assertIn("2 konto(n) synkade med Google, 0 med fel.", out)
        explanation = google_conversions.DM_REFUSALS["SERVICE_DISABLED"]
        self.assertEqual(google_conversions.upload_blocked(), explanation)
        self.assertFalse(google_conversions.upload_enabled())
        for row in (mine, theirs):
            row = self.fresh(row)
            self.assertEqual((row.status, row.attempts, row.error), ("queued", 0, ""))
            self.assertIn(row, manage_review.queued_uploads())
        for account in (self.account, self.other_account):
            account.refresh_from_db()
            self.assertNotIn("Data Manager", account.google_sync_error)
        client = self.staff_client()
        page = client.get(reverse("manage:flamingo_google")).content.decode()
        self.assertEqual(page.count(explanation), 1)
        self.assertIn("Försök ladda upp igen", page)
        queue = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn("Uppladdningen med API:t är stoppad", queue)
        # Byrån slår på API:t och försöker igen.
        client.post(reverse("manage:flamingo_google_uploads_retry"))
        self.assertEqual(google_conversions.upload_blocked(), "")
        cache.clear()
        result, _ = self.upload(accepted())
        self.assertEqual(result["sent"], 1)

    def test_not_allowlisted_is_a_refusal_for_everyone_too(self):
        row = self.queued("lead")
        with self.assertRaises(GoogleAdsError) as caught:
            self.upload(dm_error(403, "PERMISSION_DENIED", "NOT_ALLOWLISTED"))
        self.assertEqual(caught.exception.status, "UPLOAD_NOT_ALLOWED")
        self.assertEqual(
            google_conversions.upload_blocked(), google_conversions.DM_REFUSALS["NOT_ALLOWLISTED"]
        )
        self.assertEqual(self.fresh(row).attempts, 0)
        # Spärren gäller Data Manager API, inte den gamla vägen.
        with override_settings(FLAMINGO_CONVERSIONS_UPLOAD="googleads"):
            self.assertEqual(google_conversions.upload_blocked(), "")
        result, fake = self.upload(account=self.other_account)
        self.assertEqual((result["sent"], fake.requests), (0, []))


# ---------------------------------------------------------------------------
# Backoff och transactionId
# ---------------------------------------------------------------------------


class BackoffTests(DataManagerBase):
    def test_backoff_doubles_up_to_a_day(self):
        hours = [google_conversions.backoff(n) / timedelta(hours=1) for n in range(1, 9)]
        self.assertEqual(hours, [1, 2, 4, 8, 16, 24, 24, 24])

    def test_a_row_waits_out_its_backoff_and_keeps_its_transaction_id(self):
        row = self.queued("lead")
        with self.assertRaises(GoogleAdsError):
            self.upload(dm_error(500, "INTERNAL", "INTERNAL_ERROR"))
        row = self.fresh(row)
        first_wait = row.next_attempt_at
        result, fake = self.upload()  # väntar: inget anrop
        self.assertEqual((result["sent"], fake.requests), (0, []))
        with self.assertRaises(GoogleAdsError):
            self.upload(dm_error(500, "INTERNAL", "INTERNAL_ERROR"), now=first_wait)
        row = self.fresh(row)
        self.assertEqual(row.attempts, 2)
        self.assertEqual(row.next_attempt_at - first_wait, timedelta(hours=2))
        cache.clear()
        result, fake = self.upload(accepted(), now=row.next_attempt_at)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(
            fake.body(1)["events"][0]["transactionId"], f"adx-flamingo-{row.lead_id}-lead"
        )

    def test_after_the_last_try_the_api_stops_and_the_button_starts_over(self):
        row = self.queued("lead")
        ConversionUpload.objects.filter(pk=row.pk).update(
            attempts=ConversionUpload.MAX_ATTEMPTS, error="Google: nej."
        )
        result, fake = self.upload()
        self.assertEqual(fake.requests, [])
        client = self.staff_client()
        queue = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn("API:t har slutat försöka: ladda upp den med filen.", queue)
        page = client.get(reverse("manage:flamingo_google")).content.decode()
        self.assertIn("Väntar på CSV-filen", page)
        response = client.post(reverse("manage:flamingo_google_uploads_retry"))
        self.assertIn("1 konvertering som väntade efter ett nej är med.", " ".join(texts(response)))
        self.assertEqual(self.fresh(row).attempts, 0)
        result, _ = self.upload(accepted())
        self.assertEqual(result["sent"], 1)

    def test_the_transaction_id_is_stable_and_the_same_on_both_paths(self):
        row = self.queued("deal")
        expected = f"adx-flamingo-{row.lead_id}-deal"
        self.assertEqual(row.transaction_id, expected)
        self.assertEqual(google_conversions.conversion_event(row)["transactionId"], expected)
        self.assertEqual(
            google_conversions.click_conversion(row, ACTIONS["deal"])["orderId"], expected
        )
        self.assertEqual(self.fresh(row).transaction_id, expected)


# ---------------------------------------------------------------------------
# En väg per rad: CSV-filen och API:t
# ---------------------------------------------------------------------------


class OneWayTests(DataManagerBase):
    def csv_url(self):
        return reverse("manage:flamingo_conversions_csv")

    def test_a_downloaded_row_is_never_sent_by_the_api(self):
        in_file, other = self.queued("lead"), self.queued("call")
        client = self.staff_client()
        response = client.get(self.csv_url(), {"kund": self.acme.pk})
        self.assertIn(in_file.lead.gclid, response.content.decode())
        in_file = self.fresh(in_file)
        self.assertIsNotNone(in_file.downloaded_at)
        # Båda raderna var med i filen, så API:t skickar ingen av dem.
        self.assertIsNotNone(self.fresh(other).downloaded_at)
        result, fake = self.upload()
        self.assertEqual((result["sent"], fake.requests), (0, []))
        queue = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn("I en nedladdad fil", queue)
        self.assertIn("API:t skickar den inte.", queue)
        client.post(self.csv_url(), {"kund": self.acme.pk, "upload": [in_file.pk, other.pk]})
        self.assertEqual(self.fresh(in_file).status, ConversionUpload.STATUS_EXPORTED)
        # "Försök ladda upp igen" lämnar raderna i filen åt filen.
        ConversionUpload.objects.filter(pk=other.pk).update(
            status=ConversionUpload.STATUS_QUEUED, attempts=3
        )
        google_conversions.retry_now()
        self.assertEqual(self.fresh(other).attempts, 3)
        result, fake = self.upload()
        self.assertEqual(fake.requests, [])

    def test_a_row_the_api_sent_never_comes_with_a_file(self):
        sent, later = self.queued("lead"), self.queued("call")
        Lead.objects.filter(pk=later.lead_id).update(created_at=timezone.now())
        self.upload(accepted())
        self.assertEqual(self.fresh(sent).status, ConversionUpload.STATUS_SENT)
        client = self.staff_client()
        body = client.get(self.csv_url(), {"kund": self.acme.pk}).content.decode()
        self.assertNotIn(sent.lead.gclid, body)
        self.assertIn(later.lead.gclid, body)
        client.post(self.csv_url(), {"kund": self.acme.pk, "upload": [sent.pk, later.pk]})
        self.assertEqual(self.fresh(sent).status, ConversionUpload.STATUS_SENT)
        self.assertIsNone(self.fresh(sent).exported_at)
        self.assertEqual(self.fresh(later).status, ConversionUpload.STATUS_EXPORTED)

    def test_rows_waiting_after_a_no_stay_in_the_file(self):
        row = self.queued("lead")
        with self.assertRaises(GoogleAdsError):
            self.upload(dm_error(503, "UNAVAILABLE"))
        body = self.staff_client().get(self.csv_url()).content.decode()
        self.assertIn(row.lead.gclid, body)


# ---------------------------------------------------------------------------
# Googles besked om det som skickats
# ---------------------------------------------------------------------------


class VerdictTests(DataManagerBase):
    def sent_row(self, request_id, ago=timedelta(hours=1), kind="lead"):
        row = self.queued(kind)
        ConversionUpload.objects.filter(pk=row.pk).update(
            status=ConversionUpload.STATUS_SENT,
            sent_at=timezone.now() - ago,
            request_id=request_id,
            response={"api": "datamanager", "request_id": request_id},
        )
        return self.fresh(row)

    def check(self, *responses, now=None, account=None):
        cache.clear()
        fake = FakeGoogle(TOKEN_OK, *responses) if responses else FakeGoogle()
        with patch_http(fake):
            result = google_conversions.check_sent(account or self.account, now=now)
        return result, fake

    def test_success_keeps_the_row_sent(self):
        row = self.sent_row("dm-1")
        result, fake = self.check(verdict("SUCCESS"))
        self.assertEqual(result, {"confirmed": 1, "requeued": 0, "pending": 0})
        request = fake.requests[1]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, f"{STATUS_URL}?requestId=dm-1")
        self.assertIsNone(request.data)
        row = self.fresh(row)
        self.assertEqual(row.status, ConversionUpload.STATUS_SENT)
        self.assertIsNotNone(row.checked_at)
        self.assertEqual(row.response["status"], "SUCCESS")

    def test_a_failure_goes_back_to_the_queue_and_the_file(self):
        row = self.sent_row("dm-2")
        result, _ = self.check(verdict("FAILED", "CLICK_NOT_FOUND"))
        self.assertEqual(result["requeued"], 1)
        row = self.fresh(row)
        self.assertEqual(row.status, ConversionUpload.STATUS_QUEUED)
        self.assertEqual(row.error, "Google hittar inte klicket i kundens konto.")
        self.assertEqual((row.attempts, row.request_id), (1, ""))
        self.assertIsNone(row.sent_at)
        self.assertEqual(row.response["previous_request_id"], "dm-2")
        self.assertIn(row, manage_review.queued_uploads())
        # Nästa försök efter väntan, med samma transactionId.
        result, fake = self.upload(accepted("dm-3"), now=row.next_attempt_at)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(fake.body(1)["events"][0]["transactionId"], row.transaction_id)

    def test_a_duplicate_counts_as_sent(self):
        row = self.sent_row("dm-4")
        self.check(verdict("FAILED", "DUPLICATE_TRANSACTION_ID"))
        row = self.fresh(row)
        self.assertEqual(row.status, ConversionUpload.STATUS_SENT)
        self.assertEqual(row.response["note"], google_conversions.MSG_DUPLICATE)

    def test_a_permanent_failure_is_left_to_the_file(self):
        row = self.sent_row("dm-5")
        self.check(verdict("FAILED", "INVALID_GCLID"))
        row = self.fresh(row)
        self.assertEqual(row.status, ConversionUpload.STATUS_QUEUED)
        self.assertTrue(row.api_gave_up)
        self.assertIn(row, manage_review.queued_uploads())

    def test_processing_waits_and_gives_up_after_three_days(self):
        row = self.sent_row("dm-6")
        result, _ = self.check(verdict("PROCESSING"))
        self.assertEqual(result["pending"], 1)
        self.assertIsNone(self.fresh(row).checked_at)
        cache.clear()
        result, _ = self.check(verdict("PROCESSING"), now=timezone.now() + timedelta(days=3))
        self.assertEqual(result["confirmed"], 1)
        row = self.fresh(row)
        self.assertEqual(row.status, ConversionUpload.STATUS_SENT)
        self.assertEqual(row.response["note"], google_conversions.MSG_NO_VERDICT)

    def test_a_verdict_that_will_never_come_settles_the_row(self):
        row = self.sent_row("dm-7")
        self.check(dm_error(400, "INVALID_ARGUMENT", "REQUEST_TOO_OLD"))
        row = self.fresh(row)
        self.assertEqual(row.status, ConversionUpload.STATUS_SENT)
        self.assertEqual(row.response["note"], google_conversions.MSG_NO_VERDICT)

    def test_nothing_is_asked_before_half_an_hour(self):
        self.sent_row("dm-8", ago=timedelta(minutes=10))
        result, fake = self.check()
        self.assertEqual(
            (result, fake.requests), ({"confirmed": 0, "requeued": 0, "pending": 0}, [])
        )

    @override_settings(FLAMINGO_CONVERSIONS_UPLOAD="off")
    def test_verdicts_are_read_after_the_path_is_switched(self):
        row = self.sent_row("dm-9")
        self.assertFalse(google_conversions.upload_enabled())
        self.assertTrue(google_conversions.verdicts_enabled())
        self.check(verdict("FAILED", "TOO_RECENT_CLICK"))
        self.assertEqual(self.fresh(row).status, ConversionUpload.STATUS_QUEUED)


# ---------------------------------------------------------------------------
# Kommandot, prövningen, demot och nycklarna
# ---------------------------------------------------------------------------


class CommandTests(DataManagerBase):
    def run_sync(self, fake, *args):
        status_module = types.SimpleNamespace(sync_account_status=mock.Mock(return_value=None))
        out = StringIO()
        with (
            patch_http(fake),
            mock.patch.dict(sys.modules, {"apps.flamingo.google_accounts": status_module}),
            mock.patch.object(
                google_conversions,
                "ensure_conversion_actions",
                side_effect=lambda account: account.google_conversion_actions,
            ),
            mock.patch.object(google_reports, "sync_stats", return_value=0),
        ):
            call_command("flamingo_google_sync", *args, "--verbosity", "2", stdout=out)
        return out.getvalue()

    def test_the_command_sends_with_data_manager_and_reads_verdicts(self):
        new = self.queued("lead")
        old = self.queued("call")
        ConversionUpload.objects.filter(pk=old.pk).update(
            status=ConversionUpload.STATUS_SENT,
            sent_at=timezone.now() - timedelta(hours=2),
            request_id="dm-old",
        )
        fake = FakeGoogle(TOKEN_OK, accepted("dm-new"), verdict("SUCCESS"))
        out = self.run_sync(fake)
        self.assertEqual(
            [r.full_url for r in fake.requests][1:], [INGEST_URL, f"{STATUS_URL}?requestId=dm-old"]
        )
        self.assertIn("1 konverteringar skickade", out)
        self.assertIn("Googles besked: 1 klara, 0 tillbaka i kö", out)
        self.assertEqual(self.fresh(new).request_id, "dm-new")
        self.assertIsNotNone(self.fresh(old).checked_at)

    @override_settings(FLAMINGO_CONVERSIONS_UPLOAD="googleads")
    def test_the_command_follows_the_setting(self):
        row = self.queued("lead")
        fake = FakeGoogle(TOKEN_OK, (200, {"results": [{"gclid": row.lead.gclid}]}))
        self.run_sync(fake)
        self.assertTrue(fake.requests[1].full_url.endswith(":uploadClickConversions"))
        self.assertEqual(self.fresh(row).status, ConversionUpload.STATUS_SENT)

    def test_validate_only_asks_google_and_changes_nothing(self):
        good, bad = self.queued("lead"), self.queued("call")
        invalid = dm_error(
            400,
            "INVALID_ARGUMENT",
            "INVALID_ARGUMENT",
            violations=[("events.events[0].event_timestamp", "Too old.", "EVENT_TIME_INVALID")],
        )
        fake = FakeGoogle(TOKEN_OK, (200, {}), invalid)
        out = self.run_sync(fake, "--prova")
        self.assertIs(fake.body(1)["validateOnly"], True)
        self.assertIs(fake.body(2)["validateOnly"], True)
        self.assertIn("1 konverteringar godkända av Google, 1 med fel, inget skickat", out)
        self.assertIn(f"rad {bad.pk}:", out)
        for row in (good, bad):
            row = self.fresh(row)
            self.assertEqual(
                (row.status, row.attempts, row.error, row.request_id),
                (ConversionUpload.STATUS_QUEUED, 0, "", ""),
            )


class DemoTests(DataManagerBase):
    def test_the_demo_never_reaches_google(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        demo = FlamingoAccount.objects.get(pk=self.account.pk)
        queued = self.queued("lead")
        sent = self.queued("call")
        ConversionUpload.objects.filter(pk=sent.pk).update(
            status=ConversionUpload.STATUS_SENT,
            sent_at=timezone.now() - timedelta(hours=2),
            request_id="dm-demo",
        )
        fake = FakeGoogle()
        with patch_http(fake):
            self.assertEqual(google_conversions.upload_queued(demo)["sent"], 0)
            self.assertEqual(google_conversions.upload_queued(demo, validate_only=True)["sent"], 0)
            self.assertEqual(google_conversions.check_sent(demo)["confirmed"], 0)
            body = self.staff_client().get(reverse("manage:flamingo_conversions_csv")).content
            call_command("flamingo_google_sync", stdout=StringIO())
        self.assertEqual(fake.requests, [])
        self.assertNotIn(queued.lead.gclid, body.decode())
        queued = self.fresh(queued)
        self.assertEqual(
            (queued.status, queued.downloaded_at), (ConversionUpload.STATUS_QUEUED, None)
        )


class SecretTests(DataManagerBase):
    SECRETS = (ACCESS, REFRESH, CLIENT_SECRET, DEV_TOKEN)

    def assert_clean(self, *texts):
        joined = "\n".join(str(text) for text in texts)
        for secret in self.SECRETS:
            self.assertNotIn(secret, joined)
        self.assertNotIn("ya29.", joined)

    def test_the_token_never_reaches_errors_rows_or_logs(self):
        row, other = self.queued("lead"), self.queued("call")
        leaky = dm_error(
            400,
            "INVALID_ARGUMENT",
            "INVALID_ARGUMENT",
            message=f"Bad token {ACCESS} for {REFRESH} with {CLIENT_SECRET}",
            violations=[
                (
                    "events.events[0].ad_identifiers.gclid",
                    f"Header Authorization: Bearer {ACCESS} refresh_token={REFRESH}",
                    "INVALID_FORMAT",
                )
            ],
        )
        warned = accepted(
            "dm-ok",
            fieldWarnings=[
                {
                    "field": "events.events[0].consent",
                    "description": f"Saw {ACCESS} and {DEV_TOKEN}",
                    "reason": "SOME_WARNING",
                }
            ],
        )
        denied = dm_error(403, "PERMISSION_DENIED", "PERMISSION_DENIED", message=f"No {ACCESS}")
        with self.assertLogs("apps.flamingo", "WARNING") as logs:
            self.upload(leaky, warned)
            # Googles text står kvar, utan nycklarna.
            first_error = self.fresh(row).error
            self.assertEqual(first_error, "Google: Header Authorization: *** ***")
            third = self.queued("deal")
            with self.assertRaises(GoogleAdsError) as caught:
                self.upload(denied, now=timezone.now() + timedelta(hours=2))
        rows = [self.fresh(r) for r in (row, other, third)]
        self.assert_clean(
            "\n".join(logs.output),
            caught.exception.message,
            str(caught.exception),
            *[r.error for r in rows],
            *[json.dumps(r.response) for r in rows],
            *[e.get("message") for e in caught.exception.errors],
            first_error,
        )
        self.assertIn("SOME_WARNING", self.fresh(other).response["warning"])
        # Kontots fel ersatte radens: Googles text visas inte, bara vår.
        self.assertEqual(rows[0].error, google_conversions.DM_MESSAGES["PERMISSION_DENIED"])
