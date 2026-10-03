"""ADX Flamingo: landningssidorna (/lp/), förfrågningarna, sms och inkorgen
(kundresan steg 9-11)."""

import base64
import re
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import leads, sms
from .models import (
    Campaign,
    ConversionUpload,
    Fact,
    FlamingoAccount,
    Lead,
    Service,
    SmsLog,
)
from .public_views import HONEYPOT, RATE_LIMIT

User = get_user_model()
STHLM = ZoneInfo("Europe/Stockholm")
ELKS = {
    "ELKS_API_USERNAME": "u-test",
    "ELKS_API_PASSWORD": "p-test",
    "ELKS_SENDER": "ADXFlamingo",
}
NO_ELKS = {"ELKS_API_USERNAME": "", "ELKS_API_PASSWORD": "", "ELKS_SENDER": ""}


def _day(hour, minute=0):
    return datetime(2026, 10, 3, hour, minute, tzinfo=STHLM)


def _elks_ok(urlopen, provider_id="s0123"):
    response = urlopen.return_value.__enter__.return_value
    response.read.return_value = f'{{"id": "{provider_id}", "status": "created"}}'.encode()
    return urlopen


class InboxFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Lindqvist Rör AB")
        cls.other = Customer.objects.create(name="Hemlig Bygg AB")
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.acme.users.add(cls.anna)
        cls.bo = User.objects.create_user("bo@hemlig.se", email="bo@hemlig.se", password="x")
        cls.other.users.add(cls.bo)

        cls.account = FlamingoAccount.objects.create(customer=cls.acme, is_enabled=True)
        cls.other_account = FlamingoAccount.objects.create(customer=cls.other, is_enabled=True)
        for key, label, value in (
            ("telefon", "Telefon", "08-000 00 00"),
            ("adress", "Adress", "Exempelvägen 4, Nacka"),
        ):
            Fact.objects.create(
                account=cls.account, key=key, label=label, value=value, confirmed=True
            )

        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        cls.badrum = Service.objects.create(
            account=cls.account, name="Badrumsrenovering", sales_mode=Service.SALES_QUOTE
        )
        cls.secret_service = Service.objects.create(account=cls.other_account, name="Takbyte")

        cls.call_page = Campaign.objects.create(
            account=cls.account,
            service=cls.jour,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            page={
                "title": "Rörjour i Nacka",
                "lead": "Vattenläcka eller stopp? Ring oss.",
                "points": ["Jour dygnet runt, alla dagar"],
                "phone": "08-000 00 00",
                "form_title": "Hellre att vi ringer dig?",
                "questions": [],
                "note": "Stäng huvudkranen medan du väntar.",
            },
        )
        cls.quote_page = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum Nacka",
            status=Campaign.STATUS_LIVE,
            page={
                "title": "Badrumsrenovering i Nacka",
                "lead": "Berätta om ditt badrum.",
                "phone": "08-000 00 00",
                "form_title": "Berätta om ditt badrum",
                "questions": [
                    {"key": "storlek", "label": "Ungefär hur stort? (m2)", "kind": "text"},
                    {"key": "nar", "label": "När passar det?", "kind": "date"},
                ],
            },
        )
        cls.draft = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum utkast",
            page={"title": "Utkastets rubrik"},
        )
        cls.secret_page = Campaign.objects.create(
            account=cls.other_account,
            service=cls.secret_service,
            name="Hemlig kampanj",
            status=Campaign.STATUS_LIVE,
            page={"title": "Takbyte"},
        )
        cls.secret_lead = Lead.objects.create(
            account=cls.other_account, name="Hemlig Person", phone="070-999 99 99"
        )

    def setUp(self):
        cache.clear()

    def client_for(self, user, view_as=None):
        client = Client()
        client.force_login(user)
        if view_as is not None:
            session = client.session
            session[VIEW_AS_KEY] = view_as.pk
            session.save()
        return client

    def lead_for(self, **fields):
        fields.setdefault("account", self.account)
        fields.setdefault("campaign", self.quote_page)
        fields.setdefault("service", self.badrum)
        fields.setdefault("name", "Anna Lind")
        fields.setdefault("phone", "070-111 22 33")
        return Lead.objects.create(**fields)

    def quote_post(self, **extra):
        data = {
            "name": "Anna Lind",
            "phone": "070-111 22 33",
            "email": "anna@example.se",
            "message": "Vill byta allt.",
            "q_storlek": "6",
            "q_nar": "2026-10-20",
        }
        data.update(extra)
        return data


# ---------------------------------------------------------------------------
# Landningssidan
# ---------------------------------------------------------------------------


class LandingPageTests(InboxFixture, TestCase):
    def test_only_live_campaigns_are_public(self):
        anon = Client()
        self.assertEqual(anon.get("/lp/finns-inte/").status_code, 404)
        self.assertEqual(anon.get(self.draft.landing_url).status_code, 404)
        thanks = reverse("flamingo_public:thanks", args=[self.draft.page_slug])
        self.assertEqual(anon.get(thanks).status_code, 404)
        for status in (Campaign.STATUS_IN_REVIEW, Campaign.STATUS_PAUSED):
            Campaign.objects.filter(pk=self.draft.pk).update(status=status)
            with self.subTest(status=status):
                self.assertEqual(anon.get(self.draft.landing_url).status_code, 404)
                self.assertEqual(
                    self.client_for(self.anna).get(self.draft.landing_url).status_code, 404
                )
        response = anon.get(self.call_page.landing_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "flamingo/lp/page.html")

    def test_a_disabled_account_or_inactive_customer_hides_the_page(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=False)
        self.assertEqual(Client().get(self.call_page.landing_url).status_code, 404)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=True)
        Customer.objects.filter(pk=self.acme.pk).update(is_active=False)
        self.assertEqual(Client().get(self.call_page.landing_url).status_code, 404)

    def test_a_404_is_the_normal_site_404(self):
        unknown = Client().get("/finns-inte-alls-xyz/")
        response = Client().get(self.draft.landing_url)
        self.assertEqual(
            [t.name for t in response.templates][:1], [t.name for t in unknown.templates][:1]
        )

    def test_staff_previews_any_status_with_a_visible_bar(self):
        staff = self.client_for(self.staff)
        response = staff.get(self.draft.landing_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Förhandsvisning")
        self.assertContains(response, "Utkastets rubrik")
        self.assertContains(response, reverse("manage:flamingo_review", args=[self.draft.pk]))
        thanks = reverse("flamingo_public:thanks", args=[self.draft.page_slug])
        self.assertEqual(staff.get(thanks).status_code, 200)
        # Formuläret i förhandsvisningen skapar ingen förfrågan.
        response = staff.post(self.draft.landing_url, self.quote_post())
        self.assertRedirects(response, thanks)
        self.assertFalse(Lead.objects.filter(campaign=self.draft).exists())
        self.assertFalse(SmsLog.objects.exists())
        # Anonyma ser ingen remsa på en live-sida.
        self.assertNotContains(Client().get(self.call_page.landing_url), "Förhandsvisning")

    def test_the_page_is_the_customers_own(self):
        response = Client().get(self.call_page.landing_url)
        html = response.content.decode()
        # Samma företagsnamn som annonserna: utan bolagsform.
        self.assertIn('<p class="lp-business">Lindqvist Rör</p>', html)
        self.assertIn("Rörjour i Nacka", html)
        self.assertIn('name="robots" content="noindex', html)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn('href="tel:+4680000000"', html)
        self.assertIn('href="https://adx.se"', html)
        self.assertIn("Sidan drivs av ADX", html)
        self.assertIn("Exempelvägen 4, Nacka", html)
        self.assertIn("css/flamingo-lp.css", html)
        self.assertNotIn("ADX Flamingo", html)
        self.assertNotIn("flamingo.css", html)
        self.assertIn("width=device-width", html)

    def test_rating_only_from_a_confirmed_fact(self):
        fact = Fact.objects.create(
            account=self.account,
            key="betyg",
            label="Betyg",
            value="4,8 av 5 på Google",
            source=Fact.SOURCE_GOOGLE,
        )
        self.assertNotContains(Client().get(self.call_page.landing_url), "4,8 av 5")
        fact.confirmed = True
        fact.save()
        self.assertContains(Client().get(self.call_page.landing_url), "4,8 av 5 på Google")

    def test_call_mode_has_the_phone_first_and_the_form_last(self):
        html = Client().get(self.call_page.landing_url).content.decode()
        self.assertIn("Ring 08-000 00 00", html)
        self.assertLess(html.index('class="lp-call"'), html.index("<form"))
        self.assertIn("Medan du väntar", html)
        self.assertNotIn('name="email"', html)

    def test_quote_mode_asks_about_the_job(self):
        html = Client().get(self.quote_page.landing_url).content.decode()
        self.assertIn("Berätta om ditt badrum", html)
        self.assertIn('name="q_storlek"', html)
        self.assertIn('type="date"', html)
        self.assertIn('name="email"', html)
        self.assertIn("eller ring", html)
        self.assertLess(html.index("<form"), html.index("eller ring"))

    def test_click_ids_and_utm_ride_along_in_hidden_fields(self):
        url = (
            self.quote_page.landing_url
            + "?gclid=Cj0KCQ_abc-123&gbraid=0AAAAx&utm_source=google&utm_medium=cpc"
            + "&utm_term=badrum+nacka&annat=nej"
        )
        html = Client().get(url).content.decode()
        self.assertIn('name="gclid" value="Cj0KCQ_abc-123"', html)
        self.assertIn('name="gbraid" value="0AAAAx"', html)
        self.assertIn('name="utm_source" value="google"', html)
        self.assertIn('name="utm_term" value="badrum nacka"', html)
        self.assertNotIn('name="annat"', html)
        # Ett klick-id som inte ser ut som ett klick-id följer inte med.
        html = Client().get(self.quote_page.landing_url + '?gclid="><b>x').content.decode()
        self.assertNotIn('name="gclid"', html)

    def test_no_inline_styles_or_scripts(self):
        base = Path(settings.BASE_DIR) / "templates" / "flamingo"
        for folder in ("lp", "app/inbox"):
            for path in (base / folder).glob("*.html"):
                text = path.read_text(encoding="utf-8")
                with self.subTest(path=path.name):
                    self.assertNotIn("style=", text)
                    self.assertNotIn("<script", text)


class LeadFormTests(InboxFixture, TestCase):
    def test_a_form_post_creates_a_lead_on_the_right_account(self):
        data = self.quote_post(gclid="Cj0KCQ_abc-123", utm_source="google", utm_term="badrum")
        response = Client().post(self.quote_page.landing_url, data)
        thanks = reverse("flamingo_public:thanks", args=[self.quote_page.page_slug])
        self.assertRedirects(response, thanks)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual(lead.account, self.account)
        self.assertEqual(lead.service, self.badrum)
        self.assertEqual(lead.source, Lead.SOURCE_FORM)
        self.assertEqual(lead.status, Lead.STATUS_NEW)
        self.assertEqual(lead.name, "Anna Lind")
        self.assertEqual(lead.phone, "070-111 22 33")
        self.assertEqual(lead.email, "anna@example.se")
        self.assertEqual(lead.gclid, "Cj0KCQ_abc-123")
        self.assertEqual(lead.keyword, "badrum")
        self.assertEqual(lead.utm, {"utm_source": "google", "utm_term": "badrum"})
        self.assertEqual(
            lead.answers, {"Ungefär hur stort? (m2)": "6", "När passar det?": "2026-10-20"}
        )
        self.assertFalse(self.other_account.leads.exclude(pk=self.secret_lead.pk).exists())
        self.assertEqual(mail.outbox, [])
        response = Client().get(thanks)
        self.assertContains(response, "Din förfrågan är skickad till Lindqvist Rör.")
        self.assertNotContains(response, "Tack, din förfrågan")

    def test_tracking_is_read_from_the_address_when_hidden_fields_are_missing(self):
        url = self.quote_page.landing_url + "?gclid=Cj0abc&utm_campaign=host&wbraid=W1"
        self.assertEqual(Client().post(url, self.quote_post()).status_code, 302)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual(lead.gclid, "Cj0abc")
        self.assertEqual(lead.utm, {"utm_campaign": "host", "wbraid": "W1"})

    def test_input_is_sanitized_and_bad_click_ids_dropped(self):
        data = self.quote_post(
            name="<b>Anna</b> Lind",
            message="<script>alert(1)</script>Hej\nrad två",
            gclid="<script>",
            utm_source="<i>google</i>",
        )
        Client().post(self.quote_page.landing_url, data)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual(lead.name, "Anna Lind")
        self.assertNotIn("<", lead.message)
        self.assertIn("rad två", lead.message)
        self.assertEqual(lead.gclid, "")
        self.assertEqual(lead.utm, {"utm_source": "google"})

    def test_validation_errors_render_the_form_again(self):
        response = Client().post(self.quote_page.landing_url, self.quote_post(phone=""))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Skriv ditt telefonnummer.")
        response = Client().post(self.quote_page.landing_url, self.quote_post(name="x" * 121))
        self.assertContains(response, "för långt")
        response = Client().post(self.quote_page.landing_url, self.quote_post(q_nar="i morgon"))
        self.assertContains(response, "Välj ett datum.")
        self.assertFalse(Lead.objects.filter(campaign=self.quote_page).exists())
        # Ringer direkt: bara numret krävs.
        response = Client().post(self.call_page.landing_url, {"phone": "070-111 22 33"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Lead.objects.get(campaign=self.call_page).name, "")

    def test_the_honeypot_fakes_success_and_saves_nothing(self):
        data = self.quote_post(**{HONEYPOT: "http://spam.example"})
        response = Client().post(self.quote_page.landing_url, data)
        thanks = reverse("flamingo_public:thanks", args=[self.quote_page.page_slug])
        self.assertRedirects(response, thanks)
        self.assertFalse(Lead.objects.filter(campaign=self.quote_page).exists())
        self.assertFalse(SmsLog.objects.exists())

    def test_the_rate_limit_is_per_ip_and_campaign(self):
        client = Client()
        for _ in range(RATE_LIMIT):
            self.assertEqual(
                client.post(self.quote_page.landing_url, self.quote_post()).status_code, 302
            )
        response = client.post(self.quote_page.landing_url, self.quote_post())
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Ring Lindqvist Rör på", status_code=429)
        self.assertEqual(Lead.objects.filter(campaign=self.quote_page).count(), RATE_LIMIT)
        # En annan kampanj och en annan adress påverkas inte.
        self.assertEqual(
            client.post(self.call_page.landing_url, self.quote_post()).status_code, 302
        )
        other_ip = client.post(
            self.quote_page.landing_url, self.quote_post(), REMOTE_ADDR="10.1.2.3"
        )
        self.assertEqual(other_ip.status_code, 302)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(
            client.post(self.quote_page.landing_url, self.quote_post()).status_code, 403
        )
        html = client.get(self.quote_page.landing_url).content.decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html).group(1)
        data = self.quote_post(csrfmiddlewaretoken=token)
        self.assertEqual(client.post(self.quote_page.landing_url, data).status_code, 302)

    def test_sms_trouble_never_reaches_the_visitor(self):
        self.account.notify_sms = True
        self.account.notify_phone = "070-555 55 55"
        self.account.save()
        with (
            override_settings(**ELKS),
            mock.patch("apps.flamingo.sms.urlopen", side_effect=URLError("nere")),
        ):
            response = Client().post(self.quote_page.landing_url, self.quote_post())
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get(campaign=self.quote_page)
        owner = lead.sms_log.get(kind=SmsLog.KIND_OWNER)
        self.assertEqual(owner.status, SmsLog.STATUS_FAILED)
        with mock.patch("apps.flamingo.sms._notify_owner", side_effect=RuntimeError("bom")):
            response = Client().post(self.call_page.landing_url, {"phone": "070-111 22 33"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Lead.objects.filter(campaign=self.call_page).exists())
        self.assertEqual(mail.outbox, [])


# ---------------------------------------------------------------------------
# Sms
# ---------------------------------------------------------------------------


@override_settings(**NO_ELKS, SITE_BASE_URL="https://adx.se")
class SmsTests(InboxFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.account.notify_sms = True
        self.account.notify_phone = "070-555 55 55"
        self.account.autoreply_enabled = True
        self.account.autoreply_text = "Hej {namn}! Tack för din förfrågan. /Lindqvist Rör"
        self.account.save()
        self.lead = self.lead_for()

    def test_not_configured_logs_and_never_calls_out(self):
        with mock.patch("apps.flamingo.sms.urlopen") as urlopen:
            rows = sms.notify_new_lead(self.lead, now=_day(12))
        urlopen.assert_not_called()
        self.assertEqual(
            [(r.kind, r.status) for r in rows],
            [
                (SmsLog.KIND_OWNER, SmsLog.STATUS_NOT_CONFIGURED),
                (SmsLog.KIND_AUTOREPLY, SmsLog.STATUS_NOT_CONFIGURED),
            ],
        )
        owner = rows[0]
        self.assertIn("Anna Lind", owner.body)
        self.assertIn("070-111 22 33", owner.body)
        self.assertIn("Badrumsrenovering", owner.body)
        self.assertIn(f"https://adx.se/flamingo/app/inkorg/{self.lead.pk}/", owner.body)
        self.assertEqual(rows[1].body, "Hej Anna! Tack för din förfrågan. /Lindqvist Rör")
        self.assertEqual(mail.outbox, [])

    def test_settings_off_are_logged_as_disabled(self):
        self.account.notify_sms = False
        self.account.autoreply_enabled = False
        self.account.save()
        with mock.patch("apps.flamingo.sms.urlopen") as urlopen:
            rows = sms.notify_new_lead(self.lead, now=_day(12))
        urlopen.assert_not_called()
        self.assertEqual({r.status for r in rows}, {SmsLog.STATUS_DISABLED})
        self.assertEqual(rows[0].error, sms.NOTE_NOTIFY_OFF)
        self.assertEqual(rows[1].error, sms.NOTE_AUTOREPLY_OFF)

    @override_settings(**ELKS)
    def test_configured_sends_both_via_46elks(self):
        with mock.patch("apps.flamingo.sms.urlopen") as urlopen:
            _elks_ok(urlopen)
            rows = sms.notify_new_lead(self.lead, now=_day(12))
        self.assertEqual([r.status for r in rows], [SmsLog.STATUS_SENT, SmsLog.STATUS_SENT])
        self.assertEqual(rows[0].provider_id, "s0123")
        self.assertEqual(rows[0].to, "+46705555555")
        self.assertEqual(rows[1].to, "+46701112233")
        self.assertEqual(urlopen.call_count, 2)
        owner_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(owner_request.full_url, "https://api.46elks.com/a1/sms")
        self.assertEqual(owner_request.get_method(), "POST")
        self.assertEqual(urlopen.call_args_list[0].kwargs["timeout"], 10)
        expected = "Basic " + base64.b64encode(b"u-test:p-test").decode()
        self.assertEqual(owner_request.get_header("Authorization"), expected)
        owner_form = parse_qs(owner_request.data.decode())
        self.assertEqual(owner_form["from"], ["ADXFlamingo"])
        self.assertEqual(owner_form["to"], ["+46705555555"])
        self.assertIn("Ny förfrågan: Anna Lind", owner_form["message"][0])
        reply_form = parse_qs(urlopen.call_args_list[1].args[0].data.decode())
        self.assertEqual(reply_form["from"], ["Lindqvist"])
        self.assertEqual(reply_form["to"], ["+46701112233"])
        self.assertEqual(mail.outbox, [])

    @override_settings(**ELKS)
    def test_failures_are_logged_and_never_raised(self):
        for error, text in (
            (URLError("timeout"), "46elks: timeout"),
            (HTTPError(sms.ELKS_URL, 401, "Unauthorized", {}, None), "46elks: HTTP 401"),
            (ValueError("trasigt"), "ValueError"),
        ):
            with self.subTest(error=type(error).__name__):
                SmsLog.objects.all().delete()
                with mock.patch("apps.flamingo.sms.urlopen", side_effect=error):
                    rows = sms.notify_new_lead(self.lead, now=_day(12))
                self.assertEqual(len(rows), 2)
                self.assertEqual({r.status for r in rows}, {SmsLog.STATUS_FAILED})
                self.assertIn(text, rows[0].error)

    @override_settings(**ELKS)
    def test_quiet_hours_stop_the_autoreply_but_not_the_owner_notice(self):
        with mock.patch("apps.flamingo.sms.urlopen") as urlopen:
            _elks_ok(urlopen)
            rows = sms.notify_new_lead(self.lead, now=_day(22, 30))
        self.assertEqual(urlopen.call_count, 1)
        owner, reply = rows
        self.assertEqual(owner.status, SmsLog.STATUS_SENT)
        self.assertEqual(reply.status, SmsLog.STATUS_DISABLED)
        self.assertEqual(reply.error, sms.NOTE_QUIET)

    def test_quiet_hours_boundaries_are_swedish_time(self):
        self.assertTrue(sms.in_quiet_hours(_day(21)))
        self.assertTrue(sms.in_quiet_hours(_day(6, 59)))
        self.assertTrue(sms.in_quiet_hours(_day(0, 15)))
        self.assertFalse(sms.in_quiet_hours(_day(7)))
        self.assertFalse(sms.in_quiet_hours(_day(20, 59)))
        # 19:30 UTC är 21:30 i Stockholm i oktober (sommartid).
        self.assertTrue(sms.in_quiet_hours(datetime(2026, 10, 3, 19, 30, tzinfo=ZoneInfo("UTC"))))

    def test_no_autoreply_without_a_mobile_number(self):
        lead = self.lead_for(phone="08-123 456 78")
        rows = sms.notify_new_lead(lead, now=_day(12))
        self.assertEqual(rows[1].status, SmsLog.STATUS_DISABLED)
        self.assertEqual(rows[1].error, sms.NOTE_NO_MOBILE)

    def test_the_name_placeholder_disappears_without_a_name(self):
        lead = self.lead_for(name="")
        self.assertEqual(
            sms.autoreply_text(self.account, lead), "Hej! Tack för din förfrågan. /Lindqvist Rör"
        )

    def test_numbers_and_sender(self):
        self.assertEqual(sms.normalize_phone("070-123 45 67"), "+46701234567")
        self.assertEqual(sms.normalize_phone("+46 70 123 45 67"), "+46701234567")
        self.assertEqual(sms.normalize_phone("0046701234567"), "+46701234567")
        self.assertEqual(sms.normalize_phone("08-000 00 00"), "+4680000000")
        self.assertIsNone(sms.normalize_phone("123"))
        self.assertIsNone(sms.normalize_phone(""))
        self.assertTrue(sms.is_mobile("+46701234567"))
        self.assertFalse(sms.is_mobile("+4680000000"))
        self.assertEqual(sms.tel_href("08-000 00 00"), "tel:+4680000000")
        self.assertEqual(sms.sender_for(self.account), "Lindqvist")


# ---------------------------------------------------------------------------
# Inkorgen
# ---------------------------------------------------------------------------


class InboxTests(InboxFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)

    def detail(self, lead):
        return reverse("flamingo:app_lead", args=[lead.pk])

    def test_the_list_shows_own_leads_newest_first_without_junk(self):
        old = self.lead_for(name="Gammal Förfrågan")
        new = self.lead_for(name="Ny Förfrågan")
        junk = self.lead_for(name="Webbyrå Spam", status=Lead.STATUS_JUNK)
        response = self.client.get(reverse("flamingo:app_inbox"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "flamingo/app/inbox/list.html")
        html = response.content.decode()
        self.assertLess(html.index("Ny Förfrågan"), html.index("Gammal Förfrågan"))
        self.assertNotIn("Webbyrå Spam", html)
        self.assertNotIn("Hemlig Person", html)
        self.assertIn(self.detail(old), html)
        self.assertIn(self.detail(new), html)
        response = self.client.get(reverse("flamingo:app_inbox") + "?status=junk")
        self.assertContains(response, "Webbyrå Spam")
        self.assertNotContains(response, "Ny Förfrågan")
        self.assertContains(response, self.detail(junk))

    def test_another_customers_lead_is_404(self):
        url = self.detail(self.secret_lead)
        self.assertEqual(self.client.get(url).status_code, 404)
        response = self.client.post(url, {"status": Lead.STATUS_WON, "value_kr": "100"})
        self.assertEqual(response.status_code, 404)
        self.secret_lead.refresh_from_db()
        self.assertEqual(self.secret_lead.status, Lead.STATUS_NEW)
        # Kunden bo ser inte Annas förfrågningar heller.
        lead = self.lead_for()
        self.assertEqual(self.client_for(self.bo).get(self.detail(lead)).status_code, 404)

    def test_the_detail_shows_where_the_lead_came_from(self):
        lead = self.lead_for(
            gclid="Cj0abc",
            keyword="badrumsrenovering nacka",
            message="Vill byta allt inklusive golvbrunn.",
            answers={"Ungefär hur stort? (m2)": "6"},
        )
        response = self.client.get(self.detail(lead))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Var kom hen ifrån?")
        self.assertContains(response, "Google sök")
        self.assertContains(response, "Badrum Nacka")
        self.assertContains(response, '"badrumsrenovering nacka"')
        self.assertContains(response, "<dd>Ja</dd>", html=False)
        self.assertContains(response, 'href="tel:+46701112233"')
        self.assertContains(response, "Ring Anna")
        self.assertContains(response, "Vill byta allt inklusive golvbrunn.")
        self.assertContains(response, "Ungefär hur stort? (m2)")
        bare = self.lead_for(campaign=None, source=Lead.SOURCE_MANUAL)
        self.assertContains(self.client.get(self.detail(bare)), "<dd>Nej</dd>", html=False)

    def test_status_changes_and_the_value_rule(self):
        lead = self.lead_for(gclid="Cj0abc")
        url = self.detail(lead)
        response = self.client.post(url, {"status": Lead.STATUS_CONTACTED})
        self.assertRedirects(response, url)
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.STATUS_CONTACTED)

        # Vunnen utan belopp sparas inte.
        response = self.client.post(url, {"status": Lead.STATUS_WON, "value_kr": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Skriv affärens värde")
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.STATUS_CONTACTED)
        self.assertFalse(ConversionUpload.objects.filter(lead=lead).exists())
        response = self.client.post(url, {"status": Lead.STATUS_WON, "value_kr": "mycket"})
        self.assertContains(response, "hela kronor")

        # Vunnen med belopp köar konverteringen till Google.
        response = self.client.post(url, {"status": Lead.STATUS_WON, "value_kr": "186 000 kr"})
        self.assertRedirects(response, url)
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.value_kr), (Lead.STATUS_WON, 186000))
        upload = ConversionUpload.objects.get(lead=lead)
        self.assertEqual((upload.status, upload.value_kr), (ConversionUpload.STATUS_QUEUED, 186000))
        page = self.client.get(url)
        self.assertContains(page, "Till Google")

        # Nytt belopp följer med medan uppladdningen står i kö.
        self.client.post(url, {"status": Lead.STATUS_WON, "value_kr": "190000"})
        upload.refresh_from_db()
        self.assertEqual(upload.value_kr, 190000)

        # Bort från Vunnen: uppladdningen i kö tas bort.
        self.client.post(url, {"status": Lead.STATUS_LOST})
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.STATUS_LOST)
        self.assertFalse(ConversionUpload.objects.filter(lead=lead).exists())

    def test_an_exported_upload_is_left_alone(self):
        lead = self.lead_for(gclid="Cj0abc")
        leads.set_status(lead, Lead.STATUS_WON, "5000")
        ConversionUpload.objects.filter(lead=lead).update(status=ConversionUpload.STATUS_EXPORTED)
        self.client.post(self.detail(lead), {"status": Lead.STATUS_LOST})
        self.assertTrue(ConversionUpload.objects.filter(lead=lead).exists())

    def test_won_without_click_id_counts_but_is_not_uploaded(self):
        lead = self.lead_for()
        self.client.post(self.detail(lead), {"status": Lead.STATUS_WON, "value_kr": "4800"})
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.value_kr), (Lead.STATUS_WON, 4800))
        self.assertFalse(ConversionUpload.objects.filter(lead=lead).exists())
        self.assertContains(self.client.get(self.detail(lead)), "inget klick-id från Google")

    def test_set_status_rules(self):
        lead = self.lead_for()
        with self.assertRaises(ValueError):
            leads.set_status(lead, "vunnen")
        with self.assertRaises(ValueError):
            leads.set_status(lead, Lead.STATUS_WON, None)
        with self.assertRaises(ValueError):
            leads.set_status(lead, Lead.STATUS_WON, "0")
        with self.assertRaises(ValueError):
            leads.set_status(lead, Lead.STATUS_WON, "9" * 12)
        self.assertEqual(leads.parse_value_kr("186 000"), 186000)
        self.assertEqual(leads.parse_value_kr("186 000 kr"), 186000)
        self.assertEqual(leads.parse_value_kr("186.000"), 186000)
        self.assertEqual(leads.parse_value_kr("4 800,50"), 4800)
        self.assertIsNone(leads.parse_value_kr(""))
        # Ett annat status ignorerar ett ogiltigt belopp.
        leads.set_status(lead, Lead.STATUS_QUOTE, "skräp")
        self.assertEqual(lead.status, Lead.STATUS_QUOTE)

    def test_a_manual_lead_is_added_to_the_own_account(self):
        url = reverse("flamingo:app_inbox")
        response = self.client.post(
            url,
            {
                "name": "Per Lund",
                "phone": "072-111 22 33",
                "message": "Ringde om en ny beredare.",
                "service": str(self.badrum.pk),
            },
        )
        lead = Lead.objects.get(name="Per Lund")
        self.assertRedirects(response, self.detail(lead))
        self.assertEqual(lead.account, self.account)
        self.assertEqual(lead.source, Lead.SOURCE_MANUAL)
        self.assertEqual(lead.service, self.badrum)
        self.assertIsNone(lead.campaign)
        self.assertFalse(SmsLog.objects.filter(lead=lead).exists())
        # En annan kunds tjänst går inte att välja, och tjänsten slås alltid
        # upp via kontot.
        response = self.client.post(url, {"name": "Lisa", "service": str(self.secret_service.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Lead.objects.filter(name="Lisa").exists())
        lisa = leads.create_manual_lead(
            self.account, {"name": "Lisa", "service": str(self.secret_service.pk)}
        )
        self.assertIsNone(lisa.service)
        self.assertEqual(lisa.account, self.account)
        self.assertEqual(self.other_account.leads.count(), 1)
        # Utan namn och nummer: inget sparas.
        response = self.client.post(url, {"message": "Bara text"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Skriv ett namn eller ett telefonnummer.")
        self.assertFalse(Lead.objects.filter(message="Bara text").exists())
        self.assertEqual(mail.outbox, [])

    def test_staff_without_view_as_gets_the_customer_list(self):
        lead = self.lead_for()
        staff = self.client_for(self.staff)
        for url in (reverse("flamingo:app_inbox"), self.detail(lead)):
            with self.subTest(url=url):
                response = staff.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "flamingo/app/staff_index.html")
                self.assertContains(response, "Visa som kunden")

    def test_staff_viewing_as_reads_but_cannot_write(self):
        lead = self.lead_for()
        staff = self.client_for(self.staff, view_as=self.acme)
        response = staff.get(self.detail(lead))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "skrivskyddad")
        self.assertNotContains(response, ">Spara</button>")
        response = staff.post(self.detail(lead), {"status": Lead.STATUS_WON, "value_kr": "100"})
        self.assertEqual(response.status_code, 302)
        lead.refresh_from_db()
        self.assertEqual(lead.status, Lead.STATUS_NEW)
        staff.post(reverse("flamingo:app_inbox"), {"name": "Byråns test"})
        self.assertFalse(Lead.objects.filter(name="Byråns test").exists())
        list_html = staff.get(reverse("flamingo:app_inbox")).content.decode()
        self.assertNotIn('id="lagg-till"', list_html)

    def test_the_filter_chips_count_per_status(self):
        self.lead_for(status=Lead.STATUS_WON, value_kr=100)
        self.lead_for(status=Lead.STATUS_JUNK)
        self.lead_for()
        html = self.client.get(reverse("flamingo:app_inbox")).content.decode()
        self.assertRegex(html, r'Alla <span class="fl-inbox-filter__count">2</span>')
        self.assertRegex(html, r'Vunna <span class="fl-inbox-filter__count">1</span>')
        self.assertRegex(html, r'Skräp <span class="fl-inbox-filter__count">1</span>')
