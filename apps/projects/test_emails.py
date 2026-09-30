"""Kundmejlens form: HTML-version i ramen, textversion som börjar med stor bokstav."""

import re
from datetime import date
from types import SimpleNamespace

from django.core import mail
from django.test import TestCase, override_settings

from apps.projects import emails


class CustomerMailTests(TestCase):
    def test_every_preview_renders_in_the_frame(self):
        for name in ("inbjudan", "kod", "arende", "logg"):
            _title, html = emails.preview(name)
            self.assertIn("adx-logo.png", html, name)
            self.assertIn("Vänliga hälsningar", html, name)
            self.assertIn('lang="sv"', html, name)

    def test_plain_text_versions_start_every_sentence_with_a_capital(self):
        entry = SimpleNamespace(
            date=__import__("datetime").date(2026, 9, 7), text="Gjorde x.", pk=1
        )
        body = emails.log_digest_body(SimpleNamespace(name="Acme AB"), [entry], "september 2026")
        # Efter hälsningen och en tomrad ska nästa rad börja med versal.
        for text in (body,):
            for paragraph in re.split(r"\n\n+", text):
                first = paragraph.strip()[:1]
                if first.isalpha():
                    self.assertTrue(first.isupper(), paragraph[:40])

    def test_user_text_is_escaped_in_the_html(self):
        comment = SimpleNamespace(body="<script>alert(1)</script>")
        html = emails._html(
            "issue_update", "x", "Rubrik", comment=comment, issue_url="", reply_hint=""
        )
        self.assertNotIn("<script>alert(1)</script>", html)


EMAIL = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST_USER": "x",
    "EMAIL_HOST_PASSWORD": "x",
    "INQUIRY_NOTIFICATION_EMAIL": "notiser@example.com",
    "CUSTOMER_REPLY_TO_EMAIL": "giovanni@adx.se",
}


@override_settings(**EMAIL)
class ReplyToTests(TestCase):
    """Kundens svar går till giovanni@adx.se - inte till inkorgen för byråns notiser."""

    def test_customer_mails_reply_to_the_customer_address(self):
        user = SimpleNamespace(first_name="Nina", email="nina@acme.se")
        customer = SimpleNamespace(
            name="Acme AB", users=SimpleNamespace(all=lambda: [user]), email=""
        )
        emails.send_invite(user, customer)
        emails.send_login_code(user, "123456")
        entry = SimpleNamespace(date=date(2026, 9, 7), text="Gjorde x.", pk=1)
        emails.send_log_digest(customer, [entry], "september 2026")
        self.assertEqual(len(mail.outbox), 3)
        for message in mail.outbox:
            self.assertEqual(message.reply_to, ["giovanni@adx.se"], message.subject)

    def test_the_preview_shows_the_same_reply_address(self):
        from django.contrib.auth import get_user_model

        from apps.projects.models import Customer, CustomerLogEntry

        staff = get_user_model().objects.create_user("byra", password="x12345678", is_staff=True)
        customer = Customer.objects.create(name="Acme AB", email="info@acme.se")
        CustomerLogEntry.objects.create(customer=customer, text="Gjorde x.")
        self.client.force_login(staff)
        page = self.client.get(f"/manage/kunder/{customer.pk}/logg/forhandsgranska/")
        self.assertContains(page, "giovanni@adx.se")
        self.assertNotContains(page, "notiser@example.com")
