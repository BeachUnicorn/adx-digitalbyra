"""
Länkarna och spåret (README E.1 till E.5, E.7, E.8, J S2 test_s2_links):
klicket på k.adx.se, bottar och skannrar, gränserna, ut på landningssidan,
förfrågningar och klick på numret via ett utskick, avregistreringen /s/
med Ångra, valen /p/, bekräftelsen /b/ och bekräftelse-sms:en, de externa
länkarnas regler och byråns godkännande, uppräkningen och retentionen.

Länkvärdarna testas med LINK_SETTINGS (k.adx.se) och HTTP_HOST. POST:arna
där går med Client(enforce_csrf_checks=True), HTTP_ORIGIN="null" och utan
Referer, och inget svar får sätta en kaka. Inget når nätet: sms går till
_FakeElks och länkkollen till en påhittad hämtning.
"""

import re
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core import mail, signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.exports import landing_page_url
from apps.flamingo.models import Campaign, ConversionUpload, FlamingoAccount, Lead
from apps.projects.models import Customer
from apps.sms import encoding
from apps.sms.models import SmsAccount, SmsMessage

from . import attribution, codes, links, optin, retention, tokens
from . import consent as consents
from .keys import value_hash
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    AllowedHost,
    Click,
    Consent,
    ConsentLog,
    Contact,
    Event,
    LinkCode,
    Recipient,
    Suppression,
    Switchboard,
    TrackedLink,
    Utskick,
)
from .sending import checks
from .test_s1_lp import LpFixture
from .test_s2_foundation import LINK_SETTINGS, _FakeElks
from .testing import PHONE_ANNA, PHONE_BO, make_contact

K = {"HTTP_HOST": "k.adx.se", "HTTP_ORIGIN": "null"}
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
ANNA_PHONE_TYPED = "070-174 06 01"
AGENCY = {"INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example"}
ELKS = {
    "SMS_SEND_LIVE": True,
    "ELKS_API_USERNAME": "test",
    "ELKS_API_PASSWORD": "test-losen",
    "SMS_PROVIDER": "46elks",
    "SMS_CALLBACK_BASE_URL": "",
}
FN_RE = re.compile(r'name="fn" value="([^"]+)"')


def aged_token(seconds_ago=10):
    return signing.dumps({"t": int(time.time()) - seconds_ago}, salt="botcheck")


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


class LinkFixture(LpFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kontakt = make_contact(
            cls.account, first_name="Anna", phone=PHONE_ANNA, email="anna@hemma.example"
        )
        consents.set_status(
            cls.kontakt, CHANNEL_SMS, consents.YES, source="manual", evidence="kassan, 2025"
        )
        consents.set_status(
            cls.kontakt, CHANNEL_EMAIL, consents.YES, source="manual", evidence="kassan, 2025"
        )
        now = timezone.now()
        cls.utskick = Utskick.objects.create(
            account=cls.account,
            name="Höstservice värmepump",
            sms_body="Hej {förnamn|du}, boka: {länk:boka}",
            status=Utskick.Status.SENT,
            finished_at=now,
        )
        cls.recipient = Recipient.objects.create(
            utskick=cls.utskick,
            contact=cls.kontakt,
            channel=CHANNEL_SMS,
            address=PHONE_ANNA,
            status=Recipient.Status.DELIVERED,
            sent_at=now,
        )
        cls.lp = TrackedLink.objects.create(
            account=cls.account,
            utskick=cls.utskick,
            kind=TrackedLink.Kind.LP,
            key="boka",
            campaign=cls.quote_page,
            destination=landing_page_url(cls.quote_page),
            label="Boka tid",
        )
        cls.ext = TrackedLink.objects.create(
            account=cls.account,
            utskick=cls.utskick,
            kind=TrackedLink.Kind.EXTERNAL,
            key="karta",
            destination="https://exempelror.example/karta?vy=1#hitta",
        )
        cls.third = TrackedLink.objects.create(
            account=cls.account,
            utskick=cls.utskick,
            kind=TrackedLink.Kind.EXTERNAL,
            key="tre",
            destination="https://exempelror.example/tre",
            add_utm=False,
        )
        cls.hash = value_hash(CHANNEL_SMS, PHONE_ANNA)
        for code, link in (("Lp0001", cls.lp), ("Ex0001", cls.ext), ("Tr0001", cls.third)):
            LinkCode.objects.create(
                code=code,
                kind=LinkCode.Kind.LINK,
                account=cls.account,
                value_hash=cls.hash,
                recipient=cls.recipient,
                link=link,
            )
        cls.person = LinkCode.objects.create(
            code="Pe0001",
            kind=LinkCode.Kind.PERSON,
            account=cls.account,
            value_hash=cls.hash,
            recipient=cls.recipient,
        )

    def k(self):
        return Client(enforce_csrf_checks=True)

    def assertNoCookies(self, response):
        self.assertEqual(response.cookies, {}, response.cookies)
        self.assertNotIn("Set-Cookie", response.headers)

    def click(self, code="Lp0001", agent=IPHONE, client=None, **extra):
        return (client or self.k()).get(f"/{code}", HTTP_USER_AGENT=agent, **K, **extra)

    def human_click(self):
        """Ett mänskligt klick på Flamingo-sidan: (klicket, ut)."""
        response = self.click()
        self.assertEqual(response.status_code, 302)
        ut = query(response["Location"])["ut"]
        return Click.objects.get(pk=tokens.read_ut(ut)), ut

    def form_nonce(self, path, client):
        page = client.get(path, **K)
        self.assertEqual(page.status_code, 200)
        return FN_RE.search(page.content.decode()).group(1)

    def sms_status(self, contact=None):
        return Consent.objects.get(contact=contact or self.kontakt, channel=CHANNEL_SMS).status


# ---------------------------------------------------------------------------
# Koderna och token
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class TokenTests(LinkFixture, TestCase):
    def test_ut_round_trip_and_shape(self):
        token = tokens.ut_token(123456)
        self.assertRegex(token, r"^[A-Za-z0-9]{1,12}\.[A-Za-z0-9]{10}$")
        self.assertEqual(tokens.read_ut(token), 123456)
        body, sig = token.split(".")
        flipped = sig[:-1] + ("x" if sig[-1] != "x" else "y")
        self.assertIsNone(tokens.read_ut(f"{body}.{flipped}"))
        self.assertIsNone(tokens.read_ut(f"{tokens.ut_token(7).split('.')[0]}.{sig}"))
        self.assertIsNone(tokens.read_ut("abc"))
        self.assertIsNone(tokens.read_ut(""))

    def test_form_nonce_is_bound_to_the_code_and_lasts_two_hours(self):
        now = timezone.now()
        nonce = tokens.form_nonce("Pe0001", now)
        self.assertTrue(tokens.read_form_nonce("Pe0001", nonce, now))
        self.assertFalse(tokens.read_form_nonce("Pe0002", nonce, now))
        self.assertTrue(tokens.read_form_nonce("Pe0001", nonce, now + timedelta(minutes=119)))
        self.assertFalse(tokens.read_form_nonce("Pe0001", nonce, now + timedelta(minutes=121)))
        self.assertFalse(tokens.read_form_nonce("Pe0001", "", now))

    def test_undo_nonce_lasts_thirty_minutes(self):
        now = timezone.now()
        nonce = tokens.undo_nonce(5, now)
        self.assertTrue(tokens.read_undo(5, nonce, now + timedelta(minutes=29)))
        self.assertFalse(tokens.read_undo(5, nonce, now + timedelta(minutes=31)))
        self.assertFalse(tokens.read_undo(6, nonce, now))

    def test_codes_are_gsm7_and_short_in_the_sms(self):
        batch = codes.new_codes(50)
        text = " ".join(links.sms_link(code) for code in batch)
        self.assertEqual(encoding.analyse(text).encoding, "gsm7")
        self.assertEqual(len(links.sms_link("Ab12Cd")), 15)
        self.assertEqual(links.sms_link("Ab12Cd", "b"), "k.adx.se/b/Ab12Cd")


# ---------------------------------------------------------------------------
# Klicket (E.3)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class ClickTests(LinkFixture, TestCase):
    def test_a_human_click_redirects_with_ut_and_counts(self):
        response = self.click()
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        self.assertEqual(urlsplit(location).path, self.quote_page.landing_url)
        params = query(location)
        self.assertEqual(params["utm_source"], "flamingo")
        self.assertEqual(params["utm_medium"], "sms")
        self.assertEqual(params["utm_campaign"], f"utskick-{self.utskick.pk}")
        click = Click.objects.get()
        self.assertEqual(tokens.read_ut(params["ut"]), click.pk)
        self.assertEqual((click.kind, click.channel, click.device), ("human", "sms", "mobile"))
        self.assertEqual((click.account_id, click.utskick_id), (self.account.pk, self.utskick.pk))
        self.assertEqual(click.contact_id, self.kontakt.pk)
        self.assertEqual(len(click.ip_hash), 64)
        self.assertNotIn("127.0.0.1", click.ip_hash)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 1)
        self.assertIsNotNone(self.recipient.first_clicked_at)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertNoCookies(response)

    def test_head_gives_the_bare_destination_and_logs_nothing(self):
        response = self.k().head("/Lp0001", HTTP_USER_AGENT=IPHONE, **K)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], landing_page_url(self.quote_page))
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertFalse(Click.objects.exists())
        self.recipient.refresh_from_db()
        self.assertEqual((self.recipient.click_count, self.recipient.bot_hits), (0, 0))

    def test_bots_and_previews_are_counted_not_stored(self):
        agents = [
            "",
            "facebookexternalhit/1.1 Facebot Twitterbot/1.0",
            "WhatsApp/2.23.20.0 A",
            "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
            "Mozilla/5.0 (compatible; Mimecast Link Scanner)",
            "python-requests/2.31.0",
        ]
        for agent in agents:
            with self.subTest(agent=agent):
                response = self.click(agent=agent)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("ut", query(response["Location"]))
                self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertFalse(Click.objects.exists())
        self.recipient.refresh_from_db()
        self.lp.refresh_from_db()
        self.assertEqual((self.recipient.bot_hits, self.lp.bot_hits), (len(agents), len(agents)))
        self.assertEqual(self.recipient.click_count, 0)

    def test_twenty_rows_per_hour_then_repeats(self):
        for _ in range(22):
            self.assertEqual(self.click().status_code, 302)
        self.assertEqual(Click.objects.count(), 20)
        last = Click.objects.order_by("-at", "-pk").first()
        self.assertEqual(last.repeat_count, 2)

    def test_three_links_within_two_seconds_is_a_scanner(self):
        self.click("Ex0001")
        self.click("Tr0001")
        self.click("Lp0001")
        kinds = dict(Click.objects.values_list("link__key", "kind"))
        self.assertEqual(kinds["boka"], "scanner")
        self.assertEqual((kinds["karta"], kinds["tre"]), ("human", "human"))
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 2)

    def test_external_link_keeps_query_and_fragment_and_adds_utm(self):
        location = self.click("Ex0001")["Location"]
        self.assertTrue(location.startswith("https://exempelror.example/karta?vy=1&utm_source="))
        self.assertTrue(location.endswith("#hitta"))
        self.assertNotIn("ut=", location)
        self.assertEqual(self.click("Tr0001")["Location"], "https://exempelror.example/tre")

    def test_a_refused_or_pending_host_is_checked_again_at_the_click(self):
        """Säkerhetsgranskningen S2: klicket skickade vidare till målet utan
        att pröva värden igen, också efter att ADX nekat den."""
        TrackedLink.objects.filter(pk=self.ext.pk).update(
            destination="https://phish-login.example/konto"
        )
        # Ny värd (ingen rad): som en gammal kod, inget räknas.
        response = self.click("Ex0001")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Länken har gått ut", status_code=404)
        host = AllowedHost.objects.create(
            account=self.account, host="phish-login.example", status=AllowedHost.Status.PENDING
        )
        self.assertEqual(self.click("Ex0001").status_code, 404)
        self.assertEqual(self.k().head("/Ex0001", **K).status_code, 404)
        AllowedHost.objects.filter(pk=host.pk).update(status=AllowedHost.Status.REFUSED)
        self.assertEqual(self.click("Ex0001").status_code, 404)
        AllowedHost.objects.filter(pk=host.pk).update(status=AllowedHost.Status.APPROVED)
        location = self.click("Ex0001")["Location"]
        self.assertEqual(location.split("?")[0], "https://phish-login.example/konto")

    def test_customer_utm_values_are_kept(self):
        link = TrackedLink(
            account=self.account,
            utskick=self.utskick,
            kind=TrackedLink.Kind.EXTERNAL,
            destination="https://exempelror.example/?utm_source=nyhetsbrev",
        )
        params = query(links.build_destination(link, self.recipient))
        self.assertEqual(params["utm_source"], "nyhetsbrev")
        self.assertEqual(params["utm_medium"], "sms")

    def test_misses_are_limited_and_then_everything_is_429(self):
        client = self.k()
        for i in range(20):
            response = client.get(f"/Zz{i:04d}", **K)
            self.assertEqual(response.status_code, 404)
            self.assertContains(response, "Länken har gått ut", status_code=404)
        self.assertEqual(client.get("/Zz9999", **K).status_code, 429)
        # En kod som finns svarar likadant: svaret avslöjar inte vilka som finns.
        self.assertEqual(self.click(client=client).status_code, 429)
        self.assertEqual(client.get("/s/Pe0001", **K).status_code, 429)
        self.assertFalse(Click.objects.exists())
        other = self.k()
        self.assertEqual(self.click(client=other, REMOTE_ADDR="10.9.9.9").status_code, 302)

    def test_codes_are_case_sensitive_and_kind_bound(self):
        self.assertEqual(self.click("lp0001").status_code, 404)
        self.assertEqual(self.click("Pe0001").status_code, 404)
        self.assertEqual(self.k().get("/s/Lp0001", **K).status_code, 404)

    def test_only_on_the_sms_host(self):
        # adx.se har ingen klickvy: sajtens vanliga svar (snedstrecket läggs till).
        self.assertIn(Client().get("/Lp0001", HTTP_USER_AGENT=IPHONE).status_code, (301, 404))
        self.assertEqual(
            Client().get("/Lp0001", HTTP_HOST="klick.adx.se", HTTP_USER_AGENT=IPHONE).status_code,
            404,
        )
        self.assertFalse(Click.objects.exists())
        self.assertEqual(self.k().post("/Lp0001", {}, **K).status_code, 404)

    def test_a_test_sms_code_redirects_without_counting(self):
        LinkCode.objects.create(
            code="Te0001",
            kind=LinkCode.Kind.LINK,
            account=self.account,
            value_hash=self.hash,
            link=self.lp,
        )
        response = self.click("Te0001")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("ut", query(response["Location"]))
        self.assertEqual(query(response["Location"])["utm_source"], "flamingo")
        self.assertFalse(Click.objects.exists())

    def test_a_disabled_account_still_redirects(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=False)
        self.assertEqual(self.click().status_code, 302)

    def test_html_pages_keep_same_origin(self):
        response = self.k().get("/s/Pe0001", **K)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["Cache-Control"], "private, no-store, max-age=0")
        self.assertNoCookies(response)


# ---------------------------------------------------------------------------
# Landningssidan (E.4)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class LandingTests(LinkFixture, TestCase):
    def lp_get(self, ut, client=None):
        return (client or Client()).get(f"{self.quote_page.landing_url}?ut={ut}")

    def lp_post(self, ut, phone=ANNA_PHONE_TYPED, client=None, **extra):
        data = {
            "name": "Anna",
            "phone": phone,
            "email": "",
            "q_storlek": "6",
            "ut": ut,
        }
        data.update(extra)
        return (client or Client()).post(self.quote_page.landing_url, data)

    def test_the_visit_is_logged_once_per_half_hour_and_the_page_carries_ut(self):
        click, ut = self.human_click()
        response = self.lp_get(ut)
        html = response.content.decode()
        self.assertIn(f'<input type="hidden" name="ut" value="{ut}">', html)
        beacon = reverse("flamingo_public:visit_beacon", args=[self.quote_page.page_slug])
        self.assertIn(f'data-fl-visit="{beacon}"', html)
        self.lp_get(ut)
        click.refresh_from_db()
        self.assertEqual(click.lp_visits, 2)
        self.assertIsNotNone(click.first_visit_at)
        events = Event.objects.filter(kind=Event.LP_VISIT, contact=self.kontakt)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.get().utskick_id, self.utskick.pk)
        self.assertEqual(sorted(set(response.cookies) - {"csrftoken"}), [])

    def test_a_token_from_another_account_is_ignored(self):
        foreign = Click.objects.create(account=self.other_account, channel="sms")
        ut = tokens.ut_token(foreign.pk)
        html = self.lp_get(ut).content.decode()
        self.assertNotIn('name="ut"', html)
        self.assertNotIn("data-fl-visit", html)
        foreign.refresh_from_db()
        self.assertEqual(foreign.lp_visits, 0)
        self.assertEqual(self.lp_post(ut).status_code, 302)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertIsNone(lead.utskick_id)
        self.assertEqual(lead.attribution, {})

    def test_a_tampered_token_is_ignored(self):
        click, ut = self.human_click()
        body, _sig = ut.split(".")
        html = self.lp_get(f"{body}.AAAAAAAAAA").content.decode()
        self.assertNotIn('name="ut"', html)
        click.refresh_from_db()
        self.assertEqual(click.lp_visits, 0)

    def test_staff_and_the_demo_are_not_logged(self):
        click, ut = self.human_click()
        staff = Client()
        staff.force_login(self.staff)
        self.assertNotIn("data-fl-visit", self.lp_get(ut, client=staff).content.decode())
        click.refresh_from_db()
        self.assertEqual(click.lp_visits, 0)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.lp_get(ut)
        click.refresh_from_db()
        self.assertEqual(click.lp_visits, 0)

    def test_a_form_lead_gets_the_utskick_and_never_goes_to_google(self):
        click, ut = self.human_click()
        response = self.lp_post(ut, gclid="Cj0abc")
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual(lead.utskick_id, self.utskick.pk)
        self.assertEqual(lead.utskick_recipient_id, self.recipient.pk)
        self.assertEqual(lead.contact_id, self.kontakt.pk)
        self.assertEqual(lead.attribution["click"], click.pk)
        self.assertEqual(lead.attribution["name"], "Höstservice värmepump")
        self.assertEqual(lead.attribution["label"], "Boka tid")
        self.assertEqual(lead.attribution["link"], self.lp.pk)
        self.assertTrue(lead.attribution["contact_matched"])
        self.assertFalse(lead.attribution["late"])
        self.assertNotIn("ut", lead.utm)
        self.assertEqual(lead.gclid, "Cj0abc")
        self.assertFalse(lead.can_send_to_google)
        self.assertFalse(ConversionUpload.objects.filter(lead=lead).exists())
        from apps.flamingo.app_views.inbox import channel

        self.assertEqual(channel(lead), "Utskick: Höstservice värmepump")

    def test_a_forwarded_link_creates_a_separate_contact(self):
        _click, ut = self.human_click()
        self.lp_post(ut, phone="070-174 06 77", name="Bo Ek")
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual(lead.utskick_id, self.utskick.pk)
        self.assertFalse(lead.attribution["contact_matched"])
        self.assertIsNotNone(lead.contact_id)
        self.assertNotEqual(lead.contact_id, self.kontakt.pk)
        self.kontakt.refresh_from_db()
        self.assertEqual(self.kontakt.phone, PHONE_ANNA)

    def test_no_contact_link_without_a_current_dpa(self):
        from .models import DpaVersion

        _click, ut = self.human_click()
        DpaVersion.objects.update(is_current=False)
        self.lp_post(ut)
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertEqual(lead.utskick_id, self.utskick.pk)
        self.assertIsNone(lead.contact_id)

    def test_a_late_click_is_marked(self):
        click, ut = self.human_click()
        Click.objects.filter(pk=click.pk).update(at=timezone.now() - timedelta(days=31))
        self.lp_post(ut)
        self.assertTrue(Lead.objects.get(campaign=self.quote_page).attribution["late"])

    def test_utskick_leads_get_raised_limits(self):
        _click, ut = self.human_click()
        now = timezone.now()
        Lead.objects.bulk_create(
            [
                Lead(account=self.account, campaign=self.quote_page, name=f"G{i}", created_at=now)
                for i in range(30)
            ]
        )
        self.assertEqual(self.lp_post("", phone="070-174 06 55").status_code, 429)
        self.assertEqual(self.lp_post(ut).status_code, 302)
        self.assertEqual(Lead.objects.filter(utskick=self.utskick).count(), 1)

    def test_at_most_three_leads_per_click_and_hour(self):
        _click, ut = self.human_click()
        for i in range(4):
            self.assertEqual(self.lp_post(ut, phone=f"070-174 06 1{i}").status_code, 302)
        self.assertEqual(Lead.objects.filter(utskick=self.utskick).count(), 3)
        self.assertEqual(Lead.objects.filter(campaign=self.quote_page).count(), 4)

    def test_call_click_with_ut_counts_on_a_paused_campaign(self):
        click, ut = self.human_click()
        Campaign.objects.filter(pk=self.quote_page.pk).update(status=Campaign.STATUS_PAUSED)
        ring = reverse("flamingo_public:call_click", args=[self.quote_page.page_slug])
        self.assertEqual(Client().post(ring, {}).status_code, 404)
        self.assertEqual(Client().post(ring, {"ut": ut}).status_code, 204)
        lead = Lead.objects.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual(lead.utskick_id, self.utskick.pk)
        self.assertEqual(lead.attribution["click"], click.pk)
        click.refresh_from_db()
        self.assertTrue(click.called)
        # Samma besökare och samma klick: en dubblett.
        Client().post(ring, {"ut": ut})
        self.assertEqual(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).count(), 1)

    def test_a_foreign_ut_does_not_open_a_paused_campaign(self):
        foreign = Click.objects.create(account=self.other_account, channel="sms")
        Campaign.objects.filter(pk=self.quote_page.pk).update(status=Campaign.STATUS_PAUSED)
        ring = reverse("flamingo_public:call_click", args=[self.quote_page.page_slug])
        response = Client().post(ring, {"ut": tokens.ut_token(foreign.pk)})
        self.assertEqual(response.status_code, 404)

    def test_the_visit_beacon(self):
        click, ut = self.human_click()
        url = reverse("flamingo_public:visit_beacon", args=[self.quote_page.page_slug])
        client = Client(enforce_csrf_checks=True)
        response = client.post(url, {"ut": ut, "s": "45"}, HTTP_ORIGIN="null")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertNoCookies(response)
        click.refresh_from_db()
        self.assertEqual(click.engaged_seconds, 45)
        # Högst en skrivning var tionde sekund.
        client.post(url, {"ut": ut, "s": "90"})
        click.refresh_from_db()
        self.assertEqual(click.engaged_seconds, 45)
        later = timezone.now() + timedelta(seconds=11)
        self.assertTrue(attribution.record_beacon(click, 99999, now=later))
        click.refresh_from_db()
        self.assertEqual(click.engaged_seconds, Click.MAX_ENGAGED_SECONDS)
        self.assertTrue(attribution.record_beacon(click, 10, now=later + timedelta(seconds=11)))
        click.refresh_from_db()
        self.assertEqual(click.engaged_seconds, Click.MAX_ENGAGED_SECONDS)

    def test_the_beacon_ignores_foreign_and_bad_tokens(self):
        foreign = Click.objects.create(account=self.other_account, channel="sms")
        url = reverse("flamingo_public:visit_beacon", args=[self.quote_page.page_slug])
        for ut in (tokens.ut_token(foreign.pk), "nej", ""):
            self.assertEqual(Client().post(url, {"ut": ut, "s": "30"}).status_code, 204)
        foreign.refresh_from_db()
        self.assertEqual(foreign.engaged_seconds, 0)
        self.assertEqual(Client().get(url).status_code, 405)

    def test_a_beacon_upgrades_a_scanner(self):
        self.click("Ex0001")
        self.click("Tr0001")
        self.click("Lp0001")
        scanner = Click.objects.get(link=self.lp)
        self.assertEqual(scanner.kind, "scanner")
        url = reverse("flamingo_public:visit_beacon", args=[self.quote_page.page_slug])
        Client().post(url, {"ut": tokens.ut_token(scanner.pk), "s": "12"})
        scanner.refresh_from_db()
        self.assertEqual(scanner.kind, "human")
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 3)

    def test_the_script_removes_ut_and_stores_nothing(self):
        script = (settings.BASE_DIR / "static" / "js" / "flamingo-lp.js").read_text()
        self.assertIn("replaceState", script)
        self.assertIn('"ut"', script)
        self.assertIn("data-fl-visit", script)
        self.assertIn("sendBeacon", script)
        for word in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
            self.assertNotIn(word, script)


# ---------------------------------------------------------------------------
# Avregistreringen (/s/) och Ångra (E.5)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class UnsubscribeTests(LinkFixture, TestCase):
    url = "/s/Pe0001"

    def unsubscribe(self, client):
        fn = self.form_nonce(self.url, client)
        return client.post(self.url, {"fn": fn, "action": "avregistrera"}, **K)

    def test_the_page_asks_and_get_changes_nothing(self):
        response = self.k().get(self.url, **K)
        self.assertContains(response, "Vill du sluta få sms från Exempelrör?")
        self.assertContains(response, "070-*** ** 01")
        self.assertContains(response, "Avregistrera mig")
        self.assertNotContains(response, "Anna")
        self.assertNotContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, "Så hanterar Exempelrör dina uppgifter")
        self.assertNoCookies(response)
        self.assertFalse(Suppression.objects.exists())

    def test_a_post_without_the_nonce_does_nothing(self):
        client = self.k()
        response = client.post(self.url, {"action": "avregistrera"}, **K)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Sidan hann bli för gammal", status_code=400)
        stale = tokens.form_nonce("Pe0001", timezone.now() - timedelta(hours=3))
        client.post(self.url, {"action": "avregistrera", "fn": stale}, **K)
        other = tokens.form_nonce("Pe0002")
        client.post(self.url, {"action": "avregistrera", "fn": other}, **K)
        self.assertFalse(Suppression.objects.exists())
        self.assertEqual(self.sms_status(), consents.YES)

    def test_unsubscribe_suppresses_and_offers_undo_and_email(self):
        client = self.k()
        response = self.unsubscribe(client)
        self.assertEqual(response.status_code, 200)
        self.assertNoCookies(response)
        self.assertContains(response, "Du får inga fler sms från Exempelrör.")
        self.assertContains(response, "Ångra")
        self.assertContains(response, "Vill du sluta få e-post från Exempelrör också?")
        self.assertContains(response, "a***@h***.example")
        row = Suppression.objects.get(channel=CHANNEL_SMS)
        self.assertEqual(
            (row.reason, row.utskick_id, row.value_hash), ("link", self.utskick.pk, self.hash)
        )
        self.assertEqual(self.sms_status(), consents.UNSUBSCRIBED)
        log = ConsentLog.objects.filter(contact=self.kontakt, channel=CHANNEL_SMS).first()
        self.assertEqual(
            (log.source, log.new_status, log.old_status), ("link", "unsubscribed", "yes")
        )
        self.assertNotIn("Pe0001", log.source_detail)
        self.recipient.refresh_from_db()
        self.assertIsNotNone(self.recipient.stopped_at)

    def test_undo_restores_once(self):
        client = self.k()
        html = self.unsubscribe(client).content.decode()
        sparr = re.search(r'name="sparr" value="(\d+)"', html).group(1)
        un = re.search(r'name="un" value="([^"]+)"', html).group(1)
        fn = FN_RE.search(html).group(1)
        data = {"fn": fn, "action": "angra", "sparr": sparr, "un": un}
        response = client.post(self.url, data, **K)
        self.assertContains(response, "Du får sms från Exempelrör igen")
        self.assertFalse(Suppression.objects.filter(channel=CHANNEL_SMS).exists())
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_SMS)
        self.assertEqual((consent.status, consent.evidence), (consents.YES, "kassan, 2025"))
        log = ConsentLog.objects.filter(contact=self.kontakt, channel=CHANNEL_SMS).first()
        self.assertEqual((log.new_status, log.source), ("yes", "link"))
        self.assertIn("Ångra", log.source_detail)
        self.recipient.refresh_from_db()
        self.assertIsNone(self.recipient.stopped_at)
        # En gång: samma värde igen gör ingenting.
        self.unsubscribe(client)
        again = client.post(self.url, data, **K)
        self.assertContains(again, "Det går inte att ångra längre.")
        self.assertTrue(Suppression.objects.filter(channel=CHANNEL_SMS).exists())

    def test_undo_expires_after_thirty_minutes(self):
        client = self.k()
        self.unsubscribe(client)
        row = Suppression.objects.get(channel=CHANNEL_SMS)
        old = tokens.undo_nonce(row.pk, timezone.now() - timedelta(minutes=31))
        fn = tokens.form_nonce("Pe0001")
        response = client.post(
            self.url, {"fn": fn, "action": "angra", "sparr": row.pk, "un": old}, **K
        )
        self.assertContains(response, "Det går inte att ångra längre.")
        self.assertTrue(Suppression.objects.filter(pk=row.pk).exists())

    def test_no_undo_for_a_stop(self):
        consents.set_status(self.kontakt, CHANNEL_SMS, consents.UNSUBSCRIBED, source="stop")
        response = self.unsubscribe(self.k())
        self.assertContains(response, "Du får inga fler sms från Exempelrör.")
        self.assertNotContains(response, 'value="angra"')
        self.assertEqual(Suppression.objects.get(channel=CHANNEL_SMS).reason, "stop")

    def test_the_email_too(self):
        client = self.k()
        html = self.unsubscribe(client).content.decode()
        fn = FN_RE.search(html).group(1)
        response = client.post(self.url, {"fn": fn, "action": "epost"}, **K)
        self.assertContains(response, "Du får ingen e-post från Exempelrör heller.")
        self.assertEqual(Suppression.objects.get(channel=CHANNEL_EMAIL).reason, "link")
        self.assertEqual(
            Consent.objects.get(contact=self.kontakt, channel=CHANNEL_EMAIL).status,
            consents.UNSUBSCRIBED,
        )

    def test_works_for_a_disabled_account_and_a_deleted_contact(self):
        from . import contacts

        self.settings.is_enabled = False
        self.settings.save(update_fields=["is_enabled"])
        contacts.delete_contact(self.kontakt, suppress=False)
        response = self.unsubscribe(self.k())
        self.assertContains(response, "Du får inga fler sms från Exempelrör.")
        row = Suppression.objects.get(channel=CHANNEL_SMS)
        self.assertEqual(row.value_hash, self.hash)
        log = ConsentLog.objects.filter(value_hash=self.hash, contact__isnull=True).first()
        self.assertEqual((log.new_status, log.source), ("unsubscribed", "link"))

    def test_unknown_code(self):
        response = self.k().get("/s/Xx0000", **K)
        self.assertContains(response, "Länken har gått ut", status_code=404)


# ---------------------------------------------------------------------------
# Dina val (/p/)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class PreferenceTests(LinkFixture, TestCase):
    url = "/p/Pe0001"

    def save(self, client, **data):
        fn = self.form_nonce(self.url, client)
        token = aged_token()
        payload = {
            "fn": fn,
            "action": "spara",
            "bc_website": "",
            "bc_time": token,
            "bc_proof": token,
        }
        payload.update(data)
        return client.post(self.url, payload, **K)

    def sms_on(self):
        Switchboard.get_solo()
        Switchboard.objects.update(sms_enabled=True, links_ready_at=timezone.now())

    def test_the_page_shows_masked_rows(self):
        response = self.k().get(self.url, **K)
        self.assertContains(response, "Vad vill du få från Exempelrör?")
        self.assertContains(response, "070-*** ** 01")
        self.assertContains(response, "Sms med erbjudanden")
        self.assertContains(response, "E-post med erbjudanden")
        self.assertContains(response, "Information om dina bokningar skickas")
        self.assertContains(response, "Avregistrera mig från allt")
        self.assertNotContains(response, "Anna")
        self.assertNotContains(response, "csrfmiddlewaretoken")
        self.assertNoCookies(response)

    def test_turning_off_sets_declined(self):
        client = self.k()
        response = self.save(client, email="1")
        self.assertEqual(response.status_code, 302)
        self.assertNoCookies(response)
        self.assertEqual(self.sms_status(), consents.DECLINED)
        self.assertFalse(Suppression.objects.exists())
        page = client.get(response["Location"], **K)
        self.assertContains(page, "Dina val är sparade.")

    def test_turning_sms_on_sends_a_confirmation(self):
        self.sms_on()
        client = self.k()
        self.save(client, email="1")
        response = self.save(client, sms="1", email="1")
        self.assertIn("klart=sms", response["Location"])
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_SMS)
        self.assertEqual((consent.status, consent.source), (consents.PENDING, "preference"))
        self.assertIsNone(consent.confirm_sent_at)
        self.assertTrue(optin.sms_queued().filter(pk=consent.pk).exists())
        page = client.get(response["Location"], **K)
        self.assertContains(page, "Du får ett sms till 070-*** ** 01 med en länk.")

    def test_sms_is_not_offered_before_the_switch(self):
        client = self.k()
        self.save(client, email="1")
        self.save(client, sms="1", email="1")
        self.assertEqual(self.sms_status(), consents.DECLINED)

    def test_turning_on_needs_the_botcheck(self):
        self.sms_on()
        client = self.k()
        self.save(client, email="1")
        self.save(client, sms="1", email="1", bc_proof="")
        self.assertEqual(self.sms_status(), consents.DECLINED)

    def test_unsubscribe_from_everything(self):
        client = self.k()
        fn = self.form_nonce(self.url, client)
        response = client.post(self.url, {"fn": fn, "action": "allt"}, **K)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(Suppression.objects.values_list("channel", "reason")),
            {("sms", "preference"), ("email", "preference")},
        )
        page = client.get(response["Location"], **K)
        self.assertContains(page, "Du är avregistrerad.")
        self.assertNotContains(page, "Information om dina bokningar")

    def test_sign_up_email_without_an_address(self):
        from . import contacts

        contacts.change_address(self.kontakt, CHANNEL_EMAIL, "")
        Switchboard.get_solo()
        Switchboard.objects.update(doi_ready_at=timezone.now())
        client = self.k()
        page = client.get(self.url, **K)
        self.assertContains(page, 'name="epost"')
        response = self.save(client, sms="1", epost="ny@hemma.example")
        self.assertIn("klart=mejl", response["Location"])
        self.kontakt.refresh_from_db()
        self.assertEqual(self.kontakt.email, "ny@hemma.example")
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, consents.PENDING)

    def test_without_the_nonce_nothing_changes(self):
        response = self.k().post(self.url, {"action": "allt"}, **K)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Suppression.objects.exists())


# ---------------------------------------------------------------------------
# Bekräftelse-sms:en och /b/
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, **ELKS)
class ConfirmTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.fake = _FakeElks()
        patcher = mock.patch("apps.sms.elks._post", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sms_account = SmsAccount.objects.create(
            customer=self.customer, is_enabled=True, sender_name="Exempelror"
        )
        Switchboard.get_solo()
        Switchboard.objects.update(sms_enabled=True, links_ready_at=timezone.now())

    def pending(self, contact=None):
        consents.set_status(
            contact or self.kontakt, CHANNEL_SMS, consents.DECLINED, source="preference"
        )
        consents.set_status(
            contact or self.kontakt,
            CHANNEL_SMS,
            consents.PENDING,
            source="preference",
            text_shown="Ja, jag vill få erbjudanden från Exempelrör via sms.",
        )

    def code_from_sms(self):
        body = self.fake.sends[-1]["message"]
        return re.search(r"k\.adx\.se/b/([A-Za-z0-9]{6})", body).group(1)

    def test_the_tick_sends_the_confirmation_from_the_reply_number(self):
        self.pending()
        self.assertTrue(optin.sms_work_exists())
        summary = optin.send_due_sms()
        self.assertEqual(summary["sent"], 1)
        sent = self.fake.sends[0]
        self.assertEqual(sent["from"], settings.UTSKICK_REPLY_NUMBER)
        self.assertEqual(sent["to"], PHONE_ANNA)
        self.assertIn("Klicka för att få sms från Exempelrör: k.adx.se/b/", sent["message"])
        self.assertEqual(encoding.analyse(sent["message"]).encoding, "gsm7")
        message = SmsMessage.objects.get()
        self.assertEqual(message.source, "system")
        code = LinkCode.objects.get(kind=LinkCode.Kind.CONFIRM)
        self.assertEqual((code.purpose, code.contact_id), ("pref_on", self.kontakt.pk))
        self.assertEqual(message.reference, f"~b{code.pk}")
        self.assertFalse(optin.sms_work_exists())
        # En per adress och dygn.
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_SMS)
        self.assertFalse(optin.requeue_sms(consent))

    def test_nothing_is_sent_without_the_switch_or_at_the_cap(self):
        self.pending()
        Switchboard.objects.update(sms_enabled=False)
        self.assertEqual(optin.send_due_sms()["sent"], 0)
        Switchboard.objects.update(
            sms_enabled=True, sms_paused_until=timezone.now() + timedelta(minutes=5)
        )
        self.assertEqual(optin.send_due_sms()["sent"], 0)
        Switchboard.objects.update(sms_paused_until=None)
        with mock.patch.object(optin, "_cap_reached", return_value=True):
            self.assertEqual(optin.send_due_sms()["skipped"], 1)
        self.assertEqual(self.fake.sends, [])

    def test_the_demo_never_sends(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.pending()
        self.assertEqual(optin.send_due_sms(), {"sent": 0, "skipped": 0, "failed": 0, "waiting": 0})
        self.assertEqual(self.fake.calls, [])

    def test_the_link_confirms_with_a_button(self):
        self.pending()
        optin.send_due_sms()
        code = self.code_from_sms()
        client = self.k()
        url = f"/b/{code}"
        page = client.get(url, **K)
        self.assertContains(page, "Bekräfta att du vill få sms från Exempelrör.")
        self.assertContains(page, "070-*** ** 01")
        self.assertNoCookies(page)
        self.assertEqual(self.sms_status(), consents.PENDING)
        fn = FN_RE.search(page.content.decode()).group(1)
        response = client.post(url, {"fn": fn}, **K)
        self.assertEqual(response.status_code, 302)
        self.assertNoCookies(response)
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_SMS)
        self.assertEqual((consent.status, consent.source), (consents.YES, "confirm"))
        self.assertIsNotNone(consent.confirmed_at)
        self.assertEqual(consent.text_shown, "Ja, jag vill få erbjudanden från Exempelrör via sms.")
        self.assertIsNotNone(LinkCode.objects.get(code=code).used_at)
        self.assertContains(client.get(url, **K), "Klart")

    def test_start_lifts_the_suppression(self):
        consents.set_status(self.kontakt, CHANNEL_SMS, consents.UNSUBSCRIBED, source="stop")
        row = codes.create_confirm(
            self.account, value_hash=self.hash, purpose="start", contact=self.kontakt
        )
        client = self.k()
        url = f"/b/{row.code}"
        page = client.get(url, **K)
        self.assertContains(page, "Bekräfta att du vill få sms från Exempelrör igen.")
        fn = FN_RE.search(page.content.decode()).group(1)
        client.post(url, {"fn": fn}, **K)
        consent = Consent.objects.get(contact=self.kontakt, channel=CHANNEL_SMS)
        self.assertEqual((consent.status, consent.source), (consents.YES, "start"))
        self.assertFalse(Suppression.objects.filter(channel=CHANNEL_SMS).exists())

    def test_an_expired_or_changed_code(self):
        row = codes.create_confirm(
            self.account,
            value_hash=self.hash,
            purpose="pref_on",
            contact=self.kontakt,
            now=timezone.now() - timedelta(hours=25),
        )
        response = self.k().get(f"/b/{row.code}", **K)
        self.assertContains(response, "Länken har gått ut", status_code=410)
        fresh = codes.create_confirm(
            self.account, value_hash=value_hash(CHANNEL_SMS, PHONE_BO), purpose="pref_on"
        )
        response = self.k().get(f"/b/{fresh.code}", **K)
        self.assertContains(response, "Länken gäller inte längre", status_code=410)

    def test_a_post_without_the_nonce_does_nothing(self):
        row = codes.create_confirm(
            self.account, value_hash=self.hash, purpose="pref_on", contact=self.kontakt
        )
        self.pending()
        response = self.k().post(f"/b/{row.code}", {}, **K)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.sms_status(), consents.PENDING)


@override_settings(**LINK_SETTINGS)
class MinaUtskickSmsTests(LinkFixture, TestCase):
    """S1:s Mina utskick (adx.se/utskick/val/<token>/) kan slå på sms i S2."""

    def test_sms_can_be_turned_on_with_a_confirmation(self):
        Switchboard.get_solo()
        Switchboard.objects.update(sms_enabled=True, links_ready_at=timezone.now())
        consents.set_status(self.kontakt, CHANNEL_SMS, consents.DECLINED, source="preference")
        url = reverse(
            "utskick_public:preferences",
            args=[tokens.preference_token(self.account.pk, CHANNEL_SMS, self.hash)],
        )
        client = Client()
        self.assertContains(client.get(url), 'name="sms"')
        token = aged_token()
        response = client.post(
            url,
            {
                "action": "spara",
                "sms": "1",
                "email": "1",
                "bc_website": "",
                "bc_time": token,
                "bc_proof": token,
            },
        )
        self.assertIn("klart=sms", response["Location"])
        self.assertEqual(self.sms_status(), consents.PENDING)
        self.assertContains(client.get(response["Location"]), "Du får ett sms till 070-*** ** 01")


# ---------------------------------------------------------------------------
# Externa länkar och byråns godkännande (E.8)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, **AGENCY)
class ExternalLinkTests(LinkFixture, TestCase):
    """Kundens webbplats är https://exempelror.example i ADX kundregister
    (UtskickFixture)."""

    def setUp(self):
        super().setUp()
        self.draft = Utskick.objects.create(
            account=self.account, name="Vinter", created_by=self.anna
        )

    def set_website(self, customer_site="", flamingo_site=""):
        Customer.objects.filter(pk=self.customer.pk).update(website=customer_site)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(website_url=flamingo_site)
        self.account = FlamingoAccount.objects.select_related("customer").get(pk=self.account.pk)
        self.draft.account = self.account

    def test_the_address_the_customer_typed_frees_no_host(self):
        """Säkerhetsgranskningen S2: FlamingoAccount.website_url sparas innan
        något läses och utan ägarkontroll, så den gjorde vilken värd som helst
        fri från granskning (och "egen" för information)."""
        self.set_website(flamingo_site="https://phish-login.example/")
        self.assertEqual(links.own_site(self.account), "")
        with self.assertRaises(links.HostPending):
            links.clean_external(self.account, "https://konto.phish-login.example/logga-in")
        self.assertEqual(links.host_status(self.account, "phish-login.example"), "new")
        self.assertEqual(checks.own_hosts(self.account), set())
        # Byråns adress i kundregistret räknas, med underdomäner, utan www.
        self.set_website(customer_site="https://www.exempelror.example/start")
        self.assertEqual(links.own_site(self.account), "exempelror.example")
        self.assertEqual(
            links.clean_external(self.account, "https://boka.exempelror.example/"),
            "https://boka.exempelror.example/",
        )

    def test_a_shared_host_is_never_the_customers_site(self):
        for site in (
            "https://github.io",
            "https://co.uk",
            "https://org.se",
            "https://sites.google.com/view/exempelror",
            "https://www.facebook.com/exempelror",
            "https://m.facebook.com/exempelror",
            "https://linktr.ee/exempelror",
            "https://192.168.1.10/",
            "localhost",
        ):
            with self.subTest(site=site):
                self.set_website(customer_site=site)
                self.assertEqual(links.own_site(self.account), "")
        self.set_website(customer_site="https://exempelror.github.io/")
        self.assertEqual(links.own_site(self.account), "exempelror.github.io")
        with self.assertRaises(links.HostPending):
            links.clean_external(self.account, "https://evil.github.io/")
        self.set_website(customer_site="exempelror.co.uk")
        self.assertEqual(links.own_site(self.account), "exempelror.co.uk")

    def test_redirects_on_the_free_hosts_are_refused(self):
        for url in (
            "https://www.google.com/maps/../url?q=https://evil.example",
            "https://www.google.com/maps/%2e%2e/url?q=https://evil.example",
            "https://www.google.com/url?q=x",
            "https://l.facebook.com/l.php?u=https%3A%2F%2Fevil.example",
            "https://lm.facebook.com/l.php?u=x",
            "https://www.facebook.com/l.php?u=x",
            "https://www.youtube.com/redirect?q=https://evil.example",
            "https://www.linkedin.com/redir/redirect?url=x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(links.LinkRefused) as caught:
                    links.clean_external(self.account, url)
                self.assertEqual(str(caught.exception), links.REDIRECT_TEXT)
        # Punktleden löses upp och adressen sparas som webbläsaren öppnar den.
        self.assertEqual(
            links.clean_external(self.account, "https://www.google.com/maps/x/../place/y"),
            "https://www.google.com/maps/place/y",
        )
        self.assertEqual(links.resolve_dots("/a/./b/../c"), "/a/c")
        self.assertEqual(links.resolve_dots("/maps/.."), "/")
        self.assertFalse(AllowedHost.objects.exists())

    def test_the_rules(self):
        refused = {
            "/kontakt/": links.ABSOLUTE_TEXT,
            "#boka": links.ABSOLUTE_TEXT,
            "ftp://exempelror.example/": links.ABSOLUTE_TEXT,
            "javascript:alert(1)": links.ABSOLUTE_TEXT,
            "http://192.168.1.10/": links.IP_TEXT,
            "http://[::1]/": links.IP_TEXT,
            "http://127.1/": links.IP_TEXT,
            "https://exempelror.example:8080/": links.PORT_TEXT,
            "https://anna:hemligt@exempelror.example/": links.USERINFO_TEXT,
            "https://bit.ly/abc": links.SHORTENER_TEXT,
            "https://k.adx.se/Ab12Cd": links.LINK_HOST_TEXT,
            "https://exempelror.example/" + "a" * 600: links.TOO_LONG_TEXT,
        }
        for url, text in refused.items():
            with self.subTest(url=url):
                with self.assertRaises(links.LinkRefused) as caught:
                    links.clean_external(self.account, url)
                self.assertEqual(str(caught.exception), text)

    def test_allowed_without_review(self):
        cases = {
            "https://exempelror.example/boka?gclid=X1&vy=2": "https://exempelror.example/boka?vy=2",
            "https://www.exempelror.example/": "https://www.exempelror.example/",
            "https://boka.exempelror.example:443/a": "https://boka.exempelror.example/a",
            "https://www.google.com/maps/place/x": "https://www.google.com/maps/place/x",
            "https://maps.app.goo.gl/abc": "https://maps.app.goo.gl/abc",
            "https://www.instagram.com/exempelror": "https://www.instagram.com/exempelror",
            "https://reco.se/exempelror": "https://reco.se/exempelror",
        }
        for url, cleaned in cases.items():
            with self.subTest(url=url):
                self.assertEqual(links.clean_external(self.account, url), cleaned)
        with self.assertRaises(links.HostPending):
            links.clean_external(self.account, "https://www.google.com/sok?q=x")
        self.assertFalse(AllowedHost.objects.exists())

    def test_a_new_host_waits_for_the_agency(self):
        with self.assertRaises(links.HostPending) as caught:
            links.clean_external(self.account, "https://grannen.example/erbjudande")
        self.assertEqual(
            (caught.exception.host, caught.exception.status), ("grannen.example", "new")
        )
        link = links.add_link(
            self.draft, key="erbjudande", destination="https://grannen.example/erbjudande"
        )
        self.assertEqual(link.kind, "external")
        host = AllowedHost.objects.get()
        self.assertEqual(
            (host.host, host.status, host.requested_by), ("grannen.example", "pending", self.anna)
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("grannen.example", mail.outbox[0].body)
        self.assertNotIn("anna@exempelror.example", mail.outbox[0].to)
        self.assertEqual(links.link_problems(self.draft), [links.PENDING_TEXT])
        # Samma värd igen: ingen ny rad och inget nytt larm.
        links.add_link(self.draft, key="tva", destination="https://grannen.example/tva")
        self.assertEqual((AllowedHost.objects.count(), len(mail.outbox)), (1, 1))

        staff = Client()
        staff.force_login(self.staff)
        url = reverse("manage:utskick_host_decide", args=[host.pk])
        self.assertEqual(staff.post(url, {"action": "refuse"}).status_code, 302)
        host.refresh_from_db()
        self.assertEqual(host.status, "pending")
        staff.post(url, {"action": "refuse", "note": "Okänd sajt"})
        host.refresh_from_db()
        self.assertEqual((host.status, host.decided_by), ("refused", self.staff))
        self.assertEqual(
            links.link_problems(self.draft), ["ADX har inte godkänt länkar till grannen.example."]
        )
        staff.post(url, {"action": "approve"})
        self.assertEqual(links.link_problems(self.draft), [])
        self.assertEqual(
            links.clean_external(self.account, "https://grannen.example/x"),
            "https://grannen.example/x",
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_the_hosts_show_for_the_agency(self):
        host = links.request_host(self.account, "grannen.example", self.anna)
        decide = reverse("manage:utskick_host_decide", args=[host.pk])
        staff = Client()
        staff.force_login(self.staff)
        overview = staff.get(reverse("manage:utskick_overview"))
        self.assertContains(overview, "grannen.example")
        self.assertContains(overview, decide)
        card = staff.get(reverse("manage:customer_detail", args=[self.customer.pk]))
        self.assertContains(card, "grannen.example")
        self.assertContains(card, decide)
        # Kunden (och den som inte är inloggad) kan inte besluta.
        customer = Client()
        customer.force_login(self.anna)
        customer.post(decide, {"action": "approve"})
        Client().post(decide, {"action": "approve"})
        host.refresh_from_db()
        self.assertEqual(host.status, "pending")

    def test_lp_links_are_the_accounts_own_pages(self):
        link = links.add_link(self.draft, key="boka", campaign=self.quote_page, label="Boka")
        self.assertEqual((link.kind, link.destination), ("lp", landing_page_url(self.quote_page)))
        from apps.flamingo.models import Service

        service = Service.objects.create(account=self.other_account, name="Annat")
        foreign = Campaign.objects.create(
            account=self.other_account, service=service, name="Annan", page={"title": "Annan"}
        )
        with self.assertRaises(links.LinkRefused):
            links.add_link(self.draft, key="annan", campaign=foreign)
        with self.assertRaises(links.LinkRefused):
            links.add_link(self.draft, key="Fel Namn!", campaign=self.quote_page)
        with self.assertRaises(links.LinkRefused):
            links.add_link(self.utskick, key="ny", campaign=self.quote_page)

    def test_check_destinations(self):
        def fetch(url, **kwargs):
            if "trasig" in url:
                raise RuntimeError("svarar inte")
            return object()

        with mock.patch("apps.tools.analyzer.fetch", side_effect=fetch) as fetched:
            result = links.check_destinations(
                self.account, ["https://exempelror.example/", "https://trasig.example/", "/x"]
            )
            self.assertEqual(
                result, {"https://exempelror.example/": True, "https://trasig.example/": False}
            )
            self.assertEqual(fetched.call_count, 2)
            # Svaret sparas en stund.
            links.check_destinations(self.account, ["https://exempelror.example/"])
            self.assertEqual(fetched.call_count, 2)
            demo = FlamingoAccount(is_demo=True)
            self.assertEqual(
                links.check_destinations(demo, ["https://x.example/"]), {"https://x.example/": True}
            )
            self.assertEqual(fetched.call_count, 2)


# ---------------------------------------------------------------------------
# Uppräkningen och retentionen (E.3, E.7)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class RollupAndRetentionTests(LinkFixture, TestCase):
    def test_rollup_counts_humans_and_leads_and_never_goes_down(self):
        click, _ut = self.human_click()
        Click.objects.create(account=self.account, link=self.lp, channel="sms", kind="scanner")
        Lead.objects.create(
            account=self.account,
            campaign=self.quote_page,
            utskick=self.utskick,
            attribution={"click": click.pk, "link": self.lp.pk},
        )
        self.assertEqual(links.rollup(timezone.now()), {"links": 1})
        self.lp.refresh_from_db()
        self.assertEqual((self.lp.human_clicks, self.lp.leads), (1, 1))
        Click.objects.all().delete()
        links.rollup(timezone.now())
        self.lp.refresh_from_db()
        self.assertEqual(self.lp.human_clicks, 1)

    def test_purge(self):
        now = timezone.now()
        old_scanner = Click.objects.create(
            account=self.account, channel="sms", kind="scanner", at=now - timedelta(days=15)
        )
        young_human = Click.objects.create(
            account=self.account, channel="sms", kind="human", at=now - timedelta(days=15)
        )
        old_human = Click.objects.create(
            account=self.account, channel="sms", kind="human", at=now - timedelta(days=400)
        )
        confirm_old = codes.create_confirm(
            self.account, value_hash="h", purpose="pref_on", now=now - timedelta(days=8)
        )
        confirm_new = codes.create_confirm(self.account, value_hash="h", purpose="pref_on")
        LinkCode.objects.filter(pk=self.person.pk).update(created_at=now - timedelta(days=400))
        result = retention.purge_s2(now)
        self.assertFalse(Click.objects.filter(pk__in=[old_scanner.pk, old_human.pk]).exists())
        self.assertTrue(Click.objects.filter(pk=young_human.pk).exists())
        self.assertFalse(LinkCode.objects.filter(pk=confirm_old.pk).exists())
        self.assertTrue(LinkCode.objects.filter(pk=confirm_new.pk).exists())
        self.person.refresh_from_db()
        self.assertIsNone(self.person.recipient_id)
        self.assertTrue(LinkCode.objects.filter(code="Lp0001").exists())
        Utskick.objects.filter(pk=self.utskick.pk).update(finished_at=now - timedelta(days=400))
        retention.purge_s2(now)
        self.assertFalse(LinkCode.objects.filter(kind="link").exists())
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# Vakter för mallarna på länkvärdarna
# ---------------------------------------------------------------------------


class LinkTemplateGuardTests(TestCase):
    folder = Path(settings.BASE_DIR) / "templates" / "utskick" / "links"
    COMMENT = re.compile(r"\{% comment %\}.*?\{% endcomment %\}", re.S)
    TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)

    def templates(self):
        files = sorted(self.folder.glob("*.html"))
        self.assertTrue(files)
        return [(f.name, self.COMMENT.sub("", f.read_text())) for f in files]

    def test_no_csrf_tag_no_style_no_inline_script(self):
        for name, text in self.templates():
            with self.subTest(name=name):
                self.assertNotIn("csrf_token", text)
                self.assertNotIn("style=", text)
                self.assertNotRegex(text, r"<script(?![^>]*\bsrc=)")

    def test_copy_rules(self):
        forbidden = [chr(c) for c in (0x2013, 0x2014, 0x201C, 0x201D, 0x2026)]
        for name, text in self.templates():
            visible = self.TAG.sub("", text)
            with self.subTest(name=name):
                self.assertNotIn("!", visible)
                for ch in forbidden:
                    self.assertNotIn(ch, text)


# ---------------------------------------------------------------------------
# Anmälningssidan med sms (K.2.5, L Security 13)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, **ELKS)
class SignupSmsTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        from .models import ContactList, SignupForm

        self.fake = _FakeElks()
        patcher = mock.patch("apps.sms.elks._post", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sms_account = SmsAccount.objects.create(
            customer=self.customer, is_enabled=True, sender_name="Exempelror"
        )
        Switchboard.get_solo()
        Switchboard.objects.update(sms_enabled=True, links_ready_at=timezone.now())
        self.kunder = ContactList.objects.create(account=self.account, name="Kunder")
        self.sida = SignupForm.objects.create(
            account=self.account,
            title="Få våra erbjudanden",
            channels=[CHANNEL_SMS],
            is_active=True,
            add_to_list=self.kunder,
        )
        self.url = reverse("utskick_public:signup", args=["exempelror"])

    def post(self, **data):
        token = aged_token()
        payload = {
            "first_name": "Bo",
            "phone": "070-174 06 02",
            "consent_sms": "1",
            "bc_website": "",
            "bc_time": token,
            "bc_proof": token,
        }
        payload.update(data)
        return Client().post(self.url, payload)

    def test_the_page_offers_sms_with_the_exact_text(self):
        html = Client().get(self.url).content.decode()
        self.assertIn('name="phone"', html)
        self.assertIn(">Ja, jag vill få erbjudanden från Exempelrör via sms.</label>", html)
        self.assertNotRegex(html, r'name="consent_sms"[^>]*\schecked')
        self.assertNotIn('name="email"', html)

    def test_sms_signup_waits_for_the_link_in_the_sms(self):
        response = self.post()
        self.assertEqual(response.status_code, 302)
        thanks = Client().get(response["Location"])
        self.assertContains(thanks, "Om 070-*** ** 02 inte redan får erbjudanden")
        contact = Contact.objects.get(account=self.account, phone=PHONE_BO)
        self.assertEqual(self.sms_status(contact), consents.PENDING)
        self.assertFalse(contact.memberships.exists())
        self.assertEqual(optin.send_due_sms()["sent"], 1)
        body = self.fake.sends[0]["message"]
        code = re.search(r"k\.adx\.se/b/([A-Za-z0-9]{6})", body).group(1)
        self.assertEqual(LinkCode.objects.get(code=code).purpose, "signup")
        client = self.k()
        page = client.get(f"/b/{code}", **K)
        fn = FN_RE.search(page.content.decode()).group(1)
        client.post(f"/b/{code}", {"fn": fn}, **K)
        consent = Consent.objects.get(contact=contact, channel=CHANNEL_SMS)
        self.assertEqual((consent.status, consent.source), (consents.YES, "confirm"))
        self.assertEqual(consent.text_shown, "Ja, jag vill få erbjudanden från Exempelrör via sms.")
        self.assertTrue(contact.memberships.filter(list=self.kunder).exists())

    def test_an_existing_contact_is_never_changed(self):
        self.post(phone=ANNA_PHONE_TYPED, first_name="Någon")
        self.kontakt.refresh_from_db()
        self.assertEqual(self.kontakt.first_name, "Anna")
        # Anna har redan ja: ingen nedgradering till pending.
        self.assertEqual(self.sms_status(), consents.YES)

    def test_a_landline_is_refused(self):
        response = self.post(phone="08-465 004 00")
        self.assertContains(response, "Det här numret kan inte få sms.")
        self.assertFalse(Contact.objects.filter(phone="+4684650040").exists())

    def test_no_sms_without_the_customers_sms(self):
        self.sms_account.is_enabled = False
        self.sms_account.save(update_fields=["is_enabled"])
        self.assertEqual(Client().get(self.url).status_code, 404)
        settings_page = self.client_for(self.anna).get(reverse("flamingo:app_signup"))
        self.assertContains(settings_page, optin.SMS_NOT_ENABLED_TEXT)

    def test_the_customer_chooses_the_channels(self):
        client = self.client_for(self.anna)
        url = reverse("flamingo:app_signup")
        data = {"title": "Anmälan", "is_active": "1", "kanaler_visade": "1"}
        response = client.post(url, {**data, "kanal": ["email", "sms"]})
        self.assertEqual(response.status_code, 302)
        self.sida.refresh_from_db()
        self.assertEqual(self.sida.channels, ["sms", "email"])
        response = client.post(url, data)
        self.assertContains(response, "Välj minst en kanal.")
        self.sida.refresh_from_db()
        self.assertEqual(self.sida.channels, ["sms", "email"])
