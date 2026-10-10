"""
Mottagarens sidor: kundens logga överst och ADX logga nederst
(apps/utskick/branding.py, README "S3 as built", Giovannis beställning
2026-10-10).

    CustomerPageTests   varje sida en mottagare kan hamna på hos en kund
                        (anmälan, tack, bekräfta e-post, Mina utskick,
                        integritet på adx.se; /s/, /p/, /b/ på k.adx.se;
                        /a/, /v/ på klick.adx.se) visar kundens logga från
                        adx.se med företagets namn som alt, aldrig ett annat
                        kontos logga, och ADX logga med länken till adx.se
    SizeTests           loggans mått: högst 40 hög och 220 bred med
                        proportionerna kvar, aldrig större än filen, samma
                        mått i mejlets sidhuvud; stilmallen ändrar inte
                        måtten, och ADX logga har sin marginal och klickyta
    FallbackTests       utan logga (eller med en fil som inte går att läsa)
                        står företagets namn som text, och sidan svarar 200;
                        en trasig logga prövas inte vid varje visning
    GenericPageTests    startsidan, 404 och 429 på länkvärdarna har ingen
                        kunds logga men ADX logga nederst
    CookieTests         länkvärdarnas sidor sätter inga kakor, loggan kommer
                        från /media/ på adx.se och ADX logga från /static/,
                        som nginx lämnar ut utan Django
    SameTimeTests       två besökare samtidigt på en logga utan rendition:
                        filen görs om en gång (riktiga trådar)
"""

import re
import shutil
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from apps.flamingo import media
from apps.flamingo.models import MediaAsset

from . import branding, codes, keys, link_views, links, tokens
from .email import images, render
from .models import CHANNEL_EMAIL, CHANNEL_SMS, EmailImage, LinkCode
from .test_s1_public import PublicFixture, signup_post
from .test_s3_foundation import LINK_SETTINGS, make_utskick
from .test_s3_render import make_asset
from .testing import PHONE_ANNA, make_contact

BASE = Path(settings.BASE_DIR)
PUBLIC_CSS = BASE / "static" / "css" / "utskick-public.css"
_MEDIA = tempfile.mkdtemp(prefix="utskick-branding-")
SETTINGS = {
    **LINK_SETTINGS,
    "MEDIA_ROOT": _MEDIA,
    "SITE_BASE_URL": "https://adx.se",
}
K = {"HTTP_HOST": "k.adx.se"}
KLICK = {"HTTP_HOST": "klick.adx.se"}
ANNA_EMAIL = "anna.lind@hemma.example"
LOGO_RE = re.compile(
    r'<img class="up-logo" src="([^"]+)" width="(\d+)" height="(\d+)" alt="([^"]*)"'
)
ADX_RE = re.compile(
    r'<p class="up-adx"><a class="up-adx__link" href="([^"]+)" rel="noopener" '
    r'referrerpolicy="no-referrer"><img class="up-adx__logo" src="([^"]+)" '
    r'width="52" height="18" alt="ADX"></a></p>'
)


def css_rule(css, selector):
    """Deklarationerna i regeln som börjar med selector (på egen rad)."""
    start = css.index("\n" + selector + "{") + len(selector) + 2
    return css[start : css.index("}", start)]


class BrandingFixture(PublicFixture):
    """Exempelrör (öppen anmälningssida, integritetstext, klarmarkerade
    bekräftelsemejl) och Annanfirma, båda med en egen logga, och Anna i
    Exempelrörs register med sms och e-post."""

    @classmethod
    def setUpClass(cls):
        cls._branding_settings = override_settings(**SETTINGS)
        cls._branding_settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._branding_settings.disable()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.logo = make_asset(self.account, size=(400, 120), mode="RGBA", alt="Gammal alt")
        media.set_logo(self.logo)
        self.other_logo = make_asset(self.other_account, size=(300, 100), mode="RGBA")
        media.set_logo(self.other_logo)
        self.link_client = Client(enforce_csrf_checks=True)
        self.kontakt = make_contact(
            self.account, first_name="Anna", last_name="Lind", phone=PHONE_ANNA, email=ANNA_EMAIL
        )
        self.sms_hash = keys.value_hash(CHANNEL_SMS, PHONE_ANNA)
        self.email_hash = keys.value_hash(CHANNEL_EMAIL, ANNA_EMAIL)
        self.person = LinkCode.objects.create(
            code="Pe0001",
            kind=LinkCode.Kind.PERSON,
            account=self.account,
            value_hash=self.sms_hash,
        )
        self.confirm = codes.create_confirm(
            self.account, value_hash=self.sms_hash, purpose="pref_on", contact=self.kontakt
        )

    # -- adresserna ------------------------------------------------------------

    def logo_url(self, account=None):
        row = images.logo_for(account or self.account)
        return "https://adx.se" + row.file.url

    def customer_pages(self):
        """(namn, värd, sökväg) för varje sida hos Exempelrör."""
        self.sign_up()
        pending = self.email_consent()
        klick = len("https://klick.adx.se")
        return [
            ("anmälan", "adx.se", self.signup_url),
            ("tack", "adx.se", self.thanks_url),
            (
                "integritet",
                "adx.se",
                reverse("utskick_public:privacy", args=["exempelror"]),
            ),
            (
                "bekräfta e-post",
                "adx.se",
                reverse("utskick_public:confirm", args=[tokens.doi_token(pending)]),
            ),
            (
                "Mina utskick",
                "adx.se",
                reverse(
                    "utskick_public:preferences",
                    args=[tokens.preference_token(self.account.pk, CHANNEL_EMAIL, self.email_hash)],
                ),
            ),
            ("avregistrera sms", "k.adx.se", f"/s/{self.person.code}"),
            ("dina val sms", "k.adx.se", f"/p/{self.person.code}"),
            ("bekräfta sms", "k.adx.se", f"/b/{self.confirm.code}"),
            (
                "avregistrera e-post",
                "klick.adx.se",
                links.unsubscribe_url(self.account.pk, self.email_hash)[klick:],
            ),
            (
                "dina val e-post",
                "klick.adx.se",
                links.email_preferences_url(self.account.pk, self.email_hash)[klick:],
            ),
        ]

    def get(self, host, path):
        if host == "adx.se":
            response = self.client.get(path)
        else:
            response = self.link_client.get(path, HTTP_HOST=host)
        return response

    def assertAdxFooter(self, html):
        match = ADX_RE.search(html)
        self.assertIsNotNone(match, "ADX logga saknas nederst")
        self.assertEqual(match.group(1), "https://adx.se")
        self.assertEqual(match.group(2), "/static/images/adx-logo.png")
        self.assertEqual(html.count('class="up-adx"'), 1)
        # Nederst: efter sidans kort och sidfotens länk.
        self.assertGreater(html.index('class="up-adx"'), html.index('class="up-card"'))


class CustomerPageTests(BrandingFixture, TestCase):
    def test_every_page_shows_the_customers_logo_and_adx_at_the_bottom(self):
        own = self.logo_url()
        other = self.logo_url(self.other_account)
        for name, host, path in self.customer_pages():
            with self.subTest(page=name):
                response = self.get(host, path)
                self.assertIn(response.status_code, (200,), name)
                html = response.content.decode()
                match = LOGO_RE.search(html)
                self.assertIsNotNone(match, "kundens logga saknas överst")
                self.assertEqual(match.group(1), own)
                self.assertEqual(match.group(4), "Exempelrör")
                self.assertLess(html.index('class="up-logo"'), html.index('class="up-card"'))
                self.assertNotIn(other, html)
                self.assertNotIn('<p class="up-brand">', html)
                self.assertAdxFooter(html)

    def test_the_logo_is_the_mail_png_on_adx_se_40_high(self):
        response = self.get("k.adx.se", f"/s/{self.person.code}")
        url, width, height, alt = LOGO_RE.search(response.content.decode()).groups()
        self.assertTrue(url.startswith("https://adx.se/media/utskick-img/"), url)
        self.assertTrue(url.endswith(".png"), url)
        # 400 x 120 blir en fil på 267 x 80, som visas 134 x 40 (som i mejlets sidhuvud).
        self.assertEqual((width, height, alt), ("134", "40", "Exempelrör"))
        # Byggs en gång och återanvänds.
        token = tokens.preference_token(self.account.pk, CHANNEL_EMAIL, self.email_hash)
        self.get("klick.adx.se", f"/v/{token}")
        self.get("k.adx.se", f"/p/{self.person.code}")
        self.assertEqual(EmailImage.objects.filter(asset=self.logo).count(), 1)

    def test_a_wide_logo_is_at_most_220_wide(self):
        wide = make_asset(self.account, size=(1200, 100), mode="RGBA")
        media.set_logo(wide)
        info = branding.logo(self.account, "Exempelrör")
        self.assertEqual((info["width"], info["height"]), (220, 18))
        html = self.get("k.adx.se", f"/s/{self.person.code}").content.decode()
        self.assertEqual(LOGO_RE.search(html).group(2, 3), ("220", "18"))

    def test_another_accounts_page_shows_only_its_own_logo(self):
        code = LinkCode.objects.create(
            code="An0001",
            kind=LinkCode.Kind.PERSON,
            account=self.other_account,
            value_hash=self.sms_hash,
        )
        html = self.get("k.adx.se", f"/p/{code.code}").content.decode()
        self.assertEqual(LOGO_RE.search(html).group(1), self.logo_url(self.other_account))
        self.assertEqual(LOGO_RE.search(html).group(4), "Annanfirma")
        self.assertNotIn(self.logo_url(), html)
        self.assertAdxFooter(html)

    def test_a_signup_with_an_error_and_the_preview_keep_the_logo(self):
        response = self.client.post(self.signup_url, signup_post(email="inte-en-adress"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('class="up-alert"', html)
        self.assertEqual(LOGO_RE.search(html).group(1), self.logo_url())
        self.assertAdxFooter(html)
        html = self.client.get(self.signup_url + "?forhandsgranska=1").content.decode()
        self.assertEqual(LOGO_RE.search(html).group(1), self.logo_url())
        self.assertAdxFooter(html)

    def test_the_adx_link_carries_no_tracking_and_opens_normally(self):
        html = self.get("k.adx.se", f"/s/{self.person.code}").content.decode()
        match = ADX_RE.search(html)
        self.assertEqual(match.group(1), "https://adx.se")
        footer = html[html.index('class="up-adx"') :]
        self.assertNotIn("target=", footer.split("</p>")[0])
        self.assertNotIn("utm_", footer)
        self.assertNotIn("?", match.group(1))


class SizeTests(BrandingFixture, TestCase):
    def shown(self, size):
        logo = make_asset(self.account, size=size, mode="RGBA")
        media.set_logo(logo)
        info = branding.logo(self.account, "Exempelrör")
        return info["width"], info["height"]

    def test_the_shown_size_keeps_the_proportions_and_is_never_enlarged(self):
        # Filen: 80 hög, högst 440 bred, aldrig uppskalad (images.target_size).
        self.assertEqual(self.shown((400, 120)), (134, 40))
        self.assertEqual(self.shown((1200, 100)), (220, 18))
        # En liten logga (90 x 26) visas i sin egen storlek, inte 138 x 40.
        self.assertEqual(self.shown((90, 26)), (90, 26))
        self.assertEqual(self.shown((300, 300)), (40, 40))
        self.assertEqual(self.shown((120, 400)), (12, 40))
        self.assertEqual(images.display_size(0, 0), (1, 1))

    def test_the_mail_header_uses_the_same_size(self):
        wide = make_asset(self.account, size=(1200, 100), mode="RGBA")
        media.set_logo(wide)
        utskick = make_utskick(self.account, subject="Höst", created_by=self.anna)
        # Frysningen gör renditionen; förhandsvisningen ritar den.
        snap = render.snapshot(utskick)
        self.assertEqual((snap["logo"]["width"], snap["logo"]["height"]), (220, 18))
        html = render.render_html(utskick, render.context_for(utskick, mode=render.PREVIEW))
        tag = re.search(r'<img src="https://adx\.se/media/utskick-img/[^>]+>', html).group(0)
        self.assertIn('width="220" height="18"', tag)
        self.assertIn("width:220px;height:18px;", tag)
        self.assertNotIn("height:40px", tag)
        self.assertNotIn("max-width", tag)
        info = branding.logo(self.account)
        self.assertEqual((info["width"], info["height"]), (220, 18))

    def test_the_stylesheet_keeps_the_size_from_the_attributes(self):
        """Ingen fast höjd eller bredd på loggan: en height:40px med width:auto
        ritade en bred logga 476 x 40 trots width="220" height="18"."""
        css = PUBLIC_CSS.read_text("utf-8")
        rule = css_rule(css, ".up-logo")
        self.assertEqual(rule, "display:block;max-width:100%;height:auto")
        self.assertNotIn("object-fit", css)

    def test_the_adx_logo_has_its_margin_and_a_44_px_tap_target(self):
        css = PUBLIC_CSS.read_text("utf-8")
        # .up-foot p{margin:0} är mer specifik än .up-adx: marginalen måste
        # sitta på .up-foot .up-adx för att gälla.
        self.assertIn(".up-foot p{margin:0}", css)
        self.assertNotIn("\n.up-adx{", css)
        self.assertIn("margin-top:8px", css_rule(css, ".up-foot .up-adx"))
        link = css_rule(css, ".up-adx__link")
        logo = css_rule(css, ".up-adx__logo")
        padding = int(re.search(r"padding:(\d+)px 0", link).group(1))
        height = int(re.search(r"height:(\d+)px", logo).group(1))
        self.assertEqual(padding * 2 + height, 44)
        self.assertIn("opacity:.6", logo)
        # Attributen i mallen stämmer med höjden (1502 x 518 i filen).
        self.assertEqual(round(1502 * height / 518), 52)


class FallbackTests(BrandingFixture, TestCase):
    def test_without_a_logo_the_company_name_is_text(self):
        MediaAsset.objects.filter(account=self.account).update(is_logo=False)
        for name, host, path in self.customer_pages():
            with self.subTest(page=name):
                html = self.get(host, path).content.decode()
                self.assertIn('<p class="up-brand">Exempelrör</p>', html)
                self.assertNotIn('class="up-logo"', html)
                self.assertNotIn(self.logo_url(self.other_account), html)
                self.assertAdxFooter(html)

    def test_a_logo_file_that_cannot_be_read_falls_back_to_text(self):
        EmailImage.objects.filter(asset=self.logo).delete()
        self.logo.file.storage.delete(self.logo.file.name)
        response = self.get("k.adx.se", f"/p/{self.person.code}")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('<p class="up-brand">Exempelrör</p>', html)
        self.assertNotIn('class="up-logo"', html)
        self.assertAdxFooter(html)

    def test_an_unexpected_error_never_breaks_the_page(self):
        with mock.patch.object(images, "rendition", side_effect=OSError("disken")):
            response = self.get(
                "klick.adx.se",
                f"/v/{tokens.preference_token(self.account.pk, 'email', self.email_hash)}",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('<p class="up-brand">Exempelrör</p>', response.content.decode())
        self.assertIsNone(branding.logo(None))

    def test_a_broken_logo_is_not_tried_on_every_view(self):
        EmailImage.objects.filter(asset=self.logo).delete()
        self.logo.file.storage.delete(self.logo.file.name)
        with mock.patch.object(images, "rendition", wraps=images.rendition) as rendition:
            self.assertIsNone(branding.logo(self.account, "Exempelrör"))
            self.assertIsNone(branding.logo(self.account, "Exempelrör"))
            html = self.get("k.adx.se", f"/p/{self.person.code}").content.decode()
        self.assertEqual(rendition.call_count, 1)
        self.assertIn('<p class="up-brand">Exempelrör</p>', html)
        # En ny logga (en ny bild) prövas direkt.
        fresh = make_asset(self.account, size=(300, 90), mode="RGBA")
        media.set_logo(fresh)
        self.assertIsNotNone(branding.logo(self.account, "Exempelrör"))
        # Och den trasiga prövas igen när tiden har gått.
        cache.delete(branding._broken_key(self.logo))
        self.assertIsNone(cache.get(branding._broken_key(self.logo)))
        self.assertEqual(branding.BROKEN_SECONDS, 600)


class GenericPageTests(BrandingFixture, TestCase):
    def pages(self):
        with mock.patch.object(link_views, "_blocked", return_value=True):
            too_many = self.link_client.get(f"/s/{self.person.code}", **K)
        return [
            ("startsidan", self.link_client.get("/", **K), 200),
            ("startsidan klick", self.link_client.get("/", **KLICK), 200),
            ("404", self.link_client.get("/Zz9999", **K), 404),
            ("404 klick", self.link_client.get("/a/finns.inte", **KLICK), 404),
            ("429", too_many, 429),
        ]

    def test_no_customer_logo_but_adx_at_the_bottom(self):
        own, other = self.logo_url(), self.logo_url(self.other_account)
        for name, response, status in self.pages():
            with self.subTest(page=name):
                self.assertEqual(response.status_code, status)
                html = response.content.decode()
                self.assertIn('<p class="up-brand">ADX Flamingo</p>', html)
                self.assertNotIn('class="up-logo"', html)
                self.assertNotIn(own, html)
                self.assertNotIn(other, html)
                self.assertAdxFooter(html)


class CookieTests(BrandingFixture, TestCase):
    def test_link_host_pages_set_no_cookies(self):
        for name, host, path in self.customer_pages():
            if host == "adx.se":
                continue
            with self.subTest(page=name):
                response = self.get(host, path)
                self.assertEqual(response.cookies, {})
                self.assertNotIn("Set-Cookie", response.headers)
                html = response.content.decode()
                # Loggan från adx.se (länkvärdarna har ingen /media/), ADX logga
                # från länkvärdens egen /static/.
                self.assertTrue(LOGO_RE.search(html).group(1).startswith("https://adx.se/media/"))
                self.assertNotIn('src="/media/', html)

    def test_media_and_static_are_served_by_nginx_without_django(self):
        conf = (BASE / "server" / "templates" / "nginx.conf.template").read_text("utf-8")
        block = conf[conf.index("location /media/ {") :]
        block = block[: block.index("\n    }")]
        self.assertIn("alias ${MEDIA_DIR}/;", block)
        self.assertNotIn("proxy_pass", block)
        lib = (BASE / "server" / "lib.sh").read_text("utf-8")
        function = lib[lib.index("build_link_block() {") :]
        function = function[: re.search(r"\n[a-z_]+\(\) \{", function[1:]).start() + 1]
        static = function[function.index("location /static/ {") :]
        static = static[: static.index("\n    }")]
        self.assertIn("alias ${STATIC_DIR}/;", static)
        self.assertNotIn("proxy_pass", static)
        # Länkvärdarna har ingen /media/: loggan måste ha adx.se i adressen.
        self.assertNotIn("location /media/", function)
        self.assertIn("proxy_hide_header Set-Cookie;", function)


class SameTimeTests(BrandingFixture, TransactionTestCase):
    """Riktiga anslutningar i två trådar: två besökare på en sida med en logga
    som inte har någon rendition än. Den första gör om filen; den andra väntar
    på samma rendition (images._making) och hittar sedan raden."""

    def setUp(self):
        # TransactionTestCase kör inte setUpTestData.
        type(self).setUpTestData()
        super().setUp()

    def test_two_visitors_build_the_logo_once(self):
        self.assertFalse(EmailImage.objects.filter(asset=self.logo).exists())
        real = images._render
        inside, release = threading.Event(), threading.Event()
        calls, results, errors = [], [], []

        def held(*args, **kwargs):
            calls.append(args[0].pk)
            inside.set()
            release.wait(10)
            return real(*args, **kwargs)

        def visit():
            try:
                results.append(branding.logo(self.account, "Exempelrör"))
            except Exception as exc:  # noqa: BLE001 - rapporteras av testet
                errors.append(exc)
            finally:
                connection.close()

        with mock.patch.object(images, "_render", side_effect=held):
            one = threading.Thread(target=visit)
            one.start()
            self.assertTrue(inside.wait(10), "den första besökaren kom aldrig fram till filen")
            two = threading.Thread(target=visit)
            two.start()
            two.join(0.3)
            self.assertTrue(two.is_alive(), "den andra ska vänta på den första")
            release.set()
            one.join(10)
            two.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [self.logo.pk])
        self.assertEqual(EmailImage.objects.filter(asset=self.logo).count(), 1)
        self.assertEqual(len(results), 2)
        self.assertIsNotNone(results[0])
        self.assertEqual(results[0], results[1])
        self.assertEqual(images._MAKING, {})
