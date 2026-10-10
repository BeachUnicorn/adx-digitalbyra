"""
Skriptet på kundens egen webbplats (README E.3, E.6, H.2, H.5, I.9, J S4
test_s4_snippet; S4, länk-byggaren).

    SnippetFileTests     klick.adx.se/s.<ver>.js: versionen, SRI för exakt de
                         byte som serveras, sparade versioner, 404 för en annan,
                         CORS och cache, inga kakor, bara på klick
    ScriptTests          s.js: läser adx bara när den finns, en gång, tar bort den,
                         skickar till /v, inga kakor och ingen lagring, under 2 kB
    BeaconTests          klick.adx.se/v: Origin lika med domänen eller en underdomän,
                         annan eller saknad Origin ignoreras, token från ett annat
                         konto nekas, fel nyckel, storleken, klicket som besöksanropet
                         på landningssidan, site_visit en gång per klick och 30
                         minuter, last_seen_at högst varje timme, provlänken,
                         demot, gränsen per besökare, alltid 204, inga kakor
    AcceptanceTests      J S4: ett skript på en provsajt rapporterar ett besök från
                         en klick-länk (namngiven länk och sms)
    SettingsPageTests    sidan Spårningsskript: lägga till och ta bort, domänens
                         regler, byråns granskning, taggen och provlänken, demot,
                         raden i Inställningar, integritetstexten

POST:arna till /v går med Client(enforce_csrf_checks=True) och text/plain,
som navigator.sendBeacon skickar dem. Inget svar får sätta en kaka.
"""

import base64
import hashlib
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount

from . import link_views, links, site_snippet, tokens
from .models import AllowedHost, Click, Event, SiteSnippet, TrackedLink
from .test_s2_links import IPHONE, LinkFixture, query
from .test_s4_foundation import LINK_SETTINGS, make_named

KLICK = {"HTTP_HOST": "klick.adx.se"}
ORIGIN = "https://exempelror.example"
AGENCY = {"INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example"}
SCRIPT = Path(settings.BASE_DIR) / "static" / "utskick" / "s.js"


def assert_no_cookies(test, response):
    test.assertEqual(response.cookies, {})
    test.assertNotIn("Set-Cookie", response.headers)


# ---------------------------------------------------------------------------
# Filen
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class SnippetFileTests(TestCase):
    def get(self, path, method="get", **extra):
        return getattr(Client(), method)(path, **{**KLICK, **extra})

    def test_the_current_version(self):
        ver = links.snippet_version()
        self.assertRegex(ver, r"^[0-9a-f]{8}$")
        response = self.get(f"/s.{ver}.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, SCRIPT.read_bytes())
        self.assertEqual(response["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(response["Access-Control-Allow-Origin"], "*")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "cross-origin")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        assert_no_cookies(self, response)
        sri = "sha384-" + base64.b64encode(hashlib.sha384(response.content).digest()).decode()
        self.assertEqual(links.snippet_integrity(), sri)
        self.assertEqual(self.get(f"/s.{ver}.js", method="head").status_code, 200)

    def test_the_version_is_the_hash_of_the_file(self):
        self.assertEqual(
            links.snippet_version(), hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:8]
        )

    def test_another_version_is_404(self):
        ver = links.snippet_version()
        other = "0" * 8 if ver != "0" * 8 else "1" * 8
        response = self.get(f"/s.{other}.js")
        self.assertEqual(response.status_code, 404)
        assert_no_cookies(self, response)
        self.assertEqual(self.get("/s.ABCDEF12.js").status_code, 404)

    def test_only_on_the_email_host(self):
        ver = links.snippet_version()
        self.assertEqual(Client().get(f"/s.{ver}.js", HTTP_HOST="k.adx.se").status_code, 404)
        self.assertEqual(Client().get(f"/s.{ver}.js").status_code, 404)
        self.assertEqual(Client().post(f"/s.{ver}.js", **KLICK).status_code, 405)

    def test_an_archived_version_keeps_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "static" / "utskick"
            folder.mkdir(parents=True)
            old = b"(function(){ var gammal = 1; })();\n"
            old_ver = hashlib.sha256(old).hexdigest()[:8]
            (folder / f"s.{old_ver}.js").write_bytes(old)
            (folder / "s.js").write_bytes(b"(function(){})();\n")
            with override_settings(BASE_DIR=Path(tmp)):
                response = self.get(f"/s.{old_ver}.js")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, old)
                self.assertNotEqual(links.snippet_version(), old_ver)

    def test_the_tag_names_the_served_file(self):
        site = SiteSnippet(key="Ab12Cd34Ef56Gh78", domain="exempelror.example")
        tag = links.snippet_tag(site)
        ver = links.snippet_version()
        self.assertIn(f'src="https://klick.adx.se/s.{ver}.js"', tag)
        self.assertIn(f'integrity="{links.snippet_integrity(ver)}"', tag)
        self.assertIn('crossorigin="anonymous" data-k="Ab12Cd34Ef56Gh78" async', tag)


class ScriptTests(TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text("utf-8")

    def test_no_cookie_no_storage_and_small(self):
        for word in ("cookie", "localStorage", "sessionStorage", "indexedDB", "caches."):
            self.assertNotIn(word, self.text)
        self.assertLess(len(self.text.encode("utf-8")), 2048)

    def test_it_reads_adx_once_and_removes_it(self):
        self.assertIn('q.has("adx")', self.text)
        self.assertIn('q.delete("adx")', self.text)
        self.assertIn("replaceState", self.text)
        # Utan adx i adressen skickar skriptet inget (return före sendBeacon).
        self.assertLess(self.text.index('if (!q.has("adx")) return;'), self.text.index("send("))

    def test_it_beacons_to_v_on_the_scripts_own_host(self):
        self.assertIn('getAttribute("data-k")', self.text)
        self.assertIn('+ "/v"', self.text)
        self.assertIn("sendBeacon", self.text)
        self.assertIn("pagehide", self.text)
        self.assertIn("api.track", self.text)
        self.assertIn("adxFlamingo", self.text)
        self.assertNotIn("fetch(", self.text)
        self.assertNotIn("XMLHttpRequest", self.text)


# ---------------------------------------------------------------------------
# Besöksanropet
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class BeaconTests(LinkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.site = SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        self.sms_click = Click.objects.create(
            account=self.account,
            utskick=self.utskick,
            recipient=self.recipient,
            link=self.ext,
            contact=self.kontakt,
            channel=Click.Channel.SMS,
        )
        self.token = tokens.adx_token(self.sms_click.pk)

    def beacon(self, origin=ORIGIN, body=None, **data):
        payload = {"k": self.site.key, "t": self.token, "p": "/boka?namn=Anna", "s": 0, "v": 1}
        payload.update(data)
        extra = dict(KLICK)
        if origin is not None:
            extra["HTTP_ORIGIN"] = origin
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/v",
            data=body if body is not None else json.dumps(payload),
            content_type="text/plain;charset=UTF-8",
            HTTP_USER_AGENT=IPHONE,
            **extra,
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        assert_no_cookies(self, response)
        self.sms_click.refresh_from_db()
        self.site.refresh_from_db()
        return response

    def events(self):
        return Event.objects.filter(kind=Event.SITE_VISIT)

    def test_a_landing_counts_the_visit_and_the_event(self):
        response = self.beacon()
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.sms_click.lp_visits, 1)
        self.assertIsNotNone(self.sms_click.first_visit_at)
        self.assertIsNotNone(self.site.last_seen_at)
        event = self.events().get()
        self.assertEqual(event.contact_id, self.kontakt.pk)
        self.assertEqual(event.utskick_id, self.utskick.pk)
        self.assertEqual(event.recipient_id, self.recipient.pk)
        self.assertEqual(event.account_id, self.account.pk)
        self.assertEqual(
            event.data,
            {"klick": self.sms_click.pk, "sida": "/boka", "varde": "exempelror.example"},
        )

    def test_a_subdomain_origin_is_accepted(self):
        self.beacon(origin="https://www.exempelror.example")
        self.assertEqual(self.sms_click.lp_visits, 1)
        self.beacon(origin="http://butik.exempelror.example", s=40, v=0)
        self.assertEqual(self.sms_click.lp_visits, 1)

    def test_any_other_or_a_missing_origin_is_ignored(self):
        for origin in (
            "https://ond.example",
            "https://exempelror.example.ond.example",
            "https://inteexempelror.example",
            "null",
            "file://",
            "",
            None,
        ):
            with self.subTest(origin=origin):
                self.beacon(origin=origin)
        self.assertEqual(self.sms_click.lp_visits, 0)
        self.assertIsNone(self.site.last_seen_at)
        self.assertFalse(self.events().exists())

    def test_a_token_from_another_account_is_refused(self):
        foreign = Click.objects.create(account=self.other_account, channel=Click.Channel.SMS)
        self.beacon(t=tokens.adx_token(foreign.pk))
        foreign.refresh_from_db()
        self.assertEqual(foreign.lp_visits, 0)
        self.assertIsNone(self.site.last_seen_at)
        # Och ett annat kontos nyckel med den här kontots token.
        other_site = SiteSnippet.objects.create(
            account=self.other_account, domain="exempelror.example"
        )
        self.beacon(k=other_site.key)
        other_site.refresh_from_db()
        self.assertIsNone(other_site.last_seen_at)
        self.assertEqual(self.sms_click.lp_visits, 0)

    def test_a_bad_key_token_or_body_is_ignored(self):
        body, _sig = self.token.split(".")
        for data in (
            {"k": "x" * 16},
            {"k": ""},
            {"k": None},
            {"t": f"{body}.AAAAAAAAAA"},
            {"t": "nej"},
            {"t": ["lista"]},
        ):
            with self.subTest(data=data):
                self.beacon(**data)
        for raw in ("inte json", "[1, 2]", "", "{}"):
            with self.subTest(raw=raw):
                self.beacon(body=raw)
        self.assertEqual(self.sms_click.lp_visits, 0)
        self.assertIsNone(self.site.last_seen_at)

    def test_a_landing_page_ut_is_not_an_adx_token(self):
        # Granskningen: adx och ut var samma token, så en vidarebefordrad
        # landningssidas ut gällde i skriptets anrop (och tvärtom).
        self.beacon(t=tokens.ut_token(self.sms_click.pk))
        self.assertEqual(self.sms_click.lp_visits, 0)
        self.assertIsNone(self.site.last_seen_at)
        self.assertIsNone(tokens.read_ut(self.token))
        self.assertEqual(tokens.read_adx(self.token), self.sms_click.pk)

    def test_an_infinite_or_odd_seconds_value_still_answers_204(self):
        # Granskningen: "s": 1e999 blev float("inf") och int() gav OverflowError (500).
        for raw in ("1e999", "-1e999", "Infinity", "NaN", '"inf"', "[1]", "{}"):
            with self.subTest(s=raw):
                payload = (
                    f'{{"k": "{self.site.key}", "t": "{self.token}", "p": "/", "s": {raw}, "v": 0}}'
                )
                self.beacon(body=payload)
        self.assertEqual(self.sms_click.engaged_seconds, 0)

    def test_the_size_limit(self):
        self.beacon(p="/" + "a" * 1100)
        self.assertEqual(self.sms_click.lp_visits, 0)
        self.beacon(p="/" + "a" * 300)
        self.assertEqual(self.sms_click.lp_visits, 1)
        self.assertEqual(len(self.events().get().data["sida"]), 80)

    def test_engaged_seconds_and_a_scanner_becomes_human(self):
        Click.objects.filter(pk=self.sms_click.pk).update(kind=Click.Kind.SCANNER)
        self.beacon(s=0, v=1)
        self.assertEqual(self.sms_click.kind, Click.Kind.HUMAN)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 1)
        Click.objects.filter(pk=self.sms_click.pk).update(
            beacon_at=timezone.now() - timedelta(seconds=11)
        )
        self.beacon(s=45, v=0)
        self.assertEqual(self.sms_click.engaged_seconds, 45)
        self.assertEqual(self.sms_click.lp_visits, 1)
        # Högst en skrivning var tionde sekund, som på landningssidan.
        self.beacon(s=90, v=0)
        self.assertEqual(self.sms_click.engaged_seconds, 45)

    def test_site_visit_once_per_click_and_half_hour(self):
        self.beacon()
        self.beacon()
        self.assertEqual(self.sms_click.lp_visits, 2)
        self.assertEqual(self.events().count(), 1)
        self.events().update(at=timezone.now() - timedelta(minutes=31))
        self.beacon()
        self.assertEqual(self.events().count(), 2)

    def test_last_seen_at_at_most_hourly(self):
        recent = timezone.now() - timedelta(minutes=30)
        SiteSnippet.objects.filter(pk=self.site.pk).update(last_seen_at=recent)
        self.beacon()
        self.assertEqual(self.site.last_seen_at, recent)
        old = timezone.now() - timedelta(hours=2)
        SiteSnippet.objects.filter(pk=self.site.pk).update(last_seen_at=old)
        self.beacon()
        self.assertGreater(self.site.last_seen_at, old + timedelta(hours=1))

    def test_the_install_link_marks_the_script_installed(self):
        install = site_snippet.install_token(self.site)
        self.assertIsNone(tokens.read_ut(install))
        self.assertRegex(install, site_snippet.TOKEN_RE)
        self.beacon(t=install)
        self.assertIsNotNone(self.site.last_seen_at)
        self.assertEqual(self.sms_click.lp_visits, 0)
        self.assertFalse(self.events().exists())
        # Provtoken för ett annat skript gäller inte här.
        other = SiteSnippet.objects.create(account=self.other_account, domain="annan.example")
        SiteSnippet.objects.filter(pk=self.site.pk).update(last_seen_at=None)
        self.beacon(t=site_snippet.install_token(other))
        self.assertIsNone(self.site.last_seen_at)
        self.assertEqual(
            site_snippet.install_url(self.site),
            f"https://exempelror.example/?adx={install}",
        )

    def test_a_goal_on_the_same_page(self):
        self.beacon(e="Bokning", v=0)
        self.beacon(e="bokning", v=0)
        goal = self.events().get()
        self.assertEqual(goal.data["mal"], "bokning")
        self.beacon(e="inte ett namn", v=0)
        self.assertEqual(self.events().count(), 1)

    def test_a_named_click_has_no_contact_and_no_event(self):
        named = make_named(self.account, destination="https://exempelror.example/")
        click = Click.objects.create(account=self.account, link=named, channel="named")
        self.beacon(t=tokens.adx_token(click.pk))
        click.refresh_from_db()
        self.assertEqual(click.lp_visits, 1)
        self.assertFalse(self.events().exists())

    def test_the_demo_is_not_logged(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.beacon()
        self.assertEqual(self.sms_click.lp_visits, 0)
        self.assertIsNone(self.site.last_seen_at)

    def test_a_limit_per_visitor(self):
        with mock.patch.object(link_views, "BEACON_PER_HOUR", 2):
            self.beacon()
            self.beacon()
            self.beacon()
        self.assertEqual(self.sms_click.lp_visits, 2)

    def test_only_post_and_only_on_the_email_host(self):
        self.assertEqual(Client().get("/v", **KLICK).status_code, 405)
        response = Client(enforce_csrf_checks=True).post(
            "/v", data="{}", content_type="text/plain", HTTP_HOST="k.adx.se", HTTP_ORIGIN=ORIGIN
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Godkännandet i J S4: ett skript på en provsajt rapporterar ett besök
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class AcceptanceTests(LinkFixture, TestCase):
    def send(self, site, adx, origin=ORIGIN, **data):
        payload = {"k": site.key, "t": adx, "p": "/boka", "s": 0, "v": 1, **data}
        return Client(enforce_csrf_checks=True).post(
            "/v",
            data=json.dumps(payload),
            content_type="text/plain;charset=UTF-8",
            HTTP_ORIGIN=origin,
            **KLICK,
        )

    def test_install_then_a_named_link_then_the_visit(self):
        site = SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        named = make_named(self.account, slug="boka", destination="https://exempelror.example/boka")
        first = Client().get("/exempelror/boka", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertNotIn("adx", query(first["Location"]))
        self.send(site, site_snippet.install_token(site))
        site.refresh_from_db()
        self.assertTrue(site.is_installed)
        response = Client().get("/exempelror/boka", HTTP_USER_AGENT=IPHONE, **KLICK)
        adx = query(response["Location"])["adx"]
        self.assertEqual(self.send(site, adx).status_code, 204)
        self.assertEqual(self.send(site, adx, s=75, v=0).status_code, 204)
        click = Click.objects.filter(link=named).order_by("-pk").first()
        self.assertEqual((click.lp_visits, click.channel), (1, "named"))

    def test_an_sms_click_reaches_the_contacts_timeline(self):
        site = SiteSnippet.objects.create(
            account=self.account, domain="exempelror.example", last_seen_at=timezone.now()
        )
        response = Client().get("/Ex0001", HTTP_USER_AGENT=IPHONE, HTTP_HOST="k.adx.se")
        adx = query(response["Location"])["adx"]
        self.send(site, adx, origin="https://www.exempelror.example")
        event = Event.objects.get(kind=Event.SITE_VISIT)
        self.assertEqual(event.contact_id, self.kontakt.pk)
        self.assertEqual(event.data["sida"], "/boka")


# ---------------------------------------------------------------------------
# Sidan Spårningsskript, raden i Inställningar och integritetstexten
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS, **AGENCY)
class SettingsPageTests(LinkFixture, TestCase):
    url = reverse("flamingo:app_utskick_snippet")

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)

    def add(self, domain, client=None):
        return (client or self.client).post(self.url, {"action": "add", "domain": domain})

    def test_the_empty_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Lägg in skriptet", html)
        self.assertIn(
            "utan kakor", self.client.get(reverse("flamingo:app_utskick_settings")).content.decode()
        )

    def test_add_shows_the_tag_the_install_link_and_the_analytics_text(self):
        response = self.add("https://www.Exempelror.example/boka")
        self.assertRedirects(response, self.url)
        site = SiteSnippet.objects.get(account=self.account)
        self.assertEqual(site.domain, "exempelror.example")
        # Kundens webbplats i kundregistret: ingen granskning, inget larm.
        self.assertFalse(AllowedHost.objects.exists())
        self.assertEqual(mail.outbox, [])
        html = self.client.get(self.url).content.decode()
        self.assertIn(f"https://klick.adx.se/s.{links.snippet_version()}.js", html)
        self.assertIn(links.snippet_integrity(), html)
        self.assertIn(f"data-k=&quot;{site.key}&quot;", html)
        # Taggen står som text, aldrig som en riktig script-tagg i verktyget.
        self.assertIn('<code class="fl-ut-ln-code fl-ut-ln-tag">&lt;script src=&quot;', html)
        self.assertIn('data-ln-copy="&lt;script src=&quot;https://klick.adx.se/', html)
        self.assertIn(site_snippet.install_url(site).replace("&", "&amp;"), html)
        self.assertIn("Uteslut parametern adx i din webbanalys", html)
        self.assertIn("Inte sett än", html)
        self.assertNotIn('<script src="https://klick', html)
        # Granskningen: adxFlamingo.track och felsökningen stod ingenstans.
        self.assertIn(
            '<code class="fl-ut-ln-code">adxFlamingo.track(&#x27;bokning&#x27;);</code>', html
        )
        self.assertIn("Står det Inte sett än efter provlänken?", html)
        self.assertIn("Referrer-Policy no-referrer eller same-origin", html)
        self.assertIn("Lägg till fler webbplatser", html)

    def test_installed_shows_when_it_was_seen(self):
        SiteSnippet.objects.create(
            account=self.account, domain="exempelror.example", last_seen_at=timezone.now()
        )
        html = self.client.get(self.url).content.decode()
        self.assertIn("Installerat, senast sett i dag", html)
        settings_html = self.client.get(reverse("flamingo:app_utskick_settings")).content.decode()
        self.assertIn("Spårningsskript på egen sajt", settings_html)
        self.assertIn("exempelror.example · senast sett i dag", settings_html)
        self.assertIn("Installerat", settings_html)

    def test_a_domain_that_is_not_the_customers_own_goes_to_adx(self):
        self.add("annan-sajt.example")
        host = AllowedHost.objects.get(account=self.account, host="annan-sajt.example")
        self.assertEqual(host.status, AllowedHost.Status.PENDING)
        self.assertEqual(len(mail.outbox), 1)
        html = self.client.get(self.url).content.decode()
        self.assertIn("väntar på ADX:s godkännande", html)
        # Domänen gör inga länkar fria från granskningen (avvikelse 2).
        link = make_named(self.account, destination="https://annan-sajt.example/")
        self.assertFalse(links.destination_ok(link))

    def test_the_domain_rules(self):
        cases = {
            "": site_snippet.EMPTY_TEXT,
            "192.168.1.10": site_snippet.IP_TEXT,
            "localhost": site_snippet.INVALID_TEXT,
            "exempel ror.se": site_snippet.INVALID_TEXT,
            "github.io": site_snippet.SHARED_TEXT,
            "org.se": site_snippet.SHARED_TEXT,
            "klick.adx.se": site_snippet.LINK_HOST_TEXT,
            "https://adx.se/lp/vinter/": site_snippet.ADX_TEXT,
            "ftp://exempelror.example": site_snippet.INVALID_TEXT,
        }
        for raw, text in cases.items():
            with self.subTest(raw=raw):
                response = self.add(raw)
                self.assertEqual(response.status_code, 400)
                self.assertIn(text, response.content.decode())
        self.assertFalse(SiteSnippet.objects.exists())
        self.assertEqual(site_snippet.clean_domain("Exempelrör.se"), "xn--exempelrr-77a.se")
        self.assertEqual(site_snippet.clean_domain("butik.wixsite.com"), "butik.wixsite.com")

    def test_duplicates_and_the_limit(self):
        self.add("exempelror.example")
        response = self.add("www.exempelror.example")
        self.assertEqual(response.status_code, 400)
        self.assertIn(site_snippet.EXISTS_TEXT, response.content.decode())
        for n in range(SiteSnippet.MAX_PER_ACCOUNT - 1):
            SiteSnippet.objects.create(account=self.account, domain=f"sajt{n}.example")
        response = self.add("en-till.example")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SiteSnippet.objects.filter(account=self.account).count(), 5)

    def test_remove_and_a_foreign_id_is_400(self):
        site = SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        foreign = SiteSnippet.objects.create(account=self.other_account, domain="x.example")
        response = self.client.post(self.url, {"action": "remove", "site": str(foreign.pk)})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(SiteSnippet.objects.filter(pk=foreign.pk).exists())
        response = self.client.post(self.url, {"action": "remove", "site": str(site.pk)})
        self.assertRedirects(response, self.url)
        self.assertFalse(SiteSnippet.objects.filter(pk=site.pk).exists())

    def test_the_demo_changes_nothing(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.add("annan-sajt.example")
        self.assertFalse(SiteSnippet.objects.exists())
        self.assertFalse(AllowedHost.objects.exists())

    def test_staff_in_view_as_adds_for_real_and_is_logged(self):
        with self.assertLogs("apps.utskick.app_views.snippet", "INFO") as logs:
            self.add("exempelror.example", client=self.client_for(self.staff))
        self.assertIn("byrån i kundvyn", logs.output[0])
        self.assertTrue(SiteSnippet.objects.filter(account=self.account).exists())

    def test_the_privacy_text_names_the_site(self):
        url = reverse("utskick_public:privacy", args=["exempelror"])
        self.assertNotIn("Besök på exempelror.example", Client().get(url).content.decode())
        SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        html = Client().get(url).content.decode()
        self.assertIn("Besök på exempelror.example från en länk i ett utskick", html)
        self.assertIn("utan kakor", html)
        # adxFlamingo.track sparar ett mål på kontakten, så texten säger det.
        self.assertIn("om du gjorde något som webbplatsen markerar", html)

    def test_every_cell_and_no_inline_script(self):
        SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        html = self.client.get(self.url).content.decode()
        self.assertNotIn(" style=", html)
        self.assertNotIn("<script>", html)
        self.assertIn("js/flamingo-app-links.js", html)

    def test_a_named_link_to_a_seen_site_shows_skript_finns(self):
        SiteSnippet.objects.create(
            account=self.account, domain="exempelror.example", last_seen_at=timezone.now()
        )
        make_named(self.account, destination="https://exempelror.example/boka")
        rows = self.client.get(reverse("flamingo:app_links")).context["named_rows"]
        self.assertEqual(rows[0]["chip_label"], "Skript finns")
        self.assertEqual(TrackedLink.objects.filter(account=self.account).count(), 4)
