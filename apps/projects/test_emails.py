"""Kundmejlens form: HTML-version i ramen, textversion som börjar med stor bokstav."""

import re
from types import SimpleNamespace

from django.test import TestCase

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
