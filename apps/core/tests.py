"""
Felsidorna (apps/core/errors.py). Testklienten kör med DEBUG av, så en
okänd adress går genom handler404 precis som i produktion.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.core import errors
from apps.projects.models import Customer
from apps.website.models import BlockPage


class NotFoundPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        BlockPage.objects.create(title="Webbutveckling", slug="webbutveckling", is_published=True)
        BlockPage.objects.create(title="Hemligt utkast", slug="webbutvecklin", is_published=False)

    def setUp(self):
        cache.clear()

    def test_site_404_is_the_sites_own_page(self):
        response = self.client.get("/finns-inte-alls/")
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("Sidan finns", html)
        self.assertIn("/finns-inte-alls/", html)
        self.assertIn("css/site.css", html)  # sajtens ram, inte ramverkets nakna sida
        self.assertIn('content="noindex"', html)
        self.assertNotIn("The requested resource", html)

    def test_the_floating_digits_respect_reduced_motion(self):
        from django.conf import settings

        css = (settings.BASE_DIR / "static" / "css" / "site.css").read_text()
        blocks = css.split("prefers-reduced-motion")[1:]
        self.assertTrue(any(".nf-code span{animation:none}" in b[:80] for b in blocks))

    def test_suggests_the_page_the_visitor_probably_meant(self):
        response = self.client.get("/webutveckling/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Menade du", status_code=404)
        self.assertContains(response, 'href="/webbutveckling/"', status_code=404)

    def test_never_suggests_an_unpublished_page(self):
        urls = [s["url"] for s in errors.suggestions_for("/webbutvecklin/")]
        self.assertIn("/webbutveckling/", urls)
        self.assertNotIn("/webbutvecklin/", urls)

    def test_no_suggestions_for_nonsense_or_very_short_paths(self):
        self.assertEqual(errors.suggestions_for("/xq/"), [])
        self.assertEqual(errors.suggestions_for("/qqqqzzzzxxxx/"), [])

    def test_requested_path_is_escaped(self):
        response = self.client.get("/<script>alert(1)</script>/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("<script>alert(1)</script>", response.content.decode())

    def test_probes_get_one_line_and_no_database(self):
        for path in ("/wp-login.php", "/.env", "/wp-admin/setup-config.php", "/.git/config"):
            with self.assertNumQueries(0):
                response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response["Content-Type"], "text/plain", path)

    def test_offer_links_get_their_own_words_and_no_suggestions(self):
        response = self.client.get("/offert/avklippt/")
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("Offertlänken är ofullständig", html)
        self.assertNotIn("Menade du", html)
        self.assertNotIn("/offert/avklippt/", html)  # token-lika adresser ekas inte

    def test_ai_guides_answer_in_text(self):
        response = self.client.get("/aiz/finns-inte/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response["Content-Type"].startswith("text/markdown"))
        self.assertIn("/aiz/", response.content.decode())

    def test_json_clients_get_json(self):
        response = self.client.get("/finns-inte/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"ok": False, "error": "not_found"})

    def test_panel_404_uses_the_panel_for_the_agency_only(self):
        anonymous = self.client.get("/manage/finns-inte/")
        self.assertEqual(anonymous.status_code, 404)
        self.assertNotIn("manage-skin.css", anonymous.content.decode())

        user = get_user_model().objects.create_user("byra", password="x12345678", is_staff=True)
        self.client.force_login(user)
        response = self.client.get("/manage/finns-inte/")
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("manage-skin.css", html)
        self.assertIn("Till översikten", html)

    def test_portal_404_leads_the_contact_back_to_their_issues(self):
        contact = get_user_model().objects.create_user("nina", email="nina@example.com")
        Customer.objects.create(name="Acme AB").users.add(contact)
        self.client.force_login(contact)
        response = self.client.get("/kund/finns-inte/")
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("Till mina ärenden", html)
        self.assertIn("Mina ärenden", html)  # portalens meny, inte den utloggade

    def test_portal_404_when_logged_out_offers_login(self):
        response = self.client.get("/kund/finns-inte/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Logga in", status_code=404)

    def test_falls_back_to_the_standalone_page_when_rendering_fails(self):
        with mock.patch.object(errors, "_render_404", side_effect=RuntimeError("db nere")):
            with self.assertLogs("apps.core.errors", level="ERROR"):
                response = self.client.get("/finns-inte/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Sidan finns inte", response.content.decode())


class ServerErrorPageTests(TestCase):
    def test_500_needs_neither_database_nor_context(self):
        from django.test import RequestFactory

        with self.assertNumQueries(0):
            response = errors.server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        html = response.content.decode()
        self.assertIn("Något gick fel hos oss", html)
        self.assertIn("Försök igen", html)

    def test_a_crashing_view_shows_the_500_page(self):
        self.client.raise_request_exception = False
        with mock.patch("apps.core.views.JsonResponse", side_effect=RuntimeError("pang")):
            response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Något gick fel hos oss", response.content.decode())

    def test_standalone_templates_render_without_context(self):
        from django.template import loader

        for name in ("400", "403", "403_csrf", "404", "500"):
            html = loader.get_template(f"{name}.html").render()
            self.assertIn("adx-logo.png", html, name)
            self.assertIn("Till startsidan", html, name)

    def test_csrf_failure_shows_the_friendly_page(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        response = client.post("/kund/logga-in/", {"email": "a@example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("Sidan har legat öppen för länge", response.content.decode())


class PreviewRouteTests(TestCase):
    def test_preview_route_does_not_exist_outside_debug(self):
        response = self.client.get("/_fel/500/")
        self.assertEqual(response.status_code, 404)

    @override_settings(DEBUG=True)
    def test_preview_view_renders_each_page(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/_fel/404/", {"path": "/webutveckling/"})
        request.user = mock.Mock(is_authenticated=False)
        self.assertEqual(errors.preview(request, "404").status_code, 404)
        self.assertEqual(errors.preview(request, "500").status_code, 500)
        self.assertEqual(errors.preview(request, "403_csrf").status_code, 403)
