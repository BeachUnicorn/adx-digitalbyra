"""/smsz/: den öppna dokumentationen för AI-assistenter i andra projekt."""

from django.core.cache import cache
from django.test import TestCase

from . import service


class SmszTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_open_without_login_as_markdown(self):
        response = self.client.get("/smsz/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/markdown"))
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        text = response.content.decode()
        self.assertIn("http://testserver/api/sms/v1", text)
        self.assertIn("http://testserver/kund/sms/nycklar/", text)
        self.assertIn("ADX_SMS_KEY", text)
        self.assertIn("Be aldrig användaren klistra in nyckeln i chatten", text)

    def test_every_error_code_is_documented(self):
        text = self.client.get("/smsz/").content.decode()
        for code, status in service.HTTP_STATUS.items():
            with self.subTest(code=code):
                self.assertIn(f"| `{code}` | {status} |", text)

    def test_json_format(self):
        data = self.client.get("/smsz/?format=json").json()
        self.assertEqual(data["api_base"], "http://testserver/api/sms/v1")
        self.assertEqual({e["code"] for e in data["errors"]}, set(service.HTTP_STATUS))
        self.assertEqual(data["limits"]["max_parts"], service.MAX_PARTS)

    def test_no_typographic_characters(self):
        text = self.client.get("/smsz/").content.decode()
        for code in (0x2013, 0x2014, 0x201C, 0x201D, 0x2019, 0x2026):
            self.assertNotIn(chr(code), text)

    def test_only_get_and_head(self):
        self.assertEqual(self.client.head("/smsz/").status_code, 200)
        self.assertEqual(self.client.post("/smsz/").status_code, 405)
