"""
SMS-API:t (apps/sms). Inget test pratar med 46elks: elks._post byts ut mot
FakeElks, och urlopen är spärrad i varje test så att ett missat utbyte
fäller testet i stället för att skicka något.
"""

import csv
import hashlib
import http.client
import io
import json
import socket
import threading
import time as time_module
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import sentry_sdk
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from sentry_sdk.transport import Transport

from apps.common import sentry
from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import elks, encoding, numbers, pricing, service
from .manage_views import _cell
from .models import (
    UNITS_PER_KR,
    MonthlyStatement,
    SmsAccount,
    SmsApiKey,
    SmsMessage,
    validate_sender,
)

User = get_user_model()

AGENCY = "byran@adx.example"
TEST_SETTINGS = {
    "ELKS_API_USERNAME": "testanvandare",
    "ELKS_API_PASSWORD": "testlosen-hemligt",
    "SMS_PROVIDER": "46elks",
    "SMS_SEND_LIVE": True,
    "SITE_BASE_URL": "https://adx.example",
    "SMS_CALLBACK_BASE_URL": "",
    "SMS_DLR_ALLOWED_IPS": [],
    "INQUIRY_NOTIFICATION_EMAIL": AGENCY,
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "SMS_RATE_PER_SECOND": 20,
    "SMS_RATE_PER_MINUTE": 60,
    "SMS_GLOBAL_PER_MINUTE": 80,
    "SMS_DAILY_MAX_PER_KEY": 5000,
}

FICTIONAL = "+46701740605"  # PTS serie för fiktiva nummer
FICTIONAL_2 = "+46701740606"
NORWAY = "+4791234567"


class FakeElks:
    """Står i för elks._post: svarar som 46elks gjorde med dryrun 2026-10-03."""

    def __init__(self, per_part=5200, fail_send=None, fail_dryrun=None):
        self.per_part = per_part
        self.fail_send = fail_send
        self.fail_dryrun = fail_dryrun
        self.calls = []
        self.counter = 0

    def __call__(self, fields):
        self.calls.append(dict(fields))
        parts = encoding.analyse(fields["message"]).parts
        if fields.get("dryrun") == "yes":
            if self.fail_dryrun:
                raise elks.ElksError(self.fail_dryrun, 403)
            return {
                "status": "created",
                "direction": "outgoing",
                "from": fields["from"],
                "to": fields["to"],
                "message": fields["message"],
                "estimated_cost": self.per_part * parts,
                "parts": parts,
            }
        if self.fail_send:
            raise elks.ElksError(self.fail_send, 403)
        self.counter += 1
        return {
            "id": f"s{self.counter:032x}",
            "status": "created",
            "direction": "outgoing",
            "from": fields["from"],
            "to": fields["to"],
            "message": fields["message"],
            "created": "2026-10-03T13:37:42.314100",
            "cost": self.per_part * parts,
            "parts": parts,
        }

    @property
    def sends(self):
        return [c for c in self.calls if c.get("dryrun") != "yes"]

    @property
    def dryruns(self):
        return [c for c in self.calls if c.get("dryrun") == "yes"]


def no_network(*args, **kwargs):
    raise AssertionError("Testet försökte anropa 46elks på riktigt.")


@override_settings(**TEST_SETTINGS)
class SmsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Acme Bygg AB", email="kund@acme.example")
        cls.other = Customer.objects.create(name="Annan Firma", email="kund@annan.example")
        cls.contact = User.objects.create_user("nina", email="nina@acme.example")
        cls.acme.users.add(cls.contact)
        cls.other_contact = User.objects.create_user("olle", email="olle@annan.example")
        cls.other.users.add(cls.other_contact)
        cls.account = SmsAccount.objects.create(
            customer=cls.acme,
            is_enabled=True,
            # Fast datum, som tjänsteåret: årsavgiften beror på när SMS
            # aktiverades (pricing.fee_due), och testerna ska inte bero på dagen.
            enabled_at=datetime(2026, 10, 3, 9, 0, tzinfo=pricing.STOCKHOLM),
            sender_name="AcmeBygg",
            service_year_start=date(2026, 10, 3),
            # De flesta portaltesterna gäller kunden som sköter API:t själv;
            # SelfServiceTests prövar standardläget där ADX sköter det.
            customer_manages_api=True,
        )
        cls.other_account = SmsAccount.objects.create(
            customer=cls.other, is_enabled=True, enabled_at=timezone.now(), sender_name="Annan"
        )

    def setUp(self):
        cache.clear()
        patcher = mock.patch("apps.sms.elks.urlopen", side_effect=no_network)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key, self.raw = SmsApiKey.issue(self.account, "Webbshop", self.staff)
        self.other_key, self.other_raw = SmsApiKey.issue(self.other_account, "Annans", None)

    def fake(self, **kwargs):
        fake = FakeElks(**kwargs)
        patcher = mock.patch("apps.sms.elks._post", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def api_post(self, payload, raw=None, path="/api/sms/v1/messages/", **extra):
        return self.client.post(
            path,
            data=json.dumps(payload) if not isinstance(payload, str) else payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}",
            **extra,
        )

    def api_get(self, path, raw=None):
        return self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    def message(self, account=None, **kwargs):
        defaults = {
            "account": account or self.account,
            "to": FICTIONAL,
            "country": "SE",
            "sender": "AcmeBygg",
            "body": "Hej",
            "parts": 1,
            "status": SmsMessage.Status.DELIVERED,
            "provider_id": f"s{SmsMessage.objects.count() + 1:032x}",
            "provider_cost": 5200,
            "markup": 500,
            "customer_price": 5700,
            "sent_at": timezone.now(),
        }
        defaults.update(kwargs)
        return SmsMessage.objects.create(**defaults)


# ---------------------------------------------------------------- kodning


class EncodingTests(TestCase):
    def test_gsm_lengths_match_46elks(self):
        self.assertEqual(encoding.analyse("a" * 160).parts, 1)
        self.assertEqual(encoding.analyse("a" * 161).parts, 2)
        self.assertEqual(encoding.analyse("a" * 306).parts, 2)
        self.assertEqual(encoding.analyse("a" * 307).parts, 3)
        self.assertEqual(encoding.analyse("").parts, 0)

    def test_swedish_letters_are_gsm(self):
        result = encoding.analyse("Hej! Åsa, Örjan och Ärla äter é-glass på Ö.")
        self.assertEqual(result.encoding, encoding.GSM7)
        self.assertEqual(result.parts, 1)

    def test_extended_characters_count_double_and_are_not_split(self):
        # 80 par "{}" = 320 platser: 3 delar (46elks svarade 3 med dryrun).
        result = encoding.analyse("{}" * 80)
        self.assertEqual((result.encoding, result.units, result.parts), ("gsm7", 320, 3))
        self.assertEqual(encoding.analyse("€").units, 2)
        # 152 + euro (2) får inte plats i första delens 153: euron börjar del 2.
        self.assertEqual(encoding.analyse("a" * 152 + "€" + "a" * 152).parts, 3)

    def test_emoji_forces_ucs2(self):
        result = encoding.analyse("Hej \U0001f600")
        self.assertEqual(result.encoding, encoding.UCS2)
        self.assertEqual(result.parts, 1)
        self.assertIn("\U0001f600", result.non_gsm)
        # 36 emojis = 72 UTF-16-enheter: 2 delar (som 46elks).
        self.assertEqual(encoding.analyse("\U0001f600" * 36).parts, 2)

    def test_ucs2_limits(self):
        self.assertEqual(encoding.analyse("ê" * 70).parts, 1)
        self.assertEqual(encoding.analyse("ê" * 71).parts, 2)
        self.assertEqual(encoding.analyse("ê" * 134).parts, 2)
        self.assertEqual(encoding.analyse("ê" * 135).parts, 3)

    def test_curly_quote_forces_ucs2(self):
        self.assertEqual(encoding.analyse("Det ’r bra").encoding, encoding.UCS2)


# ---------------------------------------------------------------- nummer


class NumberTests(TestCase):
    def test_formats_are_normalised_to_e164_with_country(self):
        for raw in ("+46701740605", "070-174 06 05", "0046701740605", "+46 70 174 06 05"):
            number = numbers.parse(raw)
            self.assertEqual((number.e164, number.country), ("+46701740605", "SE"), raw)
        self.assertEqual(numbers.parse(NORWAY).country, "NO")

    def test_invalid_and_landline_numbers_are_refused(self):
        for raw in ("", "abc", "+4612", "+46 8 465 004 00", "+881234", "1" * 40):
            with self.assertRaises(numbers.InvalidNumber, msg=raw):
                numbers.parse(raw)

    def test_country_list(self):
        self.assertEqual(numbers.parse_country_list("se, no dk SE"), ["SE", "NO", "DK"])
        with self.assertRaises(ValueError):
            numbers.parse_country_list("SE XX")
        self.assertEqual(numbers.country_name("NO"), "Norge")


# ---------------------------------------------------------------- pris och underlag


class PricingTests(SmsTestCase):
    def test_amount_formatting(self):
        self.assertEqual(pricing.kr_text(5700), "0,57")
        self.assertEqual(pricing.kr_text(12_345_678), "1\xa0234,57")
        self.assertEqual(pricing.kr_text(9_990_000, 0), "999")
        self.assertEqual(pricing.api_amount(5700), "0.5700")
        self.assertEqual(pricing.per_unit_ore(5750), "57,5")

    def test_yearly_fee_month(self):
        a = self.account  # tjänsteåret börjar 2026-10-03
        self.assertTrue(pricing.fee_due(a, date(2026, 10, 1)))
        self.assertFalse(pricing.fee_due(a, date(2026, 11, 1)))
        self.assertFalse(pricing.fee_due(a, date(2026, 9, 1)))
        self.assertTrue(pricing.fee_due(a, date(2027, 10, 1)))
        a.is_enabled = False
        self.assertFalse(pricing.fee_due(a, date(2027, 10, 1)))
        self.assertTrue(pricing.fee_due(a, date(2027, 10, 1), sms_count=3))
        a.is_enabled = True
        a.yearly_fee_kr = 0
        self.assertFalse(pricing.fee_due(a, date(2026, 10, 1)))

    def test_statement_maths_with_fee_and_countries(self):
        october = datetime(2026, 10, 15, 12, 0, tzinfo=pricing.STOCKHOLM)
        self.message(created_at=october)
        self.message(
            created_at=october, parts=2, provider_cost=10400, markup=1000, customer_price=11400
        )
        self.message(
            created_at=october,
            country="NO",
            to=NORWAY,
            provider_cost=7000,
            customer_price=7500,
            status=SmsMessage.Status.FAILED,
            error_code="delivery_failed",
        )
        # Kostar inget och syns inte på underlaget:
        self.message(created_at=october, status=SmsMessage.Status.REJECTED, customer_price=0)
        self.message(
            created_at=october,
            status=SmsMessage.Status.FAILED,
            error_code="provider_error",
            provider_id="",
            provider_cost=0,
            markup=0,
            customer_price=0,
        )
        self.message(created_at=october, test_mode=True, provider_id="")
        self.message(created_at=datetime(2026, 9, 30, 23, 30, tzinfo=pricing.STOCKHOLM))
        # Ett annat kontos sms räknas aldrig.
        self.message(account=self.other_account, created_at=october)

        s = pricing.build_statement(self.account, date(2026, 10, 1))
        self.assertEqual(s.sms_count, 3)
        self.assertEqual(s.parts, 4)
        self.assertEqual(s.provider_cost, 5200 + 10400 + 7000)
        self.assertEqual(s.markup, 500 + 1000 + 500)
        self.assertEqual(s.fee, 999 * UNITS_PER_KR)
        lines = {line["country"]: line for line in s.lines}
        self.assertEqual((lines["SE"]["sms"], lines["SE"]["total"]), (2, 17100))
        self.assertEqual(lines["NO"]["total"], 7500)
        self.assertEqual(s.total, 5700 + 11400 + 7500 + 9_990_000)
        self.assertEqual(s.total, s.provider_cost + s.markup + s.fee)

    def test_statement_total_is_provider_plus_markup_plus_fee(self):
        november = datetime(2026, 11, 2, 9, 0, tzinfo=pricing.STOCKHOLM)
        self.message(created_at=november)
        s = pricing.build_statement(self.account, date(2026, 11, 1))
        self.assertEqual(s.fee, 0)
        self.assertEqual(s.total, s.provider_cost + s.markup + s.fee)

    def test_close_freezes_and_is_idempotent(self):
        september = date(2026, 9, 1)
        self.account.service_year_start = date(2026, 9, 10)
        self.account.save()
        self.message(created_at=datetime(2026, 9, 12, 10, 0, tzinfo=pricing.STOCKHOLM))
        now = datetime(2026, 10, 1, 3, 0, tzinfo=pricing.STOCKHOLM)
        statement, made = pricing.close_statement(self.account, september, now=now)
        self.assertTrue(made)
        self.assertEqual(statement.total, 5700 + 9_990_000)
        self.assertIsNotNone(statement.closed_at)
        # Ett sent tillägg i månaden ändrar inte det stängda underlaget.
        self.message(created_at=datetime(2026, 9, 13, 10, 0, tzinfo=pricing.STOCKHOLM))
        again, made = pricing.close_statement(self.account, september, now=now)
        self.assertFalse(made)
        self.assertEqual(again.pk, statement.pk)
        again.refresh_from_db()
        self.assertEqual(again.sms_count, 1)
        with self.assertRaises(ValueError):
            pricing.close_statement(self.account, date(2026, 10, 1), now=now)

    def test_empty_month_without_fee_is_not_saved(self):
        now = datetime(2026, 12, 1, 3, 0, tzinfo=pricing.STOCKHOLM)
        statement, made = pricing.close_statement(self.account, date(2026, 11, 1), now=now)
        self.assertIsNone(statement)
        self.assertFalse(made)
        self.assertFalse(MonthlyStatement.objects.exists())

    def test_next_fee_period(self):
        self.account.service_year_start = date(2026, 3, 5)
        now = datetime(2026, 10, 3, 12, tzinfo=pricing.STOCKHOLM)
        self.assertEqual(pricing.next_fee_period(self.account, now), date(2027, 3, 1))
        self.account.service_year_start = date(2026, 10, 3)
        self.assertEqual(pricing.next_fee_period(self.account, now), date(2026, 10, 1))


# ---------------------------------------------------------------- nycklar


class KeyTests(SmsTestCase):
    def test_only_the_hash_is_stored(self):
        key, raw = SmsApiKey.issue(self.account, "Bokning")
        self.assertTrue(raw.startswith("adxsms_"))
        self.assertEqual(key.secret_hash, hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(key.prefix, raw[:12])
        self.assertGreater(len(raw), 40)
        stored = json.dumps(list(SmsApiKey.objects.filter(pk=key.pk).values()), default=str)
        self.assertNotIn(raw, stored)
        self.assertEqual(SmsApiKey.lookup(raw), key)

    def test_revoked_or_unknown_keys_are_refused(self):
        self.fake()
        self.key.revoke(self.staff)
        response = self.api_get("/api/sms/v1/usage/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_key")
        for bad in ("", "adxsms_fel", "Bearer", self.raw + "x", "adx_" + self.raw[7:]):
            response = self.client.get("/api/sms/v1/usage/", HTTP_AUTHORIZATION=f"Bearer {bad}")
            self.assertEqual(response.status_code, 401, bad)
        self.assertEqual(self.client.get("/api/sms/v1/usage/").status_code, 401)

    def test_last_used_is_recorded(self):
        self.fake()
        self.assertIsNone(self.key.last_used_at)
        self.api_get("/api/sms/v1/usage/")
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.last_used_at)


# ---------------------------------------------------------------- 46elks-klienten


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@override_settings(**TEST_SETTINGS)
class ElksClientTests(TestCase):
    def test_send_posts_form_with_basic_auth(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["auth"] = request.headers["Authorization"]
            seen["data"] = request.data.decode()
            seen["timeout"] = timeout
            return _Response(b'{"id": "sabc", "status": "created", "cost": 5200, "parts": 1}')

        with mock.patch("apps.sms.elks.urlopen", side_effect=fake_urlopen):
            result = elks.send("AcmeBygg", FICTIONAL, "Hej", whendelivered="https://adx.example/x/")
        self.assertEqual(seen["url"], "https://api.46elks.com/a1/sms")
        self.assertTrue(seen["auth"].startswith("Basic "))
        self.assertIn("whendelivered=https%3A%2F%2Fadx.example%2Fx%2F", seen["data"])
        self.assertNotIn("dryrun", seen["data"])
        self.assertEqual(seen["timeout"], elks.TIMEOUT_SECONDS)
        self.assertEqual((result.id, result.cost, result.parts), ("sabc", 5200, 1))

    @override_settings(SMS_SEND_LIVE=False)
    def test_not_live_always_dryruns(self):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["data"] = request.data.decode()
            return _Response(b'{"status": "created", "estimated_cost": 5200, "parts": 1}')

        with mock.patch("apps.sms.elks.urlopen", side_effect=fake_urlopen):
            result = elks.send("AcmeBygg", FICTIONAL, "Hej")
        self.assertIn("dryrun=yes", seen["data"])
        self.assertTrue(result.dryrun)
        self.assertEqual(result.cost, 5200)

    def test_http_error_text_is_kept_and_credentials_never_appear(self):
        import io

        error = HTTPError(
            elks.API_URL, 403, "Forbidden", {}, io.BytesIO(b"Alphanumeric numbers may not")
        )
        with mock.patch("apps.sms.elks.urlopen", side_effect=error):
            with self.assertLogs("apps.sms", level="DEBUG") as logs:
                elks.logger.debug("start")
                with self.assertRaises(elks.ElksError) as ctx:
                    elks.send("1Acme", FICTIONAL, "Hej")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("Alphanumeric", str(ctx.exception))
        for text in [str(ctx.exception), *logs.output]:
            self.assertNotIn("testlosen-hemligt", text)
            self.assertNotIn("testanvandare", text)

    def test_failed_status_and_garbage_are_errors(self):
        for body in (b'{"status": "failed"}', b"<html>", b"[]"):
            with mock.patch("apps.sms.elks.urlopen", return_value=_Response(body)):
                with self.assertRaises(elks.ElksError, msg=body):
                    elks.send("AcmeBygg", FICTIONAL, "Hej")

    @override_settings(ELKS_API_PASSWORD="")
    def test_not_configured(self):
        self.assertFalse(elks.is_configured())
        with self.assertRaises(elks.ElksError):
            elks.estimate("AcmeBygg", FICTIONAL, "Hej")


# ---------------------------------------------------------------- API:t


class ApiSendTests(SmsTestCase):
    def test_send_reserves_sends_and_reconciles(self):
        fake = self.fake()
        response = self.api_post({"to": "070-174 06 05", "message": "Hej! Välkommen."})
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        msg = SmsMessage.objects.get(pk=data["id"])
        self.assertEqual(msg.status, SmsMessage.Status.SENT)
        self.assertEqual((msg.to, msg.country, msg.sender), (FICTIONAL, "SE", "AcmeBygg"))
        self.assertEqual((msg.provider_cost, msg.markup, msg.customer_price), (5200, 500, 5700))
        self.assertEqual(msg.estimated_cost, 5200)
        self.assertEqual(data["price_sek"], "0.5700")
        self.assertFalse(data["price_is_estimate"])
        # Utan historik: provkörning först, sedan sändningen med leveransadress.
        self.assertEqual(len(fake.dryruns), 1)
        self.assertEqual(len(fake.sends), 1)
        dlr = fake.sends[0]["whendelivered"]
        self.assertEqual(
            dlr, f"https://adx.example/api/sms/46elks/dlr/{msg.pk}/{service.dlr_signature(msg.pk)}/"
        )
        # Med ett riktigt pris i historiken behövs ingen provkörning.
        self.api_post({"to": FICTIONAL_2, "message": "Hej igen"})
        self.assertEqual(len(fake.dryruns), 1)
        self.assertEqual(len(fake.sends), 2)

    def test_markup_follows_46elks_parts(self):
        fake = self.fake()
        self.account.markup_ore_per_part = 7
        self.account.save()
        response = self.api_post({"to": FICTIONAL, "message": "a" * 200})
        msg = SmsMessage.objects.get(pk=response.json()["id"])
        self.assertEqual(msg.parts, 2)
        self.assertEqual(msg.markup, 1400)
        self.assertEqual(msg.customer_price, 2 * 5200 + 1400)
        self.assertEqual(len(fake.sends), 1)

    def test_not_enabled_is_refused_before_anything(self):
        fake = self.fake()
        self.account.is_enabled = False
        self.account.save()
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "sms_not_enabled")
        self.assertFalse(SmsMessage.objects.exists())
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.api_get("/api/sms/v1/usage/").status_code, 403)

    def test_inactive_customer_is_refused(self):
        self.fake()
        Customer.objects.filter(pk=self.acme.pk).update(is_active=False)
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.json()["error"]["code"], "sms_not_enabled")

    def test_invalid_number_is_rejected_before_46elks(self):
        fake = self.fake()
        for to in ("+4612", "08-465 004 00", "inte ett nummer"):
            response = self.api_post({"to": to, "message": "Hej"})
            self.assertEqual(response.status_code, 400, to)
            self.assertEqual(response.json()["error"]["code"], "invalid_number")
        self.assertEqual(fake.calls, [])
        rows = SmsMessage.objects.filter(status=SmsMessage.Status.REJECTED)
        self.assertEqual(rows.count(), 3)
        self.assertTrue(all(r.customer_price == 0 for r in rows))

    def test_country_allow_list(self):
        fake = self.fake(per_part=7000)
        response = self.api_post({"to": NORWAY, "message": "Hei"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "country_not_allowed")
        rejected = SmsMessage.objects.get(pk=response.json()["error"]["id"])
        self.assertEqual(rejected.country, "NO")
        self.assertEqual(fake.calls, [])
        self.account.allowed_countries = ["SE", "NO"]
        self.account.save()
        response = self.api_post({"to": NORWAY, "message": "Hei"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["country"], "NO")
        self.assertEqual(response.json()["price_sek"], "0.7500")

    def test_sender_rule(self):
        fake = self.fake()
        response = self.api_post({"to": FICTIONAL, "message": "Hej", "from": "Annan"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "sender_not_allowed")
        self.assertEqual(fake.calls, [])
        ok = self.api_post({"to": FICTIONAL, "message": "Hej", "from": "AcmeBygg"})
        self.assertEqual(ok.status_code, 201)
        ok = self.api_post({"to": FICTIONAL_2, "message": "Hej"})
        self.assertEqual(ok.status_code, 201)
        self.assertTrue(all(call["from"] == "AcmeBygg" for call in fake.calls))

    def test_message_too_long(self):
        fake = self.fake()
        response = self.api_post({"to": FICTIONAL, "message": "a" * (153 * 6 + 1)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "message_too_long")
        self.assertEqual(fake.calls, [])
        ok = self.api_post({"to": FICTIONAL, "message": "a" * (153 * 6)})
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.json()["parts"], 6)

    def test_bad_requests(self):
        self.fake()
        for payload in ("inte json", "[1, 2]", {"to": FICTIONAL}, {"message": "Hej"}):
            response = self.api_post(payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.json()["error"]["code"], "invalid_request")
        response = self.api_post({"to": FICTIONAL, "message": "Hej", "dryrun": "ja"})
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        response = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "har mellanslag"})
        self.assertEqual(response.json()["error"]["code"], "invalid_request")
        response = self.api_get("/api/sms/v1/messages/")
        self.assertEqual(response.status_code, 405)

    def test_dryrun_estimates_without_sending_or_saving(self):
        fake = self.fake()
        response = self.api_post({"to": FICTIONAL, "message": "Hej \U0001f600", "dryrun": True})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["dryrun"])
        self.assertEqual((data["encoding"], data["parts"], data["country"]), ("ucs2", 1, "SE"))
        self.assertEqual(data["price_sek"], "0.5700")
        self.assertEqual(fake.sends, [])
        self.assertFalse(SmsMessage.objects.exists())

    @override_settings(SMS_SEND_LIVE=False)
    def test_not_live_records_a_test_message(self):
        fake = self.fake()
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["test_mode"])
        self.assertEqual(fake.sends, [])
        self.assertEqual(len(fake.dryruns), 2)  # uppskattningen och "sändningen"

    def test_provider_failure_releases_the_reservation_and_alerts_agency(self):
        self.message(account=self.other_account)  # pris i historiken
        self.fake(fail_send="46elks svarade 403: The destination is disallowed by default.")
        response = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "order-1"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "provider_error")
        self.assertNotIn("46elks", response.json()["error"]["message"])
        msg = SmsMessage.objects.get(pk=response.json()["error"]["id"])
        self.assertEqual(msg.status, SmsMessage.Status.FAILED)
        self.assertEqual((msg.customer_price, msg.markup), (0, 0))
        self.assertEqual(pricing.month_to_date_units(self.account), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [AGENCY])
        self.assertIn("Acme Bygg AB", mail.outbox[0].subject)
        # Referensen släpptes: ett nytt försök med samma går igenom.
        self.fake()
        retry = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "order-1"})
        self.assertEqual(retry.status_code, 201)

    def test_unreachable_estimate_is_a_provider_error(self):
        self.fake(fail_dryrun="46elks gick inte att nå (timeout).")
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 502)
        msg = SmsMessage.objects.get()
        self.assertEqual((msg.status, msg.error_code), ("failed", "provider_error"))

    def test_reference_makes_the_call_idempotent(self):
        fake = self.fake()
        payload = {"to": FICTIONAL, "message": "Din kod är 1234", "reference": "kod-77"}
        first = self.api_post(payload)
        self.assertEqual(first.status_code, 201)
        second = self.api_post(payload)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(len(fake.sends), 1)
        conflict = self.api_post({**payload, "message": "Annan text"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "reference_conflict")
        # Samma reference hos en annan kund är en annan sak.
        other = self.api_post(payload, raw=self.other_raw)
        self.assertEqual(other.status_code, 201)

    def test_rejected_reference_can_be_reused(self):
        self.fake()
        bad = self.api_post({"to": "+4612", "message": "Hej", "reference": "x-1"})
        self.assertEqual(bad.status_code, 400)
        good = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "x-1"})
        self.assertEqual(good.status_code, 201)

    def test_get_message_is_scoped_to_the_account(self):
        self.fake()
        own = self.api_post({"to": FICTIONAL, "message": "Hej"}).json()["id"]
        foreign = self.message(account=self.other_account, body="Hemligt").pk
        response = self.api_get(f"/api/sms/v1/messages/{own}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Hej")
        response = self.api_get(f"/api/sms/v1/messages/{foreign}/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertNotIn("Hemligt", response.content.decode())
        # Utan snedstreck sist fungerar det också.
        self.assertEqual(self.api_get(f"/api/sms/v1/messages/{own}").status_code, 200)

    def test_usage(self):
        self.message()
        self.message(status=SmsMessage.Status.REJECTED, customer_price=0)
        data = self.api_get("/api/sms/v1/usage").json()
        self.assertEqual(data["messages"], 1)
        self.assertEqual(data["stopped"], 1)
        self.assertEqual(data["cost_sek"], "0.5700")
        self.assertEqual(data["cap_sek"], "500.0000")
        self.assertEqual(data["from"], "AcmeBygg")
        self.assertEqual(data["allowed_countries"], ["SE"])
        self.assertEqual(data["limits"]["max_parts"], 6)

    @override_settings(SMS_RATE_PER_SECOND=2)
    def test_burst_rate_limit(self):
        self.fake()
        codes = [self.api_get("/api/sms/v1/usage/").status_code for _ in range(3)]
        if codes[-1] != 429:  # sekunden kan ha slagit om mitt i
            codes = [self.api_get("/api/sms/v1/usage/").status_code for _ in range(3)]
        self.assertEqual(codes[-1], 429)
        response = self.api_get("/api/sms/v1/usage/")
        self.assertEqual(response.json()["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", response)

    @override_settings(SMS_DAILY_MAX_PER_KEY=1)
    def test_daily_limit_per_key(self):
        self.fake()
        self.assertEqual(self.api_post({"to": FICTIONAL, "message": "Hej"}).status_code, 201)
        response = self.api_post({"to": FICTIONAL_2, "message": "Hej"})
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response["Retry-After"]), 0)
        # En annan nyckel har sin egen gräns.
        self.assertEqual(
            self.api_post({"to": FICTIONAL, "message": "Hej"}, raw=self.other_raw).status_code,
            201,
        )

    def test_unexpected_error_is_json(self):
        self.fake()
        with mock.patch("apps.sms.service.send", side_effect=RuntimeError("bugg")):
            with self.assertLogs("apps.sms.api", level="ERROR"):
                response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")


# ---------------------------------------------------------------- taket


class CapTests(SmsTestCase):
    def setUp(self):
        super().setUp()
        self.account.monthly_cap_kr = 1
        self.account.save()

    def test_cap_blocks_with_a_clear_error_and_alerts_agency_once(self):
        fake = self.fake()
        self.assertEqual(self.api_post({"to": FICTIONAL, "message": "Hej"}).status_code, 201)
        response = self.api_post({"to": FICTIONAL_2, "message": "Hej"})
        self.assertEqual(response.status_code, 402)
        error = response.json()["error"]
        self.assertEqual(error["code"], "monthly_cap_reached")
        self.assertIn("1,00 kr", error["message"])
        blocked = SmsMessage.objects.get(pk=error["id"])
        self.assertEqual((blocked.status, blocked.customer_price), ("blocked_cap", 0))
        self.assertEqual(len(fake.sends), 1)
        self.api_post({"to": FICTIONAL_2, "message": "Hej igen"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [AGENCY])
        self.assertIn("kostnadstaket", mail.outbox[0].subject)
        # Kunden höjer taket: då går det igen.
        self.account.monthly_cap_kr = 100
        self.account.save()
        self.assertEqual(self.api_post({"to": FICTIONAL_2, "message": "Hej"}).status_code, 201)

    def test_reserved_messages_count_against_the_cap(self):
        self.fake()
        self.message(status=SmsMessage.Status.RESERVED, provider_id="", customer_price=6000)
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.json()["error"]["code"], "monthly_cap_reached")

    def test_only_this_month_counts(self):
        self.fake()
        last_month = pricing.month_bounds(pricing.current_period())[0] - timedelta(hours=1)
        self.message(created_at=last_month, customer_price=9000)
        self.assertEqual(self.api_post({"to": FICTIONAL, "message": "Hej"}).status_code, 201)

    def test_zero_cap_stops_everything(self):
        fake = self.fake()
        self.account.monthly_cap_kr = 0
        self.account.save()
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(fake.sends, [])

    def test_reservation_takes_a_row_lock(self):
        self.fake()
        with CaptureQueriesContext(connection) as queries:
            self.api_post({"to": FICTIONAL, "message": "Hej"})
        locks = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
        self.assertTrue(any('"sms_smsaccount"' in sql for sql in locks), locks)


@override_settings(**TEST_SETTINGS)
class CapConcurrencyTests(TransactionTestCase):
    """Två anrop samtidigt mot ett tak som bara rymmer ett sms: exakt ett
    går igenom. Fönstret mellan att summan läses och raden sparas görs
    medvetet långt; utan radlåset passerar båda."""

    def test_two_parallel_sends_cannot_both_pass_the_cap(self):
        customer = Customer.objects.create(name="Parallell AB")
        account = SmsAccount.objects.create(
            customer=customer, is_enabled=True, sender_name="Parallell", monthly_cap_kr=1
        )
        key, _raw = SmsApiKey.issue(account, "Nyckel")
        other = SmsAccount.objects.create(
            customer=Customer.objects.create(name="Pris AB"), is_enabled=True, sender_name="Pris"
        )
        # Ett pris i historiken, så att ingen provkörning behövs.
        SmsMessage.objects.create(
            account=other,
            to=FICTIONAL,
            country="SE",
            body="x",
            parts=1,
            status="delivered",
            provider_id="s1",
            provider_cost=5200,
            customer_price=5700,
            sent_at=timezone.now(),
        )
        real_mtd = pricing.month_to_date_units

        def slow_mtd(*args, **kwargs):
            value = real_mtd(*args, **kwargs)
            time_module.sleep(0.3)
            return value

        fake = FakeElks()
        results = []

        def worker(to):
            try:
                results.append(service.send(key, {"to": to, "message": "Hej"}))
            finally:
                connection.close()

        with (
            mock.patch("apps.sms.elks.urlopen", side_effect=no_network),
            mock.patch("apps.sms.elks._post", side_effect=fake),
            mock.patch("apps.sms.pricing.month_to_date_units", side_effect=slow_mtd),
        ):
            threads = [threading.Thread(target=worker, args=(n,)) for n in (FICTIONAL, FICTIONAL_2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        rows = SmsMessage.objects.filter(account=account)
        statuses = sorted(rows.values_list("status", flat=True))
        self.assertEqual(statuses, ["blocked_cap", "sent"])
        self.assertEqual(len(fake.sends), 1)
        self.assertEqual(sorted(bool(r.error) for r in results), [False, True])


# ---------------------------------------------------------------- leveransrapporter


class DeliveryReportTests(SmsTestCase):
    def setUp(self):
        super().setUp()
        self.fake()
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.msg = SmsMessage.objects.get(pk=response.json()["id"])
        self.url = reverse("sms_api:dlr", args=[self.msg.pk, service.dlr_signature(self.msg.pk)])
        self.csrf_client = self.client_class(enforce_csrf_checks=True)

    def post(self, data, url=None, **extra):
        return self.csrf_client.post(url or self.url, data, **extra)

    def test_delivered_then_replay_is_harmless(self):
        response = self.post(
            {
                "id": self.msg.provider_id,
                "status": "delivered",
                "delivered": "2026-10-03T12:00:00.5",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.status, "delivered")
        self.assertEqual(self.msg.delivered_at.year, 2026)
        first = self.msg.delivered_at
        again = self.post({"id": self.msg.provider_id, "status": "delivered"})
        self.assertEqual(again.status_code, 200)
        older = self.post({"id": self.msg.provider_id, "status": "sent"})
        self.assertEqual(older.status_code, 200)
        failed = self.post({"id": self.msg.provider_id, "status": "failed"})
        self.assertEqual(failed.status_code, 200)
        self.msg.refresh_from_db()
        self.assertEqual((self.msg.status, self.msg.delivered_at), ("delivered", first))

    def test_failed_keeps_the_price(self):
        self.post({"id": self.msg.provider_id, "status": "failed"})
        self.msg.refresh_from_db()
        self.assertEqual((self.msg.status, self.msg.error_code), ("failed", "delivery_failed"))
        self.assertEqual(self.msg.customer_price, 5700)

    def test_forgery_is_refused(self):
        bad_sig = reverse("sms_api:dlr", args=[self.msg.pk, "0" * 32])
        other = self.message(account=self.other_account)
        borrowed = reverse("sms_api:dlr", args=[other.pk, service.dlr_signature(self.msg.pk)])
        for url, data in (
            (bad_sig, {"id": self.msg.provider_id, "status": "delivered"}),
            (borrowed, {"id": other.provider_id, "status": "failed"}),
            (self.url, {"id": "sfel", "status": "delivered"}),
            (self.url, {"status": "delivered"}),
        ):
            response = self.post(data, url=url)
            self.assertEqual(response.status_code, 404, url)
        self.msg.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((self.msg.status, other.status), ("sent", "delivered"))
        self.assertEqual(self.post({"id": self.msg.provider_id, "status": "bra"}).status_code, 400)
        self.assertEqual(self.csrf_client.get(self.url).status_code, 405)
        self.assertEqual(self.csrf_client.post(f"{self.url}x", {}).status_code, 404)

    def test_report_before_the_id_is_saved_asks_for_a_retry(self):
        reserved = self.message(status=SmsMessage.Status.RESERVED, provider_id="")
        url = reverse("sms_api:dlr", args=[reserved.pk, service.dlr_signature(reserved.pk)])
        self.assertEqual(self.post({"id": "snytt", "status": "sent"}, url=url).status_code, 409)

    @override_settings(SMS_DLR_ALLOWED_IPS=["176.10.154.199"])
    def test_optional_ip_allow_list(self):
        data = {"id": self.msg.provider_id, "status": "delivered"}
        self.assertEqual(self.post(data).status_code, 404)
        self.assertEqual(self.post(data, HTTP_X_REAL_IP="176.10.154.199").status_code, 200)

    def test_json_body_also_works(self):
        response = self.csrf_client.post(
            self.url,
            json.dumps({"id": self.msg.provider_id, "status": "delivered"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------- portalen


class PortalTests(SmsTestCase):
    def test_contact_sees_own_dashboard_and_texts(self):
        self.message(body="Er tid är bokad")
        self.message(account=self.other_account, body="Annans hemliga text")
        self.client.force_login(self.contact)
        response = self.client.get("/kund/sms/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Er tid är bokad")
        self.assertNotContains(response, "Annans hemliga text")
        self.assertContains(response, 'href="/kund/sms/"')  # menyn
        for path in ("/kund/sms/nycklar/", "/kund/sms/dokumentation/", "/kund/sms/underlag/"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        docs = self.client.get("/kund/sms/dokumentation/")
        self.assertContains(docs, "AcmeBygg")
        self.assertContains(docs, "monthly_cap_reached")

    def test_search_filter_and_pagination(self):
        for i in range(25):
            self.message(body=f"Text nummer {i}")
        self.message(body="Stoppat testsms", status=SmsMessage.Status.REJECTED, customer_price=0)
        self.client.force_login(self.contact)
        page = self.client.get("/kund/sms/", {"q": "nummer 7"})
        self.assertContains(page, "Text nummer 7")
        self.assertNotContains(page, "Text nummer 8")
        stopped = self.client.get("/kund/sms/?status=stopped")
        self.assertContains(stopped, "Stoppat testsms")
        self.assertNotContains(stopped, "Text nummer 3")
        second = self.client.get("/kund/sms/?sida=2")
        self.assertContains(second, "Sida 2 av 2")

    def test_nav_link_sits_outside_the_seven_link_row(self):
        self.client.force_login(self.contact)
        page = self.client.get("/kund/tavla/").content.decode()
        row = page.split('class="m-nav__links"')[1].split("</ul>")[0]
        right = page.split('class="m-nav__right"')[1].split("</div>")[0]
        menu = page.split('id="mob-menu"')[1]
        self.assertNotIn("/kund/sms/", row)
        self.assertIn('href="/kund/sms/"', right)
        self.assertIn('href="/kund/sms/"', menu)
        self.assertIn("pt-nav--sms", page)
        self.assertIn("css/sms-nav.css", page)

    def test_customer_without_sms_gets_404(self):
        SmsAccount.objects.filter(pk=self.other_account.pk).update(is_enabled=False)
        self.client.force_login(self.other_contact)
        self.assertEqual(self.client.get("/kund/sms/").status_code, 404)
        self.assertEqual(self.client.get("/kund/sms/nycklar/").status_code, 404)
        home = self.client.get("/kund/tavla/")
        self.assertNotContains(home, 'href="/kund/sms/"')
        self.assertNotContains(home, "sms-nav.css")

    def test_cross_customer_key_revoke_is_404(self):
        self.client.force_login(self.contact)
        url = reverse("sms:key_revoke", args=[self.other_key.pk])
        self.assertEqual(self.client.post(url).status_code, 404)
        self.other_key.refresh_from_db()
        self.assertIsNone(self.other_key.revoked_at)

    def test_create_key_shows_secret_once_and_revoke(self):
        self.client.force_login(self.contact)
        response = self.client.post(reverse("sms:key_create"), {"name": "Bokning"}, follow=True)
        key = SmsApiKey.objects.get(name="Bokning")
        self.assertEqual(key.created_by, self.contact)
        raw = response.context["new_key"]
        self.assertTrue(raw.startswith("adxsms_"))
        self.assertEqual(SmsApiKey.lookup(raw), key)
        self.assertContains(response, raw)
        self.assertNotContains(self.client.get(reverse("sms:keys")), raw)
        self.client.post(reverse("sms:key_revoke", args=[key.pk]))
        key.refresh_from_db()
        self.assertEqual(key.revoked_by, self.contact)
        self.assertIsNone(SmsApiKey.lookup(raw))

    def test_cap_setting(self):
        self.client.force_login(self.contact)
        self.client.post(reverse("sms:cap_update"), {"monthly_cap_kr": "1 200"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.monthly_cap_kr, 1200)
        self.client.post(reverse("sms:cap_update"), {"monthly_cap_kr": "-5"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.monthly_cap_kr, 1200)

    def test_statements_page_shows_closed_and_current(self):
        MonthlyStatement.objects.create(
            account=self.account,
            period=date(2026, 9, 1),
            sms_count=3,
            parts=3,
            provider_cost=15600,
            markup=1500,
            total=17100,
            lines=[
                {
                    "country": "SE",
                    "sms": 3,
                    "parts": 3,
                    "provider_cost": 15600,
                    "markup": 1500,
                    "total": 17100,
                }
            ],
            closed_at=timezone.now(),
        )
        MonthlyStatement.objects.create(
            account=self.other_account,
            period=date(2026, 9, 1),
            total=1_234_500,
            closed_at=timezone.now(),
        )
        self.client.force_login(self.contact)
        response = self.client.get(reverse("sms:statements"))
        self.assertContains(response, "1,71 kr")
        self.assertNotContains(response, "123,45")

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get("/kund/sms/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/kund/logga-in/", response["Location"])


class ViewAsTests(SmsTestCase):
    def view_as(self, customer):
        self.client.force_login(self.staff)
        session = self.client.session
        session[VIEW_AS_KEY] = customer.pk
        session.save()

    def test_staff_sees_exactly_the_customer_pages_with_forms(self):
        self.message(body="Kundens text")
        self.view_as(self.acme)
        response = self.client.get("/kund/sms/")
        self.assertContains(response, "Kundens text")
        self.assertContains(response, "inte skrivskyddade i kundvyn")
        keys = self.client.get("/kund/sms/nycklar/")
        self.assertContains(keys, "Skapa nyckel")
        self.assertContains(keys, "Spara taket")
        self.assertContains(keys, "Återkalla")

    def test_staff_actions_in_view_as_are_real_and_in_staff_name(self):
        self.view_as(self.acme)
        self.client.post(reverse("sms:key_create"), {"name": "Hjälp"})
        key = SmsApiKey.objects.get(name="Hjälp")
        self.assertEqual((key.account, key.created_by), (self.account, self.staff))
        self.client.post(reverse("sms:cap_update"), {"monthly_cap_kr": "300"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.monthly_cap_kr, 300)

    def test_view_as_customer_without_sms_shows_a_notice(self):
        SmsAccount.objects.filter(pk=self.other_account.pk).update(is_enabled=False)
        self.view_as(self.other)
        response = self.client.get("/kund/sms/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "inte aktiverat")

    def test_staff_outside_view_as_goes_to_the_overview(self):
        self.client.force_login(self.staff)
        response = self.client.get("/kund/sms/")
        self.assertRedirects(response, reverse("manage:sms_overview"))

    def test_view_as_button_opens_sms(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("manage:sms_view_as", args=[self.acme.pk]))
        self.assertRedirects(response, reverse("sms:dashboard"))
        self.assertEqual(self.client.session[VIEW_AS_KEY], self.acme.pk)


# ---------------------------------------------------------------- panelen


class ManageTests(SmsTestCase):
    def test_panel_and_overview_are_staff_only(self):
        for user in (None, self.contact):
            self.client.logout()
            if user:
                self.client.force_login(user)
            for url in (
                reverse("manage:sms_overview"),
                reverse("manage:sms_statements_csv", args=[2026, 9]),
            ):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302, (user, url))
                self.assertTrue(
                    response["Location"].startswith(("/manage/login/", "/kund/")),
                    response["Location"],
                )
            response = self.client.post(
                reverse("manage:sms_customer_update", args=[self.acme.pk]), {"is_enabled": "on"}
            )
            self.assertEqual(response.status_code, 302)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("manage:sms_overview")).status_code, 200)
        card = self.client.get(reverse("manage:customer_detail", args=[self.acme.pk]))
        self.assertContains(card, 'id="sms"')
        self.assertContains(card, "AcmeBygg")

    def test_activation_without_email(self):
        customer = Customer.objects.create(name="Ny Kund Åkeri AB", email="ny@kund.example")
        customer.users.add(User.objects.create_user("kalle", email="kalle@kund.example"))
        self.client.force_login(self.staff)
        card = self.client.get(reverse("manage:customer_detail", args=[customer.pk]))
        self.assertContains(card, 'value="NyKundAkeri"')  # förslaget
        url = reverse("manage:sms_customer_update", args=[customer.pk])
        fields = {
            "sender_name": "NyKund",
            "markup_ore_per_part": "5",
            "yearly_fee_kr": "999",
            "monthly_cap_kr": "500",
            "allowed_countries": "se no",
        }
        self.client.post(url, {**fields, "is_enabled": "on"})
        account = SmsAccount.objects.get(customer=customer)
        self.assertTrue(account.is_enabled)
        self.assertEqual(account.enabled_by, self.staff)
        self.assertEqual(account.countries, ["SE", "NO"])
        self.assertEqual(account.service_year_start, pricing.local_today())
        self.assertEqual(mail.outbox, [])
        self.client.post(url, fields)  # utan rutan: avstängt
        account.refresh_from_db()
        self.assertFalse(account.is_enabled)
        self.assertEqual(mail.outbox, [])

    def test_activation_validates_fields(self):
        self.client.force_login(self.staff)
        customer = Customer.objects.create(name="Fel AB")
        url = reverse("manage:sms_customer_update", args=[customer.pk])
        base = {
            "markup_ore_per_part": "5",
            "yearly_fee_kr": "999",
            "monthly_cap_kr": "500",
            "allowed_countries": "SE",
            "is_enabled": "on",
        }
        for bad in (
            {"sender_name": ""},
            {"sender_name": "1Fel"},
            {"sender_name": "Fe"},
            {"sender_name": "Med mellanslag"},
            {"sender_name": "FelAB", "allowed_countries": "SE XX"},
            {"sender_name": "FelAB", "allowed_countries": ""},
            {"sender_name": "FelAB", "markup_ore_per_part": "-1"},
        ):
            self.client.post(url, {**base, **bad})
            self.assertFalse(
                SmsAccount.objects.filter(customer=customer, is_enabled=True).exists(), bad
            )

    def test_deactivation_stops_the_api(self):
        self.fake()
        self.client.force_login(self.staff)
        self.client.post(
            reverse("manage:sms_customer_update", args=[self.acme.pk]),
            {
                "sender_name": "AcmeBygg",
                "markup_ore_per_part": "5",
                "yearly_fee_kr": "999",
                "monthly_cap_kr": "500",
                "allowed_countries": "SE",
            },
        )
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.json()["error"]["code"], "sms_not_enabled")

    def test_close_month_and_csv(self):
        self.account.service_year_start = None  # ingen årsavgift i testet
        self.account.save()
        previous = pricing.previous_month(pricing.current_period())
        start, _ = pricing.month_bounds(previous)
        self.message(created_at=start + timedelta(days=2))
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("manage:sms_close_month"), {"period": f"{previous:%Y-%m}"}, follow=True
        )
        self.assertContains(response, "underlag stängda")
        statement = MonthlyStatement.objects.get(account=self.account, period=previous)
        self.assertIsNotNone(statement.closed_at)
        self.assertEqual(statement.closed_by, self.staff)
        csv_response = self.client.get(
            reverse("manage:sms_statements_csv", args=[previous.year, previous.month])
        )
        text = csv_response.content.decode("utf-8-sig")
        self.assertIn("Acme Bygg AB", text)
        self.assertIn("0,57", text)
        self.assertIn(";", text)
        self.assertNotIn("Annan Firma", text)  # inga sms, ingen avgift
        # Månaden är stängd: en kund utan sms gör den inte "inte stängd".
        overview = self.client.get(reverse("manage:sms_overview"))
        row = next(p for p in overview.context["periods"] if p["period"] == previous)
        self.assertFalse(row["open"])
        self.assertEqual(row["closed_count"], 1)
        current = pricing.current_period()
        response = self.client.post(
            reverse("manage:sms_close_month"), {"period": f"{current:%Y-%m}"}, follow=True
        )
        self.assertFalse(MonthlyStatement.objects.filter(period=current).exists())
        self.assertEqual(mail.outbox, [])


class CommandTests(SmsTestCase):
    def test_close_month_command(self):
        previous = pricing.previous_month(pricing.current_period())
        start, _ = pricing.month_bounds(previous)
        self.message(created_at=start + timedelta(hours=5))
        out = StringIO()
        call_command("sms_close_month", stdout=out)
        self.assertIn("1 underlag stängda.", out.getvalue())
        out = StringIO()
        call_command("sms_close_month", "--period", f"{previous:%Y-%m}", stdout=out)
        self.assertIn("0 underlag stängda, 1 var redan stängda.", out.getvalue())
        with self.assertRaises(CommandError):
            call_command("sms_close_month", "--period", f"{pricing.current_period():%Y-%m}")
        with self.assertRaises(CommandError):
            call_command("sms_close_month", "--period", "inte-en-månad")

    @override_settings(DEBUG=False)
    def test_demo_seed_refuses_without_debug(self):
        with self.assertRaises(CommandError):
            call_command("sms_seed_demo", "--customer", str(self.acme.pk))


class NoCustomerEmailTests(SmsTestCase):
    """Hela vägen, från aktivering till stängd månad: inget mejl går till
    kunden. Larmen går bara till byrån."""

    def test_no_email_ever_reaches_a_customer(self):
        self.client.force_login(self.staff)
        customer = Customer.objects.create(name="Mejlfri AB", email="kund@mejlfri.example")
        contact = User.objects.create_user("mejlfri", email="kontakt@mejlfri.example")
        customer.users.add(contact)
        self.client.post(
            reverse("manage:sms_customer_update", args=[customer.pk]),
            {
                "is_enabled": "on",
                "sender_name": "Mejlfri",
                "markup_ore_per_part": "5",
                "yearly_fee_kr": "999",
                "monthly_cap_kr": "1",
                "allowed_countries": "SE",
            },
        )
        account = SmsAccount.objects.get(customer=customer)
        _key, raw = SmsApiKey.issue(account, "Test")
        self.fake()
        self.api_post({"to": FICTIONAL, "message": "Hej"}, raw=raw)
        self.api_post({"to": FICTIONAL_2, "message": "Hej"}, raw=raw)  # taket
        self.fake(fail_send="46elks svarade 403: nej")
        account.monthly_cap_kr = 100
        account.save()
        self.api_post({"to": FICTIONAL_2, "message": "Hej"}, raw=raw)  # leverantörsfel
        self.client.post(reverse("manage:sms_view_as", args=[customer.pk]))
        self.client.post(reverse("sms:key_create"), {"name": "Ny"})
        call_command("sms_close_month", stdout=StringIO())
        customer_addresses = {"kund@mejlfri.example", "kontakt@mejlfri.example"}
        self.assertGreaterEqual(len(mail.outbox), 2)  # taket och leverantörsfelet
        for message in mail.outbox:
            recipients = set(message.to) | set(message.cc) | set(message.bcc)
            self.assertEqual(recipients, {AGENCY}, message.subject)
            self.assertFalse(recipients & customer_addresses)


# ---------------------------------------------------------------- granskningen 2026-10-03
#
# En klass per fynd. Varje test föll på koden före rättelsen.

STHLM = pricing.STOCKHOLM
CSS = Path(settings.BASE_DIR) / "static" / "css"
TEMPLATES = Path(settings.BASE_DIR) / "templates"


class FlakyElks(FakeElks):
    """46elks tog emot och skickade sms:et, men svaret kom aldrig fram (eller
    något annat gick fel efter anropet)."""

    def __init__(self, error, failures=1, **kwargs):
        super().__init__(**kwargs)
        self.error = error
        self.failures = failures

    def __call__(self, fields):
        result = super().__call__(fields)
        if fields.get("dryrun") != "yes" and self.failures:
            self.failures -= 1
            raise self.error
        return result


def _timeout():
    return elks.ElksError("46elks svarade inte i tid.", ambiguous=True)


@override_settings(**TEST_SETTINGS)
class ElksAmbiguityTests(TestCase):
    """H1: elks._post skiljer ett säkert fel (inget skickat) från ett oklart."""

    def ambiguous(self, error, live=True):
        with (
            override_settings(SMS_SEND_LIVE=live),
            mock.patch("apps.sms.elks.urlopen", side_effect=error),
            self.assertRaises(elks.ElksError) as ctx,
        ):
            elks.send("AcmeBygg", FICTIONAL, "Hej")
        return ctx.exception.ambiguous

    def test_classification(self):
        def http_error(code):
            return HTTPError(elks.API_URL, code, "x", {}, io.BytesIO(b"fel"))

        # Kan ha skickats:
        self.assertTrue(self.ambiguous(TimeoutError("timed out")))
        self.assertTrue(self.ambiguous(URLError(TimeoutError("timed out"))))
        self.assertTrue(self.ambiguous(ConnectionResetError()))
        self.assertTrue(self.ambiguous(http.client.RemoteDisconnected("borta")))
        self.assertTrue(self.ambiguous(http.client.IncompleteRead(b"")))
        self.assertTrue(self.ambiguous(http_error(502)))
        # Skickades säkert inte:
        self.assertFalse(self.ambiguous(http_error(403)))
        self.assertFalse(self.ambiguous(URLError(socket.gaierror(8, "nodename"))))
        self.assertFalse(self.ambiguous(URLError(ConnectionRefusedError())))
        # Provläge och provkörning skickar aldrig något.
        self.assertFalse(self.ambiguous(TimeoutError(), live=False))
        with (
            mock.patch("apps.sms.elks.urlopen", side_effect=TimeoutError()),
            self.assertRaises(elks.ElksError) as ctx,
        ):
            elks.estimate("AcmeBygg", FICTIONAL, "Hej")
        self.assertFalse(ctx.exception.ambiguous)

    def test_replies(self):
        for body, ambiguous in (
            (b"<html>", True),
            (b"[]", True),
            (b'{"status": "created"}', True),  # svar utan id
            (b'{"status": "failed"}', False),
        ):
            with (
                mock.patch("apps.sms.elks.urlopen", return_value=_Response(body)),
                self.assertRaises(elks.ElksError) as ctx,
            ):
                elks.send("AcmeBygg", FICTIONAL, "Hej")
            self.assertEqual(ctx.exception.ambiguous, ambiguous, body)


class AmbiguousSendTests(SmsTestCase):
    """H1: ett oklart svar från 46elks ger aldrig dubbel sändning, och sms:et
    debiteras när det visar sig ha gått iväg."""

    def flaky(self, error=None, failures=1):
        fake = FlakyElks(error or _timeout(), failures=failures)
        patcher = mock.patch("apps.sms.elks._post", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def dlr(self, message, data):
        url = reverse("sms_api:dlr", args=[message.pk, service.dlr_signature(message.pk)])
        return self.client.post(url, data)

    def test_timeout_holds_the_reservation_and_a_retry_never_sends_again(self):
        fake = self.flaky()
        payload = {"to": FICTIONAL, "message": "Din kod 1234", "reference": "order-9"}
        first = self.api_post(payload)
        self.assertEqual(first.status_code, 202, first.content)
        data = first.json()
        self.assertEqual(data["status"], "unknown")
        self.assertEqual(data["error"]["code"], "provider_unknown")
        self.assertTrue(data["price_is_estimate"])
        self.assertEqual(data["price_sek"], "0.5700")
        msg = SmsMessage.objects.get(pk=data["id"])
        self.assertEqual(
            (msg.status, msg.error_code, msg.needs_check, msg.provider_id),
            ("reserved", "provider_unknown", True, ""),
        )
        self.assertEqual((msg.estimated_cost, msg.customer_price), (5200, 5700))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [AGENCY])
        self.assertIn("kontrollera", mail.outbox[0].subject)
        # Samma reference igen: samma sms tillbaka, ingen ny sändning.
        second = self.api_post(payload)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual((second.json()["id"], second.json()["status"]), (msg.pk, "unknown"))
        self.assertEqual(self.api_post({**payload, "message": "Annan"}).status_code, 409)
        self.assertEqual(len(fake.sends), 1)
        detail = self.api_get(f"/api/sms/v1/messages/{msg.pk}/").json()
        self.assertEqual(detail["status"], "unknown")
        # Reservationen räknas mot taket, och kunden ser läget.
        self.assertEqual(pricing.month_to_date_units(self.account), 5700)
        self.client.force_login(self.contact)
        self.assertContains(self.client.get("/kund/sms/"), "Okänt läge")

    def test_an_unexpected_exception_is_also_held(self):
        self.flaky(RuntimeError("pang"))
        with self.assertLogs("apps.sms.service", level="ERROR"):
            response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(SmsMessage.objects.get().error_code, "provider_unknown")

    def test_a_definite_refusal_still_releases_the_reservation(self):
        self.flaky(elks.ElksError("46elks svarade 403: nej", 403))
        response = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "r-1"})
        self.assertEqual(response.status_code, 502)
        msg = SmsMessage.objects.get()
        self.assertEqual(
            (msg.status, msg.error_code, msg.customer_price, msg.needs_check),
            ("failed", "provider_error", 0, False),
        )

    def test_a_late_delivery_report_adopts_the_id_after_two_minutes(self):
        self.flaky()
        response = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "o-2"})
        msg = SmsMessage.objects.get(pk=response.json()["id"])
        report = {"id": "s" + "0" * 31 + "1", "status": "delivered"}
        # Nyss reserverat: 46elks svar kan vara på väg, så rapporten får vänta.
        self.assertEqual(self.dlr(msg, report).status_code, 409)
        SmsMessage.objects.filter(pk=msg.pk).update(
            created_at=timezone.now() - timedelta(minutes=3)
        )
        self.assertEqual(self.dlr(msg, report).status_code, 200)
        msg.refresh_from_db()
        self.assertEqual(
            (msg.status, msg.provider_id, msg.error_code), ("delivered", report["id"], "")
        )
        self.assertEqual((msg.provider_cost, msg.customer_price), (5200, 5700))
        self.assertTrue(msg.needs_check)
        self.assertIsNotNone(msg.sent_at)
        data = self.api_get(f"/api/sms/v1/messages/{msg.pk}/").json()
        self.assertEqual((data["status"], data["error"]), ("delivered", None))
        self.assertTrue(data["price_is_estimate"])
        # Sms:et debiteras: det står på underlaget.
        statement = pricing.build_statement(self.account, pricing.current_period())
        self.assertEqual(statement.sms_count, 1)
        # Upprepningen ändrar inget; ett annat id hör inte till sms:et.
        self.assertEqual(self.dlr(msg, report).status_code, 200)
        self.assertEqual(self.dlr(msg, {**report, "id": "sannat"}).status_code, 404)

    def test_a_late_failed_report_keeps_the_estimated_price(self):
        stuck = self.message(
            status=SmsMessage.Status.RESERVED,
            provider_id="",
            provider_cost=0,
            estimated_cost=5200,
            sent_at=None,
            created_at=timezone.now() - timedelta(minutes=30),
        )
        self.assertEqual(service.apply_delivery_report(stuck.pk, "sx", "failed")[0], 200)
        stuck.refresh_from_db()
        self.assertEqual(
            (stuck.status, stuck.error_code, stuck.provider_id, stuck.customer_price),
            ("failed", "delivery_failed", "sx", 5700),
        )
        self.assertTrue(stuck.needs_check)
        rejected = self.message(
            status=SmsMessage.Status.REJECTED,
            provider_id="",
            customer_price=0,
            created_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(service.apply_delivery_report(rejected.pk, "sy", "delivered")[0], 404)

    def test_the_agency_settles_it_on_the_overview(self):
        self.flaky(failures=2)
        a = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "a-1"}).json()["id"]
        b = self.api_post({"to": FICTIONAL_2, "message": "Hej", "reference": "b-1"}).json()["id"]
        self.client.force_login(self.staff)
        overview = self.client.get(reverse("manage:sms_overview"))
        self.assertContains(overview, "Kontrollera mot 46elks")
        self.assertEqual({m.pk for m in overview.context["to_check"]}, {a, b})
        card = self.client.get(reverse("manage:customer_detail", args=[self.acme.pk]))
        self.assertContains(card, "Kontrollera mot 46elks")
        self.client.post(reverse("manage:sms_resolve_check", args=[a]), {"sent": "1"})
        self.client.post(reverse("manage:sms_resolve_check", args=[b]), {"sent": "0"})
        sent, unsent = SmsMessage.objects.get(pk=a), SmsMessage.objects.get(pk=b)
        self.assertEqual(
            (sent.status, sent.provider_cost, sent.customer_price, sent.needs_check),
            ("sent", 5200, 5700, False),
        )
        self.assertEqual(
            (unsent.status, unsent.error_code, unsent.customer_price, unsent.needs_check),
            ("failed", "provider_error", 0, False),
        )
        self.assertFalse(pricing.needs_check_messages(self.account).exists())
        # b:s reference är fri igen; a:s hålls fortfarande.
        fake = self.fake()
        retry = self.api_post({"to": FICTIONAL_2, "message": "Hej", "reference": "b-1"})
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(
            self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "a-1"}).status_code,
            200,
        )
        self.assertEqual(len(fake.sends), 1)
        # Bara byrån stämmer av.
        held = self.message(status=SmsMessage.Status.RESERVED, provider_id="", needs_check=True)
        self.client.force_login(self.contact)
        self.client.post(reverse("manage:sms_resolve_check", args=[held.pk]), {"sent": "0"})
        held.refresh_from_db()
        self.assertEqual(held.status, "reserved")
        self.assertEqual(mail.outbox[0].to, [AGENCY])


class _SentryCapture(Transport):
    sent = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type in ("event", "transaction"):
                _SentryCapture.sent.append(item.payload.json)


class SentryTests(SmsTestCase):
    """M1: nycklar, signaturer, nummer och texter följer aldrig med till Sentry."""

    def test_patterns_and_untraced_reports(self):
        sig = service.dlr_signature(12)
        self.assertEqual(sentry.scrub_text(f"nyckeln {self.raw} igen"), "nyckeln [Filtered] igen")
        self.assertEqual(
            sentry.scrub_text(f"https://adx.se/api/sms/46elks/dlr/12/{sig}/"),
            "https://adx.se/api/sms/46elks/dlr/12/[Filtered]/",
        )
        self.assertEqual(sentry.scrub_text("adxsms_kort"), "adxsms_kort")  # inget att maska
        environ = {"wsgi_environ": {"PATH_INFO": f"/api/sms/46elks/dlr/12/{sig}/"}}
        self.assertEqual(sentry.traces_sampler(environ), 0.0)
        environ = {"wsgi_environ": {"PATH_INFO": "/api/sms/v1/messages/"}}
        self.assertGreater(sentry.traces_sampler(environ), 0)

    def test_frame_variables(self):
        options = sentry.options("https://x@example.invalid/1", "test", "/tmp")  # noqa: S108
        sms_vars = {"to_raw": FICTIONAL, "fields": {"message": "Hemlig text"}}
        other_vars = {
            "to": FICTIONAL,
            "body": "Hemlig text",
            "data": {"message": "Hemlig text"},
            "signature": "ab" * 16,
            "count": 3,
        }
        frames = [
            {"module": "apps.sms.elks", "vars": sms_vars},
            {"module": "apps.projects.views", "vars": other_vars},
        ]
        event = {"exception": {"values": [{"stacktrace": {"frames": frames}}]}}
        options["event_scrubber"].scrub_event(event)
        out = sentry.scrub_event(event)
        kept = out["exception"]["values"][0]["stacktrace"]["frames"]
        self.assertEqual(kept[0]["vars"], {})
        self.assertEqual(kept[1]["vars"]["count"], 3)  # resten av rapporten är kvar
        dump = json.dumps(out, default=str)
        self.assertNotIn("46701740605", dump)
        self.assertNotIn("Hemlig text", dump)
        self.assertNotIn("ab" * 16, dump)

    def test_a_real_report_from_a_send_carries_no_number_text_or_key(self):
        self.message(account=self.other_account)  # ett pris, så att ingen provkörning behövs
        _SentryCapture.sent = []
        options = sentry.options("https://x@example.invalid/1", "test", "/tmp")  # noqa: S108
        sentry_sdk.init(transport=_SentryCapture, **options)
        self.addCleanup(sentry_sdk.init)  # utan dsn: avstängd igen
        text = "Hemlig text 9137"
        with (
            mock.patch("apps.sms.elks.urlopen", side_effect=RuntimeError("pang")),
            self.assertLogs("apps.sms.service", level="ERROR"),
        ):
            response = self.api_post({"to": FICTIONAL, "message": text, "reference": "hemlig-1"})
        self.assertEqual(response.status_code, 202)
        sentry_sdk.flush()
        self.assertTrue(_SentryCapture.sent)
        dump = json.dumps(_SentryCapture.sent, ensure_ascii=False)
        msg = SmsMessage.objects.get(pk=response.json()["id"])
        for secret in (text, "46701740605", self.raw, service.dlr_signature(msg.pk)):
            self.assertNotIn(secret, dump)


@override_settings(SMS_RATE_PER_MINUTE=2)
class AccountMinuteTests(SmsTestCase):
    """M2: minutgränsen gäller kunden, inte nyckeln eller arbetaren."""

    def test_more_keys_do_not_give_more_sms(self):
        fake = self.fake()
        keys = [SmsApiKey.issue(self.account, f"k{i}")[1] for i in range(4)]
        codes = [
            self.api_post({"to": FICTIONAL, "message": f"Hej {i}"}, raw=raw).status_code
            for i, raw in enumerate(keys)
        ]
        self.assertEqual(codes, [201, 201, 429, 429])
        self.assertEqual(len(fake.sends), 2)
        response = self.api_post({"to": FICTIONAL, "message": "Hej"}, raw=keys[3])
        self.assertEqual(response.json()["error"]["code"], "rate_limited")
        self.assertTrue(1 <= int(response["Retry-After"]) <= 60)
        self.assertFalse(SmsMessage.objects.filter(account=self.account, body="Hej").exists())
        # En annan kund har sin egen gräns.
        other = self.api_post({"to": FICTIONAL, "message": "Hej"}, raw=self.other_raw)
        self.assertEqual(other.status_code, 201)

    def test_rejected_and_older_rows_do_not_count(self):
        self.fake()
        for _ in range(3):
            self.message(status=SmsMessage.Status.REJECTED, provider_id="", customer_price=0)
        self.message(created_at=timezone.now() - timedelta(seconds=61))
        self.message(created_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(self.api_post({"to": FICTIONAL, "message": "Hej"}).status_code, 201)


@override_settings(SMS_GLOBAL_PER_MINUTE=3)
class AgencyMinuteTests(SmsTestCase):
    """M2: alla kunder tillsammans stannar under 46elks gräns för kontot."""

    def test_all_customers_together(self):
        fake = self.fake()
        for _ in range(3):
            self.message(account=self.other_account)
        # Stoppade sms nådde aldrig 46elks och räknas inte.
        self.message(
            account=self.other_account,
            status=SmsMessage.Status.BLOCKED_CAP,
            provider_id="",
            customer_price=0,
        )
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        self.assertEqual(fake.sends, [])
        SmsMessage.objects.update(created_at=timezone.now() - timedelta(seconds=61))
        self.assertEqual(self.api_post({"to": FICTIONAL, "message": "Hej"}).status_code, 201)


class CloseInFlightTests(SmsTestCase):
    """M3: en månad stängs inte med sms som fortfarande väntar på 46elks."""

    def setUp(self):
        super().setUp()
        self.account.service_year_start = None  # ingen årsavgift i testerna
        self.account.save()

    def test_a_month_with_an_sms_in_flight_waits(self):
        september = date(2026, 9, 1)
        end = datetime(2026, 10, 1, tzinfo=STHLM)
        self.message(created_at=end - timedelta(days=3))
        late = self.message(
            status=SmsMessage.Status.RESERVED,
            provider_id="",
            provider_cost=0,
            estimated_cost=5200,
            sent_at=None,
            created_at=end - timedelta(seconds=2),
        )
        now = end + timedelta(seconds=30)  # cron strax efter midnatt
        with self.assertRaises(pricing.ReservationsPending):
            pricing.close_statement(self.account, september, now=now)
        result = pricing.close_month(september, now=now)
        self.assertEqual([(a.pk, n) for a, n in result.waiting], [(self.account.pk, 1)])
        self.assertIn("1 sms från månaden står fortfarande som reserverade", result.summary())
        self.assertIn("Acme Bygg AB", result.summary())
        self.assertFalse(MonthlyStatement.objects.filter(account=self.account).exists())
        # 46elks svarar: nu stängs månaden, med sms:et.
        late.status = SmsMessage.Status.SENT
        late.provider_id = "slate"
        late.provider_cost = 5200
        late.save()
        statement, made = pricing.close_statement(self.account, september, now=now)
        self.assertTrue(made)
        self.assertEqual(statement.sms_count, 2)

    def test_button_and_command_count_every_reservation(self):
        previous = pricing.previous_month(pricing.current_period())
        _start, end = pricing.month_bounds(previous)
        self.message(created_at=end - timedelta(days=1))
        self.message(status=SmsMessage.Status.RESERVED, provider_id="", created_at=end)
        self.message(
            status=SmsMessage.Status.RESERVED,
            provider_id="",
            created_at=end - timedelta(seconds=2),
        )
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("manage:sms_close_month"), {"period": f"{previous:%Y-%m}"}, follow=True
        )
        self.assertContains(response, "1 sms från månaden står fortfarande som reserverade")
        out = StringIO()
        call_command("sms_close_month", "--period", f"{previous:%Y-%m}", stdout=out)
        self.assertIn("1 sms från månaden står fortfarande som reserverade", out.getvalue())
        self.assertIn("Acme Bygg AB: inte stängt", out.getvalue())
        self.assertFalse(MonthlyStatement.objects.filter(account=self.account).exists())
        out = StringIO()
        call_command("sms_close_month", "--period", f"{previous:%Y-%m}", "--dry-run", stdout=out)
        self.assertIn("väntar på reserverade sms", out.getvalue())

    def test_one_failing_account_does_not_stop_the_others(self):
        previous = pricing.previous_month(pricing.current_period())
        start, _end = pricing.month_bounds(previous)
        self.message(created_at=start + timedelta(days=1))
        self.message(account=self.other_account, created_at=start + timedelta(days=1))
        real = pricing.close_statement

        def flaky(account, *args, **kwargs):
            if account.pk == self.account.pk:
                raise RuntimeError("pang")
            return real(account, *args, **kwargs)

        with (
            mock.patch("apps.sms.pricing.close_statement", side_effect=flaky),
            self.assertLogs("apps.sms.pricing", level="ERROR"),
        ):
            result = pricing.close_month(previous)
        self.assertEqual([a.pk for a in result.failed], [self.account.pk])
        self.assertEqual([s.account_id for s in result.created], [self.other_account.pk])
        MonthlyStatement.objects.all().delete()
        with (
            mock.patch("apps.sms.pricing.close_statement", side_effect=flaky),
            self.assertLogs("apps.sms.pricing", level="ERROR"),
            self.assertRaises(CommandError),
        ):
            call_command("sms_close_month", stdout=StringIO(), stderr=StringIO())
        self.assertTrue(MonthlyStatement.objects.filter(account=self.other_account).exists())


@override_settings(**TEST_SETTINGS)
class CloseConcurrencyTests(TransactionTestCase):
    """M4: två stängningar samtidigt (cron och knappen) ger ett underlag och
    inget IntegrityError."""

    def test_two_closes_at_once(self):
        account = SmsAccount.objects.create(
            customer=Customer.objects.create(name="Samtidigt AB"),
            is_enabled=True,
            enabled_at=datetime(2025, 9, 1, tzinfo=STHLM),
            sender_name="Samtidigt",
            service_year_start=date(2025, 9, 1),
        )
        real = pricing.build_statement

        def slow(*args, **kwargs):
            value = real(*args, **kwargs)
            time_module.sleep(0.3)
            return value

        results, errors = [], []

        def worker():
            try:
                results.append(pricing.close_statement(account, date(2026, 9, 1)))
            except Exception as exc:  # noqa: BLE001 - felet ÄR testfallet
                errors.append(exc)
            finally:
                connection.close()

        with mock.patch("apps.sms.pricing.build_statement", side_effect=slow):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(made for _statement, made in results), [False, True])
        self.assertEqual(MonthlyStatement.objects.filter(account=account).count(), 1)


class YearlyFeeTests(SmsTestCase):
    """M5: årsavgiften beror på om SMS var aktiverat under månaden, inte på
    läget när månaden stängs."""

    def test_disabling_before_the_close_keeps_the_fee(self):
        self.account.service_year_start = date(2025, 9, 15)
        self.account.enabled_at = datetime(2025, 9, 15, 10, tzinfo=STHLM)
        self.account.save()
        september = date(2026, 9, 1)
        self.assertEqual(pricing.build_statement(self.account, september).fee, 9_990_000)
        # Kunden säger upp i oktober; byrån stänger september efter det.
        self.client.force_login(self.staff)
        self.client.post(
            reverse("manage:sms_customer_update", args=[self.acme.pk]),
            {
                "sender_name": "AcmeBygg",
                "markup_ore_per_part": "5",
                "yearly_fee_kr": "999",
                "monthly_cap_kr": "500",
                "allowed_countries": "SE",
            },
        )
        self.account.refresh_from_db()
        self.assertFalse(self.account.is_enabled)
        self.assertIsNotNone(self.account.disabled_at)
        statement, made = pricing.close_statement(
            self.account, september, now=datetime(2026, 10, 6, 9, tzinfo=STHLM)
        )
        self.assertTrue(made)
        self.assertEqual(statement.fee, 9_990_000)

    def test_rules(self):
        a = self.account
        a.service_year_start = date(2025, 9, 15)
        september = date(2026, 9, 1)
        a.enabled_at = datetime(2026, 10, 2, tzinfo=STHLM)  # aktiverat först efter månaden
        self.assertFalse(pricing.fee_due(a, september))
        a.enabled_at = datetime(2025, 9, 15, tzinfo=STHLM)
        a.is_enabled = False
        a.disabled_at = datetime(2026, 8, 31, 23, 0, tzinfo=STHLM)  # avstängt före månaden
        self.assertFalse(pricing.fee_due(a, september))
        a.disabled_at = datetime(2026, 9, 1, 0, 30, tzinfo=STHLM)  # avstängt under månaden
        self.assertTrue(pricing.fee_due(a, september))
        a.disabled_at = None
        self.assertFalse(pricing.fee_due(a, september))
        self.assertTrue(pricing.fee_due(a, september, sms_count=1))
        a.is_enabled = True
        self.assertTrue(pricing.fee_due(a, september))
        a.enabled_at = None
        self.assertFalse(pricing.fee_due(a, september))


class CapChangeTests(SmsTestCase):
    """M6: en ändring av taket sparar när och av vem, och det syns."""

    def test_customer_and_agency_changes_are_recorded(self):
        self.contact.first_name, self.contact.last_name = "Nina", "Kund"
        self.contact.save()
        self.client.force_login(self.contact)
        self.client.post(reverse("sms:cap_update"), {"monthly_cap_kr": "800"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.monthly_cap_changed_by, self.contact)
        self.assertIsNotNone(self.account.monthly_cap_changed_at)
        self.assertContains(self.client.get(reverse("sms:keys")), "av Nina Kund.")
        # Byrån i kundvyn.
        self.staff.first_name, self.staff.last_name = "Gio", "Byrå"
        self.staff.save()
        self.client.force_login(self.staff)
        session = self.client.session
        session[VIEW_AS_KEY] = self.acme.pk
        session.save()
        self.client.post(reverse("sms:cap_update"), {"monthly_cap_kr": "900"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.monthly_cap_changed_by, self.staff)
        card = self.client.get(reverse("manage:customer_detail", args=[self.acme.pk]))
        self.assertContains(card, "av Gio Byrå.")

    def test_the_card_records_only_a_real_change(self):
        self.client.force_login(self.staff)
        url = reverse("manage:sms_customer_update", args=[self.acme.pk])
        fields = {
            "is_enabled": "on",
            "sender_name": "AcmeBygg",
            "markup_ore_per_part": "5",
            "yearly_fee_kr": "999",
            "monthly_cap_kr": "500",
            "allowed_countries": "SE",
        }
        self.client.post(url, fields)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.monthly_cap_changed_at)
        self.client.post(url, {**fields, "monthly_cap_kr": "700"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.monthly_cap_kr, 700)
        self.assertEqual(self.account.monthly_cap_changed_by, self.staff)


class LayoutGuardTests(SimpleTestCase):
    """M7, M8, LOW 6 och LOW 7: det som rättades i webbläsaren ska inte glida
    tillbaka. Ersätter inte en titt i 375, 768 och 1400 px."""

    def css(self, name):
        return (CSS / name).read_text(encoding="utf-8")

    def test_portal_menu_has_no_gap_above_the_hamburger(self):
        nav = self.css("sms-nav.css").replace(" ", "")
        self.assertIn("@media(min-width:761px)and(max-width:1100px)", nav)
        self.assertNotIn("min-width:781px", nav)
        self.assertIn(
            "@media(min-width:761px)and(max-width:840px){.pt-nav--sms.pt-flamingo{padding:7px9px;margin-right:4px}",
            nav.replace("\n", ""),
        )

    def test_low_value_columns_hide_at_tablet_width(self):
        css = self.css("sms.css")
        tablet = "@media (min-width:761px) and (max-width:1000px){\n"
        self.assertIn(tablet + "  .sms-table .sms-col-low{display:none}", css)
        self.assertIn(".sms-table .sms-mono{overflow-wrap:normal}", css)
        # Åtta kolumner i månadsunderlaget: bredare än portalens läsbredd.
        self.assertIn(".sms-statements.pt-narrow{max-width:1000px}", css)
        marked = {
            "manage/sms/overview.html": ("Senaste sms", "Varav årsavgifter"),
            "sms/portal/statements.html": ("Delar",),
            "sms/portal/keys.html": ("Senast använd",),
            "sms/portal/dashboard.html": ("Delar",),
        }
        for name, headings in marked.items():
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            for heading in headings:
                self.assertRegex(html, rf'<th class="[^"]*sms-col-low[^"]*">{heading}</th>', name)

    def test_chart_tip_hangs_inside_the_chart(self):
        tip = next(line for line in self.css("sms.css").splitlines() if "sms-bar__tip{" in line)
        self.assertIn("bottom:auto;top:6px", tip)

    def test_running_text_is_at_least_16px(self):
        css = self.css("sms.css")
        for selector in (".sms-live-note{", ".sms-body{", ".sms-card__usage{"):
            line = next(row for row in css.splitlines() if row.startswith(selector))
            self.assertIn("font-size:16px", line, selector)
        self.assertIn(".sms-dash .tv-prop__note", css)
        self.assertIn(".sms-card .tv-line{font-size:16px", css)


class ViewAsBannerTests(SmsTestCase):
    """M9: kundvyns banderoll säger inte "Skrivskyddat" på SMS-sidorna, där
    byrån kan göra allt kunden kan."""

    def test_banner(self):
        self.client.force_login(self.staff)
        session = self.client.session
        session[VIEW_AS_KEY] = self.acme.pk
        session.save()
        for path in ("/kund/sms/", "/kund/sms/nycklar/"):
            page = self.client.get(path)
            self.assertContains(page, "exakt det kunden ser")
            self.assertNotContains(page, "Skrivskyddat")
        self.assertContains(self.client.get("/kund/tavla/"), "Skrivskyddat")


class LowFindingTests(SmsTestCase):
    def test_csv_cells_cannot_become_formulas(self):
        """LOW 1."""
        self.acme.name = '=HYPERLINK("http://evil.example/","Acme")'
        self.acme.org_number = "+SUM(1+1)"
        self.acme.save()
        self.account.service_year_start = None
        self.account.save()
        previous = pricing.previous_month(pricing.current_period())
        start, _ = pricing.month_bounds(previous)
        self.message(created_at=start + timedelta(days=2))
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("manage:sms_statements_csv", args=[previous.year, previous.month])
        )
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        row = next(r for r in rows if "HYPERLINK" in r[0])
        self.assertTrue(row[0].startswith("'="))
        self.assertEqual(row[1], "'+SUM(1+1)")
        self.assertEqual(row[6], "0,52")  # beloppen orörda
        for value in ("=1", "+1", "-1", "@x", "\tx", "\rx"):
            self.assertEqual(_cell(value), "'" + value)
        self.assertEqual((_cell("Acme"), _cell(5)), ("Acme", 5))

    def test_trailing_newline_is_not_accepted(self):
        """LOW 2: "$" i match släppte igenom en radbrytning sist."""
        fake = self.fake()
        ok = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": "ord-1"})
        self.assertEqual(ok.status_code, 201)
        for reference in ("ord-1\n", "x" * 64 + "\n"):
            response = self.api_post({"to": FICTIONAL, "message": "Hej", "reference": reference})
            self.assertEqual(response.status_code, 400, reference)  # 64 + \n gav 500
            self.assertEqual(response.json()["error"]["code"], "invalid_request")
        self.assertEqual(len(fake.sends), 1)
        validate_sender("AcmeBygg")
        with self.assertRaises(ValidationError):
            validate_sender("AcmeBygg\n")

    def test_zero_cap_stops_even_a_free_sms(self):
        """LOW 3."""
        self.account.monthly_cap_kr = 0
        self.account.markup_ore_per_part = 0
        self.account.save()
        fake = self.fake(per_part=0)  # 46elks provkörning utan estimated_cost
        response = self.api_post({"to": FICTIONAL, "message": "Hej"})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(fake.sends, [])
        cost, source = service.estimate_cost("SE", 2, "AcmeBygg", FICTIONAL, "Hej")
        self.assertEqual((cost, source), (pricing.FALLBACK_PART_COST * 2, "fallback"))

    def test_delivery_report_survives_secret_key_rotation(self):
        """LOW 4."""
        self.fake()
        msg = SmsMessage.objects.get(
            pk=self.api_post({"to": FICTIONAL, "message": "Hej"}).json()["id"]
        )
        url = reverse("sms_api:dlr", args=[msg.pk, service.dlr_signature(msg.pk)])
        report = {"id": msg.provider_id, "status": "delivered"}
        new_key = "ny-nyckel-efter-bytet-" + "x" * 40
        with override_settings(SECRET_KEY=new_key, SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]):
            self.assertEqual(self.client.post(url, report).status_code, 200)
        with override_settings(SECRET_KEY=new_key, SECRET_KEY_FALLBACKS=[]):
            self.assertEqual(self.client.post(url, report).status_code, 404)

    def test_new_key_is_never_stored_in_the_session(self):
        """LOW 5."""
        self.client.force_login(self.contact)
        response = self.client.post(reverse("sms:key_create"), {"name": "Kassa"})
        self.assertEqual(response.status_code, 200)
        raw = response.context["new_key"]
        self.assertTrue(raw.startswith("adxsms_"))
        self.assertContains(response, raw)
        self.assertIn("no-store", response["Cache-Control"])
        for session in Session.objects.all():
            self.assertNotIn("adxsms_", json.dumps(session.get_decoded(), default=str))
        self.assertNotContains(self.client.get(reverse("sms:keys")), raw)

    def test_open_older_month_can_be_closed_from_its_row(self):
        """LOW 8."""
        self.account.service_year_start = None
        self.account.save()
        older = pricing.previous_month(pricing.previous_month(pricing.current_period()))
        start, _ = pricing.month_bounds(older)
        self.message(created_at=start + timedelta(days=1))
        self.client.force_login(self.staff)
        page = self.client.get(reverse("manage:sms_overview"))
        row = next(p for p in page.context["periods"] if p["period"] == older)
        self.assertTrue(row["open"])
        self.assertContains(page, f'name="period" value="{older:%Y-%m}"')
        self.client.post(reverse("manage:sms_close_month"), {"period": f"{older:%Y-%m}"})
        self.assertTrue(
            MonthlyStatement.objects.filter(
                account=self.account, period=older, closed_at__isnull=False
            ).exists()
        )

    def test_test_rows_are_explained_and_spare_the_cap_when_live(self):
        """LOW 9."""
        self.message(test_mode=True, provider_id="", status=SmsMessage.Status.SENT)
        self.message()
        usage = pricing.usage(self.account)  # SMS_SEND_LIVE är på i testerna
        self.assertEqual(
            (usage["cost"], usage["test_cost"], usage["cap_used"]), (11400, 5700, 5700)
        )
        self.assertEqual(pricing.month_to_date_units(self.account), 5700)
        with override_settings(SMS_SEND_LIVE=False):
            self.assertEqual(pricing.month_to_date_units(self.account), 11400)
        self.client.force_login(self.contact)
        self.assertContains(self.client.get("/kund/sms/"), "testsändningar som inte faktureras")
        # Taket fyllt av provkörningar stoppar inte en riktig sändning.
        self.account.monthly_cap_kr = 1
        self.account.save()
        self.message(
            test_mode=True, provider_id="", status=SmsMessage.Status.SENT, customer_price=9000
        )
        SmsMessage.objects.filter(test_mode=False).delete()
        self.fake()
        self.assertEqual(self.api_post({"to": FICTIONAL, "message": "Hej"}).status_code, 201)


class SelfServiceTests(SmsTestCase):
    """Giovanni 2026-10-04: nycklar, tak och dokumentation syns bara för en
    kund som sköter dem själv. Annars sköter ADX dem från kundkortet, och
    påslaget redovisas aldrig för sig på kundens underlag."""

    def setUp(self):
        SmsAccount.objects.filter(pk=self.account.pk).update(customer_manages_api=False)
        self.account.refresh_from_db()

    def test_tabs_and_pages_are_gone_for_the_customer(self):
        self.client.force_login(self.contact)
        dashboard = self.client.get("/kund/sms/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotContains(dashboard, 'href="/kund/sms/nycklar/"')
        self.assertNotContains(dashboard, 'href="/kund/sms/dokumentation/"')
        self.assertContains(dashboard, 'href="/kund/sms/underlag/"')
        for path in ("/kund/sms/nycklar/", "/kund/sms/dokumentation/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.post("/kund/sms/nycklar/ny/", {"name": "X"}).status_code, 404)
        self.assertEqual(
            self.client.post("/kund/sms/tak/", {"monthly_cap_kr": "9"}).status_code, 404
        )
        self.assertFalse(self.account.api_keys.exists())

    def test_staff_in_view_as_sees_the_same(self):
        client = self.client
        client.force_login(self.staff)
        session = client.session
        session[VIEW_AS_KEY] = self.acme.pk
        session.save()
        self.assertEqual(client.get("/kund/sms/nycklar/").status_code, 404)
        self.assertNotContains(client.get("/kund/sms/"), 'href="/kund/sms/nycklar/"')

    def test_statements_show_no_markup(self):
        self.message(provider_cost=5200, customer_price=5700)
        self.client.force_login(self.contact)
        page = self.client.get("/kund/sms/underlag/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Påslag")
        self.assertNotContains(page, "påslag")

    def test_staff_manages_keys_from_the_customer_card(self):
        client = self.client
        client.force_login(self.staff)
        url = reverse("manage:sms_keys", args=[self.acme.pk])
        page = client.post(url, {"name": "Webbshop"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("no-store", page.get("Cache-Control", ""))
        key = self.account.api_keys.get()
        self.assertContains(page, key.prefix)
        self.assertEqual(len(mail.outbox), 0)
        listing = client.get(url)
        self.assertContains(listing, "Webbshop")
        self.assertNotContains(listing, "Den nya nyckeln")
        client.post(reverse("manage:sms_key_revoke", args=[self.acme.pk, key.pk]))
        key.refresh_from_db()
        self.assertIsNotNone(key.revoked_at)

    def test_key_page_is_staff_only(self):
        self.client.force_login(self.contact)
        url = reverse("manage:sms_keys", args=[self.acme.pk])
        self.assertNotEqual(self.client.get(url).status_code, 200)
        self.assertNotEqual(self.client.post(url, {"name": "X"}).status_code, 200)
        self.assertFalse(self.account.api_keys.exists())

    def test_customer_card_checkbox_turns_self_service_on(self):
        client = self.client
        client.force_login(self.staff)
        form = {
            "is_enabled": "on",
            "customer_manages_api": "on",
            "sender_name": "AcmeBygg",
            "markup_ore_per_part": "5",
            "yearly_fee_kr": "999",
            "monthly_cap_kr": "500",
            "allowed_countries": "SE",
        }
        client.post(reverse("manage:sms_customer_update", args=[self.acme.pk]), form)
        self.account.refresh_from_db()
        self.assertTrue(self.account.customer_manages_api)
        del form["customer_manages_api"]
        client.post(reverse("manage:sms_customer_update", args=[self.acme.pk]), form)
        self.account.refresh_from_db()
        self.assertFalse(self.account.customer_manages_api)


class SenderChoiceTests(SmsTestCase):
    """Giovanni 2026-10-06: kunden får välja avsändarnamn om ADX slagit på
    det på kundkortet. Byrån larmas vid varje byte; kunden mejlas inte."""

    url = "/kund/sms/avsandare/"

    def allow(self, on=True):
        SmsAccount.objects.filter(pk=self.account.pk).update(customer_sets_sender=on)
        self.account.refresh_from_db()

    def test_off_by_default_the_customer_cannot_change_it(self):
        self.client.force_login(self.contact)
        page = self.client.get("/kund/sms/")
        self.assertNotContains(page, 'name="sender_name"')
        self.assertContains(page, "kontakta ADX")
        self.assertEqual(self.client.post(self.url, {"sender_name": "Nytt"}).status_code, 404)
        self.account.refresh_from_db()
        self.assertEqual(self.account.sender_name, "AcmeBygg")

    def test_when_allowed_the_customer_sets_it_and_adx_is_alerted(self):
        self.allow()
        self.client.force_login(self.contact)
        self.assertContains(self.client.get("/kund/sms/"), 'name="sender_name"')
        self.client.post(self.url, {"sender_name": "AcmeTak"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.sender_name, "AcmeTak")
        self.assertEqual(self.account.sender_changed_by, self.contact)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("AcmeTak", mail.outbox[0].subject)
        self.assertNotIn(self.contact.email, mail.outbox[0].to)

    def test_invalid_names_are_refused(self):
        self.allow()
        self.client.force_login(self.contact)
        for bad in ("A", "1Acme", "Acme Bygg", "Åkeri", "x" * 12, ""):
            with self.subTest(bad=bad):
                self.client.post(self.url, {"sender_name": bad})
                self.account.refresh_from_db()
                self.assertEqual(self.account.sender_name, "AcmeBygg")
        self.assertEqual(len(mail.outbox), 0)

    def test_the_api_follows_the_new_sender(self):
        self.allow()
        self.client.force_login(self.contact)
        self.client.post(self.url, {"sender_name": "AcmeTak"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.sender_name, "AcmeTak")

    def test_customer_card_checkbox(self):
        self.client.force_login(self.staff)
        form = {
            "is_enabled": "on",
            "customer_sets_sender": "on",
            "sender_name": "AcmeBygg",
            "markup_ore_per_part": "5",
            "yearly_fee_kr": "999",
            "monthly_cap_kr": "500",
            "allowed_countries": "SE",
        }
        self.client.post(reverse("manage:sms_customer_update", args=[self.acme.pk]), form)
        self.account.refresh_from_db()
        self.assertTrue(self.account.customer_sets_sender)
        del form["customer_sets_sender"]
        self.client.post(reverse("manage:sms_customer_update", args=[self.acme.pk]), form)
        self.account.refresh_from_db()
        self.assertFalse(self.account.customer_sets_sender)
