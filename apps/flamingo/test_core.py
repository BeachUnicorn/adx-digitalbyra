"""ADX Flamingo, verktygets kärna: modellerna, adresserna, behörigheten,
kundväljaren, översikten och reglerna för "tre saker"."""

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import rules
from .access import SESSION_KEY
from .app_views.overview import band_path, numbers_for
from .models import (
    Campaign,
    ConversionUpload,
    Fact,
    FlamingoAccount,
    Lead,
    Review,
    Service,
    company_slug,
    format_google_ads_id,
    make_page_slug,
)
from .testing import pages_from_campaigns

User = get_user_model()


class CoreFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Lindqvist Rör AB")
        cls.other = Customer.objects.create(name="Hemlig Bygg AB")
        cls.plain = Customer.objects.create(name="Utan Flamingo AB")
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.anna.first_name = "Anna"
        cls.anna.save()
        cls.acme.users.add(cls.anna)
        cls.bo = User.objects.create_user("bo@hemlig.se", email="bo@hemlig.se", password="x")
        cls.other.users.add(cls.bo)
        cls.cia = User.objects.create_user("cia@utan.se", email="cia@utan.se", password="x")
        cls.plain.users.add(cls.cia)

        cls.account = FlamingoAccount.objects.create(customer=cls.acme, is_enabled=True)
        cls.other_account = FlamingoAccount.objects.create(customer=cls.other, is_enabled=True)

        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        cls.badrum = Service.objects.create(account=cls.account, name="Badrumsrenovering")
        cls.secret_service = Service.objects.create(account=cls.other_account, name="Takbyte")

        cls.live = Campaign.objects.create(
            account=cls.account,
            service=cls.jour,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            page={"title": "Rörjour i Nacka"},
        )
        cls.draft = Campaign.objects.create(account=cls.account, service=cls.badrum, name="Badrum")
        cls.secret = Campaign.objects.create(
            account=cls.other_account,
            service=cls.secret_service,
            name="Hemlig kampanj",
            status=Campaign.STATUS_LIVE,
        )
        pages_from_campaigns(cls.live, cls.draft, cls.secret)
        cls.lead = Lead.objects.create(account=cls.account, campaign=cls.live, name="Sara Holm")
        cls.secret_lead = Lead.objects.create(
            account=cls.other_account, name="Hemlig Person", phone="070-999"
        )

    def client_for(self, user, view_as=None):
        client = Client()
        client.force_login(user)
        if view_as is not None:
            session = client.session
            session[VIEW_AS_KEY] = view_as.pk
            session.save()
        return client

    def app_urls(self):
        """Varje GET-adress i verktyget, med kundens egna id:n."""
        return [
            reverse("flamingo:app"),
            reverse("flamingo:app_proposal"),
            reverse("flamingo:app_business"),
            reverse("flamingo:app_google"),
            reverse("flamingo:app_settings"),
            reverse("flamingo:app_campaigns"),
            reverse("flamingo:app_campaign_new"),
            reverse("flamingo:app_campaign", args=[self.live.pk]),
            reverse("flamingo:app_inbox"),
            reverse("flamingo:app_lead", args=[self.lead.pk]),
        ]


# ---------------------------------------------------------------------------
# Modellerna
# ---------------------------------------------------------------------------


class ModelTests(CoreFixture, TestCase):
    def test_page_slug_is_customer_and_service_without_the_legal_form(self):
        self.assertEqual(company_slug("Lindqvist Rör AB"), "lindqvist-ror")
        self.assertEqual(self.live.page_slug, "lindqvist-ror-rorjour")
        self.assertEqual(self.draft.page_slug, "lindqvist-ror-badrumsrenovering")

    def test_page_slug_is_deduplicated_and_kept_within_the_field(self):
        again = Campaign.objects.create(account=self.account, service=self.jour, name="Värmdö")
        self.assertEqual(again.page_slug, "lindqvist-ror-rorjour-2")
        third = Campaign.objects.create(account=self.account, service=self.jour, name="Tyresö")
        self.assertEqual(third.page_slug, "lindqvist-ror-rorjour-3")
        long_service = Service.objects.create(account=self.account, name="Lång " * 23)
        first = Campaign.objects.create(account=self.account, service=long_service, name="A")
        second = Campaign.objects.create(account=self.account, service=long_service, name="B")
        self.assertLessEqual(len(first.page_slug), 80)
        self.assertLessEqual(len(second.page_slug), 80)
        self.assertTrue(second.page_slug.endswith("-2"))
        # Den egna sluggen krockar inte med sig själv.
        self.assertEqual(
            make_page_slug(self.acme, "Rörjour", exclude_pk=self.live.pk), "lindqvist-ror-rorjour"
        )

    def test_saving_again_keeps_the_slug(self):
        self.live.name = "Nytt namn"
        self.live.save()
        self.live.refresh_from_db()
        self.assertEqual(self.live.page_slug, "lindqvist-ror-rorjour")

    def test_campaign_helpers(self):
        self.assertEqual(self.live.sales_mode, Service.SALES_CALL)
        self.assertEqual(self.live.get_sales_mode_display(), "Ringer direkt")
        self.assertTrue(self.live.is_public)
        self.assertFalse(self.draft.is_public)
        self.assertEqual(self.live.landing_url, "/lp/lindqvist-ror-rorjour/")
        self.assertTrue(self.draft.customer_can_edit)
        self.assertFalse(self.live.customer_can_edit)
        self.assertEqual(self.draft.next_round(), 1)
        Review.objects.create(campaign=self.draft, round=1)
        self.assertEqual(self.draft.next_round(), 2)
        self.assertEqual(self.draft.pending_review().round, 1)
        snapshot = self.draft.content_snapshot()
        self.assertEqual(set(snapshot), {*Campaign.CONTENT_FIELDS, "landing"})
        self.assertNotIn("page", Campaign.CONTENT_FIELDS)
        page = self.draft.landing_page
        self.assertEqual(snapshot["landing"], {"id": page.pk, "name": page.name, "rev": page.rev})

    def test_service_is_protected_while_a_campaign_uses_it(self):
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.badrum.delete()

    def test_google_ads_id_is_normalised(self):
        self.assertEqual(format_google_ads_id("1234567890"), "123-456-7890")
        self.assertEqual(format_google_ads_id(" 123 456 7890 "), "123-456-7890")
        self.assertEqual(format_google_ads_id("123-456-7890"), "123-456-7890")
        self.assertEqual(format_google_ads_id(""), "")
        self.assertIsNone(format_google_ads_id("12345"))

    def test_only_confirmed_facts_with_a_value_are_usable(self):
        Fact.objects.create(
            account=self.account, key="telefon", label="Telefon", value="08-1", confirmed=True
        )
        Fact.objects.create(account=self.account, key="jour", label="Jour", value="Ja")
        Fact.objects.create(
            account=self.account, key="pris", label="Pris", value="", confirmed=True
        )
        self.assertEqual(self.account.confirmed_facts(), {"telefon": "08-1"})

    def test_new_accounts_have_everything_switched_off(self):
        account = FlamingoAccount.objects.create(customer=self.plain)
        self.assertFalse(account.is_enabled)
        self.assertFalse(account.notify_sms)
        self.assertFalse(account.autoreply_enabled)
        self.assertEqual(account.google_status, FlamingoAccount.GOOGLE_NOT_STARTED)
        self.assertEqual(account.scan_status, FlamingoAccount.SCAN_NONE)
        self.assertTrue(account.autoreply_text)

    def test_won_with_a_click_id_queues_a_conversion(self):
        lead = Lead.objects.create(
            account=self.account, name="Erik", gclid="abc", ad_consent=Lead.CONSENT_GRANTED
        )
        lead.set_status(Lead.STATUS_WON, value_kr=4800)
        upload = ConversionUpload.objects.get(lead=lead)
        self.assertEqual((upload.value_kr, upload.status), (4800, ConversionUpload.STATUS_QUEUED))
        self.assertIsNotNone(lead.won_at)
        lead.set_status(Lead.STATUS_WON, value_kr=5200)
        upload.refresh_from_db()
        self.assertEqual(upload.value_kr, 5200)
        lead.set_status(Lead.STATUS_LOST)
        self.assertFalse(ConversionUpload.objects.filter(lead=lead).exists())
        self.assertIsNone(lead.won_at)

    def test_a_sent_conversion_is_never_touched(self):
        lead = Lead.objects.create(
            account=self.account, name="Erik", gclid="abc", ad_consent=Lead.CONSENT_GRANTED
        )
        lead.set_status(Lead.STATUS_WON, value_kr=100)
        ConversionUpload.objects.filter(lead=lead).update(status=ConversionUpload.STATUS_SENT)
        lead.set_status(Lead.STATUS_LOST)
        self.assertTrue(ConversionUpload.objects.filter(lead=lead).exists())

    def test_won_without_click_id_or_value_queues_nothing(self):
        lead = Lead.objects.create(account=self.account, name="Lena")
        lead.set_status(Lead.STATUS_WON, value_kr=168000)
        self.assertFalse(ConversionUpload.objects.filter(lead=lead).exists())
        other = Lead.objects.create(account=self.account, name="Per", gclid="x")
        other.set_status(Lead.STATUS_WON)
        self.assertFalse(ConversionUpload.objects.filter(lead=other).exists())
        with self.assertRaises(ValueError):
            other.set_status("vunnen")

    def test_display_name_falls_back(self):
        self.assertEqual(Lead(phone="070-1").display_name, "070-1")
        self.assertEqual(Lead().display_name, "Okänd")


# ---------------------------------------------------------------------------
# Adresserna och behörigheten
# ---------------------------------------------------------------------------


class GateTests(CoreFixture, TestCase):
    def test_every_app_url_works_for_the_contact(self):
        client = self.client_for(self.anna)
        for url in self.app_urls():
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 200)
        for name in ("flamingo:app_campaign_submit", "flamingo:app_campaign_approve"):
            with self.subTest(name=name):
                response = client.post(reverse(name, args=[self.draft.pk]))
                self.assertEqual(response.status_code, 302)

    def test_without_access_every_app_url_is_an_unknown_address(self):
        unknown = Client().get("/finns-inte-alls/")
        urls = self.app_urls() + [
            reverse("flamingo:app_campaign_submit", args=[self.draft.pk]),
            reverse("flamingo:app_customer"),
        ]
        for user in (None, self.cia):
            client = self.client_for(user) if user else Client()
            for url in urls:
                for method in ("get", "post"):
                    with self.subTest(user=user, url=url, method=method):
                        response = getattr(client, method)(url)
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn("Location", response)
                        self.assertEqual(
                            [t.name for t in response.templates][:1],
                            [t.name for t in unknown.templates][:1],
                        )

    def test_another_customers_ids_are_404(self):
        client = self.client_for(self.anna)
        for url in (
            reverse("flamingo:app_campaign", args=[self.secret.pk]),
            reverse("flamingo:app_lead", args=[self.secret_lead.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 404)
        for name in ("flamingo:app_campaign_submit", "flamingo:app_campaign_approve"):
            with self.subTest(name=name):
                self.assertEqual(client.post(reverse(name, args=[self.secret.pk])).status_code, 404)

    def test_staff_without_view_as_gets_the_customer_list(self):
        client = self.client_for(self.staff)
        for url in self.app_urls():
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "flamingo/app/staff_index.html")
        html = client.get(reverse("flamingo:app")).content.decode()
        self.assertIn(reverse("manage:flamingo_view_as", args=[self.acme.pk]), html)
        self.assertIn(reverse("manage:flamingo_view_as", args=[self.other.pk]), html)
        self.assertIn("Visa som kunden", html)
        self.assertNotIn("Sara Holm", html)

    def test_staff_viewing_as_sees_the_customer(self):
        """Giovanni 2026-10-03: "visa som kund" ska visa det kunden ser. Byrån
        i kundvyn har kundens formulär, och det byrån sparar gäller."""
        client = self.client_for(self.staff, view_as=self.acme)
        response = client.get(reverse("flamingo:app"))
        self.assertContains(response, "Lindqvist Rör AB")
        self.assertNotContains(response, "Hemlig")
        self.assertContains(response, "gäller på riktigt")
        url = reverse("flamingo:app_campaign_submit", args=[self.draft.pk])
        response = client.post(url)
        self.assertNotEqual(response.get("Location"), url)

    def test_staff_viewing_as_a_customer_without_flamingo_gets_a_notice(self):
        client = self.client_for(self.staff, view_as=self.plain)
        response = client.get(reverse("flamingo:app"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kunden har inte ADX Flamingo")
        self.assertFalse(FlamingoAccount.objects.filter(customer=self.plain).exists())

    def test_landing_pages_are_always_open_and_never_indexed(self):
        """Giovanni 2026-10-04: varje kampanjs sida är öppen utan inloggning,
        vilket läge kampanjen än har och även när kontot är avstängt. Bara en
        okänd adress är 404."""
        self.assertEqual(Client().get("/lp/finns-inte/").status_code, 404)
        for campaign in (self.live, self.draft):
            with self.subTest(campaign=campaign.name):
                response = Client().get(campaign.landing_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
                self.assertNotContains(response, "Förhandsvisning")
        self.assertContains(Client().get(self.live.landing_url), "Rörjour i Nacka")
        thanks = reverse("flamingo_public:thanks", args=[self.live.page_slug])
        self.assertEqual(Client().get(thanks).status_code, 200)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=False)
        self.assertEqual(Client().get(self.live.landing_url).status_code, 200)

    def test_manage_routes_need_staff(self):
        gets = [
            reverse("manage:flamingo_queue"),
            reverse("manage:flamingo_review", args=[self.live.pk]),
            reverse("manage:flamingo_editor_csv", args=[self.live.pk]),
            reverse("manage:flamingo_conversions_csv"),
        ]
        posts = [
            reverse("manage:flamingo_publish", args=[self.live.pk]),
            reverse("manage:flamingo_google_update", args=[self.acme.pk]),
        ]
        for url in gets + posts:
            with self.subTest(url=url, who="anonym"):
                response = Client().get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/manage/login/", response["Location"])
            with self.subTest(url=url, who="kontakt"):
                response = self.client_for(self.anna).get(url)
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("/manage/", response["Location"])
        staff = self.client_for(self.staff)
        for url in gets:
            with self.subTest(url=url, who="byrån"):
                self.assertEqual(staff.get(url).status_code, 200)
        for url in posts:
            with self.subTest(url=url, who="byrån"):
                self.assertEqual(staff.post(url).status_code, 302)

    def test_nothing_here_sends_mail(self):
        client = self.client_for(self.anna)
        for url in self.app_urls():
            client.get(url)
        client.post(reverse("flamingo:app_campaign_submit", args=[self.draft.pk]))
        self.assertEqual(mail.outbox, [])


# ---------------------------------------------------------------------------
# Kundväljaren
# ---------------------------------------------------------------------------


class ChooserTests(CoreFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second = Customer.objects.create(name="Andra Rör AB")
        FlamingoAccount.objects.create(customer=cls.second, is_enabled=True)
        cls.second.users.add(cls.anna)
        cls.plain.users.add(cls.anna)  # kund utan Flamingo

    def test_the_contact_can_switch_between_own_flamingo_customers(self):
        client = self.client_for(self.anna)
        html = client.get(reverse("flamingo:app")).content.decode()
        self.assertIn('name="customer"', html)
        self.assertIn("Andra Rör AB", html)
        self.assertNotIn("Utan Flamingo AB", html)
        response = client.post(reverse("flamingo:app_customer"), {"customer": self.second.pk})
        self.assertRedirects(response, reverse("flamingo:app"))
        self.assertEqual(client.session[SESSION_KEY], self.second.pk)
        self.assertNotContains(
            client.get(reverse("flamingo:app_lead", args=[self.lead.pk])), "Sara", status_code=404
        )

    def test_foreign_and_non_flamingo_customers_are_refused(self):
        client = self.client_for(self.anna)
        for pk in (self.other.pk, self.plain.pk, "x", ""):
            with self.subTest(pk=pk):
                response = client.post(reverse("flamingo:app_customer"), {"customer": pk})
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(SESSION_KEY, client.session)

    def test_get_is_not_allowed(self):
        self.assertEqual(
            self.client_for(self.anna).get(reverse("flamingo:app_customer")).status_code, 405
        )


# ---------------------------------------------------------------------------
# Översikten
# ---------------------------------------------------------------------------


class OverviewTests(CoreFixture, TestCase):
    def setUp(self):
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(created_at=now - timedelta(hours=5))
        won = Lead.objects.create(
            account=self.account,
            name="Erik Svensson",
            gclid="g",
            created_at=now - timedelta(days=12),
        )
        won.set_status(Lead.STATUS_WON, value_kr=186000)
        Lead.objects.create(
            account=self.account, name="Skräp", status=Lead.STATUS_JUNK, created_at=now
        )
        Campaign.objects.filter(pk=self.draft.pk).update(status=Campaign.STATUS_NEEDS_CUSTOMER)
        # Den andra kundens siffror får aldrig synas.
        big = Lead.objects.create(account=self.other_account, name="Hemlig Affär", gclid="h")
        big.set_status(Lead.STATUS_WON, value_kr=999000)

    def test_the_contact_sees_own_numbers_and_three_things(self):
        response = self.client_for(self.anna).get(reverse("flamingo:app"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Anna", html)  # hälsningen
        self.assertIn("1 affär på 30 dagar.", html)
        self.assertIn("186 000 kr", html)
        self.assertIn("Svara Sara Holm", html)  # utan telefonnummer: svara, inte ring
        self.assertIn("Godkänn Badrum", html)
        self.assertIn("Kopplas när Google-rapporterna är på", html)
        self.assertIn('aria-current="page"', html)
        for secret in ("Hemlig", "999\u00a0000", self.secret.name):
            self.assertNotIn(secret, html)

    def test_numbers_never_count_junk_or_other_customers(self):
        numbers = numbers_for(self.account, timezone.now())
        self.assertEqual(numbers.leads, 2)
        self.assertEqual(numbers.deals, 1)
        self.assertEqual(numbers.deal_value_kr, 186000)
        self.assertIsNone(numbers.spend_kr)
        self.assertIsNone(numbers.kr_per_lead)
        self.assertIsNone(numbers.kr_per_deal)
        self.assertEqual(numbers.cohort_deals, 1)

    def test_the_band_is_hidden_without_leads(self):
        Lead.objects.filter(account=self.account).delete()
        html = self.client_for(self.anna).get(reverse("flamingo:app")).content.decode()
        self.assertNotIn("fl-band__svg", html)
        self.assertIn("Inga förfrågningar än", html)
        self.assertTrue(band_path(10, 2).startswith("M0 4"))

    def test_onboarding_steps_show_until_everything_is_done(self):
        html = self.client_for(self.anna).get(reverse("flamingo:app")).content.decode()
        self.assertIn("fl-steps", html)
        self.assertIn("Första kampanjen", html)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_LINKED,
            scan_status=FlamingoAccount.SCAN_DONE,
        )
        Fact.objects.create(
            account=self.account, key="tel", label="Telefon", value="1", confirmed=True
        )
        html = self.client_for(self.anna).get(reverse("flamingo:app")).content.decode()
        # Kopplat räcker för Google-steget: betalningen stoppar ingen kampanj
        # (beslut 2026-10-03) och står kvar som en påminnelse bland "tre saker".
        self.assertNotIn('class="fl-steps"', html)
        self.account.refresh_from_db()
        keys = [t.key for t in rules.onboarding_things(self.account, None)]
        self.assertIn("google_billing", keys)

    def test_kontakter_is_in_the_menu_only_with_utskick(self):
        """Flamingo 2.0 (apps/utskick, README C.2): Kontakter står efter
        Kampanjer, och bara när byrån aktiverat utskick för kunden."""
        from apps.utskick.testing import enable_utskick

        def nav(html):
            return html.split('<nav class="fl-app-nav"', 1)[1].split("</nav>", 1)[0]

        link = f'href="{reverse("flamingo:app_contacts")}"'
        html = self.client_for(self.anna).get(reverse("flamingo:app")).content.decode()
        self.assertNotIn(link, nav(html))
        enable_utskick(self.account, "core-test", "Testföretaget")
        menu = nav(self.client_for(self.anna).get(reverse("flamingo:app")).content.decode())
        self.assertIn(link, menu)
        self.assertLess(menu.index(f'href="{reverse("flamingo:app_campaigns")}"'), menu.index(link))

    def test_the_inbox_badge_counts_new_leads(self):
        html = self.client_for(self.anna).get(reverse("flamingo:app")).content.decode()
        self.assertIn('class="fl-app-nav__badge" aria-label="1 nya"', html)

    def test_ad_spend_is_the_first_of_six_tiles_and_never_a_guess(self):
        html = self.client_for(self.anna).get(reverse("flamingo:app")).content.decode()
        self.assertEqual(html.count('<p class="fl-kpi__label">'), 6)
        labels = re.findall(r'<p class="fl-kpi__label">([^<]+)</p>', html)
        self.assertEqual(labels[0], "Annonspengar")
        tile = html.split('<p class="fl-kpi__label">Annonspengar</p>', 1)[1].split("</div>", 1)[0]
        self.assertIn("Kopplas när Google-rapporterna är på", tile)
        self.assertNotIn("fl-kpi__value", tile)

    def test_the_tool_button_in_the_header_is_current_on_every_tool_page(self):
        client = self.client_for(self.anna)
        for url in self.app_urls():
            html = client.get(url).content.decode()
            self.assertRegex(
                html, r'class="fl-btn fl-btn--sm" href="[^"]+" aria-current="page">Verktyget<', url
            )
        html = client.get(reverse("flamingo:home")).content.decode()
        self.assertIn(">Verktyget<", html)
        self.assertNotIn('aria-current="page">Verktyget<', html)


# ---------------------------------------------------------------------------
# Reglerna
# ---------------------------------------------------------------------------


class RulesTests(CoreFixture, TestCase):
    def test_at_most_three_things_most_important_first(self):
        now = timezone.now()
        for hours in (3, 4, 30):
            Lead.objects.create(
                account=self.account,
                name=f"V{hours}",
                phone="1",
                created_at=now - timedelta(hours=hours),
            )
        Lead.objects.create(account=self.account, name="Gammal", created_at=now - timedelta(days=9))
        Campaign.objects.filter(pk=self.draft.pk).update(status=Campaign.STATUS_NEEDS_CUSTOMER)
        Fact.objects.create(account=self.account, key="jour", label="Jour", value="Ja")
        things = rules.three_things(self.account, now)
        self.assertEqual(len(things), 3)
        self.assertEqual(
            [t.key for t in things], ["waiting_leads", "campaigns_waiting", "stale_leads"]
        )
        self.assertEqual(things[0].title, "Ring V30")
        self.assertIn("2 förfrågningar till väntar", things[0].text)
        self.assertEqual(
            things[0].url, reverse("flamingo:app_lead", args=[Lead.objects.get(name="V30").pk])
        )
        self.assertEqual(things[2].title, "1 förfrågan saknar status")
        self.assertEqual(rules.things_headline(things), "Tre saker att göra nu.")

    def test_an_approved_campaign_no_longer_asks_for_approval(self):
        """Godkänd står kvar som needs_customer tills ADX publicerat, men
        översikten ska inte be kunden godkänna den igen."""
        Campaign.objects.filter(pk=self.draft.pk).update(status=Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertEqual(rules.campaigns_waiting(self.account, None).title, "Godkänn Badrum")
        Campaign.objects.filter(pk=self.draft.pk).update(approved_at=timezone.now())
        self.assertIsNone(rules.campaigns_waiting(self.account, None))
        keys = [t.key for t in rules.three_things(self.account)]
        self.assertNotIn("campaigns_waiting", keys)

    def test_a_fresh_lead_is_not_waiting_yet(self):
        now = timezone.now()
        Lead.objects.filter(pk=self.lead.pk).update(created_at=now - timedelta(minutes=30))
        self.assertIsNone(rules.waiting_leads(self.account, now))
        Lead.objects.filter(pk=self.lead.pk).update(created_at=now - timedelta(hours=3))
        self.assertEqual(rules.waiting_leads(self.account, now).title, "Svara Sara Holm")

    def test_onboarding_things_fill_up_when_nothing_else_waits(self):
        Lead.objects.filter(account=self.account).delete()
        Campaign.objects.filter(account=self.account).delete()
        Service.objects.filter(account=self.account).delete()
        Fact.objects.create(account=self.account, key="jour", label="Jour", value="Ja")
        things = rules.three_things(self.account)
        self.assertEqual([t.key for t in things], ["proposal", "facts", "google"])
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_ID_GIVEN
        )
        self.account.refresh_from_db()
        self.assertNotIn("google", [t.key for t in rules.three_things(self.account)])
        self.assertEqual(rules.things_headline([]), "Inget väntar på dig just nu.")

    def test_onboarding_steps(self):
        steps = rules.onboarding_for(self.account).steps
        # Tjänster finns, inga uppgifter, Google inte påbörjat, bara ett utkast
        # och en live-kampanj (live räknas som en första kampanj).
        self.assertEqual([s.state for s in steps], ["done", "now", "todo", "done"])
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_REQUESTED_NEW
        )
        self.account.refresh_from_db()
        Fact.objects.create(
            account=self.account, key="tel", label="Telefon", value="1", confirmed=True
        )
        onboarding = rules.onboarding_for(self.account)
        self.assertEqual([s.state for s in onboarding.steps], ["done", "done", "wait", "done"])
        self.assertIsNone(onboarding.next_step)
        self.assertFalse(onboarding.complete)
        self.assertEqual(onboarding.steps[0].url, reverse("flamingo:app_proposal"))

    def test_google_is_done_when_linked_and_billing_stays_a_reminder(self):
        """Kopplat men utan betalning: Google-steget är klart (en kampanj kan
        gå live, beslut 2026-10-03), men betalningen står kvar bland "tre
        saker" tills ADX bockat av den."""
        Fact.objects.create(
            account=self.account, key="tel", label="Telefon", value="1", confirmed=True
        )
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_LINKED
        )
        self.account.refresh_from_db()
        google = rules.onboarding_for(self.account).steps[2]
        self.assertEqual(google.state, "done")
        things = rules.onboarding_things(self.account, None)
        self.assertEqual([t.key for t in things], ["google_billing"])
        self.assertIn("Annonserna visas först", things[0].text)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_BILLING_OK
        )
        self.account.refresh_from_db()
        self.assertEqual(rules.onboarding_for(self.account).steps[2].state, "done")
        self.assertEqual(rules.onboarding_things(self.account, None), [])

    def test_the_other_customer_never_feeds_the_rules(self):
        now = timezone.now()
        Lead.objects.create(
            account=self.other_account, name="X", created_at=now - timedelta(days=3)
        )
        Lead.objects.filter(pk=self.lead.pk).delete()
        keys = [t.key for t in rules.three_things(self.account, now)]
        self.assertNotIn("waiting_leads", keys)

    def test_when_text(self):
        now = timezone.now()
        self.assertTrue(rules.when_text(now, now).startswith("i dag "))
        self.assertTrue(rules.when_text(now - timedelta(days=1), now).startswith("i går "))
        self.assertEqual(rules.count_word(1, "sak", "saker"), "1 sak")
        self.assertEqual(rules.count_word(2, "sak", "saker"), "2 saker")
