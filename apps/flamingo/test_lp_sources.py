"""ADX Flamingo, steg 2 (README, Giovanni 2026-10-28): landningssidornas
egna adresser, källorna och statistiken per sida.

    OwnAddressTests       /lp/<konto>/<sida>/: öppen när sidan är publicerad, 404
                          annars (byrån ser förhandsvisningen), besöket och
                          förfrågan räknas som Direkt, ett klick-id som Annons,
                          aldrig till Google, inga kakor
    CampaignAddressTests  kampanjens /lp/<slug>/ med tack/, ring/ och besok/ som
                          förut: kampanjen, källan Annons
    SourceTests           sms, e-post och namngivna länkar (QR-kod, Instagram)
                          på sidans egen adress och på kampanjens: rätt källa och
                          rätt nyckel, klicket på klick.adx.se hela vägen
    VisitFilterTests      inte räknat: byrån, kundens egen kontakt, bottar,
                          förhämtningar, HEAD, demot, samma besökare samma timme
    StatsTests            Statistik: siffrorna stämmer med besöken och
                          förfrågningarna, skräp räknas inte, gamla rader får
                          sin källa när de läses, platta frågor, perioden
    SlugTests             adressens delar: ur namnet, reserverade ord, ändra före
                          publiceringen, gamla adresser skickar vidare, aldrig
                          en annan kunds, utskickens public_slug
    TenantTests           ett annat kontos sida, adress, token och länk når aldrig hit
    ToolTests             listan, flikarna, Länkar per källa, Ny länk för, väljarna,
                          ta bort, översikten
    MigrationTests        datamigreringen ger gamla förfrågningar sin sida och källa
"""

import importlib
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.utskick import tokens
from apps.utskick.access import ForeignIds, validate_public_slug
from apps.utskick.models import Click, TrackedLink, Utskick
from apps.utskick.testing import UtskickFixture

from . import addresses, limits, sms, sources
from .app_views import pages as page_views
from .models import (
    Campaign,
    ConversionUpload,
    Fact,
    FlamingoAccount,
    LandingPage,
    LandingPageOldSlug,
    Lead,
    PageVisitDay,
    Service,
)

CHROME = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
OTHER_CHROME = CHROME.replace("Chrome/148", "Chrome/147")
LINK_SETTINGS = {
    "UTSKICK_LINK_HOSTS": ["k.adx.se", "klick.adx.se"],
    "UTSKICK_SMS_LINK_BASE": "https://k.adx.se",
    "UTSKICK_EMAIL_LINK_BASE": "https://klick.adx.se",
    "FLAMINGO_LANDING_BASE_URL": "https://adx.se",
}
PHONE = "08-465 004 10"
FORM = {"name": "Mikael Ström", "phone": "070-174 06 50"}


def visitor(ip="203.0.113.10", agent=CHROME):
    client = Client()
    client.defaults.update({"HTTP_USER_AGENT": agent, "HTTP_X_REAL_IP": ip})
    return client


def visits(page, key=None):
    rows = PageVisitDay.objects.filter(page=page)
    if key is not None:
        rows = rows.filter(key=key)
    return sum(rows.values_list("visits", flat=True))


class SourcesFixture(UtskickFixture):
    """Exempelrör (utskick på, public_slug exempelror) med den publicerade
    sidan Avloppsspolning, som kampanjen Avloppsspolning Nacka (live) också
    visar, och en opublicerad sida. Annanfirma har en egen publicerad sida."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for account in (cls.account, cls.other_account):
            Fact.objects.create(
                account=account, key="telefon", label="Telefon", value=PHONE, confirmed=True
            )
        cls.service = Service.objects.create(
            account=cls.account, name="Avloppsspolning", sales_mode=Service.SALES_QUOTE
        )
        blocks = page_views.starter_blocks(cls.account, cls.service)
        now = timezone.now()
        cls.page = LandingPage.objects.create(
            account=cls.account,
            name="Avloppsspolning",
            draft={"blocks": blocks},
            published={"blocks": blocks},
            published_at=now,
        )
        cls.draft_page = LandingPage.objects.create(
            account=cls.account, name="Varmvattenberedare", draft={"blocks": blocks}
        )
        cls.campaign = Campaign.objects.create(
            account=cls.account,
            service=cls.service,
            name="Avloppsspolning Nacka",
            area="Nacka + 10 km",
            status=Campaign.STATUS_LIVE,
            landing_page=cls.page,
        )
        other_service = Service.objects.create(
            account=cls.other_account, name="Spolning", sales_mode=Service.SALES_QUOTE
        )
        other_blocks = page_views.starter_blocks(cls.other_account, other_service)
        cls.other_page = LandingPage.objects.create(
            account=cls.other_account,
            name="Avloppsspolning",
            draft={"blocks": other_blocks},
            published={"blocks": other_blocks},
            published_at=now,
        )

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(sms, "urlopen", side_effect=AssertionError("inget sms"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def own_url(self, page=None):
        return (page or self.page).landing_url

    def click(self, channel, link=None, utskick=None, account=None):
        return Click.objects.create(
            account=account or self.account,
            channel=channel,
            link=link,
            utskick=utskick,
            kind=Click.Kind.HUMAN,
        )

    def utskick(self, name="Höstservice", mode=Utskick.ChannelMode.SMS_ONLY, **kwargs):
        return Utskick.objects.create(
            account=self.account, name=name, channel_mode=mode, status=Utskick.Status.SENT, **kwargs
        )


# ---------------------------------------------------------------------------
# Sidans egen adress
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class OwnAddressTests(SourcesFixture, TestCase):
    def test_the_address_is_account_slug_and_page_slug(self):
        self.assertEqual(
            self.account.__class__.objects.get(pk=self.account.pk).lp_slug, "exempelror"
        )
        self.assertEqual(self.page.slug, "avloppsspolning")
        self.assertEqual(self.own_url(), "/lp/exempelror/avloppsspolning/")
        self.assertEqual(
            addresses.page_url(self.page), "https://adx.se/lp/exempelror/avloppsspolning/"
        )
        self.assertEqual(addresses.page_text(self.page), "adx.se/lp/exempelror/avloppsspolning")

    def test_a_published_page_renders_and_the_visit_counts_as_direct(self):
        response = visitor().get(self.own_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertIsNone(response.context["campaign"])
        self.assertEqual(response.context["which"], "published")
        self.assertEqual(visits(self.page, "direct"), 1)
        row = PageVisitDay.objects.get(page=self.page)
        self.assertEqual((row.source, row.account_id), ("direct", self.account.pk))
        self.assertIsNone(row.campaign_id)

    def test_the_form_creates_a_direct_lead_without_campaign(self):
        client = visitor()
        response = client.post(self.own_url(), FORM)
        self.assertRedirects(
            response,
            reverse("flamingo_public:page_thanks", args=["exempelror", "avloppsspolning"]),
            fetch_redirect_response=False,
        )
        lead = Lead.objects.get()
        self.assertIsNone(lead.campaign_id)
        self.assertIsNone(lead.service_id)
        self.assertEqual(lead.landing_page_id, self.page.pk)
        self.assertEqual(lead.page_source, "direct")
        self.assertEqual(lead.account_id, self.account.pk)
        thanks = client.get(response["Location"])
        self.assertEqual(thanks.status_code, 200)
        self.assertContains(thanks, f'href="{self.own_url()}">Tillbaka till sidan')

    def test_a_click_id_is_an_ad_but_never_goes_to_google(self):
        client = visitor()
        client.get(f"{self.own_url()}?gclid=Cj0abc")
        self.assertEqual(visits(self.page, "ad"), 1)
        client.post(f"{self.own_url()}?gclid=Cj0abc", {**FORM, "gclid": "Cj0abc"})
        lead = Lead.objects.get()
        self.assertEqual((lead.page_source, lead.gclid), ("ad", "Cj0abc"))
        self.assertFalse(lead.can_send_to_google)
        self.assertFalse(ConversionUpload.objects.exists())

    def test_the_call_click_counts_on_a_published_page(self):
        url = reverse("flamingo_public:page_call_click", args=["exempelror", "avloppsspolning"])
        self.assertEqual(visitor().post(url).status_code, 204)
        lead = Lead.objects.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual((lead.landing_page_id, lead.page_source), (self.page.pk, "direct"))
        self.assertIsNone(lead.campaign_id)
        # Samma besökare samma timme räknas en gång, som på kampanjens sida.
        visitor().post(url)
        self.assertEqual(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).count(), 1)

    def test_the_call_click_needs_a_published_page_and_an_active_account(self):
        draft = reverse(
            "flamingo_public:page_call_click", args=["exempelror", "varmvattenberedare"]
        )
        self.assertEqual(visitor().post(draft).status_code, 404)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=False)
        url = reverse("flamingo_public:page_call_click", args=["exempelror", "avloppsspolning"])
        self.assertEqual(visitor().post(url).status_code, 404)
        self.assertFalse(Lead.objects.exists())

    def test_an_unpublished_page_is_404_for_visitors_and_a_preview_for_staff(self):
        url = self.own_url(self.draft_page)
        self.assertEqual(visitor().get(url).status_code, 404)
        self.assertEqual(visitor().post(url, FORM).status_code, 404)
        staff = Client()
        staff.force_login(self.staff)
        response = staff.get(url, HTTP_USER_AGENT=CHROME)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["preview"])
        self.assertContains(response, "Sidan är inte publicerad, så den egna adressen")
        staff.post(url, FORM)
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(PageVisitDay.objects.exists())

    def test_the_page_stays_open_when_the_account_is_switched_off(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_enabled=False)
        self.assertEqual(visitor().get(self.own_url()).status_code, 200)

    def test_no_cookies_but_the_forms_csrf_key(self):
        from apps.analytics.models import PageView

        click = self.click("sms", utskick=self.utskick())
        for url in (self.own_url(), f"{self.own_url()}?ut={tokens.ut_token(click.pk)}&gclid=x1"):
            with self.subTest(url=url):
                response = visitor().get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(sorted(set(response.cookies) - {"csrftoken"}), [])
        self.assertFalse(PageView.objects.exists())
        beacon = reverse(
            "flamingo_public:page_visit_beacon", args=["exempelror", "avloppsspolning"]
        )
        response = visitor().post(beacon, {"ut": tokens.ut_token(click.pk), "s": "40"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.cookies, {})

    def test_the_time_on_the_page_is_recorded_on_the_own_address(self):
        click = self.click("sms", utskick=self.utskick())
        ut = tokens.ut_token(click.pk)
        html = visitor().get(f"{self.own_url()}?ut={ut}").content.decode()
        beacon = reverse(
            "flamingo_public:page_visit_beacon", args=["exempelror", "avloppsspolning"]
        )
        self.assertIn(f'data-fl-visit="{beacon}"', html)
        visitor().post(beacon, {"ut": ut, "s": "40"})
        click.refresh_from_db()
        self.assertEqual((click.lp_visits, click.engaged_seconds), (1, 40))


# ---------------------------------------------------------------------------
# Kampanjens adress
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class CampaignAddressTests(SourcesFixture, TestCase):
    def test_the_campaign_address_is_unchanged_and_counts_as_an_ad(self):
        self.assertEqual(self.campaign.landing_url, f"/lp/{self.campaign.page_slug}/")
        response = visitor().get(self.campaign.landing_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["campaign"], self.campaign)
        self.assertEqual(visits(self.page, f"ad:c{self.campaign.pk}"), 1)
        response = visitor().post(self.campaign.landing_url, {**FORM, "gclid": "Cj0kampanj"})
        self.assertRedirects(
            response,
            reverse("flamingo_public:thanks", args=[self.campaign.page_slug]),
            fetch_redirect_response=False,
        )
        lead = Lead.objects.get()
        self.assertEqual(lead.campaign_id, self.campaign.pk)
        self.assertEqual(lead.service_id, self.service.pk)
        self.assertEqual((lead.landing_page_id, lead.page_source), (self.page.pk, "ad"))
        # Kampanjens förfrågningar går till Google som förut.
        self.assertTrue(lead.can_send_to_google)
        self.assertTrue(ConversionUpload.objects.filter(lead=lead).exists())

    def test_thanks_ring_and_besok_answer_as_before(self):
        slug = self.campaign.page_slug
        self.assertEqual(visitor().get(f"/lp/{slug}/tack/").status_code, 200)
        self.assertEqual(visitor().post(f"/lp/{slug}/ring/").status_code, 204)
        lead = Lead.objects.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual((lead.campaign_id, lead.page_source), (self.campaign.pk, "ad"))
        self.assertEqual(visitor().post(f"/lp/{slug}/besok/", {"s": "5"}).status_code, 204)
        self.assertEqual(visitor().get("/lp/finns-inte/").status_code, 404)

    def test_a_page_slug_never_shadows_the_campaigns_sub_paths(self):
        self.assertEqual(addresses.page_slug_for(self.account.pk, "Tack"), "tack-2")
        for word in ("tack", "ring", "besok"):
            with self.subTest(word=word):
                page = LandingPage.objects.create(account=self.account, name=word.title())
                self.assertNotEqual(page.slug, word)


# ---------------------------------------------------------------------------
# Källorna: sms, e-post och namngivna länkar
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class SourceTests(SourcesFixture, TestCase):
    def visit_and_ask(self, click, url=None):
        ut = tokens.ut_token(click.pk)
        url = url or self.own_url()
        client = visitor()
        self.assertEqual(client.get(f"{url}?ut={ut}").status_code, 200)
        client.post(f"{url}?ut={ut}", {**FORM, "ut": ut})
        return Lead.objects.order_by("-pk").first()

    def test_sms_on_the_own_address(self):
        utskick = self.utskick()
        lead = self.visit_and_ask(self.click("sms", utskick=utskick))
        self.assertEqual(visits(self.page, f"sms:u{utskick.pk}"), 1)
        self.assertEqual((lead.page_source, lead.utskick_id), ("sms", utskick.pk))
        self.assertIsNone(lead.campaign_id)
        self.assertFalse(lead.can_send_to_google)

    def test_email_on_the_own_address(self):
        utskick = self.utskick("Vinterbrevet", Utskick.ChannelMode.EMAIL_ONLY)
        lead = self.visit_and_ask(self.click("email", utskick=utskick))
        self.assertEqual(visits(self.page, f"email:u{utskick.pk}"), 1)
        self.assertEqual((lead.page_source, lead.utskick_id), ("email", utskick.pk))

    def test_a_named_link_on_the_own_address(self):
        link = TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            landing_page=self.page,
            destination=addresses.page_url(self.page),
            label="Affisch i verkstaden",
            slug="affisch",
            medium=TrackedLink.Medium.QR,
        )
        lead = self.visit_and_ask(self.click("named", link=link))
        self.assertEqual(visits(self.page, f"named:l{link.pk}"), 1)
        self.assertEqual((lead.page_source, lead.named_link_id), ("named", link.pk))
        self.assertIsNone(lead.utskick_id)
        row = next(r for r in sources.page_stats(self.page).rows if r.source == "named")
        self.assertEqual(
            (row.chip, row.text, row.visits, row.leads), ("QR-kod", "Affisch i verkstaden", 1, 1)
        )

    def test_sms_through_the_campaigns_address_is_still_sms(self):
        utskick = self.utskick()
        lead = self.visit_and_ask(self.click("sms", utskick=utskick), url=self.campaign.landing_url)
        self.assertEqual(visits(self.page, f"sms:u{utskick.pk}"), 1)
        self.assertEqual(visits(self.page, f"ad:c{self.campaign.pk}"), 0)
        self.assertEqual((lead.campaign_id, lead.page_source), (self.campaign.pk, "sms"))

    def test_the_click_on_klick_adx_se_goes_to_the_own_address_with_ut(self):
        link = TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            landing_page=self.page,
            destination=addresses.page_url(self.page),
            label="Instagram-profilen",
            slug="insta",
            medium=TrackedLink.Medium.INSTAGRAM,
        )
        response = Client().get(
            "/exempelror/insta", HTTP_HOST="klick.adx.se", HTTP_USER_AGENT=CHROME
        )
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        self.assertTrue(location.startswith("https://adx.se/lp/exempelror/avloppsspolning/"))
        click = Click.objects.get(link=link)
        self.assertEqual(parse_qs(urlsplit(location).query)["ut"], [tokens.ut_token(click.pk)])
        lead = self.visit_and_ask(click)
        self.assertEqual(lead.named_link_id, link.pk)
        row = next(r for r in sources.page_stats(self.page).rows if r.source == "named")
        self.assertEqual(row.chip, "Instagram")

    def test_a_link_follows_the_page_when_its_address_changes(self):
        from apps.utskick import links

        page = self.draft_page
        link = TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            landing_page=page,
            destination=addresses.page_url(page),
            slug="beredare",
            label="Beredare",
        )
        addresses.change_page_slug(page, "beredare")
        link.refresh_from_db()
        self.assertEqual(links.bare_destination(link), "https://adx.se/lp/exempelror/beredare/")

    def test_an_utskick_link_straight_to_a_page(self):
        from apps.utskick import links

        utskick = Utskick.objects.create(account=self.account, name="Utkast")
        link = links.add_link(utskick, key="spolning", page=self.page, label="Spolning")
        self.assertEqual((link.landing_page_id, link.campaign_id), (self.page.pk, None))
        self.assertEqual(link.destination, "https://adx.se/lp/exempelror/avloppsspolning/")
        click = self.click("sms", link=link, utskick=utskick)
        url = links.build_destination(link, click=click)
        self.assertTrue(url.startswith("https://adx.se/lp/exempelror/avloppsspolning/?ut="))
        other = Utskick.objects.create(account=self.other_account, name="Annan")
        with self.assertRaises(links.LinkRefused):
            links.add_link(other, key="x", page=self.page)

    def test_an_email_link_to_the_own_address_becomes_a_page_link(self):
        from apps.utskick.email import blocks as email_blocks
        from apps.utskick.sending import email as sending_email

        pages = sending_email.own_pages(self.account)
        found = email_blocks.own_page(
            self.account, "https://adx.se/lp/exempelror/avloppsspolning/#boka", pages=pages
        )
        self.assertEqual(found, self.page)
        self.assertIsNone(
            email_blocks.own_page(self.account, "https://adx.se/lp/exempelror/varmvattenberedare/")
        )


# ---------------------------------------------------------------------------
# Vilka besök som räknas
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class VisitFilterTests(SourcesFixture, TestCase):
    def test_the_same_visitor_counts_once_an_hour(self):
        visitor().get(self.own_url())
        visitor().get(self.own_url())
        visitor(ip="203.0.113.11").get(self.own_url())
        self.assertEqual(visits(self.page), 2)

    def test_staff_and_the_customers_own_contact_are_not_counted(self):
        for user in (self.staff, self.anna):
            client = visitor()
            client.force_login(user)
            self.assertEqual(client.get(self.own_url()).status_code, 200)
            self.assertEqual(client.get(self.campaign.landing_url).status_code, 200)
        self.assertEqual(visits(self.page), 0)

    def test_bots_previews_prefetch_and_head_are_not_counted(self):
        visitor(agent="").get(self.own_url())
        visitor(agent="Googlebot/2.1 (+http://www.google.com/bot.html)").get(self.own_url())
        visitor(agent="WhatsApp/2.23.20.0").get(self.own_url())
        visitor().get(self.own_url(), HTTP_SEC_PURPOSE="prefetch")
        visitor().get(self.own_url(), HTTP_PURPOSE="prefetch")
        visitor().get(self.own_url(), HTTP_SEC_FETCH_DEST="iframe")
        visitor().head(self.own_url())
        self.assertEqual(visits(self.page), 0)
        visitor().get(self.own_url(), HTTP_SEC_FETCH_DEST="document")
        self.assertEqual(visits(self.page), 1)

    def test_the_demo_is_never_counted(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        visitor().get(self.own_url())
        self.assertEqual(visits(self.page), 0)


# ---------------------------------------------------------------------------
# Statistik
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class StatsTests(SourcesFixture, TestCase):
    def test_the_numbers_match_the_visits_and_the_leads(self):
        utskick = self.utskick()
        sms_click = self.click("sms", utskick=utskick)
        ut = tokens.ut_token(sms_click.pk)
        for n in range(3):
            visitor(ip=f"203.0.113.{20 + n}").get(self.campaign.landing_url)
        for n in range(4):
            visitor(ip=f"203.0.113.{30 + n}").get(self.own_url())
        visitor(ip="203.0.113.40").get(f"{self.own_url()}?ut={ut}")
        visitor(ip="203.0.113.20").post(self.campaign.landing_url, FORM)
        visitor(ip="203.0.113.30").post(self.own_url(), FORM)
        visitor(ip="203.0.113.40").post(f"{self.own_url()}?ut={ut}", {**FORM, "ut": ut})
        junk = visitor(ip="203.0.113.41")
        junk.post(self.own_url(), FORM)
        Lead.objects.filter(ip_hash=limits.ip_hash("203.0.113.41")).update(status=Lead.STATUS_JUNK)

        stats = sources.page_stats(self.page)
        rows = {row.key: row for row in stats.rows}
        self.assertEqual([row.source for row in stats.rows], ["ad", "sms", "direct"])
        self.assertEqual(
            (rows[f"ad:c{self.campaign.pk}"].visits, rows[f"ad:c{self.campaign.pk}"].leads), (3, 1)
        )
        self.assertEqual(rows[f"ad:c{self.campaign.pk}"].text, "Avloppsspolning Nacka")
        self.assertEqual(
            (rows[f"sms:u{utskick.pk}"].visits, rows[f"sms:u{utskick.pk}"].leads), (1, 1)
        )
        self.assertEqual(rows[f"sms:u{utskick.pk}"].text, "Höstservice")
        self.assertEqual((rows["direct"].visits, rows["direct"].leads), (4, 1))
        self.assertEqual((stats.visits, stats.leads), (8, 3))
        self.assertEqual(stats.share, 37.5)

        client = self.client_for(self.anna)
        response = client.get(reverse("flamingo:app_page_stats", args=[self.page.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("<thead>", html)
        self.assertIn('<td class="is-num" data-label="Besök">4</td>', html)
        self.assertIn('<p class="fl-kpi__value">8</p>', html)
        self.assertIn("38\u00a0%", html)
        self.assertIn("Var besökarna kom ifrån", html)
        numbers = sources.list_numbers(self.account)[self.page.pk]
        self.assertEqual((numbers.visits, numbers.leads, numbers.sources), (8, 3, 3))

    def test_rows_from_the_previous_release_get_their_source_when_read(self):
        """README för utskick B.0: en förfrågan som den förra versionen skrev
        under deployen saknar sida och källa."""
        Lead.objects.create(account=self.account, campaign=self.campaign, name="Gammal")
        utskick = self.utskick()
        Lead.objects.create(
            account=self.account,
            campaign=self.campaign,
            utskick=utskick,
            attribution={"channel": "sms", "utskick": utskick.pk},
            name="Gammalt sms",
        )
        rows = {row.key: row.leads for row in sources.page_stats(self.page).rows}
        self.assertEqual(rows, {f"ad:c{self.campaign.pk}": 1, f"sms:u{utskick.pk}": 1})

    def test_replies_and_other_pages_are_not_counted(self):
        Lead.objects.create(account=self.account, source=Lead.SOURCE_REPLY, name="Svar")
        Lead.objects.create(
            account=self.account, landing_page=self.draft_page, page_source="direct", name="Annan"
        )
        Lead.objects.create(
            account=self.other_account, landing_page=self.other_page, page_source="direct"
        )
        self.assertEqual(sources.page_stats(self.page).leads, 0)

    def test_the_period(self):
        old = timezone.now() - timedelta(days=40)
        Lead.objects.create(
            account=self.account, landing_page=self.page, page_source="direct", created_at=old
        )
        PageVisitDay.objects.create(
            account=self.account,
            page=self.page,
            day=limits.stockholm_today() - timedelta(days=40),
            source="direct",
            key="direct",
            visits=7,
        )
        self.assertEqual(
            (sources.page_stats(self.page, 30).visits, sources.page_stats(self.page, 30).leads),
            (0, 0),
        )
        self.assertEqual(
            (sources.page_stats(self.page, 90).visits, sources.page_stats(self.page, 90).leads),
            (7, 1),
        )
        self.assertEqual(sources.clean_days("7"), 7)
        self.assertEqual(sources.clean_days("365"), 30)
        response = self.client_for(self.anna).get(
            reverse("flamingo:app_page_stats", args=[self.page.pk]) + "?dagar=90"
        )
        self.assertContains(response, 'aria-current="page">90 dagar</a>')
        self.assertContains(response, "Besök, 90 dagar")

    def test_the_queries_are_flat(self):
        def count(fn):
            with CaptureQueriesContext(connection) as ctx:
                fn()
            return len(ctx.captured_queries)

        stats_before = count(lambda: sources.page_stats(self.page))
        list_before = count(lambda: sources.list_numbers(self.account))
        for n in range(5):
            utskick = self.utskick(f"Utskick {n}")
            click = self.click("sms", utskick=utskick)
            ut = tokens.ut_token(click.pk)
            visitor(ip=f"198.51.100.{n}").get(f"{self.own_url()}?ut={ut}")
            visitor(ip=f"198.51.100.{n}").post(f"{self.own_url()}?ut={ut}", {**FORM, "ut": ut})
            LandingPage.objects.create(account=self.account, name=f"Sida {n}")
        self.assertEqual(len(sources.page_stats(self.page).rows), 5)
        self.assertEqual(count(lambda: sources.page_stats(self.page)), stats_before)
        self.assertEqual(count(lambda: sources.list_numbers(self.account)), list_before)
        self.assertEqual(stats_before, 3)
        self.assertEqual(list_before, 2)


# ---------------------------------------------------------------------------
# Adresserna
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class SlugTests(SourcesFixture, TestCase):
    def test_slugs_come_from_the_name_and_are_unique_per_account(self):
        again = LandingPage.objects.create(account=self.account, name="Avloppsspolning (2)")
        self.assertEqual(again.slug, "avloppsspolning-2")
        same = LandingPage.objects.create(account=self.account, name="Avloppsspolning")
        self.assertEqual(same.slug, "avloppsspolning-3")
        # Annanfirma har samma sidnamn med samma del: kontona skiljer dem åt.
        self.assertEqual(self.other_page.slug, "avloppsspolning")
        self.assertEqual(self.other_page.landing_url, "/lp/annanfirma/avloppsspolning/")

    def test_change_before_publishing_and_the_old_address_redirects(self):
        page = self.draft_page
        old = page.landing_url
        addresses.change_page_slug(page, "beredare")
        self.assertEqual(page.landing_url, "/lp/exempelror/beredare/")
        LandingPage.objects.filter(pk=page.pk).update(published_at=timezone.now())
        response = visitor().get(f"{old}?ut=abc&gclid=Cj0")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/lp/exempelror/beredare/?ut=abc&gclid=Cj0")
        self.assertEqual(visitor().get(f"{old}tack/").status_code, 302)
        self.assertEqual(visitor().post(old, FORM).status_code, 404)
        self.assertFalse(PageVisitDay.objects.exists())
        # Efter publiceringen ändras adressen aldrig.
        page.refresh_from_db()
        with self.assertRaises(addresses.SlugRefused):
            addresses.change_page_slug(page, "ny-adress")

    def test_an_old_address_belongs_to_its_page(self):
        page = self.draft_page
        addresses.change_page_slug(page, "beredare")
        other = LandingPage.objects.create(account=self.account, name="Varmvattenberedare")
        self.assertEqual(other.slug, "varmvattenberedare-2")
        with self.assertRaises(addresses.SlugRefused):
            addresses.change_page_slug(other, "varmvattenberedare")
        # Sidan själv får ta tillbaka sin gamla adress.
        addresses.change_page_slug(page, "varmvattenberedare")
        self.assertFalse(LandingPageOldSlug.objects.filter(slug="varmvattenberedare").exists())
        self.assertTrue(LandingPageOldSlug.objects.filter(slug="beredare", page=page).exists())

    def test_the_slug_rules(self):
        page = self.draft_page
        for raw, text in (
            ("", addresses.SLUG_EMPTY_TEXT),
            ("Stora Bokstäver", addresses.SLUG_FORMAT_TEXT),
            ("a/b", addresses.SLUG_FORMAT_TEXT),
            ("ring", addresses.SLUG_RESERVED_TEXT),
            ("avloppsspolning", addresses.SLUG_TAKEN_TEXT),
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesMessage(addresses.SlugRefused, text):
                    addresses.change_page_slug(page, raw)

    def test_reserved_account_slugs(self):
        from apps.projects.models import Customer

        for name in ("Admin AB", "Klick AB", "Tokenbolaget AB"):
            with self.subTest(name=name):
                customer = Customer.objects.create(name=name)
                account = FlamingoAccount.objects.create(customer=customer)
                slug = addresses.ensure_account_slug(account)
                self.assertNotIn(slug, ("admin", "klick"))
                self.assertFalse(slug.startswith("token"))

    def test_the_account_slug_is_never_another_customers(self):
        from apps.projects.models import Customer

        customer = Customer.objects.create(name="Exempelrör AB")
        account = FlamingoAccount.objects.create(customer=customer)
        self.assertEqual(addresses.ensure_account_slug(account), "exempelror-2")
        # Utskickens adress får inte bli ett annat kontos landningssidor.
        with self.assertRaises(Exception):
            validate_public_slug("exempelror-2", account_id=self.account.pk)

    def test_the_utskick_suggestion_is_the_accounts_own_lp_slug(self):
        from apps.projects.models import Customer
        from apps.utskick.access import suggest_public_slug

        customer = Customer.objects.create(name="Rörfirman Syd AB")
        account = FlamingoAccount.objects.create(customer=customer)
        LandingPage.objects.create(account=account, name="Jour")
        account.refresh_from_db()
        self.assertEqual(account.lp_slug, "rorfirman-syd")
        self.assertEqual(suggest_public_slug("Något annat", account_id=account.pk), "rorfirman-syd")

    def test_the_form_changes_the_address_and_returns(self):
        client = self.client_for(self.anna)
        stats = reverse("flamingo:app_page_stats", args=[self.draft_page.pk])
        response = client.post(
            reverse("flamingo:app_page_address", args=[self.draft_page.pk]),
            {"adress": "beredare", "next": stats},
        )
        self.assertRedirects(response, stats, fetch_redirect_response=False)
        self.draft_page.refresh_from_db()
        self.assertEqual(self.draft_page.slug, "beredare")
        response = client.post(
            reverse("flamingo:app_page_address", args=[self.page.pk]),
            {"adress": "nytt", "next": "https://evil.example/"},
            follow=True,
        )
        self.assertContains(response, "Adressen ändrades inte. Sidan är publicerad")
        self.page.refresh_from_db()
        self.assertEqual(self.page.slug, "avloppsspolning")


# ---------------------------------------------------------------------------
# Ett konto i taget
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class TenantTests(SourcesFixture, TestCase):
    def test_another_accounts_page_never_resolves_under_this_account(self):
        LandingPage.objects.filter(pk=self.other_page.pk).update(slug="hemlig")
        self.assertEqual(visitor().get("/lp/exempelror/hemlig/").status_code, 404)
        self.assertEqual(visitor().get("/lp/annanfirma/hemlig/").status_code, 200)
        LandingPageOldSlug.objects.create(
            account=self.other_account, page=self.other_page, slug="gammal"
        )
        self.assertEqual(visitor().get("/lp/exempelror/gammal/").status_code, 404)
        self.assertEqual(visitor().get("/lp/annanfirma/gammal/").status_code, 302)

    def test_another_accounts_token_gives_nothing(self):
        other = Utskick.objects.create(account=self.other_account, name="Annan")
        click = self.click("sms", utskick=other, account=self.other_account)
        ut = tokens.ut_token(click.pk)
        visitor().get(f"{self.own_url()}?ut={ut}")
        visitor().post(f"{self.own_url()}?ut={ut}", {**FORM, "ut": ut})
        self.assertEqual(visits(self.page, "direct"), 1)
        lead = Lead.objects.get()
        self.assertEqual((lead.page_source, lead.utskick_id), ("direct", None))

    def test_the_tool_never_shows_another_accounts_page(self):
        client = self.client_for(self.anna)
        for name in (
            "flamingo:app_page_stats",
            "flamingo:app_page_links",
            "flamingo:app_page_answers",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    client.get(reverse(name, args=[self.other_page.pk])).status_code, 404
                )
        response = client.post(
            reverse("flamingo:app_page_address", args=[self.other_page.pk]), {"adress": "x"}
        )
        self.assertEqual(response.status_code, 404)

    def test_the_pickers_refuse_another_accounts_page(self):
        from apps.utskick.app_views import links as link_views

        with self.assertRaises(ForeignIds):
            link_views.resolve_target(self.account, f"sida-{self.other_page.pk}")
        self.assertEqual(
            link_views.resolve_target(self.account, f"sida-{self.draft_page.pk}"), (None, None)
        )
        response = self.client_for(self.anna).post(
            reverse("flamingo:app_link_new"),
            {
                "beskrivning": "Fel",
                "mal": "lp",
                "kampanj": f"sida-{self.other_page.pk}",
                "typ": "qr",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TrackedLink.objects.filter(label="Fel").exists())


# ---------------------------------------------------------------------------
# Verktyget
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class ToolTests(SourcesFixture, TestCase):
    def test_the_list_shows_the_address_numbers_and_buttons(self):
        visitor().get(self.own_url())
        visitor().post(self.own_url(), FORM)
        html = self.client_for(self.anna).get(reverse("flamingo:app_pages")).content.decode()
        self.assertIn('<p class="fl-pg-rowaddr">adx.se/lp/exempelror/avloppsspolning</p>', html)
        self.assertIn("1 besök", html)
        self.assertIn("1 förfrågan", html)
        self.assertIn("1 källa", html)
        self.assertIn("Inte publicerad än", html)
        self.assertIn(f'href="{reverse("flamingo:app_page_stats", args=[self.page.pk])}"', html)

    def test_the_tabs_of_a_page(self):
        client = self.client_for(self.anna)
        for name, key in (
            ("flamingo:app_page_stats", "stats"),
            ("flamingo:app_page_links", "links"),
            ("flamingo:app_page_answers", "answers"),
        ):
            with self.subTest(name=name):
                response = client.get(reverse(name, args=[self.page.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["app_active"], "pages")
                self.assertEqual(response.context["app_tab"], key)
                labels = [t["label"] for t in response.context["app_tabs"]]
                self.assertEqual(labels, ["Statistik", "Länkar per källa", "Svar i formuläret"])
                html = response.content.decode()
                self.assertIn('class="fl-tabs fl-app-tabs"', html)
                self.assertIn(
                    f'href="{reverse("flamingo:app_page", args=[self.page.pk])}">Redigera', html
                )
                self.assertIn('data-ln-copy="https://adx.se/lp/exempelror/avloppsspolning/"', html)
                self.assertNotIn("Ändra adressen", html)
        draft = client.get(reverse("flamingo:app_page_stats", args=[self.draft_page.pk]))
        self.assertContains(draft, "Ändra adressen")
        self.assertContains(draft, "Inte publicerad än.")

    def test_links_per_source(self):
        utskick = self.utskick()
        from apps.utskick.models import LinkCode

        tracked = TrackedLink.objects.create(
            account=self.account,
            utskick=utskick,
            kind=TrackedLink.Kind.LP,
            landing_page=self.page,
            key="spolning",
            destination=addresses.page_url(self.page),
        )
        LinkCode.objects.create(
            code="7howOG",
            kind=LinkCode.Kind.LINK,
            account=self.account,
            value_hash="x",
            link=tracked,
        )
        TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            campaign=self.campaign,
            destination="https://adx.se/lp/x/",
            label="Affisch i verkstaden",
            slug="affisch",
            medium=TrackedLink.Medium.QR,
        )
        response = self.client_for(self.anna).get(
            reverse("flamingo:app_page_links", args=[self.page.pk])
        )
        html = response.content.decode()
        self.assertIn("Sidans egen adress", html)
        self.assertIn("Google Ads, kampanjen", html)
        self.assertIn(f"adx.se/lp/{self.campaign.page_slug}", html)
        self.assertIn("Skapas av annonsen", html)
        self.assertIn("k.adx.se/7howOG (personlig per mottagare)", html)
        self.assertIn("Höstservice", html)
        self.assertIn(">QR-kod</span> <b>", html)
        self.assertIn("klick.adx.se/exempelror/affisch", html)
        self.assertIn("Ny länk för:", html)
        for text in (">Sms</button>", ">QR-kod</a>", ">Instagram eller annat</a>"):
            self.assertIn(text, html)
        self.assertIn(f"?sida={self.page.pk}&amp;typ=qr", html)

    def test_without_utskick_the_link_buttons_say_why(self):
        self.settings.is_enabled = False
        self.settings.save()
        response = self.client_for(self.anna).get(
            reverse("flamingo:app_page_links", args=[self.page.pk])
        )
        self.assertContains(
            response, "Länkar för sms, e-post, QR-koder och Instagram finns med Sms och e-post."
        )
        self.assertNotContains(response, "Ny länk för:")

    def test_new_sms_from_the_page_has_the_link_in_the_text(self):
        response = self.client_for(self.anna).post(
            reverse("flamingo:app_utskick_new"), {"kanal": "sms", "sida": self.page.pk}
        )
        self.assertEqual(response.status_code, 302)
        utskick = Utskick.objects.get(account=self.account)
        link = TrackedLink.objects.get(utskick=utskick)
        self.assertEqual((link.landing_page_id, link.key), (self.page.pk, "avloppsspolning"))
        self.assertEqual(utskick.sms_body, "{länk:avloppsspolning}")
        # En annan kunds sida ignoreras: utkastet blir som vanligt.
        self.client_for(self.anna).post(
            reverse("flamingo:app_utskick_new"), {"kanal": "sms", "sida": self.other_page.pk}
        )
        newest = Utskick.objects.filter(account=self.account).order_by("-pk").first()
        self.assertEqual((newest.sms_body, newest.links.count()), ("", 0))

    def test_new_email_from_the_page_has_a_button_to_it(self):
        self.client_for(self.anna).post(
            reverse("flamingo:app_utskick_new"), {"kanal": "e-post", "sida": self.page.pk}
        )
        utskick = Utskick.objects.get(account=self.account)
        blocks = utskick.email_doc["blocks"]
        self.assertEqual(blocks[0]["type"], "button")
        fields = next(v for v in blocks[0]["versions"] if v["id"] == blocks[0]["active"])["fields"]
        self.assertEqual(fields["primary_url"], "https://adx.se/lp/exempelror/avloppsspolning/")

    def test_a_named_link_straight_to_the_page(self):
        client = self.client_for(self.anna)
        form = client.get(reverse("flamingo:app_link_new") + f"?sida={self.page.pk}&typ=qr")
        html = form.content.decode()
        self.assertIn(
            f'<option value="sida-{self.page.pk}" selected>Avloppsspolning</option>', html
        )
        self.assertIn('name="typ" value="qr" checked', html)
        self.assertIn('<optgroup label="Landningssidor">', html)
        self.assertIn('<optgroup label="Kampanjernas adresser">', html)
        response = client.post(
            reverse("flamingo:app_link_new"),
            {
                "beskrivning": "Affisch",
                "mal": "lp",
                "kampanj": f"sida-{self.page.pk}",
                "typ": "qr",
                "slug": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        link = TrackedLink.objects.get(label="Affisch")
        self.assertEqual(
            (link.landing_page_id, link.campaign_id, link.medium), (self.page.pk, None, "qr")
        )
        self.assertEqual(link.destination, "https://adx.se/lp/exempelror/avloppsspolning/")

    def test_a_page_with_a_named_link_cannot_be_deleted(self):
        page = LandingPage.objects.create(
            account=self.account, name="Affischsidan", published_at=timezone.now()
        )
        TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            landing_page=page,
            destination=addresses.page_url(page),
            label="Affisch i verkstaden",
            slug="affisch",
        )
        response = self.client_for(self.anna).post(
            reverse("flamingo:app_page_delete", args=[page.pk]), follow=True
        )
        self.assertContains(response, "har länken Affisch i verkstaden och kan inte tas bort")
        self.assertTrue(LandingPage.objects.filter(pk=page.pk).exists())

    def test_own_address_leads_are_not_googles_on_the_overview(self):
        from .app_views.overview import numbers_for

        visitor().post(self.own_url(), {**FORM, "gclid": "Cj0x"})
        visitor(ip="203.0.113.99").post(self.campaign.landing_url, FORM)
        numbers = numbers_for(self.account, timezone.now())
        self.assertEqual((numbers.leads, numbers.google_leads), (2, 1))


# ---------------------------------------------------------------------------
# Migreringen
# ---------------------------------------------------------------------------


class MigrationTests(SourcesFixture, TestCase):
    def test_old_leads_get_their_page_and_source_and_it_runs_again(self):
        from django.apps import apps as django_apps

        utskick = self.utskick()
        link = TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            campaign=self.campaign,
            destination="https://adx.se/lp/x/",
            label="Affisch",
            slug="affisch",
        )
        ad = Lead.objects.create(account=self.account, campaign=self.campaign)
        via_sms = Lead.objects.create(
            account=self.account,
            campaign=self.campaign,
            utskick=utskick,
            attribution={"channel": "sms"},
        )
        named = Lead.objects.create(
            account=self.account,
            campaign=self.campaign,
            attribution={"channel": "named", "link": link.pk},
        )
        manual = Lead.objects.create(account=self.account, source=Lead.SOURCE_MANUAL)
        LandingPage.objects.filter(pk=self.draft_page.pk).update(slug="")
        migration = importlib.import_module("apps.flamingo.migrations.0019_egna_adresser")
        for _run in range(2):
            migration.forwards(django_apps, None)
        for lead, source in ((ad, "ad"), (via_sms, "sms"), (named, "named")):
            with self.subTest(source=source):
                lead.refresh_from_db()
                self.assertEqual((lead.landing_page_id, lead.page_source), (self.page.pk, source))
        named.refresh_from_db()
        self.assertEqual(named.named_link_id, link.pk)
        manual.refresh_from_db()
        self.assertEqual((manual.landing_page_id, manual.page_source), (None, ""))
        self.draft_page.refresh_from_db()
        self.assertEqual(self.draft_page.slug, "varmvattenberedare")
