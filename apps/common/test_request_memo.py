"""Minnet per förfrågan (apps/common/request_memo.py)."""

import asyncio

from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from apps.common import request_memo
from apps.common.request_memo import request_memo_middleware
from apps.website.models import BlockPage, SiteSettings


class RequestMemoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.home = BlockPage.objects.create(title="Hem", slug="hem", is_published=True)
        cls.other = BlockPage.objects.create(title="Annan", slug="annan", is_published=True)
        settings = SiteSettings.load()
        settings.homepage = cls.home
        settings.save()

    def run_in(self, method, fn):
        """fn() inne i en förfrågan med metoden, genom middlewaren."""
        result = {}

        def get_response(request):
            result["value"] = fn()
            return HttpResponse()

        request = getattr(RequestFactory(), method.lower())("/")
        request_memo_middleware(get_response)(request)
        return result["value"]

    def urls(self):
        return [page.get_absolute_url() for page in (self.home, self.other) * 10]

    def test_outside_a_request_every_call_asks(self):
        self.assertFalse(request_memo.active())
        with self.assertNumQueries(20):
            urls = self.urls()
        self.assertEqual(urls[:2], ["/", "/annan/"])

    def test_a_get_asks_once(self):
        with self.assertNumQueries(1):
            urls = self.run_in("GET", self.urls)
        self.assertEqual(urls[:2], ["/", "/annan/"])
        self.assertFalse(request_memo.active())

    def test_a_post_remembers_nothing(self):
        with self.assertNumQueries(20):
            self.run_in("POST", self.urls)

    def test_a_save_inside_the_request_forgets(self):
        def change_homepage():
            before = self.other.get_absolute_url()
            settings = SiteSettings.load()
            settings.homepage = self.other
            settings.save()
            return before, self.other.get_absolute_url(), SiteSettings.cached().homepage_id

        before, after, cached_id = self.run_in("GET", change_homepage)
        self.assertEqual(before, "/annan/")
        self.assertEqual(after, "/")
        self.assertEqual(cached_id, self.other.pk)

    def test_without_settings_no_page_is_the_homepage(self):
        SiteSettings.objects.all().delete()
        unsaved = BlockPage(title="Ny", slug="ny")
        # Samma svar som före minnet: utan inställningsrad finns ingen
        # startsida, inte ens för en osparad sida.
        self.assertEqual(self.run_in("GET", unsaved.get_absolute_url), "/ny/")
        self.assertEqual(self.run_in("GET", self.home.get_absolute_url), "/hem/")

    def test_async_mode(self):
        seen = []

        async def get_response(request):
            seen.append(request_memo.active())
            seen.append(request_memo.memo("k", object) is request_memo.memo("k", object))
            return HttpResponse()

        middleware = request_memo_middleware(get_response)
        asyncio.run(middleware(RequestFactory().get("/")))
        self.assertEqual(seen, [True, True])
        self.assertFalse(request_memo.active())
