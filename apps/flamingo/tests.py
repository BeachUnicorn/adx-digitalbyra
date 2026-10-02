"""ADX Flamingo: grinden, läckorna och aktiveringen."""

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer
from apps.website.models import Block, BlockPage

from .access import SESSION_KEY
from .models import FlamingoAccount, has_flamingo

User = get_user_model()


class FlamingoFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Acme AB")
        cls.other = Customer.objects.create(name="Annan AB")
        cls.anna = User.objects.create_user("anna@acme.se", email="anna@acme.se", password="x")
        cls.acme.users.add(cls.anna)
        cls.bo = User.objects.create_user("bo@annan.se", email="bo@annan.se", password="x")
        cls.other.users.add(cls.bo)
        FlamingoAccount.objects.create(customer=cls.acme, is_enabled=True)
        cls.home = BlockPage.objects.create(
            title="ADX Flamingo",
            slug="flamingo",
            design=BlockPage.DESIGN_FLAMINGO,
            is_published=True,
        )
        Block.objects.create(page=cls.home, block_type="prose", data={"title": "Välkommen"})
        cls.prices = BlockPage.objects.create(
            title="Priser", slug="fl-priser", design=BlockPage.DESIGN_FLAMINGO, is_published=False
        )

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client


class GateTests(FlamingoFixture, TestCase):
    def test_everyone_without_flamingo_gets_the_plain_site_404(self):
        unknown = Client().get("/finns-inte-alls/")
        for client in (Client(), self.client_for(self.bo)):
            for path in ("/flamingo/", "/flamingo/app/", "/flamingo/fl-priser/", "/flamingo/x/"):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("Location", response)
                    # Samma mall som vilken okänd adress som helst.
                    self.assertEqual(
                        [t.name for t in response.templates][:1],
                        [t.name for t in unknown.templates][:1],
                    )

    def test_without_access_flamingo_behaves_exactly_like_an_unknown_address(self):
        """Status och rubriker som för en adress som inte finns, för varje
        metod och variant (granskningen 2026-10-03 hittade tre skillnader)."""
        pairs = [
            ("/flamingo/", "/finns-inte/"),
            ("/flamingo/?x=1", "/finns-inte/?x=1"),
            ("/flamingo", "/finns-inte"),
            ("/flamingo/app", "/finns-inte/app"),
            ("/flamingo/app/", "/finns-inte/app/"),
            ("/flamingo/fl-priser/", "/finns-inte/fl-priser/"),
        ]
        headers = ("Location", "X-Frame-Options", "X-Robots-Tag", "Cache-Control")
        for user in (None, self.bo):
            for method in ("get", "head", "post", "put", "patch", "delete", "options"):
                for gated, unknown in pairs:
                    with self.subTest(user=user, method=method, path=gated):
                        client = Client(enforce_csrf_checks=True)
                        if user:
                            client.force_login(user)
                        a = getattr(client, method)(gated)
                        b = getattr(client, method)(unknown)
                        self.assertEqual(a.status_code, b.status_code)
                        for header in headers:
                            want = b.get(header, "").replace("finns-inte", "flamingo")
                            self.assertEqual(a.get(header, ""), want, header)

    def test_staff_viewing_as_cannot_write(self):
        client = self.client_for(self.staff)
        session = client.session
        session[VIEW_AS_KEY] = self.acme.pk
        session.save()
        response = client.post("/flamingo/app/", {"x": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/flamingo/app/")

    def test_a_contact_with_flamingo_sees_published_pages_only(self):
        client = self.client_for(self.anna)
        response = client.get("/flamingo/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Välkommen", response.content.decode())
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(client.get("/flamingo/fl-priser/").status_code, 404)
        self.assertEqual(client.get("/flamingo/app/").status_code, 200)

    def test_staff_sees_drafts(self):
        client = self.client_for(self.staff)
        self.assertEqual(client.get("/flamingo/fl-priser/").status_code, 200)

    def test_deactivation_and_an_inactive_customer_take_effect_at_once(self):
        client = self.client_for(self.anna)
        self.assertEqual(client.get("/flamingo/").status_code, 200)
        FlamingoAccount.objects.filter(customer=self.acme).update(is_enabled=False)
        self.assertEqual(client.get("/flamingo/").status_code, 404)
        FlamingoAccount.objects.filter(customer=self.acme).update(is_enabled=True)
        Customer.objects.filter(pk=self.acme.pk).update(is_active=False)
        self.assertEqual(client.get("/flamingo/").status_code, 404)

    def test_a_tampered_session_choice_never_picks_a_foreign_customer(self):
        client = self.client_for(self.anna)
        session = client.session
        session[SESSION_KEY] = self.other.pk
        session.save()
        response = client.get("/flamingo/app/")
        self.assertContains(response, "Acme AB")
        self.assertNotContains(response, "Annan AB")

    def test_staff_viewing_as_a_customer_without_flamingo_gets_a_notice(self):
        client = self.client_for(self.staff)
        session = client.session
        session[VIEW_AS_KEY] = self.other.pk
        session.save()
        response = client.get("/flamingo/")
        self.assertContains(response, "får 404 här")

    def test_unknown_flamingo_path_for_an_insider_uses_the_flamingo_404(self):
        response = self.client_for(self.anna).get("/flamingo/finns-inte/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("flamingo/404.html", [t.name for t in response.templates])


class LeakTests(FlamingoFixture, TestCase):
    def test_flamingo_pages_never_answer_on_the_public_slug_route(self):
        self.assertEqual(Client().get("/fl-priser/").status_code, 404)
        BlockPage.objects.filter(pk=self.prices.pk).update(is_published=True)
        self.assertEqual(Client().get("/fl-priser/").status_code, 404)
        self.assertEqual(self.client_for(self.staff).get("/fl-priser/").status_code, 404)

    def test_not_in_the_sitemap_menus_link_picker_or_homepage(self):
        from apps.manage.forms import MenuItemForm
        from apps.website.links import linkable_targets
        from apps.website.models import SiteSettings

        BlockPage.objects.filter(pk=self.prices.pk).update(is_published=True)
        xml = Client().get("/sitemap.xml").content.decode()
        self.assertNotIn("flamingo", xml)
        self.assertNotIn("fl-priser", xml)
        picker_ids = {t["link"]["id"] for t in linkable_targets() if t["link"]["kind"] == "page"}
        self.assertNotIn(self.home.pk, picker_ids)
        self.assertNotIn(self.home, MenuItemForm().fields["page"].queryset)
        # Även om någon pekar ut en Flamingo-sida som startsida.
        settings = SiteSettings.load()
        settings.homepage = self.home
        settings.save()
        response = Client().get("/")
        self.assertNotIn("Välkommen", response.content.decode())

    def test_the_absolute_url_lives_under_flamingo(self):
        self.assertEqual(self.home.get_absolute_url(), "/flamingo/")
        self.assertEqual(self.prices.get_absolute_url(), "/flamingo/fl-priser/")

    def test_robots_txt_does_not_announce_the_area(self):
        self.assertNotIn("flamingo", Client().get("/robots.txt").content.decode())

    def test_a_customer_on_flamingo_sees_no_staff_tools(self):
        html = self.client_for(self.anna).get("/flamingo/").content.decode()
        self.assertNotIn("/manage/blocks/", html)
        self.assertNotIn("c-admin-dock", html)


class ManageTests(FlamingoFixture, TestCase):
    def test_activation_on_the_customer_card_never_mails_the_customer(self):
        client = self.client_for(self.staff)
        response = client.post(f"/manage/kunder/{self.other.pk}/flamingo/", {"is_enabled": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith("#flamingo"))
        account = FlamingoAccount.objects.get(customer=self.other)
        self.assertTrue(account.is_enabled)
        self.assertEqual(account.enabled_by, self.staff)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self.client_for(self.bo).get("/flamingo/").status_code, 200)
        client.post(f"/manage/kunder/{self.other.pk}/flamingo/", {})
        self.assertFalse(has_flamingo(self.other))

    def test_a_contact_cannot_toggle_flamingo(self):
        response = self.client_for(self.bo).post(
            f"/manage/kunder/{self.other.pk}/flamingo/", {"is_enabled": "on"}
        )
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(has_flamingo(self.other))

    def test_card_list_and_overview_show_flamingo(self):
        client = self.client_for(self.staff)
        self.assertContains(client.get(f"/manage/kunder/{self.acme.pk}/"), 'id="flamingo"')
        self.assertContains(client.get("/manage/kunder/"), "m-badge--flamingo")
        overview = client.get("/manage/flamingo/")
        self.assertContains(overview, "Acme AB")
        self.assertContains(overview, "/flamingo/")

    def test_view_as_opens_flamingo_read_only(self):
        client = self.client_for(self.staff)
        response = client.post(f"/manage/kunder/{self.acme.pk}/flamingo/visa/")
        self.assertEqual(response["Location"], "/flamingo/")
        self.assertContains(client.get("/flamingo/"), "Du ser Flamingo som Acme AB")

    def test_the_page_form_reserves_addresses(self):
        from apps.manage.forms import BlockPageForm

        # Upptagen adress får ett suffix; reservationen gäller den lediga.
        taken = BlockPageForm(data={"title": "X", "slug": "flamingo", "design": "", "order": 1})
        self.assertTrue(taken.is_valid())
        self.assertEqual(taken.cleaned_data["slug"], "flamingo-2")
        self.home.delete()
        adx = BlockPageForm(data={"title": "X", "slug": "flamingo", "design": "", "order": 1})
        self.assertFalse(adx.is_valid())
        self.assertIn("slug", adx.errors)
        tool = BlockPageForm(data={"title": "X", "slug": "app", "design": "flamingo", "order": 1})
        self.assertFalse(tool.is_valid())
        ok = BlockPageForm(data={"title": "Hej", "slug": "", "design": "flamingo", "order": 1})
        self.assertTrue(ok.is_valid(), ok.errors)

    def test_the_block_picker_follows_the_page_design(self):
        from apps.manage.block_schema import types_for_design

        client = self.client_for(self.staff)
        adx_page = BlockPage.objects.create(title="Om", slug="om-test", is_published=True)
        adx_only = next(t for t in types_for_design("") if t not in types_for_design("flamingo"))
        response = client.post(
            f"/manage/pages/{self.home.pk}/blocks/add/", {"block_type": adx_only}
        )
        self.assertFalse(self.home.blocks.filter(block_type=adx_only).exists())
        response = client.post(f"/manage/pages/{adx_page.pk}/blocks/add/", {"block_type": adx_only})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(adx_page.blocks.filter(block_type=adx_only).exists())
        detail = client.get(f"/manage/pages/{self.home.pk}/").content.decode()
        self.assertNotIn(f'value="{adx_only}"', detail)


class PortalEntryTests(FlamingoFixture, TestCase):
    def test_only_flamingo_customers_see_the_way_in(self):
        self.assertContains(self.client_for(self.anna).get("/kund/tavla/"), "ADX Flamingo")
        self.assertNotContains(self.client_for(self.bo).get("/kund/tavla/"), "ADX Flamingo")


class PreviewTests(FlamingoFixture, TestCase):
    def test_the_draft_preview_can_render_a_flamingo_page(self):
        from apps.assistant.preview import _render_public

        html = _render_public("/flamingo/")
        self.assertIn("Välkommen", html)


class SeedFlamingoTests(TestCase):
    """seed_flamingo: Flamingos landningssida ur seed_data/flamingo_pages.json."""

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra-seed", password="x12345678", is_staff=True)

    def seed(self):
        from io import StringIO

        from django.core.management import call_command

        call_command("seed_flamingo", stdout=StringIO())
        return BlockPage.objects.get(slug=BlockPage.FLAMINGO_HOME_SLUG)

    def staff_client(self):
        client = Client()
        client.force_login(self.staff)
        return client

    def test_the_page_is_built_from_flamingo_blocks_only(self):
        from apps.manage.block_schema import types_for_design

        page = self.seed()
        self.assertEqual(page.design, BlockPage.DESIGN_FLAMINGO)
        self.assertEqual(page.title, "ADX Flamingo")
        self.assertFalse(page.is_published, "en ny sida ska skapas som utkast")
        types = list(page.blocks.order_by("order").values_list("block_type", flat=True))
        self.assertEqual(types[0], "fl_hero")
        self.assertEqual(types[-1], "bar")
        allowed = set(types_for_design(BlockPage.DESIGN_FLAMINGO))
        self.assertEqual([t for t in types if t not in allowed], [])

    def test_staff_sees_the_rendered_page(self):
        page = self.seed()
        hero = page.blocks.get(block_type="fl_hero").data
        response = self.staff_client().get("/flamingo/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, hero["title"])
        self.assertContains(response, 'href="/flamingo/app/"')
        self.assertContains(response, "Exempeldata.")

    def test_links_go_through_the_editors_link_cleaner(self):
        page = self.seed()
        hero = page.blocks.get(block_type="fl_hero").data
        self.assertEqual(hero["primary"]["url"], {"kind": "path", "path": "/flamingo/app/"})
        bar = page.blocks.get(block_type="bar").data
        self.assertEqual(bar["link"]["url"], {"kind": "path", "path": "/flamingo/app/"})

    def test_a_rerun_replaces_the_blocks_and_keeps_the_publication(self):
        page = self.seed()
        count = page.blocks.count()
        BlockPage.objects.filter(pk=page.pk).update(is_published=True)
        page = self.seed()
        self.assertTrue(page.is_published)
        self.assertEqual(page.blocks.count(), count)
        self.assertEqual(BlockPage.objects.filter(design=BlockPage.DESIGN_FLAMINGO).count(), 1)

    def test_the_faq_answers_only_behind_the_gate(self):
        from apps.faq.models import FAQSection

        page = self.seed()
        BlockPage.objects.filter(pk=page.pk).update(is_published=True)
        faq_block = page.blocks.get(block_type="faq")
        section = FAQSection.objects.get(pk=faq_block.data["faq_section_id"])
        self.assertGreaterEqual(section.items.count(), 6)
        self.assertContains(self.staff_client().get("/flamingo/"), section.items.first().question)

        public = Client()
        self.assertEqual(public.get(f"/faq/{section.slug}/").status_code, 404)
        self.assertNotIn(section.slug, public.get("/faq/").content.decode())
        self.assertNotIn(section.slug, public.get("/sitemap.xml").content.decode())

        # Sektionen är Flamingos uttryckligen: ett FAQ-block på en ADX-sida
        # (även dolt, även på ett utkast) gör den aldrig publik, och blocket
        # visar inte frågorna (granskningen 2026-10-03).
        self.assertEqual(section.design, "flamingo")
        adx_page = BlockPage.objects.create(title="Om", slug="om-faq-test", is_published=True)
        Block.objects.create(page=adx_page, block_type="faq", data={"faq_section_id": section.pk})
        self.assertEqual(public.get(f"/faq/{section.slug}/").status_code, 404)
        self.assertNotIn(section.slug, public.get("/sitemap.xml").content.decode())
        self.assertNotContains(public.get("/om-faq-test/"), section.items.first().question)


class DesignSwitchTests(FlamingoFixture, TestCase):
    """Granskningen 2026-10-03: byte av design fick en sida att krascha."""

    def test_a_page_with_flamingo_blocks_cannot_switch_to_adx(self):
        from apps.manage.forms import BlockPageForm

        Block.objects.create(page=self.home, block_type="fl_hero", data={"title": "Hej"})
        form = BlockPageForm(
            instance=self.home,
            data={"title": "ADX Flamingo", "slug": "flamingo", "design": "", "order": 1},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("fl_hero", str(form.errors["design"]))

    def test_a_block_outside_the_page_design_renders_nothing_instead_of_500(self):
        adx = BlockPage.objects.create(title="Fel", slug="fel-design", is_published=True)
        Block.objects.create(page=adx, block_type="fl_hero", data={"title": "Syns inte"})
        response = Client().get("/fel-design/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Syns inte")

    def test_the_footer_page_choice_is_adx_only(self):
        from apps.manage.forms import SiteSettingsForm

        queryset = SiteSettingsForm().fields["footer_component_page"].queryset
        self.assertNotIn(self.home, queryset)

    def test_flamingo_home_is_not_served_twice(self):
        self.assertEqual(self.client_for(self.staff).get("/flamingo/flamingo/").status_code, 404)

    def test_a_page_link_to_flamingo_is_never_drawn(self):
        from apps.website.links import MISSING, resolve_link

        self.assertEqual(resolve_link({"kind": "page", "id": self.home.pk}).status, MISSING)
