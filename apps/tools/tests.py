"""
Tester för Hemsidekollen.

Tyngdpunkten ligger på två saker: att SSRF-skyddet håller (verktyget hämtar
adresser användare skriver in, på en server med instansroll), och att
kontrollerna dömer rätt på känd HTML. Nätverk mockas - tester som ringer
internet är inte tester.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .analyzer import (
    AnalysError,
    Sida,
    _Extractor,
    check_teknik,
    check_tillganglighet,
    normalize_url,
)
from .models import SiteReport

GOOD_HTML = """<!doctype html><html lang="sv"><head>
<title>Testbolaget - snickare i Umeå</title>
<meta name="description" content="Vi bygger altaner och renoverar kök i Umeå med omnejd.">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head><body><main>
<h1>Snickare i Umeå</h1><h2>Altaner</h2>
<img src="a.jpg" alt="Färdig altan">
<label for="e">E-post</label><input id="e" type="email">
</main></body></html>"""

BAD_HTML = """<html><head><title></title>
<meta name="viewport" content="width=device-width, user-scalable=no">
</head><body>
<h1>Ett</h1><h1>Två</h1><h3>Hoppade över H2</h3>
<img src="a.jpg"><img src="b.jpg">
<input type="text"><textarea></textarea>
</body></html>"""


def _sida(html, url="https://example.se/", ms=300):
    s = Sida()
    s.url = url
    s.status = 200
    s.ms = ms
    s.bytes = len(html)
    s.html = html
    s.headers = {"content-encoding": "gzip"}
    return s


def _extract(html):
    e = _Extractor()
    e.feed(html)
    return e


class SsrfGuardTests(TestCase):
    """Det farliga: en URL en användare valt hämtas från VÅR server."""

    def test_private_addresses_are_refused(self):
        from apps.tools.analyzer import fetch

        for target in (
            "http://169.254.169.254/latest/meta-data/",  # EC2-metadata = instansrollens nycklar
            "http://localhost/",
            "http://127.0.0.1/manage/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
        ):
            with self.subTest(target=target):
                with self.assertRaises(AnalysError):
                    fetch(target)

    def test_only_http_and_https(self):
        with self.assertRaises(AnalysError):
            normalize_url("ftp://example.se/")

    def test_odd_ports_are_refused(self):
        with self.assertRaises(AnalysError):
            normalize_url("https://example.se:8443/")

    def test_a_bare_domain_is_normalised_to_https(self):
        self.assertEqual(normalize_url("example.se"), "https://example.se/")


class CheckTests(TestCase):
    def _by_title(self, rows):
        return {r["titel"]: r for r in rows}

    def test_good_page_passes_the_checks(self):
        rows = self._by_title(check_teknik(_sida(GOOD_HTML), _extract(GOOD_HTML)))
        self.assertEqual(rows["Sidtitel"]["status"], "ok")
        self.assertEqual(rows["Metabeskrivning"]["status"], "ok")
        self.assertEqual(rows["H1-rubrik"]["status"], "ok")
        self.assertEqual(rows["Mobilanpassning"]["status"], "ok")
        till = self._by_title(check_tillganglighet(_extract(GOOD_HTML)))
        self.assertEqual(till["Språkangivelse"]["status"], "ok")
        self.assertEqual(till["Alt-texter"]["status"], "ok")
        self.assertEqual(till["Formuläretiketter"]["status"], "ok")
        self.assertEqual(till["Zoom"]["status"], "ok")

    def test_bad_page_is_called_out(self):
        rows = self._by_title(check_teknik(_sida(BAD_HTML), _extract(BAD_HTML)))
        self.assertEqual(rows["Sidtitel"]["status"], "fel")
        self.assertEqual(rows["H1-rubrik"]["status"], "varning")
        till = self._by_title(check_tillganglighet(_extract(BAD_HTML)))
        self.assertEqual(till["Språkangivelse"]["status"], "fel")
        self.assertEqual(till["Formuläretiketter"]["status"], "fel")
        self.assertEqual(till["Zoom"]["status"], "fel")
        self.assertEqual(till["Rubrikordning"]["status"], "varning")

    def test_wordpress_generator_is_flagged(self):
        html = GOOD_HTML.replace(
            "</head>", '<meta name="generator" content="WordPress 6.4"></head>'
        )
        rows = self._by_title(check_teknik(_sida(html), _extract(html)))
        self.assertEqual(rows["Plattform"]["status"], "varning")


class ViewTests(TestCase):
    """Testfasen: verktyget finns BARA i /manage/ - ingen publik yta."""

    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("verktygare", password="x", is_staff=True)
        self.visitor = User.objects.create_user("besokare", password="x")

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("manage:hemsidekollen"))
        self.assertEqual(response.status_code, 302)

    def test_non_staff_gets_404(self):
        self.client.force_login(self.visitor)
        self.assertEqual(self.client.get(reverse("manage:hemsidekollen")).status_code, 404)

    def test_not_in_the_sitemap_and_blocked_by_robots(self):
        """Publicering är ett beslut, inte en bieffekt."""
        xml = self.client.get("/sitemap.xml").content.decode()
        self.assertNotIn("hemsidekollen", xml)
        robots = self.client.get("/robots.txt").content.decode()
        self.assertIn("Disallow: /manage/", robots)

    @patch("apps.tools.views.analyze")
    def test_a_run_is_saved_to_history(self, mock_analyze):
        mock_analyze.return_value = {
            "url": "https://example.se/",
            "status": 200,
            "grupper": [],
            "summering": {"ok": 1, "varningar": 0, "fel": 0},
        }
        self.client.force_login(self.staff)
        response = self.client.post(reverse("manage:hemsidekollen"), {"url": "example.se"})
        self.assertEqual(response.status_code, 200)
        report = SiteReport.objects.get()
        self.assertEqual(report.url, "https://example.se/")
        self.assertEqual(report.created_by, self.staff)

    @patch("apps.tools.views.analyze", side_effect=AnalysError("Kunde inte hämta"))
    def test_an_error_is_shown_not_raised(self, mock_analyze):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("manage:hemsidekollen"), {"url": "example.se"})
        self.assertContains(response, "Kunde inte hämta")
        self.assertEqual(SiteReport.objects.count(), 0)
