"""
Förfrågningsflödets vakttester: botskyddets tre lager, den tysta fejkade
framgången (en fälld bot får exakt samma svar som en människa, utan
sidoeffekter), och att ett äkta flöde skapar raden med attribution.
"""

import time

from django.core import signing
from django.test import Client, TestCase
from django.urls import reverse

from apps.inquiries.models import Inquiry, NewsletterSignup


def _aged_token(seconds_ago=10):
    """En giltig botcheck-token utfärdad bakåt i tiden (människotempo)."""
    return signing.dumps({"t": int(time.time()) - seconds_ago}, salt="botcheck")


def _valid_post(**overrides):
    token = _aged_token()
    data = {
        "topic": "Annat / vet inte än",
        "company_name": "Testbolaget AB",
        "name": "Test Person",
        "email": "test@example.com",
        "phone": "",
        "customer_type": "company",
        "budget": "25_100",
        "timeline": "quarter",
        "description": "Vi behöver en ny webbplats.",
        "bc_website": "",
        "bc_time": token,
        "bc_proof": token,
    }
    data.update(overrides)
    return data


class BotcheckTests(TestCase):
    def test_real_submission_creates_inquiry(self):
        response = Client().post(reverse("inquiries:submit"), _valid_post())
        self.assertEqual(response.status_code, 302)
        inquiry = Inquiry.objects.get()
        self.assertEqual(inquiry.name, "Test Person")
        self.assertEqual(inquiry.topic, "Annat / vet inte än")
        self.assertIn(inquiry.reference, response["Location"])

    def test_honeypot_gets_silent_fake_success(self):
        """Boten får samma redirect som en människa - och ingen rad."""
        response = Client().post(
            reverse("inquiries:submit"), _valid_post(bc_website="spam.example")
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/forfragan/tack/", response["Location"])
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_too_fast_submission_is_rejected_silently(self):
        token = signing.dumps({"t": int(time.time())}, salt="botcheck")
        response = Client().post(
            reverse("inquiries:submit"), _valid_post(bc_time=token, bc_proof=token)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_missing_js_proof_is_rejected_silently(self):
        response = Client().post(reverse("inquiries:submit"), _valid_post(bc_proof=""))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Inquiry.objects.count(), 0)

    def test_fake_reference_looks_real(self):
        """Fejkreferensen ur den tysta framgången har äkta format."""
        response = Client().post(reverse("inquiries:submit"), _valid_post(bc_website="x"))
        reference = response["Location"].rstrip("/").rsplit("/", 1)[-1]
        self.assertTrue(reference.startswith("ADX-"), reference)

    def test_invalid_form_rerenders_with_errors(self):
        response = Client().post(reverse("inquiries:submit"), _valid_post(email="inte-en-adress"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "adx-form")

    def test_thank_you_never_hits_the_database(self):
        """Tack-sidan slår inte upp referensen - fejk och äkta ser identiska ut."""
        response = Client().get(
            reverse("inquiries:thank_you", kwargs={"reference": "ADX-FAKE1234"})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ADX-FAKE1234")


class NewsletterTests(TestCase):
    def test_signup_with_botcheck(self):
        token = _aged_token()
        response = Client().post(
            "/nyhetsbrev/",
            {
                "email": "prenumerant@example.com",
                "bc_website": "",
                "bc_time": token,
                "bc_proof": token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NewsletterSignup.objects.count(), 1)

    def test_bot_signup_is_silently_dropped(self):
        token = _aged_token()
        response = Client().post(
            "/nyhetsbrev/",
            {"email": "bot@example.com", "bc_website": "spam", "bc_time": token, "bc_proof": token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(NewsletterSignup.objects.count(), 0)

    def test_duplicate_email_does_not_crash(self):
        token = _aged_token()
        for _ in range(2):
            Client().post(
                "/nyhetsbrev/",
                {
                    "email": "same@example.com",
                    "bc_website": "",
                    "bc_time": token,
                    "bc_proof": token,
                },
            )
        self.assertEqual(NewsletterSignup.objects.count(), 1)
