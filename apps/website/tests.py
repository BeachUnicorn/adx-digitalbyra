"""
ADX-sajtens vakttester.

Registry-synkvakten är viktigast (mönsterkatalogen §11): en blocktyp
registreras på tre ställen - modellens choices, blockschemat och en mall -
och wrappern renderar okända typer som INGENTING, tyst. Synkvakten gör en
missad registrering till ett byggfel med namnet på det som saknas.
"""

from pathlib import Path

from django.conf import settings as django_settings
from django.core.management import call_command
from django.test import Client, TestCase

from apps.manage.block_schema import BLOCK_EDIT_SCHEMA
from apps.website.models import BlockType
from apps.website.theme import text_is_dark


class BlockRegistrySyncTests(TestCase):
    """En blocktyp = en choice + en schemapost + en mall. Alltid alla tre."""

    def test_every_block_type_has_schema_and_template(self):
        template_dir = Path(django_settings.BASE_DIR) / "templates" / "website" / "blocks"
        missing = []
        for value, _label in BlockType.choices:
            if value not in BLOCK_EDIT_SCHEMA:
                missing.append(f"{value}: saknar post i BLOCK_EDIT_SCHEMA")
            if not (template_dir / f"{value}.html").exists():
                missing.append(f"{value}: saknar templates/website/blocks/{value}.html")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_every_schema_key_is_a_block_type(self):
        choices = {value for value, _ in BlockType.choices}
        phantom = [key for key in BLOCK_EDIT_SCHEMA if key not in choices]
        self.assertEqual(
            phantom,
            [],
            f"Schemaposter utan modell-choice (redigerbara men orenderbara): {phantom}",
        )


class ThemePortTests(TestCase):
    """Python-porten av guidens luminansregel får aldrig driva ifrån JS:en.
    Facit: guidens egna sidfärger och deras faktiska textlägen."""

    def test_light_pages_get_dark_text(self):
        for color in ("#f7fcff",):
            self.assertTrue(text_is_dark(color), color)

    def test_dark_pages_get_light_text(self):
        for color in ("#121111", "#0e3a52", "#7a2b35", "#4a2d73", "#2f6f4f"):
            self.assertFalse(text_is_dark(color), color)

    def test_junk_color_falls_back_safely(self):
        self.assertTrue(text_is_dark("inte-en-färg"))


class SeededSiteTests(TestCase):
    """Kedjetest: seed_site -> varje seedad sida svarar 200 med sin färg.
    Fångar trasiga block, saknade mallar och döda seed-länkar i ett svep."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)

    def test_all_pages_respond(self):
        client = Client()
        for path in [
            "/",
            "/tjanster/",
            "/webbutveckling/",
            "/automation/",
            "/content/",
            "/hosting/",
            "/domain/",
            "/epost/",
            "/portfolio/",
            "/paket/",
            "/kontakt/",
            "/digitalbyra/",
            "/digitalbyra/goteborg/",
            "/sitemap.xml",
        ]:
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_page_color_reaches_the_body(self):
        response = Client().get("/paket/")
        self.assertContains(response, 'data-gradient="#121111"')
        self.assertContains(response, 'class="text-light"')

    def test_kontakt_carries_the_inquiry_form(self):
        response = Client().get("/kontakt/")
        self.assertContains(response, 'name="topic"')
        self.assertContains(response, 'name="bc_website"')

    def test_internal_seed_links_resolve(self):
        """Varje intern länk på de seedade sidorna ska svara - en seedad
        död länk är ett byggfel, inte ett innehållsproblem."""
        import re

        client = Client()
        seen = set()
        for path in ["/", "/tjanster/", "/paket/", "/kontakt/", "/webbutveckling/"]:
            html = client.get(path).content.decode()
            for href in re.findall(r'href="(/[^"#]*)"', html):
                if href.startswith(("/static/", "/media/", "/manage/", "/admin/")):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                self.assertEqual(client.get(href).status_code, 200, f"{href} (länkad från {path})")


class ForeignDatabaseGuardTests(TestCase):
    """Bootvakten mot kopierade .env-pekare (incidenten 2026-08-27: kopians
    DATABASE_URL pekade kvar på systersajtens lokala databas och migrate +
    seed skrev in i fel projekt). Vakten ska stoppa kända främmande namn
    högt vid boot - aldrig tyst skada."""

    def test_foreign_names_are_refused(self):
        from django.core.exceptions import ImproperlyConfigured

        from config.settings import base

        for name in ("skandivvs", "test_kronan_db", "jungfru_db"):
            with self.assertRaises(ImproperlyConfigured):
                base._refuse_foreign_database(name)

    def test_own_names_pass(self):
        from config.settings import base

        base._refuse_foreign_database("adx_dev")
        base._refuse_foreign_database("adx_db")


class LinkIntegrityTests(TestCase):
    """Länkregeln (Giovannis egen post i mönsterkatalogen): inga döda länkar
    får existera i tysthet. Referens före sträng, resolvern dömer varje mål,
    menyer döljer döda poster vid rendering, och ägaren larmas på översikten."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)

    def test_seeded_site_has_zero_dead_links(self):
        from apps.website.links import dead_links

        problems = dead_links()
        self.assertEqual(
            problems,
            [],
            "Seedade döda länkar är byggfel: "
            + "; ".join(f"{p.location}: {p.target} ({p.status})" for p in problems),
        )

    def test_menu_items_reference_pages_not_strings(self):
        from apps.website.models import MenuItem

        stringly = MenuItem.objects.filter(page=None).exclude(url="")
        allowed = {"/digitalbyra/"}  # ruttmål utanför sidsystemet
        rogue = [i.url for i in stringly if i.url not in allowed]
        self.assertEqual(rogue, [], f"Menyposter med rå URL i stället för sid-FK: {rogue}")

    def test_unpublishing_a_page_hides_and_alarms(self):
        from apps.website.links import UNPUBLISHED, dead_links
        from apps.website.models import BlockPage

        page = BlockPage.objects.get(slug="paket")
        page.is_published = False
        page.save(update_fields=["is_published"])

        # 1. Menyn döljer posten för besökare - ingen död länk skickas ut.
        html = Client().get("/").content.decode()
        self.assertNotIn('href="/paket/"', html)

        # 2. Även BLOCKENS länkar mot sidan döljs vid rendering.
        html_tjanster = Client().get("/tjanster/").content.decode()
        self.assertNotIn('href="/paket/"', html_tjanster)

        # 3. Ägaren larmas: länkar mot sidan rapporteras som brutna.
        problems = dead_links()
        self.assertTrue(
            any("Paket" in p.target and p.status == UNPUBLISHED for p in problems),
            f"Avpublicerad sida gav inget larm: {[(p.target, p.status) for p in problems]}",
        )

    def test_dashboard_shows_the_alarm(self):
        from django.contrib.auth import get_user_model

        from apps.website.models import BlockPage

        BlockPage.objects.filter(slug="paket").update(is_published=False)
        user = get_user_model().objects.create_user("redaktor", password="x")
        client = Client()
        client.force_login(user)
        response = client.get("/manage/")
        self.assertContains(response, "död")
        response = client.get("/manage/lankar/")
        self.assertContains(response, "/paket/")

    def test_resolver_judges_correctly(self):
        from apps.website import links

        self.assertEqual(links.resolve_link("/kontakt/").status, links.OK)
        self.assertEqual(links.resolve_link("/finns-inte-alls/").status, links.MISSING)
        self.assertEqual(links.resolve_link("https://example.com/").status, links.EXTERNAL)
        self.assertEqual(links.resolve_link("").status, links.SKIPPED)
        self.assertEqual(links.resolve_link("/digitalbyra/goteborg/").status, links.OK)
        # Strängar till interna mål blir id-beskrivare vid parse
        parsed = links.parse_href("/kontakt/")
        self.assertEqual(parsed["kind"], "page")


class LinkDescriptorTests(TestCase):
    """Hela vägen (Giovannis order): interna länkar lagras som id-referenser,
    aldrig adresser. Kronjuvelen är slug-bytes-testet - det är själva skälet
    till att beskrivare finns."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)

    def test_seeded_internal_links_are_descriptors(self):
        from apps.website.links import _schema_url_fields
        from apps.website.models import Block

        stringly = []
        for block in Block.objects.all():
            data = block.data or {}
            for key, list_key in _schema_url_fields(block.block_type):
                rows = data.get(list_key) or [] if list_key else [data]
                for row in rows:
                    value = row
                    for part in key.split("."):
                        value = value.get(part) if isinstance(value, dict) else None
                    if isinstance(value, str) and value.startswith("/"):
                        stringly.append(f"{block.page.slug}/{block.block_type}: {key} = {value}")
        self.assertEqual(
            stringly, [], "Interna länkar lagrade som strängar:\n" + "\n".join(stringly)
        )

    def test_links_survive_a_slug_change(self):
        """Sidan byter webbadress - varje länk mot den följer med, och
        larmet förblir tyst. Detta är hela poängen med id-referenser."""
        from apps.website.links import dead_links
        from apps.website.models import BlockPage

        page = BlockPage.objects.get(slug="paket")
        page.slug = "priser-och-paket"
        page.save()

        html = Client().get("/").content.decode()
        self.assertIn('href="/priser-och-paket/"', html)
        self.assertNotIn('href="/paket/"', html)
        self.assertEqual(dead_links(), [])

    def test_legacy_string_still_renders(self):
        """Beskrivare väntas, sträng tolereras: gamla rader renderar via
        parse_href tills de sparas om."""
        from apps.website.models import Block

        block = Block.objects.filter(block_type="bar", page__slug="hem").first()
        block.data["link"]["url"] = "/kontakt/"
        block.save(update_fields=["data"])
        html = Client().get("/").content.decode()
        self.assertIn('href="/kontakt/"', html)

    def test_editor_roundtrip_stores_descriptors(self):
        """Länkväljarens dolda input POST:ar beskrivaren som JSON (Set
        link-mönstret från adx) - och den lagras som beskrivare, aldrig
        som sträng. Skräp i inputen kasseras tyst till tom länk."""
        import json

        from apps.manage.block_schema import clean_block_data
        from apps.website.models import BlockPage

        kontakt = BlockPage.objects.get(slug="kontakt")
        data = clean_block_data(
            "bar",
            {
                "label": "Testrad",
                "link.label": "Skicka",
                "link.url": json.dumps({"kind": "page", "id": kontakt.pk}),
            },
        )
        self.assertEqual(data["link"]["url"], {"kind": "page", "id": kontakt.pk})

        data = clean_block_data(
            "bar",
            {
                "label": "Testrad",
                "link.label": "Extern",
                "link.url": json.dumps({"kind": "external", "url": "https://example.com/"}),
            },
        )
        self.assertEqual(data["link"]["url"], {"kind": "external", "url": "https://example.com/"})

        # Legacy: en rå sträng (gammal rad, handskrivet) uppgraderas via
        # parse_href; oigenkännligt skräp blir tom länk - inte en krasch.
        data = clean_block_data("bar", {"label": "T", "link.label": "L", "link.url": "/kontakt/"})
        self.assertEqual(data["link"]["url"], {"kind": "page", "id": kontakt.pk})
        data = clean_block_data(
            "bar", {"label": "T", "link.label": "L", "link.url": '{"kind": "evil", "id": 1}'}
        )
        self.assertEqual(data["link"]["url"], "")

    def test_deleted_target_hides_link_and_alarms(self):
        from apps.website.links import MISSING, dead_links
        from apps.website.models import Block, BlockPage

        BlockPage.objects.filter(slug="portfolio").delete()
        # Hem-sidans folio finns inte, men tjänstesidornas related pekar hit?
        # Bygg ett säkert fall: peka bar-blocket mot en raderad sida.
        block = Block.objects.filter(block_type="bar", page__slug="hem").first()
        block.data["link"]["url"] = {"kind": "page", "id": 99999}
        block.save(update_fields=["data"])

        html = Client().get("/").content.decode()
        self.assertNotIn("99999", html)
        self.assertTrue(any(p.status == MISSING for p in dead_links()))


class LinkPickerEndpointTests(TestCase):
    """Set link-modalens två endpoints (porterade från Atlas Holly/adx):
    alternativlistan visar NAMN, adresskontrollen reparerar och föreslår
    direktlänkar som överlever adressbyten."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.client = Client()
        self.client.force_login(get_user_model().objects.create_user("redaktor", password="x"))

    def test_options_require_login(self):
        self.assertEqual(Client().get("/manage/lankar/val/").status_code, 302)

    def test_options_are_named_and_grouped(self):
        data = self.client.get("/manage/lankar/val/").json()
        options = data["options"]
        self.assertTrue(options)
        groups = {o["group"] for o in options}
        self.assertIn("Sidor", groups)
        self.assertIn("Städer", groups)
        by_label = {o["label"]: o for o in options}
        self.assertIn("Kontakt", by_label)
        self.assertEqual(by_label["Kontakt"]["link"]["kind"], "page")
        # Redaktören ska aldrig behöva läsa en rå beskrivare i listan.
        for opt in options:
            self.assertNotIn("kind", opt["label"])

    def test_check_recognizes_internal_path_and_suggests_direct_link(self):
        from apps.website.models import BlockPage

        kontakt = BlockPage.objects.get(slug="kontakt")
        res = self.client.post(
            "/manage/lankar/kontrollera/", {"href": "/kontakt/"}, content_type="application/json"
        )
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["suggestion"], {"kind": "page", "id": kontakt.pk})
        self.assertEqual(data["label"], "Kontakt")

    def test_check_flags_a_dead_path(self):
        data = self.client.post(
            "/manage/lankar/kontrollera/",
            {"href": "/finns-inte-alls/"},
            content_type="application/json",
        ).json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["note"])

    def test_check_accepts_external_url(self):
        data = self.client.post(
            "/manage/lankar/kontrollera/",
            {"href": "https://example.com/"},
            content_type="application/json",
        ).json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data["suggestion"])
        self.assertEqual(data["link"], {"kind": "external", "url": "https://example.com/"})


class RawDescriptorLeakGuardTests(TestCase):
    """Incident 2026-08-27: Django admin visade "{'id': 11, 'kind': 'page'}"
    rakt i redigeringsformuläret. Beskrivare är lagringsformat - en människa
    ska ALDRIG se dem. Vakten letar Python-dict-repr (enkelfnuttar) i allt
    en redaktör kan öppna; den dolda inputens JSON (dubbelfnuttar) är ok."""

    LEAK_MARKERS = ("&#x27;kind&#x27;", "{&#x27;", "'kind':")

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)

    def assert_no_leak(self, html, where):
        for marker in self.LEAK_MARKERS:
            self.assertNotIn(marker, html, f"Rå beskrivare läcker i {where} (träff: {marker})")

    def test_block_is_not_registered_in_django_admin(self):
        """Blockdata är rå JSON - den redigeras i /manage/, aldrig i admin.
        (Samma modell som adx: ingen BlockAdmin existerar.)"""
        from django.contrib import admin

        from apps.website.models import Block

        self.assertFalse(admin.site.is_registered(Block))

    def test_manage_block_editors_show_names_not_descriptors(self):
        from django.contrib.auth import get_user_model

        from apps.website.models import Block, BlockPage

        client = Client()
        client.force_login(get_user_model().objects.create_user("redaktor", password="x"))
        for page in BlockPage.objects.all():
            html = client.get(f"/manage/pages/{page.pk}/").content.decode()
            self.assert_no_leak(html, f"/manage/pages/{page.pk}/")
        for block in Block.objects.exclude(data={}):
            html = client.get(f"/manage/blocks/{block.pk}/").content.decode()
            self.assert_no_leak(html, f"block {block.pk} ({block.block_type})")

    def test_django_admin_pages_show_no_descriptors(self):
        from django.contrib.auth import get_user_model

        from apps.website.models import BlockPage

        client = Client()
        client.force_login(
            get_user_model().objects.create_user(
                "admin", password="x", is_staff=True, is_superuser=True
            )
        )
        for page in BlockPage.objects.all():
            url = f"/admin/website/blockpage/{page.pk}/change/"
            html = client.get(url).content.decode()
            self.assert_no_leak(html, url)

    def test_editor_pill_shows_where_the_link_goes(self):
        """Bar-blocket på hem länkar till en sida - redigeraren ska visa
        sidans NAMN i pillen, inte en adress och inte en beskrivare."""
        from django.contrib.auth import get_user_model

        from apps.website.links import resolve_link
        from apps.website.models import Block

        block = Block.objects.filter(block_type="bar", page__slug="hem").first()
        target = resolve_link(block.data["link"]["url"])
        client = Client()
        client.force_login(get_user_model().objects.create_user("redaktor", password="x"))
        html = client.get(f"/manage/blocks/{block.pk}/").content.decode()
        self.assertIn(target.label, html)
        self.assertIn("Byt länk", html)
