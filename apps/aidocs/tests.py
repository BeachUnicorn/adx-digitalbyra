"""
AI-guiderna: handskakningen är öppen och hemlighetsfri, guiderna kräver en
giltig kod, koderna är kortlivade, återkallbara och loggade.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import AccessCode, hash_code


@override_settings(ADX_STATUS_KEY="delad-hemlig-nyckel")
class AiGuideTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = get_user_model().objects.create_user("g", password="x", is_staff=True)
        self.code_obj, self.code = AccessCode.issue(self.staff, hours=1, note="test")

    def test_handshake_is_open_and_holds_no_secrets(self):
        r = Client().get("/aiz/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/markdown", r["Content-Type"])
        body = r.content.decode()
        self.assertIn("/aiz/guide/?kod=", body)
        self.assertIn("ADX-XXXX-XXXX", body)
        self.assertNotIn("delad-hemlig-nyckel", body)
        self.assertEqual(r["X-Robots-Tag"], "noindex, nofollow")

    def test_guides_require_a_valid_code(self):
        self.assertEqual(Client().get("/aiz/guide/").status_code, 403)
        self.assertEqual(
            Client().get("/aiz/guide/overvakning/", {"kod": "ADX-FELF-ELFE"}).status_code, 403
        )
        r = Client().get("/aiz/guide/", {"kod": self.code})
        self.assertEqual(r.status_code, 200)
        self.assertIn("/aiz/guide/overvakning/?kod=", r.content.decode())

    def test_the_guide_carries_source_key_and_steps(self):
        body = Client().get("/aiz/guide/overvakning/", {"kod": self.code.lower()}).content.decode()
        self.assertIn("def status_view(request):", body, "källkoden läses ur repot")
        self.assertIn("ADX_STATUS_KEY=delad-hemlig-nyckel", body)
        self.assertIn('path("status/adx/", status_view', body)
        self.assertIn("vår plattform", body)
        data = Client().get("/aiz/guide/overvakning/", {"kod": self.code, "format": "json"}).json()
        self.assertEqual(data["files"][0]["path"], "core/status_endpoint.py")
        self.assertIn("status_view", data["files"][0]["content"])
        self.assertEqual(data["env"]["ADX_STATUS_KEY"], "delad-hemlig-nyckel")

    def test_header_works_and_every_fetch_is_logged(self):
        r = Client().get(
            "/aiz/guide/overvakning/", HTTP_X_ADX_CODE=self.code, HTTP_USER_AGENT="Claude-Test"
        )
        self.assertEqual(r.status_code, 200)
        self.code_obj.refresh_from_db()
        self.assertEqual(self.code_obj.uses, 1)
        entry = self.code_obj.log.get()
        self.assertEqual((entry.guide, entry.user_agent), ("overvakning", "Claude-Test"))

    def test_expired_and_revoked_codes_stop_working(self):
        self.code_obj.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        self.code_obj.save()
        self.assertEqual(Client().get("/aiz/guide/", {"kod": self.code}).status_code, 403)
        _obj, other = AccessCode.issue(self.staff, hours=24)
        client = Client()
        client.force_login(self.staff)
        client.post(f"/manage/ai-guider/{_obj.pk}/aterkalla/")
        self.assertEqual(Client().get("/aiz/guide/", {"kod": other}).status_code, 403)

    def test_codes_are_hashed_and_shown_once(self):
        self.assertNotIn(self.code[-4:], self.code_obj.code_hash)
        self.assertEqual(self.code_obj.code_hash, hash_code(self.code.replace("-", " ").lower()))
        client = Client()
        client.force_login(self.staff)
        client.post("/manage/ai-guider/ny/", {"hours": "24", "note": "BD Group"})
        first = client.get("/manage/ai-guider/").content.decode()
        self.assertIn("visas bara nu", first)
        self.assertRegex(first, r"ADX-[A-Z2-9]{4}-[A-Z2-9]{4}")
        second = client.get("/manage/ai-guider/").content.decode()
        self.assertNotIn("visas bara nu", second)
        self.assertIn("BD Group", second)

    def test_guessing_is_rate_limited(self):
        client = Client()
        for _ in range(10):
            self.assertEqual(client.get("/aiz/guide/", {"kod": "ADX-AAAA-AAAA"}).status_code, 403)
        self.assertEqual(client.get("/aiz/guide/", {"kod": self.code}).status_code, 429)

    def test_the_section_stays_out_of_search_and_analytics(self):
        robots = Client().get("/robots.txt").content.decode()
        self.assertIn("Disallow: /aiz/", robots)
        self.assertNotIn("Disallow: /ai/", robots, "/ai/ är fri för innehåll")
        from apps.analytics.middleware import _SKIP_PREFIXES

        self.assertIn("/aiz/", _SKIP_PREFIXES)

    def test_customer_contacts_cannot_create_codes(self):
        from apps.projects.models import Customer

        user = get_user_model().objects.create_user("kund@x.se", password="x")
        Customer.objects.create(name="Kund").users.add(user)
        client = Client()
        client.force_login(user)
        self.assertEqual(client.post("/manage/ai-guider/ny/").status_code, 302)
        self.assertEqual(AccessCode.objects.count(), 1)
