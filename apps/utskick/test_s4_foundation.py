"""
Grunden för S4 (README J S4): modellerna och migreringen (B.0 och
raderingsregeln i databasen), adresserna till stubbarna (I.1, E.1),
hjälpen för namngivna länkar och skriptet (E.6), QR-koderna, "Avsluta
utskick och radera allt" och modulernas signaturer (S4-HANDOFF.md).

    ModelTests          segmenten, skripten, regeln för namngivna länkar, händelsen
    DbOnDeleteTests     de nya främmande nycklarnas regler, och en äldre version som raderar
    AppRouteTests       varje S4-adress i verktyget: kontots, 404 annars, POST-regler, flikarna
    LinkHostRouteTests  /<konto>/<slug>, /s.<ver>.js och /v bara på klick, inga kakor
    NamedLinkTests      slugens form och adressen
    SnippetFileTests    versionen, SRI, sparade versioner och taggen
    OwnDomainTests      skriptets domäner gör inga länkar fria från granskningen
    QrTests             SVG och PNG, aldrig Micro QR, svaret
    EndAccountTests     S4:s rader går med "Avsluta utskick och radera allt"
    GuardTests          segno, static_version och skriptets regler (inga kakor, ingen lagring)
    SignatureTests      modulerna och namnen som byggarna anropar
"""

import base64
import hashlib
import importlib
import inspect
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ForeignKey
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse

from . import access, dbfk, links, manage_views, nav, qr
from .models import (
    RESERVED_PUBLIC_SLUGS,
    Event,
    Segment,
    SignupForm,
    SiteSnippet,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from .testing import UtskickFixture

User = get_user_model()
BASE = Path(settings.BASE_DIR)
MIGRATION = "apps.utskick.migrations.0004_segment_och_skript"
#: S4:s migreringar (0005: de tidigare adresserna, säkerhetsgranskningen).
MIGRATIONS = (MIGRATION, "apps.utskick.migrations.0005_gamla_adresser")

LINK_SETTINGS = {
    "UTSKICK_LINK_HOSTS": ["k.adx.se", "klick.adx.se"],
    "UTSKICK_SMS_LINK_BASE": "https://k.adx.se",
    "UTSKICK_EMAIL_LINK_BASE": "https://klick.adx.se",
}


def make_named(account, slug="vinter", **kwargs):
    data = {
        "kind": TrackedLink.Kind.EXTERNAL,
        "destination": "https://exempelror.example/vinter",
        "label": "Affisch i verkstaden",
    }
    data.update(kwargs)
    return TrackedLink.objects.create(account=account, slug=slug, **data)


# ---------------------------------------------------------------------------
# Modellerna och migreringen
# ---------------------------------------------------------------------------


class ModelTests(UtskickFixture, TestCase):
    def test_a_segment_and_its_defaults(self):
        segment = Segment.objects.create(account=self.account, name="Service i höst")
        segment.refresh_from_db()
        self.assertEqual(segment.rules, {})
        self.assertEqual(
            (segment.cached_count, segment.cached_sms, segment.cached_email), (0, 0, 0)
        )
        self.assertIsNone(segment.counted_at)
        self.assertEqual(str(segment), f"Segment {segment.pk}")
        self.assertEqual(list(self.account.utskick_segments.all()), [segment])

    def test_one_name_per_account(self):
        Segment.objects.create(account=self.account, name="Kunder i Bromma")
        Segment.objects.create(account=self.other_account, name="Kunder i Bromma")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Segment.objects.create(account=self.account, name="Kunder i Bromma")

    def test_rule_count(self):
        rules = {
            "all": [
                {"f": "list", "op": "in", "v": [1]},
                {"any": [{"f": "tag", "op": "in", "v": [2]}, {"f": "kind", "op": "eq", "v": "x"}]},
            ]
        }
        self.assertEqual(Segment(rules=rules).rule_count, 3)
        for broken in ({}, {"all": "x"}, [], None, {"all": [1, "x"]}):
            with self.subTest(rules=broken):
                self.assertEqual(Segment(rules=broken).rule_count, 0)

    def test_a_snippet_gets_a_public_key(self):
        site = SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        other = SiteSnippet.objects.create(account=self.account, domain="butik.example")
        self.assertRegex(site.key, r"^[A-Za-z0-9_-]{16}$")
        self.assertNotEqual(site.key, other.key)
        self.assertFalse(site.is_installed)
        self.assertNotIn("exempelror", str(site))
        self.assertEqual(set(self.account.utskick_sites.all()), {site, other})

    def test_one_row_per_account_and_domain(self):
        SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        SiteSnippet.objects.create(account=self.other_account, domain="exempelror.example")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteSnippet.objects.create(account=self.account, domain="exempelror.example")

    def test_a_link_without_utskick_needs_a_slug(self):
        link = make_named(self.account)
        self.assertTrue(link.is_named)
        utskick = Utskick.objects.create(account=self.account, name="Höstservice")
        own = TrackedLink.objects.create(
            account=self.account, utskick=utskick, kind="external", key="boka", destination="x"
        )
        self.assertFalse(own.is_named)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrackedLink.objects.create(account=self.account, kind="external", destination="x")

    def test_one_slug_per_account(self):
        make_named(self.account)
        make_named(self.other_account)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_named(self.account)

    def test_the_account_goes_with_its_s4_rows(self):
        segment = Segment.objects.create(account=self.account, name="A")
        site = SiteSnippet.objects.create(account=self.account, domain="exempelror.example")
        link = make_named(self.account)
        self.account.delete()
        self.assertFalse(Segment.objects.filter(pk=segment.pk).exists())
        self.assertFalse(SiteSnippet.objects.filter(pk=site.pk).exists())
        self.assertFalse(TrackedLink.objects.filter(pk=link.pk).exists())

    def test_site_visit_is_an_s4_event_kind(self):
        self.assertEqual(Event.S4_KINDS, ("site_visit",))

    def test_the_link_hosts_first_segments_are_reserved(self):
        for slug in ("a", "b", "c", "m", "o", "p", "s", "v", "w"):
            with self.subTest(slug=slug):
                self.assertIn(slug, RESERVED_PUBLIC_SLUGS)
                with self.assertRaises(ValidationError):
                    access.validate_public_slug(slug)

    def test_slugs_the_link_hosts_never_pass_to_the_app_are_reserved(self):
        """nginx och asgi_app svarar 404 för allt som börjar med /token,
        /register, /mcp och så vidare på länkvärdarna."""
        for slug in ("mcp", "mcpherson-ror", "tokenbolaget", "register-ab", "revoket", "authorize"):
            with self.subTest(slug=slug), self.assertRaises(ValidationError):
                access.validate_public_slug(slug)
        self.assertEqual(access.validate_public_slug("ror-token"), "ror-token")
        self.assertEqual(access.suggest_public_slug("Tokenbolaget AB"), "kund-tokenbolaget")
        self.assertEqual(access.suggest_public_slug("MCP Rör AB"), "kund-mcp-ror")


class DbOnDeleteTests(UtskickFixture, TestCase):
    def test_the_migration_lists_every_new_foreign_key(self):
        for path in MIGRATIONS:
            module = importlib.import_module(path)
            listed = set(module.S4_FOREIGN_KEYS)
            found = set()
            for op in module.Migration.operations:
                for name, field in getattr(op, "fields", None) or ():
                    if isinstance(field, ForeignKey):
                        found.add(("utskick", op.name.lower(), name))
                field = getattr(op, "field", None)
                if isinstance(field, ForeignKey):
                    found.add(("utskick", op.model_name.lower(), op.name))
            with self.subTest(migration=path):
                self.assertEqual(listed, found)

    def test_each_rule_is_in_the_database(self):
        from django.apps import apps as django_apps

        for path in MIGRATIONS:
            module = importlib.import_module(path)
            for app_label, model_name, field_name in module.S4_FOREIGN_KEYS:
                model = django_apps.get_model(app_label, model_name)
                field = model._meta.get_field(field_name)
                action = dbfk.sql_action(field)
                with self.subTest(field=f"{model_name}.{field_name}"):
                    self.assertIsNotNone(action)
                    rules = dbfk.rules(connection, model._meta.db_table)
                    self.assertEqual(rules.get(field.column), dbfk.CONFDELTYPE[action])

    def test_no_new_column_on_an_older_table(self):
        """B.0: 0004 och 0005 lägger bara till tabeller, en regel och ett index."""
        from django.db.migrations import AddField

        for path in MIGRATIONS:
            module = importlib.import_module(path)
            operations = module.Migration.operations
            created = {op.name.lower() for op in operations if hasattr(op, "fields")}
            for op in operations:
                if isinstance(op, AddField):
                    with self.subTest(field=f"{op.model_name}.{op.name}"):
                        self.assertIn(op.model_name.lower(), created)

    def test_the_named_link_rule_and_index_are_in_the_database(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, TrackedLink._meta.db_table
            )
        self.assertIn("utskick_link_named_slug", constraints)
        self.assertIn("utskick_link_named", constraints)

    def test_an_older_release_can_delete_a_user(self):
        user = User.objects.create_user("tillfallig-s4")
        segment = Segment.objects.create(account=self.account, name="A", created_by=user)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM auth_user WHERE id = %s", [user.pk])
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        segment.refresh_from_db()
        self.assertIsNone(segment.created_by_id)


# ---------------------------------------------------------------------------
# Adresserna (I.1, E.1)
# ---------------------------------------------------------------------------

#: (namn, argument: "pk" utskickets/radens pk, "fmt" QR-formatet; metoden stubben svarar på)
APP_ROUTES = [
    ("flamingo:app_segment_new", (), "get"),
    ("flamingo:app_segment", ("segment",), "get"),
    ("flamingo:app_segment_count", (), "post"),
    ("flamingo:app_contact_sms", ("contact",), "get"),
    ("flamingo:app_signup_qr", ("fmt",), "get"),
    ("flamingo:app_utskick_follow_up", ("utskick",), "post"),
    ("flamingo:app_utskick_export", ("utskick",), "get"),
    ("flamingo:app_links", (), "get"),
    ("flamingo:app_link_new", (), "get"),
    ("flamingo:app_link", ("link",), "get"),
    ("flamingo:app_link_qr", ("link", "fmt"), "get"),
    ("flamingo:app_utskick_snippet", (), "get"),
]
APP_PATHS = {
    "flamingo:app_segment_new": "/flamingo/app/kontakter/segment/ny/",
    "flamingo:app_segment": "/flamingo/app/kontakter/segment/{segment}/",
    "flamingo:app_segment_count": "/flamingo/app/kontakter/segment/antal/",
    "flamingo:app_contact_sms": "/flamingo/app/kontakter/{contact}/sms/",
    "flamingo:app_signup_qr": "/flamingo/app/kontakter/anmalan/qr.svg",
    "flamingo:app_utskick_follow_up": "/flamingo/app/utskick/{utskick}/folj-upp/",
    "flamingo:app_utskick_export": "/flamingo/app/utskick/{utskick}/export/",
    "flamingo:app_links": "/flamingo/app/utskick/lankar/",
    "flamingo:app_link_new": "/flamingo/app/utskick/lankar/ny/",
    "flamingo:app_link": "/flamingo/app/utskick/lankar/{link}/",
    "flamingo:app_link_qr": "/flamingo/app/utskick/lankar/{link}/qr.svg",
    "flamingo:app_utskick_snippet": "/flamingo/app/utskick/installningar/skript/",
}
#: Svar som en byggd eller obyggd vy får ge för det egna kontot.
OWN_STATUSES = (200, 302, 400, 409, 501)


class AppRouteTests(UtskickFixture, TestCase):
    def setUp(self):
        super().setUp()
        from .testing import make_contact

        self.own = {
            "segment": Segment.objects.create(account=self.account, name="Service i höst"),
            "contact": make_contact(self.account, first_name="Anna"),
            "utskick": Utskick.objects.create(account=self.account, name="Höstservice"),
            "link": make_named(self.account),
        }
        self.foreign = {
            "segment": Segment.objects.create(account=self.other_account, name="Hemligt"),
            "contact": make_contact(self.other_account, first_name="Hemlig"),
            "utskick": Utskick.objects.create(account=self.other_account, name="Hemligt"),
            "link": make_named(self.other_account, slug="hemlig"),
        }
        SignupForm.objects.create(account=self.account, title="Erbjudanden", is_active=True)
        self.client = self.client_for(self.anna)

    def url(self, name, args, rows=None):
        rows = rows or self.own
        values = ["svg" if arg == "fmt" else rows[arg].pk for arg in args]
        return reverse(name, args=values)

    def test_the_paths_are_the_contracts(self):
        for name, args, _method in APP_ROUTES:
            with self.subTest(name=name):
                expected = APP_PATHS[name].format(**{k: v.pk for k, v in self.own.items()})
                self.assertEqual(self.url(name, args), expected)
        png = reverse("flamingo:app_link_qr", args=[self.own["link"].pk, "png"])
        self.assertTrue(png.endswith("/qr.png"))

    def test_only_svg_and_png(self):
        path = f"/flamingo/app/utskick/lankar/{self.own['link'].pk}/qr.gif"
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/flamingo/app/kontakter/anmalan/qr.jpg").status_code, 404)

    def test_every_route_answers_for_the_own_account(self):
        for name, args, method in APP_ROUTES:
            with self.subTest(name=name):
                response = getattr(self.client, method)(self.url(name, args))
                self.assertIn(response.status_code, OWN_STATUSES)

    def test_another_accounts_rows_are_404(self):
        for name, args, method in APP_ROUTES:
            if not [a for a in args if a != "fmt"]:
                continue
            with self.subTest(name=name):
                response = getattr(self.client, method)(self.url(name, args, self.foreign))
                self.assertEqual(response.status_code, 404)

    def test_everything_is_404_when_utskick_is_off(self):
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for client in (self.client, self.client_for(self.staff)):
            for name, args, method in APP_ROUTES:
                with self.subTest(name=name, staff=client is not self.client):
                    response = getattr(client, method)(self.url(name, args))
                    self.assertEqual(response.status_code, 404)

    def test_post_routes_refuse_get(self):
        for name, args, method in APP_ROUTES:
            if method == "post":
                with self.subTest(name=name):
                    self.assertEqual(self.client.get(self.url(name, args)).status_code, 405)

    def test_staff_in_view_as_reaches_the_pages(self):
        staff = self.client_for(self.staff)
        for name in ("flamingo:app_links", "flamingo:app_utskick_snippet"):
            with self.subTest(name=name):
                self.assertEqual(staff.get(reverse(name)).status_code, 200)

    def test_the_links_tab(self):
        keys = [key for key, _name, _label in nav.UTSKICK_TABS]
        self.assertEqual(keys, ["utskick", "links", "health", "settings"])
        html = self.client.get(reverse("flamingo:app_links")).content.decode()
        self.assertIn('<summary class="fl-subnav__summary">Utskick: Länkar</summary>', html)
        html = self.client.get(reverse("flamingo:app_utskick_snippet")).content.decode()
        self.assertIn('<summary class="fl-subnav__summary">Utskick: Inställningar</summary>', html)

    def test_segments_live_under_listor(self):
        response = self.client.get(reverse("flamingo:app_segment_new"))
        self.assertEqual(response.context["app_active"], "contacts")
        self.assertIn(
            '<summary class="fl-subnav__summary">Kontakter: Listor</summary>',
            response.content.decode(),
        )


@override_settings(**LINK_SETTINGS)
class LinkHostRouteTests(TestCase):
    def path(self, name, *args):
        return reverse(f"links:{name}", urlconf="config.urls_links", args=list(args))

    def test_the_paths(self):
        self.assertEqual(self.path("named", "exempelror", "vinter"), "/exempelror/vinter")
        self.assertEqual(self.path("snippet", "1a2b3c4d"), "/s.1a2b3c4d.js")
        self.assertEqual(self.path("snippet_beacon"), "/v")

    def test_the_named_links_never_shadow_the_older_paths(self):
        expected = {
            "/Ab12Cd": "click",
            "/s/Ab12Cd": "sms_unsubscribe",
            "/p/Ab12Cd": "sms_preferences",
            "/b/Ab12Cd": "confirm",
            "/m/" + "A" * 20: "email_click",
            "/a/" + "a" * 50: "email_unsubscribe",
            "/v/" + "a" * 50: "email_preferences",
            "/w/" + "a" * 20: "web_view",
            "/v": "snippet_beacon",
            "/s.1a2b3c4d.js": "snippet",
            "/exempelror/vinter": "named",
            "/exempel_ror/host-2026": "named",
        }
        for path, name in expected.items():
            with self.subTest(path=path):
                self.assertEqual(resolve(path, urlconf="config.urls_links").url_name, name)

    def test_only_on_the_email_host(self):
        for path in ("/exempelror/vinter", "/s.1a2b3c4d.js"):
            with self.subTest(path=path):
                self.assertEqual(Client().get(path, HTTP_HOST="k.adx.se").status_code, 404)
                self.assertEqual(Client().get(path).status_code, 404)
        self.assertEqual(Client().post("/v", HTTP_HOST="k.adx.se").status_code, 404)

    def test_no_cookie_and_no_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/v",
            data='{"k": "x", "t": "y", "p": "/", "s": 3}',
            content_type="text/plain",
            HTTP_HOST="klick.adx.se",
            HTTP_ORIGIN="https://exempelror.example",
        )
        self.assertNotIn(response.status_code, (403, 405, 500))
        self.assertEqual(response.cookies, {})
        for path in ("/exempelror/vinter", "/s.1a2b3c4d.js"):
            with self.subTest(path=path):
                response = Client().get(path, HTTP_HOST="klick.adx.se")
                self.assertNotIn(response.status_code, (403, 405, 500))
                self.assertEqual(response.cookies, {})
                self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")


# ---------------------------------------------------------------------------
# Hjälpen för namngivna länkar och skriptet (E.1, E.6)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class NamedLinkTests(UtskickFixture, TestCase):
    def test_the_address(self):
        link = make_named(self.account)
        self.assertEqual(links.named_link_url(link), "https://klick.adx.se/exempelror/vinter")
        self.assertEqual(links.named_link_text(link), "klick.adx.se/exempelror/vinter")
        self.assertEqual(links.named_link_url(link, "annan"), "https://klick.adx.se/annan/vinter")

    def test_the_slug(self):
        for raw, slug in (("vinter", "vinter"), (" Vinter-2026 ", "vinter-2026"), ("a", "a")):
            with self.subTest(raw=raw):
                self.assertEqual(links.clean_named_slug(raw), slug)
        for raw in ("", "-vinter", "vinter-", "vin ter", "vinter_2026", "vinterdäck", "x" * 41):
            with self.subTest(raw=raw), self.assertRaises(links.LinkRefused):
                links.clean_named_slug(raw)


@override_settings(**LINK_SETTINGS)
class SnippetFileTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "static" / "utskick").mkdir(parents=True)
        override = override_settings(BASE_DIR=self.root)
        override.enable()
        self.addCleanup(override.disable)

    def write(self, name, text):
        path = self.root / "static" / "utskick" / name
        path.write_text(text, encoding="utf-8")
        return path.read_bytes()

    @staticmethod
    def ver(data):
        return hashlib.sha256(data).hexdigest()[:8]

    def test_without_the_file(self):
        self.assertEqual(links.snippet_version(), "")
        self.assertIsNone(links.snippet_body("1a2b3c4d"))
        self.assertEqual(links.snippet_integrity(), "")

    def test_the_version_and_sri_follow_the_bytes(self):
        data = self.write("s.js", "(function(){})();\n")
        ver = self.ver(data)
        self.assertEqual(links.snippet_version(), ver)
        self.assertEqual(links.snippet_body(ver), data)
        sri = "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()
        self.assertEqual(links.snippet_integrity(), sri)
        self.assertEqual(links.snippet_url(), f"https://klick.adx.se/s.{ver}.js")
        self.assertEqual(links.beacon_url(), "https://klick.adx.se/v")
        changed = self.write("s.js", "(function(){ return 1; })();\n")
        self.assertEqual(links.snippet_version(), self.ver(changed))
        self.assertIsNone(links.snippet_body(ver))

    def test_an_archived_version_is_served_only_when_its_hash_matches(self):
        old = b"(function(){ var a = 1; })();\n"
        old_ver = self.ver(old)
        self.write(f"s.{old_ver}.js", old.decode())
        self.write("s.js", "(function(){})();\n")
        self.assertEqual(links.snippet_body(old_ver), old)
        self.assertTrue(links.snippet_integrity(old_ver).startswith("sha384-"))
        self.write("s.0000beef.js", "(function(){})();\n")
        self.assertIsNone(links.snippet_body("0000beef"))
        for bad in ("", "1a2b3c4", "1A2B3C4D", "../s.js", None):
            with self.subTest(ver=bad):
                self.assertIsNone(links.snippet_body(bad))

    def test_the_tag(self):
        self.write("s.js", "(function(){})();\n")

        class Site:
            key = 'Ab12"<x>'

        tag = links.snippet_tag(Site())
        self.assertTrue(tag.startswith(f'<script src="{links.snippet_url()}"'))
        self.assertIn(f'integrity="{links.snippet_integrity()}"', tag)
        self.assertIn('crossorigin="anonymous"', tag)
        self.assertIn("async></script>", tag)
        self.assertIn('data-k="Ab12&quot;&lt;x&gt;"', tag)


class OwnDomainTests(UtskickFixture, TestCase):
    def test_a_snippet_domain_is_not_free_from_review(self):
        SiteSnippet.objects.create(account=self.account, domain="annan-sajt.example")
        self.assertNotIn("annan-sajt.example", links.own_domains(self.account))
        self.assertEqual(links.host_status(self.account, "annan-sajt.example"), links.STATUS_NEW)
        # Kundens webbplats i kundregistret är fortfarande fri.
        self.assertIn("exempelror.example", links.own_domains(self.account))


# ---------------------------------------------------------------------------
# QR-koderna
# ---------------------------------------------------------------------------


class QrTests(SimpleTestCase):
    URL = "https://klick.adx.se/exempelror/vinter"

    def test_svg(self):
        body = qr.svg(self.URL).decode("ascii")
        self.assertTrue(body.startswith("<svg "))
        self.assertRegex(body, r'viewBox="0 0 (\d+) \1"')
        self.assertRegex(body, r'width="\d+" height="\d+"')
        self.assertNotIn("<?xml", body)
        self.assertNotIn("class=", body)
        self.assertNotIn("<script", body)

    def test_png(self):
        self.assertTrue(qr.png(self.URL).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_never_micro_qr(self):
        import segno

        self.assertFalse(qr._code("https://k").is_micro)
        self.assertFalse(segno.make_qr("1", error="m").is_micro)

    def test_an_empty_address_is_refused(self):
        with self.assertRaises(ValueError):
            qr.svg("")

    def test_the_response(self):
        response = qr.response(self.URL, "svg", filename="klick exempelror/vinter")
        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertEqual(
            response["Content-Disposition"], 'inline; filename="klick-exempelror-vinter.svg"'
        )
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        download = qr.response(self.URL, "png", filename="vinter", download=True)
        self.assertEqual(download["Content-Type"], "image/png")
        self.assertEqual(download["Content-Disposition"], 'attachment; filename="vinter.png"')
        with self.assertRaises(Http404):
            qr.response(self.URL, "gif", filename="vinter")


# ---------------------------------------------------------------------------
# Avsluta utskick och radera allt
# ---------------------------------------------------------------------------


class EndAccountTests(UtskickFixture, TestCase):
    def test_the_s4_rows_go_and_the_other_accounts_stay(self):
        for account in (self.account, self.other_account):
            Segment.objects.create(account=account, name="A")
            SiteSnippet.objects.create(account=account, domain="sajt.example")
            make_named(account)
        manage_views.end_account(self.account, self.staff)
        for model in (Segment, SiteSnippet, TrackedLink):
            with self.subTest(model=model.__name__):
                self.assertFalse(model.objects.filter(account=self.account).exists())
                self.assertTrue(model.objects.filter(account=self.other_account).exists())


# ---------------------------------------------------------------------------
# Vakter
# ---------------------------------------------------------------------------


class GuardTests(SimpleTestCase):
    def test_segno_is_a_dependency(self):
        importlib.import_module("segno")
        self.assertIn('"segno>=', (BASE / "pyproject.toml").read_text("utf-8"))
        self.assertIn('name = "segno"', (BASE / "uv.lock").read_text("utf-8"))

    def test_the_s4_static_files_are_versioned(self):
        listed = (BASE / "apps" / "manage" / "context_processors.py").read_text("utf-8")
        for name in (
            "css/flamingo-app-segment.css",
            "css/flamingo-app-utskick-report.css",
            "css/flamingo-app-utskick-links.css",
            "js/flamingo-app-segment.js",
            "js/flamingo-app-links.js",
        ):
            with self.subTest(name=name):
                self.assertIn(f'"{name}"', listed)

    def test_the_snippet_sets_no_cookie_and_uses_no_storage(self):
        """E.6, H.5, LEK 9 kap. 28 §: inga kakor, ingen lagring, under 2 kB."""
        folder = BASE / "static" / "utskick"
        files = [folder / "s.js", *sorted(folder.glob("s.*.js"))]
        self.assertTrue(files[0].is_file())
        for path in files:
            text = path.read_text("utf-8")
            with self.subTest(file=path.name):
                for word in ("cookie", "localStorage", "sessionStorage", "indexedDB", "caches."):
                    self.assertNotIn(word, text)
                self.assertLess(len(text.encode("utf-8")), 2048)

    def test_the_stubs_are_gone(self):
        """Grunden lade tillbaka stubbmallarna; segmentbyggaren tog bort dem
        med den sista stubben (S4-HANDOFF.md): inga stubbmallar, ingen
        render_stub och ingen not_built kvar."""
        stub = BASE / "templates" / "flamingo" / "app"
        self.assertFalse((stub / "utskick" / "_stub.html").exists())
        self.assertFalse((stub / "kontakter" / "_stub.html").exists())
        for path in (BASE / "apps" / "utskick").rglob("*.py"):
            if path.name.startswith("test_"):
                continue
            text = path.read_text("utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("render_stub", text)
                self.assertNotIn("not_built", text)


# ---------------------------------------------------------------------------
# Modulerna och signaturerna som byggarna anropar (S4-HANDOFF.md)
# ---------------------------------------------------------------------------

#: modul -> {namn: parametrarna i ordning, eller None för en konstant, klass eller vy}
SIGNATURES = {
    "apps.utskick.segments": {
        "MAX_RULES": None,
        "MAX_GROUPS": None,
        "FIELDS": None,
        "OPS": None,
        "OPENED_LOCKED_TEXT": None,
        "SegmentError": None,
        "clean": ["account", "rules"],
        "compile_q": ["account_id", "rules", "now"],
        "contacts": ["account", "rules", "now"],
        "count": ["account", "rules", "now"],
        "refresh": ["segment", "now"],
        "matches_q": ["account_id", "segment_ids", "now"],
        "for_contact": ["contact", "now"],
        "opened_locked": ["account"],
        "describe": ["account", "rules"],
        "follow_up_rules": ["utskick"],
        "create_follow_up": ["utskick", "user", "now"],
    },
    "apps.utskick.reports": {
        "FUNNEL_STEPS": None,
        "CHART_HOURS": None,
        "EXPORT_HEADER": None,
        "funnel": ["utskick", "numbers"],
        "clicks_per_hour": ["utskick", "now", "hours"],
        "per_link": ["utskick"],
        "lp_behaviour": ["utskick"],
        "full": ["utskick", "now"],
        "export_rows": ["utskick"],
    },
    "apps.utskick.timeline": {
        "REPLY_HABIT_MIN": None,
        "contact_sms_items": ["contact", "limit"],
        "reply_habit": ["contact", "now"],
    },
    "apps.utskick.contact_sms": {
        "problem": ["contact", "now"],
        "thread_for": ["contact", "now"],
        "send": ["contact", "text", "actor", "now"],
    },
    "apps.utskick.qr": {
        "FORMATS": None,
        "svg": ["data", "scale"],
        "png": ["data", "scale"],
        "response": ["data", "fmt", "filename", "download"],
    },
    "apps.utskick.links": {
        "NAMED_SLUG_RE": None,
        "SNIPPET_SOURCE": None,
        "clean_named_slug": ["raw"],
        "named_link_url": ["link", "public_slug"],
        "named_link_text": ["link", "public_slug"],
        "snippet_version": [],
        "snippet_body": ["ver"],
        "snippet_integrity": ["ver"],
        "snippet_url": ["ver"],
        "beacon_url": [],
        "snippet_tag": ["site"],
    },
    "apps.utskick.link_views": {"named": None, "snippet": None, "snippet_beacon": None},
    "apps.utskick.app_views.segments": {
        "segment_new": None,
        "segment_detail": None,
        "segment_count": None,
    },
    "apps.utskick.app_views.report": {"utskick_follow_up": None, "utskick_export": None},
    "apps.utskick.app_views.links": {
        "link_list": None,
        "link_new": None,
        "link_detail": None,
        "link_qr": None,
        "signup_qr": None,
    },
    "apps.utskick.app_views.snippet": {"snippet_settings": None},
    "apps.utskick.app_views.contact_sms": {"contact_sms": None},
}


class SignatureTests(SimpleTestCase):
    def test_every_module_has_its_names(self):
        for module_name, names in SIGNATURES.items():
            module = importlib.import_module(module_name)
            for name, params in names.items():
                with self.subTest(name=f"{module_name}.{name}"):
                    self.assertTrue(hasattr(module, name))
                    if params is None:
                        continue
                    found = list(inspect.signature(getattr(module, name)).parameters)
                    self.assertEqual(found[: len(params)], params)

    def test_the_rule_vocabulary(self):
        from . import segments

        self.assertEqual(
            set(segments.FIELDS),
            {
                "list",
                "tag",
                "field",
                # S4 (segment-byggaren): kontaktens fasta fält (contact:<namn>).
                "contact",
                "consent",
                "kind",
                "got_utskick",
                "opened",
                "clicked",
                "visited_lp",
                "lead",
                # Svar i formulär (Giovanni 2026-10-10): answer:<sida>.<fråga>.
                "answer",
                "replied",
                "source",
                "created",
            },
        )
        self.assertIn("before_months", segments.OPS)
        self.assertIn("not_within_days", segments.OPS)
        self.assertNotIn(chr(0x21), segments.OPENED_LOCKED_TEXT)
        self.assertTrue(segments.OPENED_LOCKED_TEXT.endswith("."))
