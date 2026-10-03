"""
Tester för Hemsidekollen.

Tyngdpunkten ligger på två saker: att SSRF-skyddet håller (verktyget hämtar
adresser användare skriver in, på en server med instansroll), och att
kontrollerna dömer rätt på känd HTML. Nätverk mockas - tester som ringer
internet är inte tester.
"""

import contextlib
import ipaddress
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import analyzer
from .analyzer import (
    AnalysError,
    Sida,
    _Extractor,
    check_teknik,
    check_tillganglighet,
    fetch,
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

    def test_non_staff_never_reaches_the_tool(self):
        """Portalgrinden släpper bara in staff i /manage/ (sedan 2026-10-03)."""
        self.client.force_login(self.visitor)
        response = self.client.get(reverse("manage:hemsidekollen"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/kund/"))

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


# ---------------------------------------------------------------------------
# Hämtningen mot en riktig server (granskningen 2026-10-03: redirects följdes
# utan kontroll, och DNS kunde bytas mellan kontroll och anrop)
# ---------------------------------------------------------------------------

#: Värdnamnet testerna låtsas är publikt. Det löses upp till testservern på
#: 127.0.0.1; allt annat går genom det riktiga SSRF-skyddet.
PUBLIC_HOST = "sajt.example.se"


class LocalSite:
    """En riktig HTTP-server på 127.0.0.1 (slumpad port) som noterar varje
    anrop: (sökväg, Host-huvud). routes: {sökväg: route(handler, site)}."""

    def __init__(self, routes):
        self.seen = []
        self.dripping_stopped = threading.Event()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.seen.append((self.path, self.headers.get("Host")))
                route = routes.get(self.path.split("?")[0])
                if route is None:
                    route = status_route(404)
                route(self, site)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://{PUBLIC_HOST}:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def paths(self):
        return [path for path, _ in self.seen]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def html_route(body="<html><head><title>Sajten</title></head><body>Hej</body></html>"):
    def route(handler, site):
        data = body.encode() if isinstance(body, str) else body
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    return route


def status_route(status):
    def route(handler, site):
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return route


def redirect_route(location, status=302):
    """location får innehålla {port} (testserverns port)."""

    def route(handler, site):
        handler.send_response(status)
        handler.send_header("Location", location.format(port=site.port))
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return route


def drip_route(head_only=False):
    """En server som skickar en byte i taget, så länge någon läser."""

    def route(handler, site):
        try:
            if head_only:
                handler.wfile.write(b"HTTP/1.1 200 OK\r\n")
            else:
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html")
                handler.send_header("Content-Length", "100000")
                handler.end_headers()
            for _ in range(100):
                handler.wfile.write(b"X-Drip: 1\r\n" if head_only else b"x")
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass
        finally:
            site.dripping_stopped.set()

    return route


@contextlib.contextmanager
def as_public(site):
    """PUBLIC_HOST pekar på testservern och dess port räknas som tillåten.
    Varje annan adress går genom det riktiga _assert_public. Ger listan med
    värdnamnen som kontrollerades, i ordning."""
    real = analyzer._assert_public
    checked = []

    def fake(host):
        checked.append(host)
        if host == PUBLIC_HOST:
            return "127.0.0.1"
        return real(host)

    with (
        patch.object(analyzer, "ALLOWED_PORTS", (80, 443, site.port)),
        patch.object(analyzer, "_assert_public", side_effect=fake),
    ):
        yield checked


class FetchRedirectTests(SimpleTestCase):
    """Varje hopp prövas innan det anropas, och anslutningen går till den IP
    som prövades."""

    def serve(self, routes):
        site = LocalSite(routes)
        self.addCleanup(site.close)
        return site

    def test_a_redirect_to_an_internal_address_is_never_requested(self):
        site = self.serve(
            {
                "/": redirect_route("http://127.0.0.1:{port}/internal"),
                "/internal": html_route("hemligt"),
            }
        )
        with as_public(site) as checked, self.assertRaises(AnalysError) as caught:
            fetch(site.base + "/")
        self.assertEqual(site.paths, ["/"], "det interna hoppet får aldrig anropas")
        self.assertEqual(checked, [PUBLIC_HOST, "127.0.0.1"])
        self.assertIn("internt nät", str(caught.exception))

    def test_redirects_to_metadata_odd_ports_and_other_schemes_are_refused(self):
        for location in (
            "http://169.254.169.254/latest/meta-data/",
            f"http://{PUBLIC_HOST}:8080/",
            "ftp://ftp.example.se/",
            "file:///etc/passwd",
        ):
            with self.subTest(location=location):
                site = self.serve({"/": redirect_route(location)})
                with as_public(site), self.assertRaises(AnalysError):
                    fetch(site.base + "/")
                self.assertEqual(site.paths, ["/"])

    def test_a_public_redirect_is_followed_and_every_hop_is_checked(self):
        site = self.serve({"/": redirect_route("/ny-sida/", status=301), "/ny-sida/": html_route()})
        with as_public(site) as checked:
            sida = fetch(site.base + "/")
        self.assertEqual(sida.status, 200)
        self.assertEqual(sida.url, site.base + "/ny-sida/")
        self.assertEqual(sida.redirects, [site.base + "/"])
        self.assertIn("<title>Sajten</title>", sida.html)
        self.assertEqual(checked, [PUBLIC_HOST, PUBLIC_HOST])
        # Anslutningen går till den kontrollerade IP:n men Host-huvudet är
        # fortfarande värdnamnet.
        self.assertEqual(
            site.seen,
            [("/", f"{PUBLIC_HOST}:{site.port}"), ("/ny-sida/", f"{PUBLIC_HOST}:{site.port}")],
        )

    def test_with_hosts_a_redirect_to_another_host_is_never_looked_up_or_requested(self):
        site = self.serve(
            {
                "/": redirect_route("https://annan.example.se/sida"),
                "/egen/": redirect_route("/sida/"),
                "/sida/": html_route(),
            }
        )
        with as_public(site) as checked, self.assertRaises(AnalysError) as caught:
            fetch(site.base + "/", hosts=(PUBLIC_HOST,))
        self.assertEqual(site.paths, ["/"])
        self.assertEqual(checked, [PUBLIC_HOST], "den andra värden slås aldrig upp")
        self.assertIn("annan webbplats", str(caught.exception))
        # Samma värd går bra, också efter en omdirigering.
        with as_public(site):
            sida = fetch(site.base + "/egen/", hosts=(PUBLIC_HOST,))
        self.assertEqual(sida.status, 200)
        with as_public(site), self.assertRaises(AnalysError):
            fetch(site.base + "/sida/", hosts=("annan.example.se",))

    def test_too_many_redirects(self):
        site = self.serve({"/": redirect_route("/")})
        with as_public(site), self.assertRaises(AnalysError) as caught:
            fetch(site.base + "/")
        self.assertIn("För många", str(caught.exception))
        self.assertEqual(len(site.paths), analyzer.MAX_REDIRECTS + 1)

    def test_an_http_error_is_an_error(self):
        site = self.serve({"/": status_route(500)})
        with as_public(site), self.assertRaises(AnalysError) as caught:
            fetch(site.base + "/")
        self.assertIn("HTTP 500", str(caught.exception))

    def test_the_connection_goes_to_the_checked_ip_not_a_new_lookup(self):
        """DNS rebinding: första uppslagningen ger en publik adress, nästa en
        intern. fetch slår bara upp en gång och ansluter till den publika."""
        answers = iter(["93.184.216.34", "127.0.0.1", "127.0.0.1"])

        def getaddrinfo(host, port, *args, **kwargs):
            ip = next(answers)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

        with (
            patch.object(analyzer.socket, "getaddrinfo", side_effect=getaddrinfo) as lookups,
            patch.object(
                analyzer.socket, "create_connection", side_effect=ConnectionRefusedError
            ) as connect,
            self.assertRaises(AnalysError),
        ):
            fetch(f"http://{PUBLIC_HOST}/")
        self.assertEqual(lookups.call_count, 1)
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 80))

    def test_https_keeps_the_hostname_for_sni_and_the_certificate(self):
        """Uppkopplingen går till den kontrollerade IP:n, men TLS frågar efter
        och verifierar certifikatet för värdnamnet."""
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        context = analyzer.ssl.create_default_context()
        with (
            patch.object(analyzer.socket, "getaddrinfo", return_value=public),
            patch.object(analyzer.socket, "create_connection") as connect,
            patch.object(analyzer.ssl, "create_default_context", return_value=context),
            patch.object(
                context, "wrap_socket", side_effect=analyzer.ssl.SSLError("stopp")
            ) as wrap,
            self.assertRaises(AnalysError),
        ):
            fetch(f"https://{PUBLIC_HOST}/")
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        self.assertEqual(wrap.call_args.kwargs["server_hostname"], PUBLIC_HOST)
        self.assertTrue(context.check_hostname)

    def test_a_slow_body_is_cut_off_at_the_time_limit(self):
        site = self.serve({"/": drip_route()})
        start = time.monotonic()
        with as_public(site), self.assertRaises(AnalysError) as caught:
            fetch(site.base + "/", time_limit=1.0)
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertIn("inte i tid", str(caught.exception))
        # Anslutningen är stängd: servern märker det och slutar skicka.
        self.assertTrue(site.dripping_stopped.wait(3.0))

    def test_slow_headers_are_cut_off_too(self):
        site = self.serve({"/": drip_route(head_only=True)})
        start = time.monotonic()
        with as_public(site), self.assertRaises(AnalysError):
            fetch(site.base + "/", time_limit=1.0)
        self.assertLess(time.monotonic() - start, 3.0)

    def test_the_body_is_capped_at_max_bytes(self):
        site = self.serve({"/": html_route(b"a" * 300_000)})
        with as_public(site):
            sida = fetch(site.base + "/", max_bytes=100_000)
            full = fetch(site.base + "/")
        self.assertEqual(sida.bytes, 100_000)
        self.assertEqual(len(sida.html), 100_000)
        self.assertEqual(full.bytes, 300_000)
        self.assertEqual(analyzer.MAX_BYTES, 2 * 1024 * 1024, "Hemsidekollens tak är kvar")

    def test_hemsidekollen_still_works_end_to_end(self):
        site = self.serve({"/": redirect_route("/start/"), "/start/": html_route(GOOD_HTML)})
        with as_public(site), patch.object(analyzer, "check_epost_doman", return_value=[]):
            report = analyzer.analyze(site.base + "/")
        self.assertEqual(report["status"], 200)
        self.assertTrue(report["url"].endswith("/start/"))
        self.assertGreater(report["summering"]["ok"], 0)


class BlockedAddressTests(SimpleTestCase):
    def test_disguised_and_shared_addresses_are_blocked(self):
        for raw in (
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "64:ff9b::a9fe:a9fe",
            "2002:7f00:1::1",
            "100.64.0.1",
            "169.254.169.254",
            "10.1.2.3",
            "fd00::1",
            "0.0.0.0",
        ):
            with self.subTest(ip=raw):
                self.assertTrue(analyzer._blocked(ipaddress.ip_address(raw)))
        for raw in ("93.184.216.34", "2a00:1450:4001:80b::2004"):
            with self.subTest(ip=raw):
                self.assertFalse(analyzer._blocked(ipaddress.ip_address(raw)))

    def test_a_bad_port_is_an_error_not_a_crash(self):
        with self.assertRaises(AnalysError):
            normalize_url("https://example.se:99999/")
