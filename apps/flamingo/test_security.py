"""ADX Flamingo: regressionstester för säkerhetsgranskningen 2026-10-03.

Varje klass motsvarar ett fynd och visar att angreppet inte längre går:

    SsrfScanTests           kundens hemsida som språngbräda mot vårt eget nät
    SpoofedIpTests          en påhittad X-Forwarded-For runt spärren på /lp/
    LeadSpamTests           spärren i processens minne, ingen gräns per kampanj
    SmsAbuseTests           besökarens text i sms till valfritt nummer
    ScanThrottleTests       obegränsat antal läsningar av hemsidan
    AiThrottleTests         obegränsat antal AI-förslag, AI-anrop under lås
    RatingTests             ett betyg från hemsidan på den publika sidan
    BudgetTests             ett jättebelopp gav ett serverfel
    CsvTests                formelskyddet ändrade riktiga texter i Editor-filen
    LandingTrackingTests    kundernas sidor räknades som besök på adx.se
"""

import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.forms.models import model_to_dict
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.analytics.models import PageView
from apps.analytics.tracking import SESSION_COOKIE, VISITOR_COOKIE
from apps.manage.forms import BlockPageForm
from apps.projects.models import Customer
from apps.tools.tests import LocalSite, as_public, html_route, redirect_route, status_route
from apps.website.models import BlockPage

from . import checks, exports, generator, limits, scan, sms
from .app_views import campaigns as campaign_views
from .models import Campaign, Fact, FlamingoAccount, Lead, Service, SmsLog
from .public_views import HONEYPOT

User = get_user_model()
STHLM = ZoneInfo("Europe/Stockholm")
ELKS = {
    "ELKS_API_USERNAME": "u-test",
    "ELKS_API_PASSWORD": "p-test",
    "ELKS_SENDER": "ADXFlamingo",
}
CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
PHISHING_NAME = "https://adx-login.example/verify gnu"


def _at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=STHLM)


class Fixture:
    @classmethod
    def setUpTestData(cls):
        cls.acme = Customer.objects.create(name="Lindqvist Rör AB")
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.acme.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(customer=cls.acme, is_enabled=True)
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
        cls.live = Campaign.objects.create(
            account=cls.account,
            service=cls.jour,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            area="Nacka + 15 km",
            page={"title": "Rörjour i Nacka", "phone": "08-000 00 00"},
        )
        cls.draft = Campaign.objects.create(
            account=cls.account,
            service=cls.jour,
            name="Rörjour Värmdö",
            area="Värmdö + 15 km",
            daily_budget_kr=200,
        )

    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(self.anna)


# ---------------------------------------------------------------------------
# 1. SSRF genom läsningen av hemsidan
# ---------------------------------------------------------------------------


class SsrfScanTests(Fixture, TestCase):
    """Granskningen: en omdirigering från kundens sajt till 127.0.0.1 följdes,
    och felet visade HTTP-statusen från den interna tjänsten."""

    def serve(self, routes):
        site = LocalSite(routes)
        self.addCleanup(site.close)
        return site

    def scan(self, site):
        with as_public(site), mock.patch.object(scan.llm, "is_configured", return_value=False):
            result = scan.scan_website(self.account, site.base + "/")
        self.account.refresh_from_db()
        return result

    def test_a_redirect_into_our_network_is_never_requested(self):
        site = self.serve(
            {
                "/": redirect_route("http://127.0.0.1:{port}/internal"),
                "/internal": html_route("<p>intern</p>"),
            }
        )
        result = self.scan(site)
        self.assertFalse(result.ok)
        self.assertEqual(site.paths, ["/"], "den interna adressen fick ett anrop")
        self.assertEqual(self.account.scan_error, scan.READ_FAILED)

    def test_the_customer_never_sees_why_a_fetch_failed(self):
        site = self.serve({"/": status_route(500)})
        self.scan(site)
        self.assertEqual(self.account.scan_error, scan.READ_FAILED)
        self.assertNotIn("500", self.account.scan_error)
        self.assertNotIn("HTTP", self.account.scan_error)

    def test_a_public_site_is_still_read_with_the_page_cap(self):
        site = self.serve({"/": html_route("<html><body><h1>Rörjour</h1></body></html>")})
        calls = []
        real = scan.fetch

        def spy(url, **kwargs):
            calls.append(kwargs)
            return real(url, **kwargs)

        with mock.patch.object(scan, "fetch", side_effect=spy):
            result = self.scan(site)
        self.assertTrue(result.ok)
        self.assertEqual(site.paths, ["/"])
        self.assertEqual(calls[0]["max_bytes"], scan.PAGE_MAX_BYTES)
        self.assertLessEqual(calls[0]["time_limit"], scan.FETCH_BUDGET)


# ---------------------------------------------------------------------------
# 2-3. Spärren på /lp/: IP-adressen och räkningen
# ---------------------------------------------------------------------------


class SpoofedIpTests(Fixture, TestCase):
    def post(self, **headers):
        return Client().post(self.live.landing_url, {"phone": "070-111 22 33"}, **headers)

    def test_a_spoofed_forwarded_header_does_not_reset_the_limit(self):
        """Granskningen: 40 inskick från en adress med en ny påhittad första
        post i X-Forwarded-For gav 40 förfrågningar."""
        for n in range(40):
            self.post(
                HTTP_X_FORWARDED_FOR=f"203.0.113.{n}, 198.51.100.9",
                HTTP_X_REAL_IP="198.51.100.9",
            )
        self.assertEqual(Lead.objects.filter(campaign=self.live).count(), limits.LEADS_PER_IP)

    def test_without_x_real_ip_the_last_forwarded_entry_counts(self):
        for n in range(15):
            self.post(HTTP_X_FORWARDED_FOR=f"203.0.113.{n}, 198.51.100.9")
        self.assertEqual(Lead.objects.filter(campaign=self.live).count(), limits.LEADS_PER_IP)

    def test_only_a_keyed_hash_of_the_ip_is_stored(self):
        self.post(HTTP_X_REAL_IP="198.51.100.9")
        lead = Lead.objects.get(campaign=self.live)
        self.assertEqual(lead.ip_hash, limits.ip_hash("198.51.100.9"))
        self.assertEqual(len(lead.ip_hash), 64)
        self.assertNotEqual(lead.ip_hash, hashlib.sha256(b"198.51.100.9").hexdigest())
        self.assertNotIn("198.51.100.9", str(model_to_dict(lead)))


class LeadSpamTests(Fixture, TestCase):
    def post(self, ip, **data):
        return Client().post(
            self.live.landing_url, {"phone": "070-111 22 33", **data}, HTTP_X_REAL_IP=ip
        )

    def test_the_limit_is_counted_in_the_database_not_in_a_cache(self):
        for _ in range(limits.LEADS_PER_IP):
            self.assertEqual(self.post("198.51.100.9").status_code, 302)
            cache.clear()  # en annan arbetare, eller en omstart
        response = self.post("198.51.100.9")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Ring Lindqvist Rör på 08-000 00 00", status_code=429)
        self.assertEqual(Lead.objects.count(), limits.LEADS_PER_IP)

    def test_each_campaign_takes_at_most_thirty_an_hour_from_everyone(self):
        for n in range(limits.LEADS_PER_CAMPAIGN):
            self.assertEqual(self.post(f"198.51.100.{n + 1}").status_code, 302)
        response = self.post("192.0.2.200")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "tar inte emot fler förfrågningar", status_code=429)
        self.assertContains(response, "08-000 00 00", status_code=429)
        self.assertEqual(Lead.objects.count(), limits.LEADS_PER_CAMPAIGN)

    def test_leads_older_than_an_hour_do_not_count(self):
        hashed = limits.ip_hash("198.51.100.9")
        for _ in range(limits.LEADS_PER_CAMPAIGN):
            lead = Lead.objects.create(account=self.account, campaign=self.live, ip_hash=hashed)
            Lead.objects.filter(pk=lead.pk).update(created_at=lead.created_at - timedelta(hours=2))
        self.assertEqual(self.post("198.51.100.9").status_code, 302)

    def test_the_honeypot_still_saves_nothing(self):
        response = self.post("198.51.100.9", **{HONEYPOT: "http://spam.example"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Lead.objects.exists())

    def test_the_count_and_the_insert_happen_under_the_campaign_lock(self):
        request = SimpleNamespace(META={"REMOTE_ADDR": "198.51.100.9"}, GET={})
        with mock.patch.object(
            limits.Campaign.objects, "select_for_update", wraps=Campaign.objects.select_for_update
        ) as lock:
            lead, refused = limits.create_form_lead(self.live, {"phone": "070"}, request)
        self.assertEqual(refused, "")
        self.assertIsNotNone(lead)
        lock.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Sms
# ---------------------------------------------------------------------------


@override_settings(**ELKS, SITE_BASE_URL="https://adx.se")
class SmsAbuseTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.account.notify_sms = True
        self.account.notify_phone = "+46701234567"
        self.account.autoreply_enabled = True
        self.account.autoreply_text = "Hej {namn}! Tack, vi hör av oss."
        self.account.save()
        patcher = mock.patch.object(sms, "_post_to_elks", return_value="s1")
        self.elks = patcher.start()
        self.addCleanup(patcher.stop)

    def lead(self, name="Anna Lind", phone="0761234567"):
        return Lead.objects.create(
            account=self.account, campaign=self.live, service=self.jour, name=name, phone=phone
        )

    def sent_bodies(self):
        return [c.args[2] for c in self.elks.call_args_list]

    def test_a_link_in_the_name_never_reaches_an_sms(self):
        """Granskningen: autosvaret blev 'Hej https://adx-login.example/verify!'
        till ett nummer besökaren valt, med kundens namn som avsändare."""
        rows = sms.notify_new_lead(self.lead(name=PHISHING_NAME), now=_at(3, 12))
        self.assertEqual([r.status for r in rows], [SmsLog.STATUS_SENT, SmsLog.STATUS_SENT])
        owner, reply = self.sent_bodies()
        self.assertEqual(reply, "Hej! Tack, vi hör av oss.")
        self.assertTrue(owner.startswith("Ny förfrågan: gnu, 0761234567."), owner)
        for body in (owner, reply):
            with self.subTest(body=body):
                self.assertNotIn("adx-login", body)
                self.assertNotIn("example", body)
                self.assertNotIn("verify", body)

    def test_first_name_must_look_like_a_first_name(self):
        for raw, expected in (
            ("Anna Lind", "Anna"),
            ("Åsa-Karin Öberg", "Åsa-Karin"),
            ("Søren", "Søren"),
            ("evil.example", ""),
            ("www.evil.se", ""),
            ("Anna1", ""),
            ("A" * 21, ""),
            ("<b>Anna</b>", ""),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(sms.first_name(raw), expected)

    def test_visitor_text_is_stripped_of_addresses_and_odd_characters(self):
        self.assertEqual(sms.visitor_text("Ring www.evil.se nu"), "Ring nu")
        self.assertEqual(sms.visitor_text("Anna <script>"), "Anna script")
        self.assertEqual(sms.visitor_text("besök evil.example/x idag"), "besök idag")
        self.assertEqual(sms.visitor_text("a.b.c"), "a b c")
        self.assertEqual(sms.visitor_text("A. Andersson"), "A. Andersson")
        self.assertLessEqual(len(sms.visitor_text("Anna " * 30)), sms.VISITOR_NAME_MAX)

    def test_one_autoreply_per_number_and_day(self):
        sms.notify_new_lead(self.lead(), now=_at(3, 12))
        rows = sms.notify_new_lead(self.lead(), now=_at(3, 13))
        self.assertEqual(rows[0].status, SmsLog.STATUS_SENT, "ägaren får alltid veta")
        self.assertEqual(rows[1].status, SmsLog.STATUS_DISABLED)
        self.assertEqual(rows[1].error, sms.NOTE_ALREADY_REPLIED)
        # Samma nummer skrivet på ett annat sätt är samma nummer.
        rows = sms.notify_new_lead(self.lead(phone="+46 76 123 45 67"), now=_at(3, 14))
        self.assertEqual(rows[1].status, SmsLog.STATUS_DISABLED)
        # Ett dygn senare går det igen.
        SmsLog.objects.filter(kind=SmsLog.KIND_AUTOREPLY, status=SmsLog.STATUS_SENT).update(
            created_at=_at(3, 12)
        )
        rows = sms.notify_new_lead(self.lead(), now=_at(4, 13))
        self.assertEqual(rows[1].status, SmsLog.STATUS_SENT)

    def test_the_slot_is_reserved_before_46elks_is_called(self):
        """Kontrollen: sex förfrågningar samtidigt från samma nummer gav sex
        autosvar, eftersom gränsen prövades innan något sparats. Nu sparas
        raden som "sending" (med kontot låst) innan 46elks anropas, så en
        förfrågan som kommer under anropet ser den."""
        nested = []

        def elks(sender, to, body):
            # Under autosvarets anrop (inte ägarens) kommer nästa förfrågan.
            if to == "+46761234567" and not nested:
                nested.append(sms.notify_new_lead(self.lead(), now=_at(3, 12)))
            return "s1"

        self.elks.side_effect = elks
        first = sms.notify_new_lead(self.lead(), now=_at(3, 12))
        second = nested[0]
        self.assertEqual([r.status for r in first], [SmsLog.STATUS_SENT, SmsLog.STATUS_SENT])
        self.assertEqual(second[1].status, SmsLog.STATUS_DISABLED)
        self.assertEqual(second[1].error, sms.NOTE_ALREADY_REPLIED)
        self.assertEqual(
            SmsLog.objects.filter(kind=SmsLog.KIND_AUTOREPLY, status=SmsLog.STATUS_SENT).count(), 1
        )
        self.assertFalse(SmsLog.objects.filter(status=SmsLog.STATUS_SENDING).exists())

    def test_a_failed_send_frees_nothing_it_did_not_send(self):
        self.elks.side_effect = TimeoutError("långsamt")
        rows = sms.notify_new_lead(self.lead(), now=_at(3, 12))
        self.assertEqual({r.status for r in rows}, {SmsLog.STATUS_FAILED})
        self.assertEqual(sms.sent_today(self.account, _at(3, 12)), 0)
        self.assertFalse(sms.replied_recently(self.account, "+46761234567", _at(3, 12)))

    def test_at_most_fifty_sms_per_account_and_day(self):
        SmsLog.objects.bulk_create(
            SmsLog(
                account=self.account,
                kind=SmsLog.KIND_OWNER,
                to="+46701234567",
                body="x",
                status=SmsLog.STATUS_SENT,
                created_at=_at(3, 8),
            )
            for _ in range(sms.SMS_DAILY_MAX)
        )
        rows = sms.notify_new_lead(self.lead(phone="0769999999"), now=_at(3, 12))
        self.assertEqual({r.status for r in rows}, {SmsLog.STATUS_DISABLED})
        self.assertEqual({r.error for r in rows}, {sms.NOTE_DAILY_LIMIT})
        self.elks.assert_not_called()
        # Nästa dag (svensk tid) börjar räkningen om.
        rows = sms.notify_new_lead(self.lead(phone="0769999998"), now=_at(4, 8))
        self.assertEqual([r.status for r in rows], [SmsLog.STATUS_SENT, SmsLog.STATUS_SENT])
        self.assertEqual(mail.outbox, [])


# ---------------------------------------------------------------------------
# 5. Läsningar och AI-förslag
# ---------------------------------------------------------------------------


class ScanThrottleTests(Fixture, TestCase):
    def test_a_second_scan_right_away_is_refused_before_anything_is_fetched(self):
        url = reverse("flamingo:app_proposal")
        done = scan.ScanResult(ok=True, pages=1)
        with mock.patch.object(scan, "scan_website", return_value=done) as scanned:
            self.client.post(url, {"action": "scan", "website_url": "lindqvistror.se"})
            response = self.client.post(
                url, {"action": "scan", "website_url": "lindqvistror.se"}, follow=True
            )
        self.assertEqual(scanned.call_count, 1)
        self.assertContains(response, "lästes av alldeles nyss")

    def test_the_rules(self):
        start = _at(3, 8)
        self.assertEqual(limits.reserve_scan(self.account, now=start), "")
        self.assertEqual(
            limits.reserve_scan(self.account, now=start + timedelta(minutes=1)),
            limits.SCAN_TOO_SOON,
        )
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            scan_status=FlamingoAccount.SCAN_RUNNING
        )
        self.account.refresh_from_db()
        self.assertEqual(
            limits.reserve_scan(self.account, now=start + timedelta(seconds=90)), limits.SCAN_BUSY
        )
        # En läsning som stått som "hämtas" i mer än två minuter har avbrutits.
        moment = start + timedelta(minutes=3)
        self.assertEqual(limits.reserve_scan(self.account, now=moment), "")
        for _ in range(limits.SCAN_DAILY_MAX - 2):
            moment += timedelta(minutes=3)
            self.assertEqual(limits.reserve_scan(self.account, now=moment), "")
        moment += timedelta(minutes=3)
        self.assertEqual(limits.reserve_scan(self.account, now=moment), limits.SCAN_DAILY_LIMIT)
        self.assertEqual(limits.reserve_scan(self.account, now=_at(4, 0, 5)), "")
        self.account.refresh_from_db()
        self.assertEqual(self.account.scan_count, 1)


class AiThrottleTests(Fixture, TestCase):
    def ai_on(self):
        response = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    name=generator.TOOL_NAME,
                    input={"rubriker": ["Rörjour i Nacka"], "beskrivningar": ["Ring oss."]},
                )
            ]
        )
        return (
            mock.patch.object(generator.llm, "is_configured", return_value=True),
            mock.patch.object(generator.llm, "check_budget", return_value=None),
            mock.patch.object(generator.llm, "call", return_value=response),
        )

    def test_at_most_twenty_ai_proposals_a_day_then_templates(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            ai_day=limits.stockholm_today(), ai_count=limits.AI_DAILY_MAX - 1
        )
        configured, budget, call = self.ai_on()
        with configured, budget, call as called:
            first = generator.build_proposal(self.draft)
            second = generator.build_proposal(self.draft)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(first.source, generator.SOURCE_AI)
        self.assertEqual(second.source, generator.SOURCE_TEMPLATES)
        self.assertEqual(second.note, generator.NOTE_AI_DAILY_LIMIT)
        self.assertTrue(second.headlines, "mallarna skriver förslaget, det fallerar aldrig")

    def test_regenerate_calls_the_model_without_holding_the_lock(self):
        url = reverse("flamingo:app_campaign", args=[self.draft.pk])
        baseline = len(connection.atomic_blocks)
        depths = []
        real = generator.build_proposal

        def spy(campaign, **kwargs):
            depths.append((len(connection.atomic_blocks), kwargs.get("save")))
            return real(campaign, **kwargs)

        with mock.patch.object(generator, "build_proposal", side_effect=spy):
            response = self.client.post(url, {"section": "regenerate"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(depths, [(baseline, False)])
        self.draft.refresh_from_db()
        self.assertTrue(self.draft.headlines, "förslaget sparades efteråt")

    def test_a_campaign_sent_for_review_meanwhile_is_not_overwritten(self):
        url = reverse("flamingo:app_campaign", args=[self.draft.pk])
        real = generator.build_proposal

        def submitted_meanwhile(campaign, **kwargs):
            proposal = real(campaign, **kwargs)
            Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.STATUS_IN_REVIEW)
            return proposal

        with mock.patch.object(generator, "build_proposal", side_effect=submitted_meanwhile):
            response = self.client.post(url, {"section": "regenerate"}, follow=True)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.headlines, [])
        self.assertContains(response, "hos ADX för granskning")


# ---------------------------------------------------------------------------
# 6. Betyg
# ---------------------------------------------------------------------------


class RatingTests(Fixture, TestCase):
    def rating(self, source):
        return Fact.objects.create(
            account=self.account,
            key="betyg",
            label="Betyg",
            value="4,9 av 5",
            source=source,
            confirmed=True,
        )

    def test_a_rating_from_the_site_or_the_customer_is_never_public(self):
        for source in (Fact.SOURCE_SITE, Fact.SOURCE_CUSTOMER):
            with self.subTest(source=source):
                fact = self.rating(source)
                html = Client().get(self.live.landing_url).content.decode()
                self.assertNotIn("4,9 av 5", html)
                self.assertNotIn("lp-rating", html)
                fact.delete()

    def test_a_rating_from_google_is_shown(self):
        self.rating(Fact.SOURCE_GOOGLE)
        self.assertContains(Client().get(self.live.landing_url), "4,9 av 5")

    def test_the_ads_only_use_a_rating_from_google(self):
        fact = self.rating(Fact.SOURCE_SITE)
        self.assertEqual(generator.info_for(self.draft).rating, "")
        fact.source = Fact.SOURCE_GOOGLE
        fact.save()
        self.assertEqual(generator.info_for(self.draft).rating, "4,9")

    def test_the_scan_cannot_create_a_rating(self):
        """Granskningen: AI-uppgifterna rating och omdomen klarade sig igenom
        och syntes som stjärna på /lp/ efter "Bekräfta alla"."""
        page_text = (
            "Kunderna ger oss 4,9 av 5 i snitt. 126 omdömen. Fem stjärnor. "
            "Reviews: 4.9. Grundat 1998."
        )
        data = {
            "facts": [
                {"key": "rating", "label": "Betyg", "value": "4,9 av 5"},
                {"key": "omdomen", "label": "Omdömen", "value": "126 omdömen"},
                {"key": "snitt", "label": "Kundbetyg", "value": "4,9 i snitt"},
                {"key": "stjarnor", "label": "Stjärnor", "value": "Fem stjärnor"},
                {"key": "reviews", "label": "Reviews", "value": "Reviews: 4.9"},
                {"key": "nojdhet", "label": "Kundnöjdhet", "value": "Kunderna ger oss 4,9 av 5"},
                {"key": "grundat", "label": "Grundat", "value": "Grundat 1998"},
            ]
        }
        _, facts = scan.clean_ai_proposal(data, page_text)
        self.assertEqual([key for key, _, _ in facts], ["grundat"])

    def test_a_rating_under_another_name_is_caught(self):
        """Kontrollen: "Trustpilot 4,9" från sajten blev uppgiften
        Trustpilot: 4,9 (och ett betyg från en annan sajt likadant), och efter "Bekräfta alla"
        punkter på /lp/ och rader i annonserna."""
        page_text = (
            "Trustpilot 4,9 och Hantverkarsajten 4,8. 4,7 stjärnor på Facebook. Grundat 1998."
        )
        data = {
            "facts": [
                {"key": "trustpilot", "label": "Trustpilot", "value": "4,9"},
                {"key": "hantverkarsajten", "label": "Hantverkarsajten", "value": "4,8"},
                {"key": "facebook", "label": "Facebook", "value": "4,7 stjärnor på Facebook"},
                {"key": "grundat", "label": "Grundat", "value": "Grundat 1998"},
            ]
        }
        _, facts = scan.clean_ai_proposal(data, page_text)
        self.assertEqual([key for key, _, _ in facts], ["grundat"])

    def test_a_confirmed_rating_under_another_name_is_never_used(self):
        """Även om det redan står bland uppgifterna (sparat före rättningen,
        eller skrivet av kunden) når det varken annonserna, sidan eller
        kontrollernas tillåtna siffror."""
        for source in (Fact.SOURCE_SITE, Fact.SOURCE_CUSTOMER):
            with self.subTest(source=source):
                fact = Fact.objects.create(
                    account=self.account,
                    key="trustpilot",
                    label="Trustpilot",
                    value="4,9",
                    source=source,
                    confirmed=True,
                )
                self.assertNotIn("trustpilot", self.account.confirmed_facts())
                info = generator.info_for(self.draft)
                self.assertEqual(info.rating, "")
                self.assertNotIn(("Trustpilot", "4,9"), info.claims)
                self.draft.headlines = ["Trustpilot 4,9", "Rörjour i Värmdö", "Ring oss"]
                messages = [p.message for p in checks.validate(self.draft) if p.index == 0]
                self.assertIn("Siffran 4,9 finns inte bland dina bekräftade uppgifter.", messages)
                self.live.page = {**self.live.page, "points": []}
                self.live.save()
                self.assertNotContains(Client().get(self.live.landing_url), "4,9")
                fact.delete()

    def test_the_customer_sees_why_it_is_not_used(self):
        Fact.objects.create(
            account=self.account,
            key="trustpilot",
            label="Trustpilot",
            value="4,9",
            source=Fact.SOURCE_CUSTOMER,
            confirmed=True,
        )
        response = self.client.get(reverse("flamingo:app_business"))
        self.assertContains(response, "Betyg hämtas bara från Google.")

    def test_from_adx_the_same_fact_is_used_as_written(self):
        Fact.objects.create(
            account=self.account,
            key="trustpilot",
            label="Trustpilot",
            value="4,9",
            source=Fact.SOURCE_ADX,
            confirmed=True,
        )
        self.assertEqual(self.account.confirmed_facts()["trustpilot"], "4,9")

    def test_ordinary_facts_are_not_mistaken_for_ratings(self):
        from .models import is_rating_like

        for key, label, value in (
            ("grundat", "Grundat", "1998"),
            ("telefon", "Telefon", "08-000 00 00"),
            ("adress", "Adress", "Storgatan 5, Nacka"),
            ("pris-rorjour", "Pris för rörjour", "950 kr"),
            ("erfarenhet", "Erfarenhet", "25 år i branschen"),
            ("nojda", "Nöjda kunder", "98 procent nöjda kunder"),
            ("facebook", "Facebook", "Följ oss på Facebook"),
        ):
            with self.subTest(label=label):
                self.assertFalse(is_rating_like(key, label, value))


# ---------------------------------------------------------------------------
# 7. Budgeten
# ---------------------------------------------------------------------------


class BudgetTests(Fixture, TestCase):
    def save(self, budget):
        url = reverse("flamingo:app_campaign", args=[self.draft.pk])
        return self.client.post(
            url,
            {
                "section": "settings",
                "place": "Värmdö",
                "radius_km": "15",
                "daily_budget_kr": budget,
            },
            follow=True,
        )

    def test_a_huge_budget_is_a_message_not_a_server_error(self):
        response = self.save("99999999999")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, campaign_views.BUDGET_MAX_MESSAGE)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.daily_budget_kr, 200)

    def test_the_same_limits_as_a_new_campaign(self):
        for budget, message in (
            ("5001", campaign_views.BUDGET_MAX_MESSAGE),
            ("49", campaign_views.BUDGET_MIN_MESSAGE),
            ("-5", campaign_views.BUDGET_MIN_MESSAGE),
        ):
            with self.subTest(budget=budget):
                self.assertContains(self.save(budget), message)
                self.draft.refresh_from_db()
                self.assertEqual(self.draft.daily_budget_kr, 200)
        self.save("300")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.daily_budget_kr, 300)

    def test_the_model_refuses_it_too(self):
        from django.core.exceptions import ValidationError

        field = Campaign._meta.get_field("daily_budget_kr")
        field.run_validators(checks.BUDGET_MAX)
        for wrong in (checks.BUDGET_MIN - 1, checks.BUDGET_MAX + 1):
            with self.subTest(budget=wrong), self.assertRaises(ValidationError):
                field.run_validators(wrong)


# ---------------------------------------------------------------------------
# 8. CSV till Google Ads Editor
# ---------------------------------------------------------------------------


class CsvTests(Fixture, TestCase):
    def test_ad_texts_that_start_like_a_formula_are_flagged(self):
        self.draft.headlines = ["+46 8 123 45 67", "-20% på jour", "Rörjour i Värmdö"]
        self.draft.descriptions = ["=HYPERLINK(1)", "@hem", "Ring oss om stopp i avloppet."]
        problems = checks.validate(self.draft)
        flagged = {(p.field, p.index) for p in problems if p.message == checks.FORMULA_MESSAGE}
        self.assertEqual(
            flagged,
            {("headlines", 0), ("headlines", 1), ("descriptions", 0), ("descriptions", 1)},
        )

    def test_the_generator_never_proposes_such_a_text(self):
        context = checks.context_for(self.draft)
        candidates = ["+Rörjour i Värmdö", "-Rörjour i Värmdö", "@Rörjour", "Rörjour i Värmdö"]
        self.assertEqual(generator._fit(candidates, 30, 15, context), ["Rörjour i Värmdö"])

    def test_keywords_lose_a_leading_plus_or_minus(self):
        self.draft.keywords = [
            {"text": "-rör", "match": "phrase"},
            {"text": "+avlopp", "match": "exact"},
        ]
        self.draft.negatives = ["-gratis"]
        self.assertEqual(exports.keyword_rows(self.draft), [("rör", "Phrase"), ("avlopp", "Exact")])
        self.assertEqual(exports.negative_rows(self.draft), [("gratis", "Negative Broad")])
        csv_text = exports.google_ads_editor_csv(self.draft)
        self.assertNotIn("'-rör", csv_text)
        self.assertNotIn("'+avlopp", csv_text)
        self.assertEqual(campaign_views._keyword_text("-Rör"), "rör")

    def test_a_service_name_that_starts_like_a_formula_is_refused(self):
        """Kontrollen: tjänsten "-20% Rörjour" blev "'-20% Rörjour" i
        Editor-filens Ad Group och Campaign."""
        from .app_views.onboarding import ServiceForm

        form = ServiceForm(
            {"name": "-20% Rörjour", "sales_mode": Service.SALES_CALL}, account=self.account
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["name"], [checks.FORMULA_MESSAGE])
        response = self.client.post(
            reverse("flamingo:app_campaign_new"),
            {
                "service": "ny",
                "new_service": "=Avlopp",
                "sales_mode": Service.SALES_QUOTE,
                "place": "Nacka",
                "radius_km": "15",
                "budget": "200",
            },
        )
        self.assertContains(response, "Börja inte med =")
        self.assertFalse(Service.objects.filter(name__contains="Avlopp").exists())
        # En tjänst som redan finns stoppas vid inskicket.
        Service.objects.filter(pk=self.jour.pk).update(name="+Rörjour")
        self.draft.refresh_from_db()
        problems = [p for p in checks.validate(self.draft) if p.field == "service"]
        self.assertEqual([p.message for p in problems], [checks.FORMULA_MESSAGE])
        self.assertEqual(scan.clean_service_name("+Rörjour"), "Rörjour")

    def test_safe_cell_is_still_the_backstop(self):
        self.assertEqual(exports.safe_cell("=1+1"), "'=1+1")


# ---------------------------------------------------------------------------
# 9. Kundernas landningssidor är inte adx.se:s besök
# ---------------------------------------------------------------------------


class LandingTrackingTests(Fixture, TestCase):
    def test_a_landing_page_is_not_tracked_and_gets_no_adx_cookies(self):
        response = Client().get(self.live.landing_url, HTTP_USER_AGENT=CHROME)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PageView.objects.exists())
        self.assertNotIn(VISITOR_COOKIE, response.cookies)
        self.assertNotIn(SESSION_COOKIE, response.cookies)

    def test_an_adx_page_cannot_take_the_lp_address(self):
        form = BlockPageForm(
            data={"title": "Landning", "slug": "lp", "design": BlockPage.DESIGN_ADX, "order": 0}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("reserverad", " ".join(form.errors.get("slug", [])))
