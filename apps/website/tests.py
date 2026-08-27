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
