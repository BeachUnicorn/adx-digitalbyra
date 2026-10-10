"""ADX Flamingo: sidornas adresser i verktyget (Giovanni 2026-10-10, "Good
build the landing page urls").

    PageListLinkTests     listan: "Öppna sidan" till kampanjens /lp/<slug>/,
                          en länk per kampanj på en delad sida, "Förhandsvisa"
                          för en sida utan kampanj
    EditorAddressTests    sidbyggaren: "Visa sidan", adressen och Kopiera
    CampaignStatusLinkTests  samma adress i varje läge (utkast, granskas,
                          väntar på kunden, pausad): sidan är alltid öppen
    PagePreviewTests      förhandsvisningen: bara inloggad, bara kundens egen
                          sida (också för byrån i kundvyn), noindex, utkastet,
                          formuläret skapar ingenting (inte heller fällan för
                          robotar), HEAD
    DemoPageUrlTests      demokontot: listan, sidbyggaren och förhandsvisningen
    OwnVisitTests         kundens egna besök räknas inte (klick på numret,
                          besök och tid från ett utskick, också på en pausad
                          kampanj); en kontakt hos en annan kund räknas, och
                          formuläret fungerar för alla
"""

import copy
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import pagebuilder, sms
from .app_views import pages as page_views
from .models import Campaign, Fact, FlamingoAccount, LandingPage, Lead, Service
from .test_demo import DemoFixture

User = get_user_model()
PHONE = "08-000 00 00"
LANDING_BASE = {"FLAMINGO_LANDING_BASE_URL": "https://adx.se"}


def _hero(blocks):
    return next(b for b in blocks if b["type"] == "hero")


class PageUrlFixture:
    """CS Auto med en sida som en live-kampanj visar, en delad sida (två
    kampanjer) och en sida utan kampanj, vars utkast skiljer sig från den
    publicerade versionen. Annan Bygg är en annan kund med egen kontakt och
    egen sida."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(name="CS Auto AB")
        cls.kund = User.objects.create_user("kund@csauto.se", email="kund@csauto.se", password="x")
        cls.customer.users.add(cls.kund)
        cls.account = FlamingoAccount.objects.create(customer=cls.customer, is_enabled=True)
        for key, label, value in (
            ("telefon", "Telefon", PHONE),
            ("adress", "Adress", "Verkstadsvägen 4, Nacka"),
            ("omrade", "Område", "Nacka och Värmdö"),
        ):
            Fact.objects.create(
                account=cls.account, key=key, label=label, value=value, confirmed=True
            )
        cls.service = Service.objects.create(
            account=cls.account, name="Däckbyte", sales_mode=Service.SALES_QUOTE
        )
        blocks = page_views.starter_blocks(cls.account, cls.service)
        now = timezone.now()
        cls.single = LandingPage.objects.create(
            account=cls.account,
            name="Däck",
            draft={"blocks": blocks},
            published={"blocks": blocks},
            published_at=now,
        )
        cls.shared = LandingPage.objects.create(
            account=cls.account,
            name="Service",
            draft={"blocks": blocks},
            published={"blocks": blocks},
            published_at=now,
        )
        draft = copy.deepcopy(blocks)
        pagebuilder.active_version(_hero(draft))["fields"]["title"] = "Utkastets rubrik"
        cls.unused = LandingPage.objects.create(
            account=cls.account,
            name="Vinter",
            draft={"blocks": draft},
            published={"blocks": blocks},
            published_at=now,
        )
        cls.live = Campaign.objects.create(
            account=cls.account,
            service=cls.service,
            name="Däckbyte Nacka",
            area="Nacka + 10 km",
            status=Campaign.STATUS_LIVE,
            landing_page=cls.single,
        )
        cls.nacka = Campaign.objects.create(
            account=cls.account,
            service=cls.service,
            name="Service Nacka",
            area="Nacka + 10 km",
            status=Campaign.STATUS_LIVE,
            landing_page=cls.shared,
        )
        cls.varmdo = Campaign.objects.create(
            account=cls.account,
            service=cls.service,
            name="Service Värmdö",
            area="Värmdö + 10 km",
            landing_page=cls.shared,
        )

        cls.other_customer = Customer.objects.create(name="Annan Bygg AB")
        cls.annan = User.objects.create_user("olle@annan.se", email="olle@annan.se", password="x")
        cls.other_customer.users.add(cls.annan)
        cls.other_account = FlamingoAccount.objects.create(
            customer=cls.other_customer, is_enabled=True
        )
        cls.other_page = LandingPage.objects.create(
            account=cls.other_account, name="Hemlig sida", draft={"blocks": blocks}
        )

    def setUp(self):
        super().setUp()
        cache.clear()

    def client_for(self, user, view_as=None):
        client = Client()
        client.force_login(user)
        if view_as is not None:
            session = client.session
            session[VIEW_AS_KEY] = view_as.pk
            session.save()
        return client

    def preview_url(self, page=None):
        return reverse("flamingo:app_page_preview", args=[(page or self.unused).pk])


# ---------------------------------------------------------------------------
# Listan
# ---------------------------------------------------------------------------


@override_settings(**LANDING_BASE)
class PageListLinkTests(PageUrlFixture, TestCase):
    def html(self, user=None, **kwargs):
        client = self.client_for(user or self.kund, **kwargs)
        return client.get(reverse("flamingo:app_pages")).content.decode()

    def test_one_campaign_opens_its_address_in_a_new_tab(self):
        html = self.html()
        self.assertIn(
            f'<a class="fl-btn fl-btn--ghost fl-btn--sm" href="{self.live.landing_url}" '
            'target="_blank" rel="noopener">Öppna sidan<span class="fl-sr"> Däck (ny flik)'
            "</span></a>",
            html,
        )
        self.assertIn("css/flamingo-app-pages.css", html)

    def test_a_shared_page_gets_one_link_per_campaign_next_to_its_name(self):
        html = self.html()
        for campaign in (self.nacka, self.varmdo):
            with self.subTest(campaign=campaign.name):
                self.assertIn(
                    f'?flik=sidan">{campaign.name}</a><a class="fl-pg-open" '
                    f'href="{campaign.landing_url}" target="_blank" rel="noopener">Öppna sidan'
                    f'<span class="fl-sr"> för {campaign.name} (ny flik)</span></a>',
                    html,
                )
        # Inget gemensamt "Öppna sidan" för den delade sidan: länkarna står
        # vid kampanjerna (Däck har sin egen knapp).
        self.assertEqual(html.count(">Öppna sidan<"), 3)

    def test_a_page_without_campaign_gets_a_preview(self):
        html = self.html()
        self.assertIn(
            f'href="{self.preview_url()}" target="_blank" rel="noopener">Förhandsvisa'
            '<span class="fl-sr"> Vinter (ny flik)</span>',
            html,
        )
        self.assertEqual(html.count(">Förhandsvisa<"), 1)
        self.assertNotIn(self.preview_url(self.single), html)

    def test_staff_viewing_as_the_customer_sees_the_same_links(self):
        html = self.html(self.staff, view_as=self.customer)
        for href in (
            self.live.landing_url,
            self.nacka.landing_url,
            self.varmdo.landing_url,
            self.preview_url(),
        ):
            with self.subTest(href=href):
                self.assertIn(f'href="{href}" target="_blank" rel="noopener">', html)

    def test_another_customers_pages_are_never_linked(self):
        html = self.html()
        self.assertNotIn(self.preview_url(self.other_page), html)
        self.assertNotIn("Hemlig sida", html)


# ---------------------------------------------------------------------------
# Sidbyggaren
# ---------------------------------------------------------------------------


@override_settings(**LANDING_BASE)
class EditorAddressTests(PageUrlFixture, TestCase):
    def html(self, page, user=None, **kwargs):
        client = self.client_for(user or self.kund, **kwargs)
        response = client.get(reverse("flamingo:app_page", args=[page.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_one_campaign_shows_view_page_the_address_and_copy(self):
        html = self.html(self.single)
        address = f"https://adx.se/lp/{self.live.page_slug}/"
        self.assertIn(
            f'<a class="pb-btn pb-btn--ghost pb-btn--sm" href="{self.live.landing_url}" '
            'target="_blank" rel="noopener">Visa sidan<span class="fl-sr"> (ny flik)</span></a>',
            html,
        )
        self.assertIn(f'<span class="fl-pg-addr__url">{address}</span>', html)
        # Kopiera är dold utan skript (flamingo-app-links.js visar den);
        # adressen står kvar som text att markera.
        self.assertIn(f'data-ln-copy="{address}" hidden>Kopiera', html)
        self.assertIn("js/flamingo-app-links.js", html)
        self.assertIn("css/flamingo-app-pages.css", html)
        self.assertNotIn(">Förhandsvisa<", html)

    def test_a_shared_page_has_one_row_per_campaign(self):
        html = self.html(self.shared)
        for campaign in (self.nacka, self.varmdo):
            with self.subTest(campaign=campaign.name):
                self.assertIn(f'<span class="fl-pg-addr__camp">{campaign.name}</span>', html)
                self.assertIn(f'href="{campaign.landing_url}" target="_blank"', html)
                self.assertIn(f'data-ln-copy="https://adx.se/lp/{campaign.page_slug}/"', html)
        self.assertEqual(html.count(">Visa sidan<"), 2)

    def test_no_campaign_means_preview_and_nothing_to_copy(self):
        html = self.html(self.unused)
        self.assertIn(
            f'href="{self.preview_url()}" target="_blank" rel="noopener">Förhandsvisa', html
        )
        self.assertIn(
            "Förhandsvisningen visar utkastet och syns bara för den som är inloggad. "
            "Sidan får en adress när en kampanj använder den.",
            html,
        )
        self.assertNotIn("data-ln-copy", html)
        self.assertNotIn(">Visa sidan<", html)

    @override_settings(FLAMINGO_LANDING_BASE_URL="https://sidor.example")
    def test_the_copied_address_is_the_one_the_ads_use(self):
        """Kopiera ger annonsernas adress (exports.landing_page_url); länken
        går till samma värd som verktyget, så att inloggningen följer med och
        besöket inte räknas."""
        html = self.html(self.single)
        self.assertIn(f'data-ln-copy="https://sidor.example/lp/{self.live.page_slug}/"', html)
        self.assertIn(f'href="{self.live.landing_url}" target="_blank"', html)

    def test_staff_viewing_as_the_customer_gets_the_same_strip(self):
        html = self.html(self.single, user=self.staff, view_as=self.customer)
        self.assertIn(
            f'href="{self.live.landing_url}" target="_blank" rel="noopener">Visa sidan', html
        )


@override_settings(**LANDING_BASE)
class CampaignStatusLinkTests(PageUrlFixture, TestCase):
    """En kampanj som inte är live har samma adress: /lp/ är alltid öppen
    (public_views._campaign_for). Kunden ser den publicerade sidan, som en
    besökare, och räknas inte."""

    STATUSES = (
        Campaign.STATUS_DRAFT,
        Campaign.STATUS_IN_REVIEW,
        Campaign.STATUS_NEEDS_CUSTOMER,
        Campaign.STATUS_PAUSED,
    )

    def test_every_status_links_to_the_campaigns_address(self):
        kund = self.client_for(self.kund)
        for status in self.STATUSES:
            with self.subTest(status=status):
                Campaign.objects.filter(pk=self.live.pk).update(status=status)
                html = kund.get(reverse("flamingo:app_pages")).content.decode()
                self.assertIn(
                    f'href="{self.live.landing_url}" target="_blank" rel="noopener">Öppna sidan'
                    '<span class="fl-sr"> Däck (ny flik)',
                    html,
                )
                editor = kund.get(reverse("flamingo:app_page", args=[self.single.pk]))
                self.assertContains(
                    editor,
                    f'href="{self.live.landing_url}" target="_blank" rel="noopener">Visa sidan',
                )
                self.assertNotContains(editor, ">Förhandsvisa<")
                page = kund.get(self.live.landing_url)
                self.assertEqual(page.status_code, 200)
                self.assertFalse(page.context["preview"])
                self.assertEqual(page.context["which"], "published")
                self.assertEqual(page.context["call_beacon"], "")

    def test_the_staff_strip_says_the_page_is_open(self):
        """Byrån ser en sida som inte är live som förhandsvisning; remsan sa
        att den bara syns för ADX, vilket inte stämt sedan 2026-10-04."""
        Campaign.objects.filter(pk=self.live.pk).update(status=Campaign.STATUS_PAUSED)
        response = self.client_for(self.staff).get(self.live.landing_url)
        self.assertTrue(response.context["preview"])
        self.assertContains(
            response,
            "Sidan är öppen för alla som har adressen, men formuläret skickar inget för ADX.",
        )
        self.assertNotContains(response, "syns bara för ADX")


# ---------------------------------------------------------------------------
# Förhandsvisningen
# ---------------------------------------------------------------------------


class PagePreviewTests(PageUrlFixture, TestCase):
    def test_the_customer_sees_the_draft_with_noindex(self):
        response = self.client_for(self.kund).get(self.preview_url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Utkastets rubrik", html)
        self.assertIn('<meta name="robots" content="noindex, nofollow">', html)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("<b>Förhandsvisning.</b> Du ser utkastet.", html)
        self.assertIn("Förhandsvisning: formuläret skickar inget.", html)
        # Ingenting räknas: inget klick på numret, inget besök.
        self.assertNotIn("data-fl-beacon", html)
        self.assertNotIn("data-fl-visit", html)
        self.assertIn(f'action="{self.preview_url()}"', html)

    def test_only_logged_in_and_only_the_own_customers_page(self):
        anonymous = Client()
        self.assertEqual(anonymous.get(self.preview_url()).status_code, 404)
        self.assertEqual(anonymous.post(self.preview_url(), {"phone": PHONE}).status_code, 404)
        annan = self.client_for(self.annan)
        self.assertEqual(annan.get(self.preview_url()).status_code, 404)
        self.assertEqual(annan.post(self.preview_url(), {"phone": PHONE}).status_code, 404)
        kund = self.client_for(self.kund)
        self.assertEqual(kund.get(self.preview_url(self.other_page)).status_code, 404)
        self.assertEqual(
            self.client_for(self.annan).get(self.preview_url(self.other_page)).status_code, 200
        )
        self.assertEqual(
            kund.get(reverse("flamingo:app_page_preview", args=[999999])).status_code, 404
        )

    def test_staff_needs_the_customer_view_and_then_sees_only_that_customer(self):
        staff = self.client_for(self.staff)
        response = staff.get(self.preview_url())
        self.assertNotContains(response, "Utkastets rubrik")
        self.assertTemplateUsed(response, "flamingo/app/staff_index.html")
        as_customer = self.client_for(self.staff, view_as=self.customer)
        response = as_customer.get(self.preview_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Utkastets rubrik")
        self.assertEqual(as_customer.get(self.preview_url(self.other_page)).status_code, 404)

    def test_the_form_creates_nothing_and_shows_the_thanks_page(self):
        client = self.client_for(self.kund)
        before = Lead.objects.count()
        with mock.patch.object(sms, "urlopen", side_effect=AssertionError("inget sms")) as elks:
            response = client.post(self.preview_url(), {"name": "Anna", "phone": "070-174 06 50"})
        thanks = f"{self.preview_url()}?tack=1"
        self.assertRedirects(response, thanks, fetch_redirect_response=False)
        elks.assert_not_called()
        self.assertEqual(Lead.objects.count(), before)
        response = client.get(thanks)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        html = response.content.decode()
        self.assertIn("Förhandsvisning: ingen förfrågan skapades.", html)
        self.assertIn(f'<a href="{self.preview_url()}">Tillbaka till sidan</a>', html)

    def test_the_honeypot_also_only_shows_the_thanks_page(self):
        from .public_views import HONEYPOT

        response = self.client_for(self.kund).post(
            self.preview_url(), {"phone": "070-174 06 50", HONEYPOT: "https://spam.example"}
        )
        self.assertRedirects(
            response, f"{self.preview_url()}?tack=1", fetch_redirect_response=False
        )
        self.assertEqual(Lead.objects.count(), 0)

    def test_head_answers_like_get(self):
        response = self.client_for(self.kund).head(self.preview_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(Client().head(self.preview_url()).status_code, 404)

    def test_errors_show_like_on_the_real_page(self):
        response = self.client_for(self.kund).post(self.preview_url(), {"phone": ""})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Skriv ditt telefonnummer.")
        self.assertEqual(Lead.objects.count(), 0)


# ---------------------------------------------------------------------------
# Demokontot
# ---------------------------------------------------------------------------


class DemoPageUrlTests(DemoFixture, TestCase):
    def test_list_editor_and_preview_render_for_staff_viewing_as_the_demo(self):
        client = self.staff_client()
        session = client.session
        session[VIEW_AS_KEY] = self.customer.pk
        session.save()
        pages = list(LandingPage.objects.filter(account=self.account))
        self.assertTrue(pages)
        html = client.get(reverse("flamingo:app_pages")).content.decode()
        self.assertIn(self.live.landing_url, html)
        for page in pages:
            with self.subTest(page=page.name):
                editor = client.get(reverse("flamingo:app_page", args=[page.pk]))
                self.assertEqual(editor.status_code, 200)
                self.assertContains(editor, "fl-pg-addr")
                preview = client.get(reverse("flamingo:app_page_preview", args=[page.pk]))
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview["X-Robots-Tag"], "noindex, nofollow")


# ---------------------------------------------------------------------------
# Kundens egna besök
# ---------------------------------------------------------------------------


class OwnVisitTests(PageUrlFixture, TestCase):
    def ring(self, client):
        return client.post(reverse("flamingo_public:call_click", args=[self.live.page_slug]))

    def test_the_customers_own_contact_is_not_counted(self):
        kund = self.client_for(self.kund)
        response = kund.get(self.live.landing_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["preview"])
        self.assertEqual(response.context["call_beacon"], "")
        self.assertNotIn("data-fl-beacon", response.content.decode())
        self.assertEqual(self.ring(kund).status_code, 204)
        self.assertFalse(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).exists())

    def test_a_contact_of_another_customer_is_counted(self):
        annan = self.client_for(self.annan)
        response = annan.get(self.live.landing_url)
        beacon = reverse("flamingo_public:call_click", args=[self.live.page_slug])
        self.assertEqual(response.context["call_beacon"], beacon)
        self.assertEqual(self.ring(annan).status_code, 204)
        lead = Lead.objects.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual(lead.campaign_id, self.live.pk)

    def test_anonymous_visitors_and_staff_are_unchanged(self):
        self.assertNotEqual(Client().get(self.live.landing_url).context["call_beacon"], "")
        staff = self.client_for(self.staff)
        self.assertEqual(staff.get(self.live.landing_url).context["call_beacon"], "")
        self.ring(staff)
        self.assertFalse(Lead.objects.exists())
        self.ring(Client())
        self.assertEqual(Lead.objects.filter(source=Lead.SOURCE_CALL_CLICK).count(), 1)

    def test_the_form_still_works_for_the_customer(self):
        with mock.patch.object(sms, "urlopen") as elks:
            response = self.client_for(self.kund).post(
                self.live.landing_url, {"name": "Test", "phone": "070-174 06 50"}
            )
        self.assertEqual(response.status_code, 302)
        lead = Lead.objects.get(campaign=self.live)
        self.assertEqual(lead.phone, "070-174 06 50")
        elks.assert_not_called()

    def test_a_call_through_an_utskick_on_a_paused_campaign(self):
        """Ett klick från ett utskick räknas också när kampanjen inte är live
        (public_views._live_campaign), men aldrig för kundens egen kontakt."""
        from apps.utskick import tokens
        from apps.utskick.models import Click

        Campaign.objects.filter(pk=self.live.pk).update(status=Campaign.STATUS_PAUSED)
        click = Click.objects.create(account=self.account, channel="sms")
        ut = tokens.ut_token(click.pk)
        url = reverse("flamingo_public:call_click", args=[self.live.page_slug])

        self.assertEqual(self.client_for(self.kund).post(url, {"ut": ut}).status_code, 204)
        self.assertFalse(Lead.objects.exists())
        self.assertEqual(self.client_for(self.annan).post(url, {"ut": ut}).status_code, 204)
        lead = Lead.objects.get(source=Lead.SOURCE_CALL_CLICK)
        self.assertEqual(lead.campaign_id, self.live.pk)
        # Utan utskicket räknas inget klick på en pausad kampanj.
        self.assertEqual(self.client_for(self.annan).post(url).status_code, 404)

    def test_utskick_visits_and_time_on_page_are_not_counted_for_the_customer(self):
        from apps.utskick import tokens
        from apps.utskick.models import Click

        click = Click.objects.create(account=self.account, channel="sms")
        ut = tokens.ut_token(click.pk)
        url = f"{self.live.landing_url}?ut={ut}"
        beacon = reverse("flamingo_public:visit_beacon", args=[self.live.page_slug])

        kund = self.client_for(self.kund)
        html = kund.get(url).content.decode()
        self.assertNotIn("data-fl-visit", html)
        # Formuläret bär ändå token: en förfrågan får utskicket som vanligt.
        self.assertIn(f'name="ut" value="{ut}"', html)
        self.assertEqual(kund.post(beacon, {"ut": ut, "s": "40"}).status_code, 204)
        click.refresh_from_db()
        self.assertEqual((click.lp_visits, click.engaged_seconds), (0, 0))

        annan = self.client_for(self.annan)
        self.assertIn("data-fl-visit", annan.get(url).content.decode())
        annan.post(beacon, {"ut": ut, "s": "40"})
        click.refresh_from_db()
        self.assertEqual((click.lp_visits, click.engaged_seconds), (1, 40))
