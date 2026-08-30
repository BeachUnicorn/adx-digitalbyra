"""
ADX-sajtens vakttester.

Registry-synkvakten är viktigast (mönsterkatalogen §11): en blocktyp
registreras på tre ställen - modellens choices, blockschemat och en mall -
och wrappern renderar okända typer som INGENTING, tyst. Synkvakten gör en
missad registrering till ett byggfel med namnet på det som saknas.
"""

import re
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

    def test_every_block_type_describes_itself(self):
        """
        Fjärde registreringen: 'purpose'. AI-redaktören kan inte se sajten -
        blockkatalogen (hamta_blockkatalog) är dess enda bild av hur ett block
        ser ut, och den byggs härifrån. En ny blocktyp utan beskrivning blir
        ett block modellen väljer på namnet och fyller på måfå, så den saknade
        raden ska smälla vid bygget och inte hos kunden.
        """
        missing = [
            key
            for key, schema in BLOCK_EDIT_SCHEMA.items()
            if not schema.get("purpose", "").strip()
        ]
        self.assertEqual(missing, [], f"Blocktyper utan 'purpose' i BLOCK_EDIT_SCHEMA: {missing}")

    def test_list_only_block_types_say_where_their_content_lives(self):
        """
        Ett block vars innehåll ligger helt i listor blir tomt om modellen
        bara sätter fält. Tre typer är sådana (chips, marquee, contact_cards)
        och de gick länge bara att skapa tomma - beskrivningen måste därför
        nämna listan vid namn.
        """
        for key, schema in BLOCK_EDIT_SCHEMA.items():
            if schema["fields"] or not schema.get("lists"):
                continue
            with self.subTest(typ=key):
                purpose = schema["purpose"]
                names = [lst["key"] for lst in schema["lists"]]
                self.assertTrue(
                    any(f"'{name}'" in purpose for name in names),
                    f"{key}: beskrivningen nämner inte listan {names}",
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
            "/webbyra/",
            "/webbyra/goteborg/",
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
        allowed = {"/webbyra/"}  # ruttmål utanför sidsystemet
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
        self.assertEqual(links.resolve_link("/webbyra/goteborg/").status, links.OK)
        # Den gamla ortsadressen 301:ar och ska räknas som levande, inte död.
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


class VvsLegacyGuardTests(TestCase):
    """
    Identitetsvakten från genomlysningen 2026-08-27 (mönster efter
    ForeignDatabaseGuardTests): ADX-webappen är kopierad från skandivvs-
    webappen, och oreviderat VVS-arv låg kvar i modeller, mallar, AI-verktyg
    och byggkonfiguration tills det rensades 2026-08-28. Vakten grep-ar kod,
    mallar och package.json efter kända arv-markörer och failar med fil:rad
    så en regression aldrig passerar tyst.

    Vitlistat: config/settings/base.py (bootvakten mot främmande databaser
    nämner systersajterna vid namn - det är dess jobb) och den här filen
    (vaktens egna mönster plus ForeignDatabaseGuardTests). Migrationer
    skannas inte - de är frusen historik.
    """

    #: Markörer för skandivvs-arvet. `reco` matchas bara som eget ord eller
    #: följt av skiljetecken (reco.se, reco_widget, Reco-widget) - annars
    #: träffar den oskyldiga ord som "record" och "recognizes".
    PATTERNS = [
        re.compile(r"skandivvs", re.IGNORECASE),
        re.compile(r"skanditiptap", re.IGNORECASE),
        re.compile(r"skvvs", re.IGNORECASE),
        re.compile(r"r[öo]rmokare", re.IGNORECASE),
        re.compile(r"plumber", re.IGNORECASE),
        re.compile(r"jungfru", re.IGNORECASE),
        re.compile(r"rot-?avdrag", re.IGNORECASE),
        re.compile(r"\breco\b|reco[._-]", re.IGNORECASE),
    ]

    SCAN_ROOTS = ["apps", "config", "templates", "src", "static/js"]
    EXTRA_FILES = ["package.json", "esbuild.config.mjs"]
    EXTENSIONS = {".py", ".html", ".js", ".mjs", ".json", ".md"}
    SKIP_DIRS = {"migrations", "dist", "node_modules", "__pycache__"}
    #: Incidentdokumentation får nämna arvet vid namn: bootvakten mot
    #: främmande databaser, den här filen, och mejlporteringsincidentens
    #: tvillingtest (2026-08-27).
    WHITELIST = {
        "config/settings/base.py",
        "apps/website/tests.py",
        "apps/inquiries/tests.py",
    }

    def _files(self):
        base = Path(django_settings.BASE_DIR)
        for root in self.SCAN_ROOTS:
            for path in sorted((base / root).rglob("*")):
                if not path.is_file() or path.suffix not in self.EXTENSIONS:
                    continue
                rel = path.relative_to(base)
                if self.SKIP_DIRS & set(rel.parts):
                    continue
                yield rel, path
        for name in self.EXTRA_FILES:
            yield Path(name), base / name

    def test_no_vvs_legacy_markers_in_code_templates_or_build(self):
        hits = []
        for rel, path in self._files():
            if str(rel) in self.WHITELIST:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for pattern in self.PATTERNS:
                    if pattern.search(line):
                        hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
                        break
        self.assertEqual(
            hits,
            [],
            "Skandivvs-arv i ADX-koden (se genomlysningen 2026-08-27):\n" + "\n".join(hits),
        )


class SitemapTests(TestCase):
    """Sitemapen får inte motsäga sig själv."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)

    def _locations(self):
        import re

        xml = Client().get("/sitemap.xml").content.decode()
        return re.findall(r"<loc>([^<]+)</loc>", xml)

    def test_no_url_appears_twice(self):
        """
        Startsidan är en BlockPage vars adress är "/", och låg därför både i
        StaticSitemap (priority 1.0) och i BlockPageSitemap (0.8) - samma URL
        med två olika prioriteter.
        """
        import collections

        dupes = [u for u, n in collections.Counter(self._locations()).items() if n > 1]
        self.assertEqual(dupes, [], f"URL:er som ligger flera gånger i sitemapen: {dupes}")

    def test_the_homepage_is_still_listed(self):
        self.assertTrue(any(u.endswith("/") and u.count("/") == 3 for u in self._locations()))

    def test_the_index_pages_are_listed(self):
        """
        Ortsöversikten saknades i sitemapen. Den är ingen BlockPage och kommer
        därför inte med via BlockPageSitemap, och AreaSitemap listar bara
        områdena - inte sidan som samlar dem. Alltså låg den enda sida som
        länkar till alla ortssidor helt utanför sitemapen.
        """
        locations = self._locations()
        for path in ("/webbyra/", "/faq/"):
            with self.subTest(path=path):
                self.assertTrue(
                    any(u.endswith(path) for u in locations),
                    f"{path} saknas i sitemapen",
                )


class KeywordPageSeedTests(TestCase):
    """
    Sökordssidornas seed är ADDITIV och bär med sig sina bilder.

    Till skillnad från seed_site får den köras på en produktion där kunden
    hunnit redigera: en sida som finns lämnas orörd, och ingenting tas bort.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)
        call_command("seed_sokordssidor", verbosity=0)

    def test_the_pages_are_created_and_respond(self):
        from apps.website.models import BlockPage

        for slug in ("skapa-hemsida", "hemsida-vvs", "hemsida-tandlakare", "om-oss"):
            with self.subTest(slug=slug):
                self.assertTrue(BlockPage.objects.filter(slug=slug, is_published=True).exists())
                self.assertEqual(Client().get(f"/{slug}/").status_code, 200)

    def test_rerunning_creates_nothing_new(self):
        from apps.website.models import Block, BlockPage

        before = (BlockPage.objects.count(), Block.objects.count())
        call_command("seed_sokordssidor", verbosity=0)
        self.assertEqual((BlockPage.objects.count(), Block.objects.count()), before)

    def test_an_edited_page_is_never_overwritten(self):
        """Efter första seeden är produktionen sanningskällan."""
        from apps.website.models import BlockPage

        page = BlockPage.objects.get(slug="skapa-hemsida")
        page.title = "Kundens egen rubrik"
        page.save(update_fields=["title"])

        call_command("seed_sokordssidor", verbosity=0)

        page.refresh_from_db()
        self.assertEqual(page.title, "Kundens egen rubrik")

    def test_the_case_image_is_imported_with_its_alt_text(self):
        from apps.website.models import MediaFile

        media = MediaFile.objects.get(file="media/case-skandi-vvs.jpg")
        self.assertTrue(media.alt_text.strip(), "bilden måste ha alt-text")
        html = Client().get("/case-skandi-vvs/").content.decode()
        self.assertIn(media.file.url, html)
        self.assertIn(media.alt_text, html)

    def test_the_portfolio_card_gets_the_image_and_a_link(self):
        """Ett case utan bild och utan väg vidare övertygar ingen."""
        from apps.website.models import Block

        block = Block.objects.filter(block_type="folio", page__slug="portfolio").first()
        card = next(c for c in block.data["cards"] if "skandi" in c["title"].lower())
        self.assertTrue(card["image_id"])
        self.assertEqual(card["url"]["kind"], "page")

    def test_every_keyword_page_has_a_unique_title_and_a_description(self):
        import json
        import re
        from pathlib import Path

        seed = json.loads(
            (Path(django_settings.BASE_DIR) / "seed_data" / "adx_sokordssidor.json").read_text()
        )
        titles, missing = [], []
        for entry in seed["sidor"]:
            html = Client().get(f"/{entry['slug']}/").content.decode()
            title = re.search(r"<title>(.*?)</title>", html, re.S)
            desc = re.search(r'<meta name="description" content="(.*?)"', html, re.S)
            titles.append(title.group(1).strip() if title else "")
            if not desc or not desc.group(1).strip():
                missing.append(entry["slug"])
        self.assertEqual(missing, [], f"sidor utan metabeskrivning: {missing}")
        dupes = {t for t in titles if titles.count(t) > 1}
        self.assertEqual(dupes, set(), f"dubblerade sidtitlar: {dupes}")

    def test_no_dead_internal_links_on_the_new_pages(self):
        from apps.website.links import dead_links

        problems = dead_links()
        self.assertEqual(
            problems,
            [],
            "Seedade döda länkar är byggfel: "
            + "; ".join(f"{p.location}: {p.target} ({p.status})" for p in problems),
        )


class CaseBlockLinkTests(TestCase):
    """
    Case-blockets link.url renderades aldrig.

    Schemat har alltid haft fältet, men mallen läste bara link.label och ritade
    den som kicker - alltså ett fält redaktören (och AI:n) kunde fylla i utan
    att något hände. Etiketten ska fortsatt fungera ensam som kicker.
    """

    def setUp(self):
        from apps.website.models import Block, BlockPage

        self.page = BlockPage.objects.create(title="Case", slug="ett-case", is_published=True)
        self.block = Block.objects.create(
            page=self.page,
            block_type="case",
            order=1,
            data={"case_title": "Kund", "body": "Text"},
        )

    def _html(self):
        return Client().get("/ett-case/").content.decode()

    def test_a_link_with_url_becomes_a_link(self):
        self.block.data["link"] = {
            "label": "Besök sajten",
            "url": {"kind": "external", "url": "https://example.com/"},
        }
        self.block.save(update_fields=["data"])
        self.assertIn('href="https://example.com/"', self._html())

    def test_a_label_without_url_stays_a_kicker(self):
        """Startsidans case använder etiketten dekorativt - det får inte brytas."""
        self.block.data["link"] = {"label": "Multi-tenant"}
        self.block.save(update_fields=["data"])
        html = self._html()
        self.assertIn("Multi-tenant", html)
        self.assertNotIn('class="btn" href=""', html)


class TemplateSyntaxLeakTests(TestCase):
    """
    Ingen mallsyntax får läcka ut i det renderade svaret.

    Bakgrund: en {# #}-kommentar skrevs över tre rader i area_detail.html.
    Django-kommentarer med den syntaxen får bara vara EN rad - en flerradig
    blir inte en kommentar utan vanlig text, och kommentaren syntes på alla
    109 ortssidor. Vakten mäter symptomet oavsett orsak: en oavslutad tagg,
    ett felstavat filter eller en kommentar som inte parsas fångas likadant.

    {{ }} förekommer legitimt i JSON-LD (FAQPage-strukturen), så de blocken
    klipps bort innan sökningen - inte hela markörlistan.
    """

    #: Sekvenser som aldrig ska finnas i ett renderat svar.
    MARKERS = ("{#", "#}", "{%", "%}", "{{", "}}")
    _JSON_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>.*?</script>", re.S | re.I)

    @classmethod
    def setUpTestData(cls):
        from apps.areas.models import Area, AreaLevel

        call_command("seed_site", verbosity=0)
        call_command("seed_sokordssidor", verbosity=0)
        region = Area.objects.create(name="Skåne län", level=AreaLevel.REGION)
        Area.objects.create(name="Malmö", level=AreaLevel.MUNICIPALITY, parent=region)

    def _leaks(self, html):
        body = self._JSON_LD.sub("", html)
        return [m for m in self.MARKERS if m in body]

    def _paths(self):
        """Varje sida i sitemapen plus de index som inte ligger där."""
        import re as _re

        xml = Client().get("/sitemap.xml").content.decode()
        paths = {
            _re.sub(r"^https?://[^/]+", "", u) for u in _re.findall(r"<loc>([^<]+)</loc>", xml)
        }
        paths.update({"/", "/webbyra/", "/faq/", "/kontakt/"})
        return sorted(paths)

    def test_no_template_syntax_reaches_the_visitor(self):
        client = Client()
        offenders = []
        for path in self._paths():
            html = client.get(path).content.decode()
            leaks = self._leaks(html)
            if leaks:
                offenders.append(f"{path}: {', '.join(leaks)}")
        self.assertEqual(
            offenders,
            [],
            "Orenderad mallsyntax i svaret:\n" + "\n".join(offenders),
        )

    def test_the_guard_would_catch_a_multiline_comment(self):
        """Vakten ska faktiskt fånga felet den skrevs för."""
        from django.template import Context, Template

        rendered = Template("{# rad ett\n   rad två #}\n<p>Hej</p>").render(Context({}))
        self.assertTrue(self._leaks(rendered), "en flerradig {# #} ska fångas")


class TemplateCommentSyntaxTests(TestCase):
    """
    Statisk vakt mot orsaken: {# #} får inte spänna över flera rader.

    Django stänger den kommentarsformen vid radslutet. Skriver man flera rader
    blir resten vanlig text i sidan. Felet är osynligt i koden - det ser ut som
    en kommentar - så det ska fångas vid bygget och inte av en kund.
    Flerradiga kommentarer skrivs med {% comment %}...{% endcomment %}.
    """

    def test_no_multiline_hash_comments_in_templates(self):
        base = Path(django_settings.BASE_DIR)
        offenders = []
        for path in sorted((base / "templates").rglob("*.html")):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                for match in re.finditer(r"\{#", line):
                    if "#}" not in line[match.end() :]:
                        rel = path.relative_to(base)
                        offenders.append(f"{rel}:{lineno}: {line.strip()[:70]}")
        self.assertEqual(
            offenders,
            [],
            "Flerradig {# #} - använd {% comment %}...{% endcomment %}:\n" + "\n".join(offenders),
        )


class AdminDockTests(TestCase):
    """
    Admindocken var helt död.

    Markupen fanns och CSS:en fanns - .c-admin-dock.is-open öppnar menyn -
    men INGENTING satte klassen. site.js innehöll bara mobilmenyn och
    botskyddet, så knappen gick inte att klicka på sedan den byggdes.

    Vakten nedan är den intressanta: den kontrollerar att varje id som
    markupen hänger sitt beteende på också nämns i site.js. Markup som
    skeppas utan beteende är exakt det som hände här.
    """

    #: id:n i admin_dock.html som JS:en måste känna till.
    DOCK_IDS = ("admin-dock", "admin-dock-toggle", "admin-dock-menu")

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.staff = get_user_model().objects.create_user("dockuser", password="x", is_staff=True)
        call_command("seed_site", verbosity=0)

    def test_the_dock_is_hidden_from_visitors(self):
        self.assertNotContains(Client().get("/"), "c-admin-dock")

    def test_the_dock_is_shown_to_staff(self):
        self.client.force_login(self.staff)
        html = self.client.get("/").content.decode()
        for dock_id in self.DOCK_IDS:
            self.assertIn(f'id="{dock_id}"', html)

    def test_the_toggle_is_an_orb_not_an_icon(self):
        """Knappen ritas av en shader med en CSS-orb som fallback."""
        self.client.force_login(self.staff)
        html = self.client.get("/").content.decode()
        self.assertIn('id="admin-orb-canvas"', html)
        self.assertIn("c-admin-dock__orb", html)
        self.assertIn("js/admin-orb.js", html)

    def test_the_shader_is_only_loaded_for_staff(self):
        """Besökare ska aldrig hämta en shader de inte kan se."""
        self.assertNotContains(Client().get("/"), "admin-orb.js")

    def test_the_orb_has_a_fallback_that_is_styled(self):
        """
        Utan WebGL sätts aldrig .orb-live, och då måste CSS-orben stå kvar -
        annars blir knappen en tom ruta. Samma vakt som för redigeringsorben:
        markup utan CSS är det som gick fel med pennan.
        """
        css = (Path(django_settings.BASE_DIR) / "static" / "css" / "site.css").read_text()
        for cls in (".c-admin-dock__orb", ".c-admin-dock__canvas", ".c-admin-dock.orb-live"):
            with self.subTest(cls=cls):
                self.assertIn(cls, css)

    def test_the_canvas_never_swallows_the_click(self):
        """Canvasen ligger ovanpå knappen - utan detta går den inte att klicka."""
        css = (Path(django_settings.BASE_DIR) / "static" / "css" / "site.css").read_text()
        block = css.split(".c-admin-dock__canvas {")[1].split("}")[0]
        self.assertIn("pointer-events: none", block)

    def test_the_shader_pauses_when_it_should(self):
        """En raymarchad shader ska inte köra i en dold flik eller mot någon
        som bett om mindre rörelse."""
        js = (Path(django_settings.BASE_DIR) / "static" / "js" / "admin-orb.js").read_text()
        self.assertIn("visibilitychange", js)
        self.assertIn("prefers-reduced-motion", js)
        self.assertIn("cancelAnimationFrame", js)

    def test_site_js_actually_handles_the_dock(self):
        """Utan det här är knappen ett dekorativt element."""
        js = (Path(django_settings.BASE_DIR) / "static" / "js" / "site.js").read_text()
        missing = [i for i in self.DOCK_IDS if i not in js]
        self.assertEqual(
            missing,
            [],
            f"admin_dock.html hänger beteende på id:n som site.js inte känner till: {missing}",
        )
        self.assertIn("is-open", js, "site.js sätter aldrig klassen CSS:en väntar på")


class EditOrbTests(TestCase):
    """
    Redigeringsorben (hette edit_pencil och ritade en penna fram till
    2026-08-30).

    Pennan hade INGEN CSS alls - varken .edit-pencil eller wrappern - så den
    låg ostilad i elementets övre vänstra hörn i normalt flöde. Vakten nedan
    kontrollerar att varje klass partialen använder också är definierad i
    site.css, vilket är precis det som saknades.
    """

    #: Klasser som edit_orb.html och dess wrapper hänger utseendet på.
    ORB_CLASSES = (".edit-orb", ".has-edit-orb", ".edit-orb__cloud", ".edit-orb__gloss")

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user("orbuser", password="x")
        call_command("seed_site", verbosity=0)

    def test_no_orb_for_visitors(self):
        self.assertNotContains(Client().get("/"), "edit-orb")

    def test_the_orb_is_rendered_for_logged_in_users(self):
        self.client.force_login(self.user)
        html = self.client.get("/").content.decode()
        self.assertIn('class="edit-orb"', html)
        self.assertIn("has-edit-orb", html)

    def test_the_orb_links_into_manage(self):
        self.client.force_login(self.user)
        html = self.client.get("/").content.decode()
        self.assertRegex(html, r'class="edit-orb" href="/manage/blocks/\d+/"')

    def test_the_pencil_is_gone(self):
        self.client.force_login(self.user)
        html = self.client.get("/").content.decode()
        self.assertNotIn("edit-pencil", html)
        self.assertNotIn("<svg", html.split('class="edit-orb"')[1][:400])

    def test_every_orb_class_is_styled(self):
        """Grundfelet: markup som skeppas utan CSS ser ostilad ut i hörnet."""
        css = (Path(django_settings.BASE_DIR) / "static" / "css" / "site.css").read_text()
        missing = [c for c in self.ORB_CLASSES if c not in css]
        self.assertEqual(missing, [], f"Klasser utan CSS i site.css: {missing}")

    def test_the_orb_is_positioned_left_and_centred(self):
        """Kravet: till vänster, lodrätt centrerad - inte i övre hörnet."""
        css = (Path(django_settings.BASE_DIR) / "static" / "css" / "site.css").read_text()
        block = css.split(".edit-orb {")[1].split("}")[0]
        self.assertIn("position: absolute", block)
        self.assertIn("top: 50%", block)
        self.assertIn("translateY(-50%)", block)
        self.assertIn("left:", block)
        self.assertIn(".has-edit-orb { position: relative; }", css)

    def test_the_animation_respects_reduced_motion(self):
        css = (Path(django_settings.BASE_DIR) / "static" / "css" / "site.css").read_text()
        reduced = css.split("prefers-reduced-motion")[-1]
        self.assertIn("animation: none", reduced)


class BorderPolicyTests(TestCase):
    """
    Kantlinjer: bort från meny, logotyp och kort - kvar på tabeller och fält.

    Giovannis beslut 2026-08-30. Menyn, loggan och korten bärs upp av sin
    frostade bakgrund och blev hårdare av kanten. I en tabell och ett
    formulärfält är linjen däremot funktion: den skiljer rader åt och visar
    var man får skriva. Regeln låses här för att den annars är osynlig - en
    border som smyger tillbaka syns bara för den som råkar titta.
    """

    @classmethod
    def setUpTestData(cls):
        cls.css = (Path(django_settings.BASE_DIR) / "static" / "css" / "site.css").read_text()

    def _rule(self, selector):
        """CSS-blocket för en selektor, utan efterföljande regler."""
        head = self.css.split(selector + "{", 1)
        if len(head) == 1:
            head = self.css.split(selector + " {", 1)
        self.assertEqual(len(head), 2, f"hittar inte regeln {selector}")
        return head[1].split("}", 1)[0]

    def _has_border(self, block):
        import re

        return bool(re.search(r"(^|;|\s)border\s*:", block))

    def test_the_nav_and_logo_pill_have_no_border(self):
        self.assertFalse(self._has_border(self._rule("  .pill")))

    def test_cards_and_containers_have_no_border(self):
        self.assertFalse(self._has_border(self._rule("  .panel")))

    def test_the_mobile_menu_has_no_border(self):
        self.assertFalse(self._has_border(self._rule("  .mobile-menu")))

    def test_tables_keep_their_rules(self):
        """Radlinjerna är det som gör en jämförelsetabell läsbar."""
        self.assertIn("border-bottom", self._rule("  .compare th"))
        self.assertIn("border-bottom", self._rule("  .compare td"))

    def test_form_fields_keep_their_border(self):
        """Ett fält utan kant syns inte som ett fält."""
        self.assertTrue(self._has_border(self._rule("  .nl-form input")))
        self.assertTrue(
            self._has_border(self._rule(".adx-form input,.adx-form select,.adx-form textarea"))
        )

    def test_the_ghost_button_keeps_its_outline(self):
        """Den har ingen fyllnad - utan kanten ser den inte ut som en knapp."""
        self.assertTrue(self._has_border(self._rule("  .btn.ghost")))


class RobotsAndSchemaTests(TestCase):
    """
    Sökmotorgrunden (2026-08-30): robots.txt fanns inte alls, och den enda
    strukturerade datan var FAQPage på /faq/ - trots att SiteSettings bar
    alla fält för Organization/LocalBusiness.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)

    def _schemas(self, path):
        import json as _json

        html = Client().get(path).content.decode()
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        return [_json.loads(b) for b in blocks]

    def test_robots_txt_exists_and_points_at_the_sitemap(self):
        response = Client().get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Sitemap:", body)
        self.assertIn("/sitemap.xml", body)
        self.assertIn("Disallow: /manage/", body)

    def test_every_page_carries_organization_and_website_schema(self):
        types = [s.get("@type") for s in self._schemas("/")]
        self.assertIn(["Organization", "ProfessionalService"], types)
        self.assertIn("WebSite", types)

    def test_area_pages_carry_breadcrumbs_with_the_parent_chain(self):
        from apps.areas.models import Area, AreaLevel

        lan = Area.objects.create(name="Skåne län", level=AreaLevel.REGION)
        kommun = Area.objects.create(name="Malmö", level=AreaLevel.MUNICIPALITY, parent=lan)
        crumbs = next(
            s for s in self._schemas(kommun.get_absolute_url()) if s["@type"] == "BreadcrumbList"
        )
        names = [i["name"] for i in crumbs["itemListElement"]]
        self.assertEqual(names, ["Hem", "Orter", "Skåne län", "Malmö"])

    def test_service_pages_carry_service_schema(self):
        schemas = self._schemas("/webbutveckling/")
        service = next(s for s in schemas if s.get("@type") == "Service")
        self.assertEqual(service["provider"]["@id"].split("#")[1], "organization")
        self.assertTrue(service["name"])

    def test_faq_blocks_carry_faqpage_schema(self):
        """Schemat fanns bara på /faq/ - inte där sektionerna faktiskt möts."""
        schemas = self._schemas("/webbutveckling/")
        self.assertTrue(any(s.get("@type") == "FAQPage" for s in schemas))

    def test_schema_json_is_valid_on_every_public_page_type(self):
        """Trasig JSON-LD är värre än ingen - Google ignorerar hela blocket."""
        for path in ("/", "/webbutveckling/", "/kontakt/"):
            with self.subTest(path=path):
                self.assertTrue(self._schemas(path))  # json.loads höjer vid fel
