"""
Mätningen: klick på telefonnumret som förfrågningar, konverteringarna till
Google utan fråga om samtycke på sidan (beslut 2026-10-03, google_conversions.py),
Googles rapporter (google_reports.py), översiktens annonspengar och
flamingo_google_sync.

Inget här når nätet: urlopen byts mot FakeGoogle (test_google_ads.py) och
google_accounts.sync_account_status byts mot en attrapp.
"""

import re
import sys
import types
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.models import Customer

from . import exports, google_conversions, google_reports, leads, limits, manage_review
from .app_views.overview import ad_spend_kr, numbers_for, period_start
from .google_ads import STOCKHOLM, GoogleAdsError
from .models import (
    Campaign,
    CampaignDayStats,
    ConversionUpload,
    Fact,
    FlamingoAccount,
    GoogleAdsConnection,
    Lead,
    Service,
    SmsLog,
)
from .rules import waiting_leads
from .templatetags.flamingo_app import kr
from .test_google_ads import API, CONFIGURED, NOTHING, TOKEN_OK, FakeGoogle, google_error

User = get_user_model()
CUSTOMER_ID = "1234567891"
ELKS = {
    "ELKS_API_USERNAME": "u-test",
    "ELKS_API_PASSWORD": "p-test",
    "ELKS_SENDER": "ADXFlamingo",
}
DATETIME_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


def patch_http(fake):
    return mock.patch("apps.flamingo.google_ads.urlopen", fake)


class MeasureFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.acme = Customer.objects.create(name="Lindqvist Rör AB")
        cls.acme.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(
            customer=cls.acme,
            is_enabled=True,
            google_ads_customer_id="123-456-7891",
            google_status=FlamingoAccount.GOOGLE_BILLING_OK,
        )
        Fact.objects.create(
            account=cls.account,
            key="telefon",
            label="Telefon",
            value="08-000 00 00",
            confirmed=True,
        )
        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        cls.badrum = Service.objects.create(
            account=cls.account, name="Badrumsrenovering", sales_mode=Service.SALES_QUOTE
        )
        cls.call_page = Campaign.objects.create(
            account=cls.account,
            service=cls.jour,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            page={"title": "Rörjour i Nacka", "phone": "08-000 00 00"},
        )
        cls.quote_page = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum Nacka",
            status=Campaign.STATUS_LIVE,
            page={"title": "Badrumsrenovering i Nacka", "phone": "08-000 00 00"},
        )
        cls.draft = Campaign.objects.create(
            account=cls.account, service=cls.badrum, name="Utkast", page={"title": "Utkast"}
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def ring_url(self, campaign):
        return reverse("flamingo_public:call_click", args=[campaign.page_slug])

    def lead(self, **fields):
        fields.setdefault("account", self.account)
        fields.setdefault("campaign", self.quote_page)
        fields.setdefault("service", self.badrum)
        fields.setdefault("created_at", timezone.now() - timedelta(hours=8))
        return Lead.objects.create(**fields)


# ---------------------------------------------------------------------------
# Klicket på numret
# ---------------------------------------------------------------------------


class CallClickTests(MeasureFixture, TestCase):
    def test_a_click_becomes_a_lead_with_click_ids(self):
        data = {
            "gclid": "Cj0KCQ_abc-123",
            "wbraid": "W1",
            "utm_source": "google",
            "utm_medium": "cpc",
            "utm_term": "rörjour nacka",
            # Sidan frågar inte om samtycke: ett postat svar läses aldrig.
            "ad_consent": "granted",
            "name": "ignoreras",
        }
        response = Client().post(self.ring_url(self.call_page), data, REMOTE_ADDR="10.0.0.1")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        lead = Lead.objects.get(campaign=self.call_page)
        self.assertEqual(lead.source, Lead.SOURCE_CALL_CLICK)
        self.assertEqual((lead.name, lead.phone, lead.email), ("", "", ""))
        self.assertEqual((lead.gclid, lead.wbraid, lead.gbraid), ("Cj0KCQ_abc-123", "W1", ""))
        self.assertEqual(lead.utm["utm_source"], "google")
        self.assertEqual(lead.keyword, "rörjour nacka")
        self.assertEqual(lead.ad_consent, Lead.CONSENT_UNKNOWN)
        self.assertEqual(lead.ip_hash, limits.ip_hash("10.0.0.1"))
        self.assertEqual((lead.account, lead.service), (self.account, self.jour))
        self.assertEqual(lead.display_name, "Klick på telefonnumret")
        # gclid: samtalet köas som konvertering hos Google, utan fråga.
        self.assertEqual(
            list(lead.conversions.values_list("kind", flat=True)), [ConversionUpload.KIND_CALL]
        )

    def test_keyword_param_and_bad_values(self):
        data = {"keyword": "akut rör", "gclid": "<script>", "ad_consent": "ja tack"}
        Client().post(self.ring_url(self.call_page), data)
        lead = Lead.objects.get(campaign=self.call_page)
        self.assertEqual(lead.keyword, "akut rör")
        self.assertEqual(lead.gclid, "")
        self.assertEqual(lead.ad_consent, "")
        self.assertFalse(lead.conversions.exists())

    def test_a_gclid_queues_without_consent_but_a_braid_alone_does_not(self):
        Client().post(
            self.ring_url(self.call_page),
            {"gclid": "G1", "ad_consent": "denied"},
            REMOTE_ADDR="10.0.0.1",
        )
        Client().post(self.ring_url(self.call_page), {"gclid": "G2"}, REMOTE_ADDR="10.0.0.2")
        Client().post(
            self.ring_url(self.call_page),
            {"gbraid": "B3", "ad_consent": "granted"},
            REMOTE_ADDR="10.0.0.3",
        )
        self.assertEqual(Lead.objects.filter(campaign=self.call_page).count(), 3)
        self.assertEqual(
            sorted(ConversionUpload.objects.values_list("lead__gclid", "kind")),
            [("G1", "call"), ("G2", "call")],
        )
        # Inget samtycke hittas på, och inget postat svar sparas.
        consents = set(Lead.objects.values_list("ad_consent", flat=True))
        self.assertEqual(consents, {""})

    def test_only_live_public_pages_count(self):
        staff = Client()
        staff.force_login(self.staff)
        # Utkastet: 404 även för byrån (ingen förhandsvisning här).
        self.assertEqual(Client().post(self.ring_url(self.draft)).status_code, 404)
        self.assertEqual(staff.post(self.ring_url(self.draft)).status_code, 404)
        self.assertEqual(Client().post("/lp/finns-inte/ring/").status_code, 404)
        for status in (Campaign.STATUS_PAUSED, Campaign.STATUS_IN_REVIEW):
            Campaign.objects.filter(pk=self.call_page.pk).update(status=status)
            with self.subTest(status=status):
                self.assertEqual(Client().post(self.ring_url(self.call_page)).status_code, 404)
        Campaign.objects.filter(pk=self.call_page.pk).update(status=Campaign.STATUS_LIVE)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.assertEqual(Client().post(self.ring_url(self.call_page)).status_code, 404)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=False, is_enabled=False)
        self.assertEqual(Client().post(self.ring_url(self.call_page)).status_code, 404)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=True)
        Customer.objects.filter(pk=self.acme.pk).update(is_active=False)
        self.assertEqual(Client().post(self.ring_url(self.call_page)).status_code, 404)
        self.assertFalse(Lead.objects.exists())

    def test_get_is_not_allowed_and_staff_is_not_counted(self):
        self.assertEqual(Client().get(self.ring_url(self.call_page)).status_code, 405)
        staff = Client()
        staff.force_login(self.staff)
        self.assertEqual(staff.post(self.ring_url(self.call_page)).status_code, 204)
        self.assertFalse(Lead.objects.exists())

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(self.ring_url(self.call_page)).status_code, 403)
        html = client.get(self.call_page.landing_url).content.decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html).group(1)
        response = client.post(self.ring_url(self.call_page), {"csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).count(), 1)

    def test_one_click_per_visitor_and_hour(self):
        url = self.ring_url(self.call_page)
        for _ in range(3):
            self.assertEqual(Client().post(url, REMOTE_ADDR="10.0.0.1").status_code, 204)
        self.assertEqual(Lead.objects.count(), 1)
        # En annan besökare räknas.
        Client().post(url, REMOTE_ADDR="10.0.0.2")
        self.assertEqual(Lead.objects.count(), 2)
        # Efter en timme räknas samma besökare igen.
        Lead.objects.update(created_at=timezone.now() - timedelta(minutes=61))
        Client().post(url, REMOTE_ADDR="10.0.0.1")
        self.assertEqual(Lead.objects.count(), 3)

    def test_a_visitor_who_already_sent_the_form_is_not_counted_again(self):
        self.lead(
            campaign=self.call_page, ip_hash=limits.ip_hash("10.0.0.5"), created_at=timezone.now()
        )
        Client().post(self.ring_url(self.call_page), REMOTE_ADDR="10.0.0.5")
        self.assertFalse(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).exists())

    def test_rate_limits_per_campaign_and_per_visitor(self):
        url = self.ring_url(self.call_page)
        with mock.patch.object(limits, "CALL_CLICKS_PER_CAMPAIGN", 2):
            for n in range(4):
                Client().post(url, REMOTE_ADDR=f"10.0.1.{n}")
        self.assertEqual(Lead.objects.filter(campaign=self.call_page).count(), 2)
        with mock.patch.object(limits, "CALL_CLICKS_PER_IP", 1):
            Client().post(self.ring_url(self.quote_page), REMOTE_ADDR="10.0.1.0")
        self.assertFalse(Lead.objects.filter(campaign=self.quote_page).exists())
        # Klicken stänger inte formuläret: de räknas för sig.
        with mock.patch.object(limits, "LEADS_PER_CAMPAIGN", 2):
            response = Client().post(
                self.call_page.landing_url, {"phone": "070-111 22 33"}, REMOTE_ADDR="10.0.2.1"
            )
        self.assertEqual(response.status_code, 302)

    def test_one_ipv6_network_is_one_visitor(self):
        # En anslutning har ett helt /64: att byta adress inom det ger inga
        # fler klick (granskningen 2026-10-03: 40 adresser gav 30 förfrågningar).
        url = self.ring_url(self.call_page)
        for n in range(1, 41):
            Client().post(url, {"gclid": f"G{n}"}, REMOTE_ADDR=f"2001:db8::{n:x}")
        self.assertEqual(
            Lead.objects.filter(campaign=self.call_page).count(), limits.CALL_CLICKS_PER_IP
        )
        self.assertEqual(limits.ip_bucket("2001:db8::1"), "2001:db8::/64")
        self.assertEqual(limits.ip_bucket("2001:db8::ffff"), limits.ip_bucket("2001:db8::1"))
        self.assertNotEqual(limits.ip_bucket("2001:db8:0:1::1"), limits.ip_bucket("2001:db8::1"))
        self.assertEqual(limits.ip_bucket("::ffff:10.0.0.1"), "10.0.0.1")
        self.assertEqual(limits.ip_bucket("10.0.0.1"), "10.0.0.1")
        self.assertEqual(limits.ip_hash("2001:db8::1"), limits.ip_hash("2001:db8::2"))

    def test_two_ad_clicks_behind_one_address_both_count(self):
        # Mobiloperatörernas CGNAT: många bakom samma IPv4-adress. Var sitt
        # annonsklick (gclid) är var sin besökare; samma klick räknas en gång.
        url = self.ring_url(self.call_page)
        Client().post(url, {"gclid": "G-anna"}, REMOTE_ADDR="10.9.9.9")
        Client().post(url, {"gclid": "G-bo"}, REMOTE_ADDR="10.9.9.9")
        Client().post(url, {"gclid": "G-anna"}, REMOTE_ADDR="10.9.9.9")
        self.assertEqual(sorted(Lead.objects.values_list("gclid", flat=True)), ["G-anna", "G-bo"])

    @override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@example.com")
    def test_the_campaign_cap_alerts_adx_once_an_hour(self):
        url = self.ring_url(self.call_page)
        with mock.patch.object(limits, "CALL_CLICKS_PER_CAMPAIGN", 2):
            for n in range(6):
                Client().post(url, {"gclid": f"G{n}"}, REMOTE_ADDR=f"10.0.3.{n}")
        self.assertEqual(Lead.objects.filter(campaign=self.call_page).count(), 2)
        self.assertEqual(len(mail.outbox), 1)
        alert = mail.outbox[0]
        self.assertEqual(alert.to, ["byran@example.com"])
        self.assertIn("gränsen för klick på numret", alert.subject)
        self.assertIn("Rörjour Nacka", alert.body)
        # Aldrig till kunden.
        self.assertNotIn("anna@ror.se", alert.to)

    def test_no_sms_for_a_click(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            notify_sms=True, notify_phone="070-555 55 55"
        )
        with (
            override_settings(**ELKS),
            mock.patch("apps.flamingo.sms.urlopen") as urlopen,
        ):
            Client().post(self.ring_url(self.call_page), {"gclid": "G"})
        urlopen.assert_not_called()
        self.assertFalse(SmsLog.objects.exists())
        self.assertEqual(mail.outbox, [])
        self.assertTrue(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).exists())


# ---------------------------------------------------------------------------
# Sidan: ingen fråga om samtycke, och numrets länkar
# ---------------------------------------------------------------------------


class LandingMeasureTests(MeasureFixture, TestCase):
    def test_the_page_never_asks_about_consent(self):
        """Beslut 2026-10-03: konverteringarna går till Google utan en fråga
        på sidan. Ingen remsa, inget dolt fält, inget i skriptet."""
        for query in ("", "?gclid=Cj0abc", "?gbraid=0AAAAx", "?wbraid=W1"):
            with self.subTest(query=query):
                html = Client().get(self.quote_page.landing_url + query).content.decode()
                self.assertNotIn("lp-consent", html)
                self.assertNotIn("data-fl-consent", html)
                self.assertNotIn("Får vi berätta för Google", html)
                self.assertNotIn('name="ad_consent"', html)
        script = (settings.BASE_DIR / "static" / "js" / "flamingo-lp.js").read_text()
        self.assertNotIn("ad_consent", script)
        self.assertNotIn("sessionStorage", script)
        css = (settings.BASE_DIR / "static" / "css" / "flamingo-lp.css").read_text()
        self.assertNotIn("lp-consent", css)

    def test_tel_links_are_marked_and_the_beacon_is_only_for_visitors(self):
        html = Client().get(self.quote_page.landing_url).content.decode()
        self.assertIn("js/flamingo-lp.js", html)
        tel_links = re.findall(r'<a [^>]*href="tel:[^"]*"[^>]*>', html)
        self.assertGreaterEqual(len(tel_links), 3)  # överst, "eller ring", sidfoten
        for link in tel_links:
            self.assertIn("data-fl-call", link)
        self.assertIn(f'data-fl-beacon="{self.ring_url(self.quote_page)}"', html)
        call = Client().get(self.call_page.landing_url).content.decode()
        self.assertRegex(call, r'<a class="lp-call" href="tel:[^"]+" data-fl-call>')
        staff = Client()
        staff.force_login(self.staff)
        self.assertNotIn("data-fl-beacon", staff.get(self.quote_page.landing_url).content.decode())
        self.assertNotIn("data-fl-beacon", staff.get(self.draft.landing_url).content.decode())
        thanks = reverse("flamingo_public:thanks", args=[self.quote_page.page_slug])
        self.assertNotIn("data-fl-beacon", Client().get(thanks).content.decode())

    def test_the_form_queues_a_conversion_without_asking(self):
        url = self.quote_page.landing_url + "?gclid=Cj0abc"
        form = {"name": "Anna", "phone": "070-111 22 33"}
        self.assertEqual(Client().post(url, form, REMOTE_ADDR="10.0.0.1").status_code, 302)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual((lead.ad_consent, lead.gclid), ("", "Cj0abc"))
        self.assertEqual(
            list(lead.conversions.values_list("kind", flat=True)), [ConversionUpload.KIND_LEAD]
        )
        # Ett postat svar om samtycke läses aldrig (sidan frågar inte).
        for consent in ("granted", "denied", "kanske"):
            Client().post(url, {**form, "ad_consent": consent}, REMOTE_ADDR="10.0.0.2")
        self.assertEqual(set(Lead.objects.values_list("ad_consent", flat=True)), {""})
        # Utan gclid köas inget.
        Client().post(self.quote_page.landing_url, form, REMOTE_ADDR="10.0.0.3")
        self.assertEqual(ConversionUpload.objects.filter(lead__gclid="").count(), 0)


# ---------------------------------------------------------------------------
# Kön: allt med gclid, samtycket avgör inte
# ---------------------------------------------------------------------------


class QueueRuleTests(MeasureFixture, TestCase):
    def test_deals_need_a_gclid_but_not_consent(self):
        granted = self.lead(name="Ja", gclid="G1", ad_consent=Lead.CONSENT_GRANTED)
        denied = self.lead(name="Nej", gclid="G2", ad_consent=Lead.CONSENT_DENIED)
        unknown = self.lead(name="Vet ej", gclid="G3")
        braid = self.lead(name="iPhone", wbraid="W", ad_consent=Lead.CONSENT_GRANTED)
        for lead in (granted, denied, unknown, braid):
            lead.set_status(Lead.STATUS_WON, value_kr=1000)
        self.assertEqual(
            sorted(ConversionUpload.objects.values_list("lead__name", "kind")),
            [("Ja", "deal"), ("Nej", "deal"), ("Vet ej", "deal")],
        )

    def test_junk_removes_a_queued_arrival_and_undoing_it_queues_again(self):
        lead = leads.create_lead(self.quote_page, {"name": "Anna", "phone": "070", "gclid": "G1"})
        self.assertTrue(lead.conversions.filter(kind=ConversionUpload.KIND_LEAD).exists())
        lead.set_status(Lead.STATUS_JUNK)
        self.assertFalse(lead.conversions.exists())
        lead.set_status(Lead.STATUS_CONTACTED)
        self.assertTrue(lead.conversions.filter(kind=ConversionUpload.KIND_LEAD).exists())
        # Det som redan gått iväg rörs aldrig.
        lead.conversions.update(status=ConversionUpload.STATUS_SENT)
        lead.set_status(Lead.STATUS_JUNK)
        self.assertEqual(lead.conversions.get().status, ConversionUpload.STATUS_SENT)

    def test_the_csv_writes_each_kind_with_its_name(self):
        moment = timezone.now() - timedelta(days=2)
        form = self.lead(name="Form", gclid="GF", ad_consent="granted", created_at=moment)
        click = self.lead(
            source=Lead.SOURCE_CALL_CLICK, gclid="GC", ad_consent="granted", created_at=moment
        )
        form.queue_arrival_conversion()
        click.queue_arrival_conversion()
        form.set_status(Lead.STATUS_WON, value_kr=25000)
        # En rad utan samtycke kommer med, med tom cell för samtycket: inget
        # svar hittas på (beslut 2026-10-03).
        old = self.lead(name="Gammal", gclid="GO")
        ConversionUpload.objects.create(lead=old, kind=ConversionUpload.KIND_DEAL, value_kr=5)
        uploads = manage_review.queued_uploads()
        self.assertEqual(uploads.count(), 4)
        lines = exports.offline_conversions_csv(uploads).splitlines()
        self.assertTrue(lines[1].endswith("Conversion Currency,Ad User Data"))
        rows = sorted(lines[2:])
        self.assertEqual(len(rows), 4)
        self.assertRegex(
            rows[0], r"^GC,ADX Flamingo samtal,\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,,SEK,Granted$"
        )
        self.assertRegex(rows[1], r"^GF,ADX Flamingo affär,[^,]+,25000,SEK,Granted$")
        self.assertRegex(rows[2], r"^GF,ADX Flamingo förfrågan,[^,]+,,SEK,Granted$")
        self.assertRegex(rows[3], r"^GO,ADX Flamingo affär,[^,]+,5,SEK,$")


# ---------------------------------------------------------------------------
# Konverteringarna till Google
# ---------------------------------------------------------------------------

ACTIONS = {
    "lead": f"customers/{CUSTOMER_ID}/conversionActions/11",
    "call": f"customers/{CUSTOMER_ID}/conversionActions/12",
    "deal": f"customers/{CUSTOMER_ID}/conversionActions/13",
}


def partial_failure(*items):
    """partialFailureError med (index, grupp, kod)."""
    return {
        "code": 3,
        "message": "Multiple errors in details.",
        "details": [
            {
                "@type": f"type.googleapis.com/google.ads.googleads.{API}.errors.GoogleAdsFailure",
                "errors": [
                    {
                        "errorCode": {group: code},
                        "message": f"{code} hos Google.",
                        "location": {
                            "fieldPathElements": [{"fieldName": "conversions", "index": index}]
                        },
                    }
                    for index, group, code in items
                ],
            }
        ],
    }


#: Uppladdningen med API:t är av från början (Google tar inte emot nya
#: användare av uploadClickConversions sedan 2026-06-15).
UPLOADS_ON = {**CONFIGURED, "GOOGLE_ADS_UPLOAD_CONVERSIONS": True}


@override_settings(**UPLOADS_ON)
class UploadTests(MeasureFixture, TestCase):
    def setUp(self):
        super().setUp()
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_conversion_actions=ACTIONS)
        self.account.refresh_from_db()

    def queued(self, kind, **fields):
        fields.setdefault("gclid", f"G-{kind}")
        lead = self.lead(**fields)
        value = 186000 if kind == ConversionUpload.KIND_DEAL else None
        if kind == ConversionUpload.KIND_DEAL:
            Lead.objects.filter(pk=lead.pk).update(status=Lead.STATUS_WON, value_kr=value)
            lead.refresh_from_db()
        return ConversionUpload.objects.create(lead=lead, kind=kind, value_kr=value)

    def test_the_payload(self):
        # Sidan frågar inte (ad_consent tomt): samtycket utelämnas. Ett
        # riktigt svar skickas som det är.
        lead_row = self.queued("lead")
        deal_row = self.queued("deal", ad_consent=Lead.CONSENT_GRANTED)
        fake = FakeGoogle(TOKEN_OK, (200, {"results": [{"gclid": "G-lead"}, {"gclid": "G-deal"}]}))
        with patch_http(fake):
            result = google_conversions.upload_queued(self.account)
        self.assertEqual(result, {"sent": 2, "failed": 0, "waiting": 0})
        request = fake.requests[1]
        self.assertEqual(
            request.full_url,
            f"https://googleads.googleapis.com/{API}/customers/{CUSTOMER_ID}:uploadClickConversions",
        )
        self.assertEqual(request.get_header("Login-customer-id"), "1234567890")
        body = fake.body(1)
        self.assertIs(body["partialFailure"], True)
        self.assertIs(body["validateOnly"], False)
        first, second = body["conversions"]
        self.assertEqual(first["gclid"], "G-lead")
        self.assertEqual(first["conversionAction"], ACTIONS["lead"])
        self.assertNotIn("consent", first)
        self.assertEqual(second["consent"], {"adUserData": "GRANTED"})
        self.assertEqual(first["currencyCode"], "SEK")
        self.assertNotIn("conversionValue", first)
        self.assertNotIn("gbraid", first)
        self.assertRegex(first["conversionDateTime"], DATETIME_WITH_OFFSET)
        local = timezone.localtime(lead_row.lead.created_at, STOCKHOLM)
        self.assertTrue(first["conversionDateTime"].startswith(f"{local:%Y-%m-%d %H:%M:%S}+0"))
        self.assertEqual(second["conversionAction"], ACTIONS["deal"])
        self.assertEqual(second["conversionValue"], 186000)
        for row in (lead_row, deal_row):
            row.refresh_from_db()
            self.assertEqual(row.status, ConversionUpload.STATUS_SENT)
            self.assertIsNotNone(row.sent_at)
            self.assertEqual(row.response["api"], "uploadClickConversions")

    def test_the_time_is_stockholm_with_offset(self):
        row = self.queued("deal")
        Lead.objects.filter(pk=row.lead_id).update(won_at=datetime(2026, 1, 15, 8, 30, tzinfo=UTC))
        row.refresh_from_db()
        item = google_conversions.click_conversion(row, ACTIONS["deal"])
        self.assertEqual(item["conversionDateTime"], "2026-01-15 09:30:00+01:00")
        # Samma tid som filen skriver.
        self.assertEqual(exports.conversion_time(row), "2026-01-15 09:30:00")

    def test_young_clicks_wait_and_rows_without_gclid_stop(self):
        young = self.queued("lead", created_at=timezone.now() - timedelta(hours=1))
        no_gclid = self.queued("lead", gclid="")
        with patch_http(FakeGoogle()):  # inget anrop alls
            result = google_conversions.upload_queued(self.account)
        self.assertEqual(result["failed"], 1)
        young.refresh_from_db()
        no_gclid.refresh_from_db()
        self.assertEqual(young.status, ConversionUpload.STATUS_QUEUED)
        self.assertEqual(no_gclid.status, ConversionUpload.STATUS_FAILED)
        self.assertEqual(no_gclid.error, google_conversions.MSG_NO_GCLID)

    def test_rows_without_consent_are_sent_without_a_consent(self):
        """En rad som köades innan, eller utan svar: skickas, utan consent."""
        legacy = self.queued("deal")
        denied = self.queued("lead", gclid="G-nej", ad_consent=Lead.CONSENT_DENIED)
        fake = FakeGoogle(TOKEN_OK, (200, {"results": [{"gclid": "G-deal"}, {"gclid": "G-nej"}]}))
        with patch_http(fake):
            result = google_conversions.upload_queued(self.account)
        self.assertEqual(result, {"sent": 2, "failed": 0, "waiting": 0})
        by_gclid = {item["gclid"]: item for item in fake.body(1)["conversions"]}
        self.assertNotIn("consent", by_gclid["G-deal"])
        self.assertEqual(by_gclid["G-nej"]["consent"], {"adUserData": "DENIED"})
        for row in (legacy, denied):
            row.refresh_from_db()
            self.assertEqual(row.status, ConversionUpload.STATUS_SENT)

    def test_partial_failure_marks_each_row(self):
        rows = [
            self.queued("lead", gclid="A"),
            self.queued("call", source=Lead.SOURCE_CALL_CLICK, gclid="B"),
            self.queued("deal", gclid="C"),
            self.queued("lead", gclid="D"),
        ]
        payload = {
            "results": [{"gclid": "A"}, {}, {}, {}],
            "partialFailureError": partial_failure(
                (1, "conversionUploadError", "EXPIRED_EVENT"),
                (2, "conversionUploadError", "TOO_RECENT_CONVERSION_ACTION"),
                (3, "conversionUploadError", "CLICK_CONVERSION_ALREADY_EXISTS"),
            ),
            "jobId": "42",
        }
        with (
            patch_http(FakeGoogle(TOKEN_OK, (200, payload))),
            self.assertLogs("apps.flamingo.google_conversions", "WARNING") as logs,
        ):
            result = google_conversions.upload_queued(self.account)
        self.assertIn("EXPIRED_EVENT", logs.output[0])
        self.assertEqual(result, {"sent": 2, "failed": 1, "waiting": 1})
        for row in rows:
            row.refresh_from_db()
        sent, expired, recent, already = rows
        self.assertEqual(sent.status, ConversionUpload.STATUS_SENT)
        self.assertEqual(sent.response["job_id"], "42")
        self.assertEqual(expired.status, ConversionUpload.STATUS_FAILED)
        self.assertIn("för gammalt", expired.error)
        self.assertEqual(recent.status, ConversionUpload.STATUS_QUEUED)
        self.assertIn("nyss", recent.error)
        self.assertEqual(already.status, ConversionUpload.STATUS_SENT)
        self.assertEqual(already.response["note"], "Fanns redan hos Google.")
        # Nästa körning skickar bara raden som väntade.
        cache.clear()
        fake = FakeGoogle(TOKEN_OK, (200, {"results": [{"gclid": "C"}]}))
        with patch_http(fake):
            google_conversions.upload_queued(self.account)
        self.assertEqual([c["gclid"] for c in fake.body(1)["conversions"]], ["C"])
        recent.refresh_from_db()
        self.assertEqual(recent.status, ConversionUpload.STATUS_SENT)
        self.assertEqual(recent.error, "")

    def test_an_error_for_the_whole_call_keeps_the_rows(self):
        row = self.queued("lead")
        quota = google_error(429, "RESOURCE_EXHAUSTED", "quotaError", "RESOURCE_EXHAUSTED")
        with (
            patch_http(FakeGoogle(TOKEN_OK, quota)),
            self.assertRaises(GoogleAdsError) as caught,
            self.assertLogs("apps.flamingo.google_ads", "WARNING") as logs,
        ):
            google_conversions.upload_queued(self.account)
        for secret in (
            CONFIGURED["GOOGLE_ADS_REFRESH_TOKEN"],
            CONFIGURED["GOOGLE_ADS_CLIENT_SECRET"],
        ):
            self.assertNotIn(secret, "\n".join(logs.output))
        self.assertTrue(caught.exception.is_quota_error)
        row.refresh_from_db()
        self.assertEqual(row.status, ConversionUpload.STATUS_QUEUED)
        self.assertIn("kvoten", row.error)

    @override_settings(GOOGLE_ADS_UPLOAD_CONVERSIONS=False)
    def test_uploads_are_off_unless_turned_on(self):
        row = self.queued("lead")
        fake = FakeGoogle()
        with patch_http(fake):
            result = google_conversions.upload_queued(self.account)
        self.assertEqual(result, {"sent": 0, "failed": 0, "waiting": 0})
        self.assertEqual(fake.requests, [])
        self.assertFalse(google_conversions.upload_enabled())
        row.refresh_from_db()
        self.assertEqual(row.status, ConversionUpload.STATUS_QUEUED)

    def not_allowlisted(self, http=403):
        return google_error(
            http,
            "PERMISSION_DENIED",
            "notAllowlistedError",
            "CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE",
            message="Customer is not allowlisted for accessing this feature.",
        )

    def test_not_allowlisted_stops_uploads_for_everyone_and_keeps_the_rows(self):
        rows = [self.queued("lead"), self.queued("deal")]
        fake = FakeGoogle(TOKEN_OK, self.not_allowlisted())
        with patch_http(fake), self.assertRaises(GoogleAdsError) as caught:
            google_conversions.upload_queued(self.account)
        self.assertEqual(caught.exception.status, "UPLOAD_NOT_ALLOWED")
        self.assertIn("Data Manager API", caught.exception.message)
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, ConversionUpload.STATUS_QUEUED)
        connection = GoogleAdsConnection.objects.get(pk=GoogleAdsConnection.SOLO_PK)
        self.assertIsNotNone(connection.conversion_upload_blocked_at)
        self.assertFalse(google_conversions.upload_enabled())
        # Nästa körning försöker inte, och raderna finns kvar för CSV-filen.
        cache.clear()
        with patch_http(FakeGoogle()) as later:
            self.assertEqual(google_conversions.upload_queued(self.account)["sent"], 0)
        self.assertEqual(later.requests if hasattr(later, "requests") else [], [])
        self.assertEqual(manage_review.queued_uploads().count(), 2)

    def test_not_allowlisted_per_row_keeps_the_rows_too(self):
        rows = [self.queued("lead"), self.queued("call")]
        details = {
            "@type": f"type.googleapis.com/google.ads.googleads.{API}.errors.GoogleAdsFailure",
            "errors": [
                {
                    "errorCode": {
                        "notAllowlistedError": "CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE"
                    },
                    "message": "Not allowlisted.",
                    "location": {
                        "fieldPathElements": [{"fieldName": "conversions", "index": index}]
                    },
                }
                for index in (0, 1)
            ],
        }
        payload = {
            "partialFailureError": {"code": 7, "message": "x", "details": [details]},
            "results": [{}, {}],
        }
        with patch_http(FakeGoogle(TOKEN_OK, (200, payload))), self.assertRaises(GoogleAdsError):
            google_conversions.upload_queued(self.account)
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, ConversionUpload.STATUS_QUEUED)
        self.assertTrue(google_conversions.upload_blocked())

    def test_the_queue_and_the_google_page_say_uploads_are_stopped(self):
        self.queued("lead")
        google_conversions.block_uploads()
        client = Client()
        client.force_login(self.staff)
        html = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn("Uppladdningen med API:t är stoppad", html)
        self.assertIn("Ladda ner CSV", html)
        self.assertIn("ladda upp den i kontot", html)
        page = client.get(reverse("manage:flamingo_google")).content.decode()
        self.assertIn("Försök ladda upp igen", page)
        response = client.post(reverse("manage:flamingo_google_uploads_retry"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(google_conversions.upload_blocked(), "")
        self.assertTrue(google_conversions.upload_enabled())

    def test_nothing_happens_without_api_demo_or_link(self):
        row = self.queued("lead")
        with override_settings(**NOTHING), patch_http(FakeGoogle()):
            self.assertEqual(google_conversions.upload_queued(self.account)["sent"], 0)
        for change in ({"is_demo": True}, {"google_status": FlamingoAccount.GOOGLE_ID_GIVEN}):
            FlamingoAccount.objects.filter(pk=self.account.pk).update(**change)
            account = FlamingoAccount.objects.get(pk=self.account.pk)
            with self.subTest(change=change), patch_http(FakeGoogle()):
                self.assertEqual(google_conversions.upload_queued(account)["sent"], 0)
                self.assertEqual(google_reports.sync_stats(account), 0)
            FlamingoAccount.objects.filter(pk=self.account.pk).update(
                is_demo=False, google_status=FlamingoAccount.GOOGLE_BILLING_OK
            )
        row.refresh_from_db()
        self.assertEqual(row.status, ConversionUpload.STATUS_QUEUED)


@override_settings(**CONFIGURED)
class ConversionActionTests(MeasureFixture, TestCase):
    def test_creates_the_missing_ones_once(self):
        existing = {
            "results": [
                {
                    "conversionAction": {
                        "resourceName": ACTIONS["deal"],
                        "name": "ADX Flamingo affär",
                        "type": "UPLOAD_CLICKS",
                        "status": "ENABLED",
                    }
                }
            ]
        }
        created = {
            "results": [{"resourceName": ACTIONS["lead"]}, {"resourceName": ACTIONS["call"]}]
        }
        fake = FakeGoogle(TOKEN_OK, (200, existing), (200, created))
        with patch_http(fake):
            stored = google_conversions.ensure_conversion_actions(self.account)
        self.assertEqual(stored, ACTIONS)
        self.account.refresh_from_db()
        self.assertEqual(self.account.google_conversion_actions, ACTIONS)
        query = fake.body(1)["query"]
        self.assertIn("FROM conversion_action", query)
        self.assertIn("'ADX Flamingo förfrågan'", query)
        self.assertTrue(
            fake.requests[2].full_url.endswith(f"customers/{CUSTOMER_ID}/conversionActions:mutate")
        )
        operations = fake.body(2)["operations"]
        self.assertEqual(
            [(op["create"]["name"], op["create"]["category"]) for op in operations],
            [
                ("ADX Flamingo förfrågan", "SUBMIT_LEAD_FORM"),
                ("ADX Flamingo samtal", "PHONE_CALL_LEAD"),
            ],
        )
        for op in operations:
            self.assertEqual(op["create"]["type"], "UPLOAD_CLICKS")
            self.assertEqual(op["create"]["countingType"], "ONE_PER_CLICK")
            self.assertEqual(op["create"]["status"], "ENABLED")
        # Andra gången: inget anrop alls.
        with patch_http(FakeGoogle()):
            self.assertEqual(google_conversions.ensure_conversion_actions(self.account), ACTIONS)

    def test_a_name_taken_by_another_type_is_reported(self):
        existing = {
            "results": [
                {
                    "conversionAction": {
                        "resourceName": "customers/1/conversionActions/99",
                        "name": "ADX Flamingo samtal",
                        "type": "WEBPAGE",
                    }
                }
            ]
        }
        created = {
            "results": [{"resourceName": ACTIONS["lead"]}, {"resourceName": ACTIONS["deal"]}]
        }
        with patch_http(FakeGoogle(TOKEN_OK, (200, existing), (200, created))):
            with self.assertRaises(GoogleAdsError) as caught:
                google_conversions.ensure_conversion_actions(self.account)
        self.assertIn("ADX Flamingo samtal", caught.exception.message)
        self.account.refresh_from_db()
        self.assertEqual(
            self.account.google_conversion_actions,
            {"lead": ACTIONS["lead"], "deal": ACTIONS["deal"]},
        )

    def test_demo_never_calls_google(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.account.refresh_from_db()
        with patch_http(FakeGoogle()), self.assertRaises(GoogleAdsError) as caught:
            google_conversions.ensure_conversion_actions(self.account)
        self.assertEqual(caught.exception.status, "DEMO")


# ---------------------------------------------------------------------------
# Rapporterna och översikten
# ---------------------------------------------------------------------------


def stats_row(campaign_id, day, cost, clicks=3, impressions=40, currency="SEK"):
    return {
        "campaign": {
            "resourceName": f"customers/{CUSTOMER_ID}/campaigns/{campaign_id}",
            "id": str(campaign_id),
        },
        "segments": {"date": day},
        "metrics": {
            "costMicros": str(cost),
            "impressions": str(impressions),
            "clicks": str(clicks),
            "conversions": 1.0,
        },
        "customer": {"currencyCode": currency},
    }


@override_settings(**CONFIGURED)
class ReportTests(MeasureFixture, TestCase):
    def setUp(self):
        super().setUp()
        Campaign.objects.filter(pk=self.call_page.pk).update(google_campaign_id="555")
        Campaign.objects.filter(pk=self.quote_page.pk).update(
            google_campaign_id=f"customers/{CUSTOMER_ID}/campaigns/777"
        )
        self.today = date(2026, 10, 3)

    def test_sync_upserts_days_and_removes_stale_ones(self):
        CampaignDayStats.objects.create(
            campaign=self.call_page, date=date(2026, 10, 1), cost_micros=1, clicks=99
        )
        CampaignDayStats.objects.create(
            campaign=self.call_page, date=date(2026, 9, 30), cost_micros=5_000_000
        )
        CampaignDayStats.objects.create(
            campaign=self.call_page, date=date(2026, 8, 1), cost_micros=7_000_000
        )
        rows = [
            stats_row(555, "2026-10-01", 120_400_000),
            stats_row(555, "2026-10-02", 80_000_000, clicks=2),
            stats_row(777, "2026-10-02", 50_000_000, clicks=1, impressions=10),
            stats_row(999, "2026-10-02", 1_000_000),  # inte vår kampanj
        ]
        fake = FakeGoogle(TOKEN_OK, (200, {"results": rows}))
        with patch_http(fake):
            stored = google_reports.sync_stats(self.account, days=30, today=self.today)
        self.assertEqual(stored, 3)
        query = fake.body(1)["query"]
        self.assertIn("campaign.id IN (555, 777)", query)
        self.assertIn("segments.date BETWEEN '2026-09-04' AND '2026-10-03'", query)
        self.assertIn("metrics.cost_micros", query)
        self.assertTrue(
            fake.requests[1].full_url.endswith(f"customers/{CUSTOMER_ID}/googleAds:search")
        )
        day = CampaignDayStats.objects.get(campaign=self.call_page, date=date(2026, 10, 1))
        self.assertEqual((day.cost_micros, day.clicks, day.impressions), (120_400_000, 3, 40))
        self.assertEqual(day.cost_kr, 120)
        # 30 sep fanns inte i Googles svar längre; 1 aug ligger utanför perioden.
        self.assertFalse(CampaignDayStats.objects.filter(date=date(2026, 9, 30)).exists())
        self.assertTrue(CampaignDayStats.objects.filter(date=date(2026, 8, 1)).exists())
        # Igen: samma rader skrivs över, inga dubbletter.
        cache.clear()
        with patch_http(FakeGoogle(TOKEN_OK, (200, {"results": rows}))):
            google_reports.sync_stats(self.account, days=30, today=self.today)
        self.assertEqual(CampaignDayStats.objects.count(), 4)

    def test_another_currency_stores_nothing(self):
        rows = [stats_row(555, "2026-10-01", 1_000_000, currency="EUR")]
        with patch_http(FakeGoogle(TOKEN_OK, (200, {"results": rows}))):
            with self.assertRaises(GoogleAdsError) as caught:
                google_reports.sync_stats(self.account, today=self.today)
        self.assertIn("EUR", caught.exception.message)
        self.assertFalse(CampaignDayStats.objects.exists())

    def test_ad_spend_kr(self):
        now = timezone.now()
        since = now - timedelta(days=30)
        self.assertIsNone(ad_spend_kr(self.account, since))
        CampaignDayStats.objects.create(
            campaign=self.call_page,
            date=timezone.localdate(now) - timedelta(days=60),
            cost_micros=9_000_000,
        )
        # Rapporten finns men inget kostade i perioden: 0, inte okänt.
        self.assertEqual(ad_spend_kr(self.account, since), 0)
        CampaignDayStats.objects.create(
            campaign=self.call_page, date=timezone.localdate(now), cost_micros=1_200_400_000
        )
        CampaignDayStats.objects.create(
            campaign=self.quote_page,
            date=timezone.localdate(now) - timedelta(days=3),
            cost_micros=300_000_000,
        )
        self.assertEqual(ad_spend_kr(self.account, since), 1500)

    def test_spend_and_leads_cover_the_same_30_days(self):
        # 100 kr om dagen i 40 dagar: "30 dagar" är 30 kalenderdagar i svensk
        # tid, i dag medräknad, för både kostnaden och förfrågningarna.
        now = datetime(2026, 10, 3, 12, 0, tzinfo=STOCKHOLM)
        today = now.date()
        CampaignDayStats.objects.bulk_create(
            CampaignDayStats(
                campaign=self.call_page,
                date=today - timedelta(days=n),
                cost_micros=100_000_000,
            )
            for n in range(40)
        )
        inside = self.lead(created_at=datetime(2026, 9, 4, 0, 5, tzinfo=STOCKHOLM))
        self.lead(created_at=datetime(2026, 9, 3, 23, 55, tzinfo=STOCKHOLM))
        numbers = numbers_for(self.account, now)
        self.assertEqual(numbers.spend_kr, 3000)
        self.assertEqual(numbers.leads, 1)
        self.assertEqual(period_start(now).date(), inside.created_at.astimezone(STOCKHOLM).date())

    def test_the_overview_shows_spend_and_kr_per_lead(self):
        client = Client()
        client.force_login(self.anna)
        html = client.get(reverse("flamingo:app")).content.decode()
        self.assertIn("Kopplas när Google-rapporterna är på", html)
        now = timezone.now()
        CampaignDayStats.objects.create(
            campaign=self.call_page,
            date=timezone.localdate(now),
            cost_micros=1_500_000_000,
            clicks=30,
            impressions=900,
        )
        html = client.get(reverse("flamingo:app")).content.decode()
        self.assertIn(kr(1500), html)
        self.assertIn("Ingen förfrågan att räkna på än", html)
        self.assertNotIn("Kopplas när Google-rapporterna är på", html)
        for n in range(3):
            self.lead(name=f"Kund {n}", created_at=now - timedelta(days=1))
        self.lead(name="Klick", source=Lead.SOURCE_CALL_CLICK, created_at=now - timedelta(days=1))
        html = client.get(reverse("flamingo:app")).content.decode()
        self.assertIn("Kr per förfrågan", html)
        self.assertIn(kr(375), html)  # 1 500 kr på fyra förfrågningar, klicket medräknat
        self.assertIn("fick 30 klick enligt Google", html)


# ---------------------------------------------------------------------------
# Kommandot
# ---------------------------------------------------------------------------


class SyncCommandTests(MeasureFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        other = Customer.objects.create(name="Hemlig Bygg AB")
        cls.other_account = FlamingoAccount.objects.create(
            customer=other,
            is_enabled=True,
            google_ads_customer_id="222-333-4444",
            google_status=FlamingoAccount.GOOGLE_BILLING_OK,
        )
        demo = Customer.objects.create(name="Demo AB")
        FlamingoAccount.objects.create(
            customer=demo,
            is_enabled=True,
            is_demo=True,
            google_ads_customer_id="999-999-9999",
            google_status=FlamingoAccount.GOOGLE_BILLING_OK,
        )
        plain = Customer.objects.create(name="Utan id AB")
        FlamingoAccount.objects.create(customer=plain, is_enabled=True)

    def run_command(self, status_sync=None, **patches):
        fake_module = types.SimpleNamespace(
            sync_account_status=status_sync or mock.Mock(return_value=None)
        )
        out = StringIO()
        with (
            mock.patch.dict(sys.modules, {"apps.flamingo.google_accounts": fake_module}),
            mock.patch.object(
                google_conversions,
                "ensure_conversion_actions",
                patches.get("ensure", mock.Mock(return_value=ACTIONS)),
            ),
            mock.patch.object(
                google_conversions,
                "upload_queued",
                patches.get(
                    "upload", mock.Mock(return_value={"sent": 1, "failed": 0, "waiting": 0})
                ),
            ),
            mock.patch.object(
                google_reports, "sync_stats", patches.get("stats", mock.Mock(return_value=5))
            ),
        ):
            call_command("flamingo_google_sync", stdout=out)
        return out.getvalue(), fake_module.sync_account_status

    @override_settings(**NOTHING)
    def test_without_the_api_it_says_so_and_does_nothing(self):
        out, status_sync = self.run_command()
        self.assertEqual(
            out.strip().splitlines(), ["Google Ads API är inte inkopplat, inget synkades."]
        )
        status_sync.assert_not_called()

    @override_settings(**UPLOADS_ON)
    def test_every_real_account_is_synced_and_errors_do_not_stop_the_others(self):
        upload = mock.Mock(
            side_effect=[
                GoogleAdsError("Inloggningen når inte kontot.", status="PERMISSION_DENIED"),
                {"sent": 2, "failed": 0, "waiting": 0},
            ]
        )
        stats = mock.Mock(return_value=4)
        out, status_sync = self.run_command(upload=upload, stats=stats)
        synced = [call.args[0].pk for call in status_sync.call_args_list]
        self.assertEqual(synced, [self.account.pk, self.other_account.pk])
        self.assertEqual(upload.call_count, 2)
        # Rapporten läses för båda: ett fel i uppladdningen stoppar den inte.
        self.assertEqual(stats.call_count, 2)
        self.assertIn("1 konto(n) synkade med Google, 1 med fel.", out)
        self.account.refresh_from_db()
        self.assertEqual(
            self.account.google_sync_error, "Uppladdningen: Inloggningen når inte kontot."
        )

    @override_settings(**UPLOADS_ON)
    def test_a_conversion_name_of_the_wrong_type_does_not_stop_uploads_or_reports(self):
        ensure = mock.Mock(
            side_effect=GoogleAdsError(
                google_conversions.MSG_WRONG_TYPE.format(name="ADX Flamingo affär"),
                status="CONVERSION_ACTION_TYPE",
            )
        )
        upload = mock.Mock(return_value={"sent": 1, "failed": 0, "waiting": 1})
        stats = mock.Mock(return_value=3)
        out, _ = self.run_command(ensure=ensure, upload=upload, stats=stats)
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(stats.call_count, 2)
        self.account.refresh_from_db()
        self.assertIn("Konverteringarna:", self.account.google_sync_error)
        self.assertIn("ADX Flamingo affär", self.account.google_sync_error)

    @override_settings(**CONFIGURED)
    def test_uploads_are_left_to_the_csv_by_default_and_reports_still_run(self):
        upload = mock.Mock()
        stats = mock.Mock(return_value=2)
        out, _ = self.run_command(upload=upload, stats=stats)
        upload.assert_not_called()
        self.assertEqual(stats.call_count, 2)
        self.assertIn("2 konto(n) synkade med Google, 0 med fel.", out)

    @override_settings(**CONFIGURED)
    def test_a_report_error_is_recorded_after_the_status_warning(self):
        def status(account):
            FlamingoAccount.objects.filter(pk=account.pk).update(
                google_sync_error="Kontot är avstängt av Google hos Google."
            )

        stats = mock.Mock(side_effect=[GoogleAdsError("Fel valuta.", status="CURRENCY"), 1])
        out, _ = self.run_command(status_sync=mock.Mock(side_effect=status), stats=stats)
        self.account.refresh_from_db()
        self.assertEqual(
            self.account.google_sync_error,
            "Kontot är avstängt av Google hos Google. Rapporten: Fel valuta.",
        )
        self.assertIn("1 konto(n) synkade med Google, 1 med fel.", out)

    @override_settings(**CONFIGURED)
    def test_a_failed_status_read_counts_as_failed(self):
        failure = GoogleAdsError("Kontot är inte aktivt.", status="PERMISSION_DENIED")
        # sync_account_status returnerar felet (det kastas bara för ADX-fel).
        status = mock.Mock(
            side_effect=lambda account: failure if account.pk == self.account.pk else None
        )
        out, _ = self.run_command(status_sync=status)
        self.assertIn("1 konto(n) synkade med Google, 1 med fel.", out)
        self.assertIn("Läget: Kontot är inte aktivt.", out)

    @override_settings(**CONFIGURED)
    def test_a_broken_adx_connection_in_the_status_step_stops_the_run(self):
        status = mock.Mock(
            side_effect=GoogleAdsError(
                "Koppla igen.", status="UNAUTHENTICATED", codes=["oauth.invalid_grant"]
            )
        )
        out, _ = self.run_command(status_sync=status)
        self.assertEqual(status.call_count, 1)
        self.assertIn("Stoppad", out)
        self.assertIn("0 konto(n) synkade med Google, 1 med fel.", out)

    @override_settings(**CONFIGURED)
    def test_a_broken_adx_connection_stops_the_run(self):
        ensure = mock.Mock(
            side_effect=GoogleAdsError(
                "Koppla igen.", status="UNAUTHENTICATED", codes=["oauth.invalid_grant"]
            )
        )
        out, status_sync = self.run_command(ensure=ensure)
        self.assertEqual(ensure.call_count, 1)
        self.assertIn("Stoppad", out)

    @override_settings(**CONFIGURED)
    def test_an_account_not_under_the_mcc_only_gets_its_status_read(self):
        FlamingoAccount.objects.filter(pk=self.other_account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        ensure = mock.Mock(return_value=ACTIONS)
        out, status_sync = self.run_command(ensure=ensure)
        self.assertEqual(status_sync.call_count, 2)
        self.assertEqual([c.args[0].pk for c in ensure.call_args_list], [self.account.pk])


# ---------------------------------------------------------------------------
# Inkorgen, kön och tre saker
# ---------------------------------------------------------------------------


class InboxAndQueueTests(MeasureFixture, TestCase):
    def test_a_call_click_in_the_inbox(self):
        lead = self.lead(
            campaign=self.call_page,
            service=self.jour,
            source=Lead.SOURCE_CALL_CLICK,
            gclid="G1",
        )
        client = Client()
        client.force_login(self.anna)
        listing = client.get(reverse("flamingo:app_inbox")).content.decode()
        self.assertIn("Klick på telefonnumret", listing)
        detail = client.get(reverse("flamingo:app_lead", args=[lead.pk]))
        self.assertContains(detail, "Någon klickade på numret")
        self.assertContains(detail, "Google sök")
        # Ingen fråga om samtycke, varken på sidan eller här.
        self.assertNotContains(detail, "Får Google veta?")
        self.assertNotContains(detail, "sagt ja")
        self.assertContains(detail, "Beloppet går till Google som en konvertering")
        response = client.post(
            reverse("flamingo:app_lead", args=[lead.pk]), {"status": "won", "value_kr": "4 800"}
        )
        self.assertEqual(response.status_code, 302)
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.value_kr), (Lead.STATUS_WON, 4800))
        self.assertEqual(lead.conversions.get(kind=ConversionUpload.KIND_DEAL).value_kr, 4800)

    def test_the_three_things_say_click_not_answer(self):
        self.lead(source=Lead.SOURCE_CALL_CLICK, created_at=timezone.now() - timedelta(hours=3))
        thing = waiting_leads(self.account, timezone.now())
        self.assertEqual(thing.title, "Sätt status på ett klick på numret")

    def test_the_queue_page_shows_kinds_names_and_failures(self):
        lead = self.lead(name="Sara", gclid="G1", ad_consent=Lead.CONSENT_GRANTED)
        lead.queue_arrival_conversion()
        failed = self.lead(name="Gammal", gclid="G2", ad_consent=Lead.CONSENT_GRANTED)
        ConversionUpload.objects.create(
            lead=failed,
            kind=ConversionUpload.KIND_LEAD,
            status=ConversionUpload.STATUS_FAILED,
            error="Klicket är för gammalt.",
        )
        client = Client()
        client.force_login(self.staff)
        with override_settings(**NOTHING):
            html = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn("ADX Flamingo förfrågan", html)
        self.assertIn("ADX Flamingo samtal", html)
        self.assertIn("Klicket är för gammalt.", html)
        self.assertIn("Sara", html)
        self.assertNotIn("Uppladdningen med API:t är på", html)
        # Med API:t men uppladdningen av (från början): filen är vägen.
        with override_settings(**CONFIGURED):
            html = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertNotIn("Uppladdningen med API:t är på", html)
        self.assertIn("ladda upp den i kontot", html)
        with override_settings(**UPLOADS_ON):
            html = client.get(reverse("manage:flamingo_queue")).content.decode()
        self.assertIn("Uppladdningen med API:t är på", html)
