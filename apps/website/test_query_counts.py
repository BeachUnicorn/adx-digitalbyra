"""
Antalet databasfrågor per publik sida är fast.

Varje menylänk slog upp startsidan på nytt och varje länk i blocken hämtade
sin sida för sig: 97 frågor på en branschsida, 68 på 404-sidan (Sentry
ADX-DIGITALBYRA-8, F, G, H och J). Under skannerskuren 2026-10-09 höll
varje sådan förfrågan sin Postgres-anslutning hela tiden. Nu läses
inställningarna, menyerna, tjänsterna och startsidan en gång per förfrågan
(apps/common/request_memo.py, apps/website/chrome.py), och sidorna som
blockens länkar pekar på i en fråga (links.prime_pages).

Ändras en siffra här ska det vara för att sidan faktiskt behöver en fråga
till, inte för att en ny loop frågar per rad.
"""

from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from apps.website.models import Block, BlockPage, Menu, MenuItem


def _count(client, path):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(path)
    return response, len(ctx.captured_queries)


class PageQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_site", verbosity=0)
        call_command("seed_sokordssidor", verbosity=0)

    def test_industry_page(self):
        # Hero med sidlänkar, related-block med path-länkar, FAQ-block,
        # länkringen, menyerna och sidfoten: 97 frågor före.
        #   sidan, inställningarna, startsidans id, huvudmenyn (3),
        #   sidfoten (3), tjänsterna, blocken, länkarnas sidor (id och
        #   slug, 2), ringen, FAQ-sektionen och dess frågor (2).
        with self.assertNumQueries(16):
            response = self.client.get("/hemsida-elfirma/")
        self.assertContains(response, "Hemsida för elfirma")
        self.assertContains(response, 'href="/hemsida-vvs/"')  # path-länk i related
        self.assertContains(response, 'href="/kontakt/"')  # sidlänk i hero och bar

    def test_service_page(self):
        with self.assertNumQueries(14):
            response = self.client.get("/webbutveckling/")
        self.assertEqual(response.status_code, 200)

    def test_homepage(self):
        with self.assertNumQueries(12):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_404_page(self):
        # Sidan som inte finns (1), sedan 404-sidans meny, sidfot och
        # tjänster ur samma minne: 68 frågor före. Förslagen ("menade du")
        # läses ur sitemapen en gång per tio minuter och process
        # (apps/core/errors.py); de hämtas före mätningen.
        cache.clear()
        self.client.get("/finns-inte-heller/")
        with self.assertNumQueries(10):
            response = self.client.get("/finns-inte-alls/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Sidan finns", status_code=404)

    def test_faq_index(self):
        # En fråga för alla sektioner med antalet frågor, inte en per sektion.
        with self.assertNumQueries(10):
            response = self.client.get("/faq/")
        self.assertEqual(response.status_code, 200)

    def test_more_menu_links_cost_no_queries(self):
        """Fler poster i menyerna ger inte fler frågor (det var 2-3 per post)."""
        _, before = _count(self.client, "/hemsida-elfirma/")
        header = Menu.objects.get(location="header")
        footer = Menu.objects.filter(location="footer").first()
        pages = list(BlockPage.objects.filter(is_published=True, design="")[:12])
        for i, page in enumerate(pages):
            MenuItem.objects.create(menu=header, label=f"Extra {i}", page=page, order=500 + i)
            MenuItem.objects.create(menu=footer, label=f"Extra {i}", page=page, order=500 + i)
        response, after = _count(self.client, "/hemsida-elfirma/")
        self.assertContains(response, "Extra 11")
        self.assertEqual(after, before)

    def test_more_block_links_cost_no_queries(self):
        """Fler sidlänkar i blocken ger inte fler frågor (det var 4 per länk)."""
        _, before = _count(self.client, "/hemsida-elfirma/")
        page = BlockPage.objects.get(slug="hemsida-elfirma")
        targets = list(BlockPage.objects.filter(is_published=True, design="").exclude(pk=page.pk))
        Block.objects.create(
            page=page,
            block_type="related",
            order=999,
            data={
                "title": "Fler sidor",
                "links": [
                    {"label": f"Länk {i}", "url": {"kind": "page", "id": target.pk}}
                    for i, target in enumerate(targets[:10])
                ]
                + [
                    {"label": f"Väg {i}", "url": {"kind": "path", "path": f"/{target.slug}/"}}
                    for i, target in enumerate(targets[10:20])
                ],
            },
        )
        response, after = _count(self.client, "/hemsida-elfirma/")
        self.assertContains(response, "Länk 9")
        self.assertContains(response, "Väg 9")
        # Blocket i sig kostar inget: sidorna hämtas redan i de två frågor
        # som länkarna i de andra blocken använder.
        self.assertEqual(after, before)

    def test_odd_block_data_fails_no_page_the_templates_can_render(self):
        """Förhämtningen hoppar över data av fel form. Den får aldrig fälla en
        sida som mallarna klarar: varje block ger samma svar med och utan."""
        page = BlockPage.objects.get(slug="hemsida-elfirma")
        odd = [
            [1, 2],
            "en sträng",
            {"title": "Strängen", "links": "inte en lista"},
            {"title": "Objektet", "links": {"url": {"kind": "page", "id": 1}}},
            {
                "title": "Konstiga länkar",
                "links": [
                    "en sträng",
                    {"label": "Sida utan id", "url": {"kind": "page"}},
                    {"label": "Utan url"},
                    {"label": "Tom väg", "url": {"kind": "path", "path": ""}},
                    {"label": "Väg utan sida", "url": {"kind": "path", "path": "/finns-ej/"}},
                ],
            },
        ]
        client = Client(raise_request_exception=False)
        for data in odd:
            with self.subTest(data=data):
                block = Block.objects.create(page=page, block_type="related", order=999, data=data)
                with mock.patch("apps.website.views.prime_pages"):
                    without = client.get("/hemsida-elfirma/").status_code
                primed = client.get("/hemsida-elfirma/").status_code
                block.delete()
                self.assertEqual(without, 200)
                self.assertEqual(primed, 200)
