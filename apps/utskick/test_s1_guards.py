"""Vakterna för Kontakter och utskick (README J S1 test_s1_guards, B.0,
C.3, H.7, I.2):

    TemplateGuardTests      inga style= eller inbäddade skript, data-label på
                            varje cell, thead i panelens tabeller, ingen
                            ! eller [ ] i texten, stilmallar i static_version
    ModuleGuardTests        django.core.mail bara i alerts.py, SES send_email
                            bara i email/transport.py, email.message bara i
                            email/mime.py
    MigrationRuleTests      B.0: ett nytt fält på en äldre tabell är null
                            eller har db_default
    ProductionKeyTests      produktionen startar inte utan nycklarna
    NoNetworkRunnerTests    testkörningen når aldrig nätet
"""

import ast
import importlib
import os
import re
import socket
import sys
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import AddField, CreateModel
from django.test import Client, SimpleTestCase, TestCase

BASE = Path(settings.BASE_DIR)
TEMPLATES = BASE / "templates"
APP = BASE / "apps" / "utskick"

#: Verktygets mallar för kontakter och utskick (alla undermappar).
APP_FOLDERS = (
    TEMPLATES / "flamingo" / "app" / "kontakter",
    TEMPLATES / "flamingo" / "app" / "utskick",
)
#: Alla mallar utan style= och inbäddade skript. Bekräftelsemejlet
#: (utskick/mail) har sina stilar inbäddade, som mejl måste. Länkvärdarnas
#: sidor (S2, utskick/links) också: de har dessutom aldrig {% csrf_token %}
#: (test_s2_links.LinkTemplateGuardTests).
NO_INLINE_FOLDERS = (
    *APP_FOLDERS,
    TEMPLATES / "utskick" / "public",
    TEMPLATES / "utskick" / "links",
    TEMPLATES / "manage" / "utskick",
)
MANAGE_FOLDER = TEMPLATES / "manage" / "utskick"
#: Mejlens mallar (S3): bekräftelsemejlet och Brev. Stilarna står inbäddade
#: som mejl måste (inga style=-vakter här), men inga skript, inga relativa
#: adresser och samma copy-regler som sidorna. Brevs egen vakt (tabeller,
#: tokens ur mockupen) står i test_s3_render.
MAIL_FOLDERS = (TEMPLATES / "utskick" / "mail", TEMPLATES / "utskick" / "brev")


def _templates(*folders):
    paths = []
    for folder in folders:
        if folder.is_dir():
            paths += sorted(folder.rglob("*.html"))
    return paths


def _copy(text):
    """Det som syns: utan kommentarer, malltaggar och html-taggar."""
    text = re.sub(r"{% comment %}.*?{% endcomment %}", "", text, flags=re.S)
    text = re.sub(r"{%.*?%}|{{.*?}}|{#.*?#}", "", text, flags=re.S)
    return re.sub(r"<[^>]+>", "", text)


class TemplateGuardTests(SimpleTestCase):
    def test_the_folders_are_walked_recursively(self):
        names = {path.relative_to(TEMPLATES).as_posix() for path in _templates(*APP_FOLDERS)}
        self.assertIn("flamingo/app/kontakter/list.html", names)
        self.assertIn("flamingo/app/kontakter/import/map.html", names)
        # S4 (integrationen): segmentbyggaren, rapporten, Länkar och skriptet
        # ligger i samma mappar och vaktas av samma regler.
        for name in (
            "flamingo/app/kontakter/segment.html",
            "flamingo/app/kontakter/_segment_row.html",
            "flamingo/app/utskick/_report_full.html",
            "flamingo/app/utskick/export.html",
            "flamingo/app/utskick/links.html",
            "flamingo/app/utskick/link.html",
            "flamingo/app/utskick/link_form.html",
            "flamingo/app/utskick/snippet.html",
        ):
            self.assertIn(name, names)
        self.assertIn(
            "manage/utskick/overview.html",
            {p.relative_to(TEMPLATES).as_posix() for p in _templates(MANAGE_FOLDER)},
        )

    def test_no_inline_styles_or_scripts(self):
        for path in _templates(*NO_INLINE_FOLDERS):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(TEMPLATES).as_posix()):
                self.assertNotIn("style=", text)
                self.assertNotRegex(text, r"<script(?![^>]*\ssrc=)")

    def test_every_cell_in_the_tool_has_a_label(self):
        for path in _templates(*APP_FOLDERS):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<td\b[^>]*>", text):
                with self.subTest(path=path.name, cell=match.group(0)):
                    self.assertIn("data-label=", match.group(0))

    def test_every_table_in_the_panel_has_a_header_row(self):
        for path in _templates(MANAGE_FOLDER):
            text = path.read_text(encoding="utf-8")
            tables = re.findall(r"<table\b.*?</table>", text, flags=re.S)
            for table in tables:
                with self.subTest(path=path.name):
                    self.assertIn("<thead>", table)

    def test_no_exclamation_marks_or_brackets_in_the_copy(self):
        for path in _templates(*NO_INLINE_FOLDERS):
            text = _copy(path.read_text(encoding="utf-8"))
            with self.subTest(path=path.relative_to(TEMPLATES).as_posix()):
                self.assertNotIn("!", text)
                self.assertNotRegex(text, r"\[\s*\]")

    def test_the_context_says_kontakt_never_contact(self):
        """Namnregeln: "Kontakter" i Flamingo är också portalens användare,
        så mallarna kallar registrets rader kontakt/kontakter."""
        for path in _templates(*APP_FOLDERS):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotRegex(text, r"{{\s*contact[.\s|}]|{%\s*for\s+contact\s")

    def test_every_stylesheet_and_script_is_versioned(self):
        """Varje css- och js-fil som utskickens mallar laddar står i
        static_version-listan, så att en ändring får en ny adress."""
        listed = (BASE / "apps" / "manage" / "context_processors.py").read_text("utf-8")
        folders = (*NO_INLINE_FOLDERS, TEMPLATES / "flamingo" / "lp")
        for path in _templates(*folders):
            text = path.read_text(encoding="utf-8")
            for name in re.findall(r"{% static ['\"]((?:css|js)/[^'\"]+)['\"] %}", text):
                with self.subTest(path=path.name, file=name):
                    self.assertIn(f'"{name}"', listed)
                    self.assertTrue((BASE / "static" / name).is_file(), name)


class MailTemplateGuardTests(SimpleTestCase):
    """S3 (integrationen): mejlens mallar."""

    def test_the_mail_folders_exist(self):
        names = {p.relative_to(TEMPLATES).as_posix() for p in _templates(*MAIL_FOLDERS)}
        self.assertIn("utskick/mail/doi.html", names)
        self.assertIn("utskick/brev/layout.html", names)
        self.assertIn("utskick/brev/blocks/hero.html", names)

    def test_no_scripts_and_no_relative_addresses_in_mail(self):
        for path in _templates(*MAIL_FOLDERS):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(TEMPLATES).as_posix()):
                self.assertNotIn("<script", text.lower())
                self.assertNotIn("{% static", text)
                self.assertNotIn("{% csrf_token", text)
                self.assertNotRegex(text, r"""(?:src|href)=["']/(?!/)""")

    def test_no_exclamation_marks_or_brackets_in_the_mail_copy(self):
        for path in _templates(*MAIL_FOLDERS):
            text = re.sub(r"<style\b.*?</style>", "", path.read_text("utf-8"), flags=re.S)
            text = _copy(text)
            with self.subTest(path=path.relative_to(TEMPLATES).as_posix()):
                self.assertNotIn("!", text)
                self.assertNotRegex(text, r"\[\s*\]")


class SiteGuardTests(TestCase):
    """Sajtens delar som utskick rör (README C.3)."""

    def test_robots_keeps_crawlers_off_the_public_pages_and_the_apis(self):
        lines = Client().get("/robots.txt").content.decode().splitlines()
        self.assertIn("Disallow: /utskick/", lines)
        self.assertIn("Disallow: /api/", lines)
        self.assertIn("Disallow: /lp/", lines)

    def test_no_adx_page_can_take_the_utskick_address(self):
        from apps.manage.forms import BlockPageForm

        form = BlockPageForm(data={"title": "Utskick", "slug": "utskick", "order": 0})
        self.assertFalse(form.is_valid())
        self.assertIn("reserverad", " ".join(form.errors.get("slug", [])))
        # Avtalssidan (D6) är en vanlig ADX-sida med just den adressen.
        form = BlockPageForm(data={"title": "Biträdesavtal", "slug": "bitradesavtal", "order": 0})
        self.assertNotIn("slug", form.errors)

    def test_the_public_pages_are_never_counted_as_visits(self):
        from apps.analytics.middleware import _SKIP_PREFIXES

        self.assertIn("/utskick/", _SKIP_PREFIXES)


class ModuleGuardTests(SimpleTestCase):
    def modules(self):
        for path in sorted(APP.rglob("*.py")):
            rel = path.relative_to(APP).as_posix()
            if rel.startswith("migrations/") or path.name.startswith("test_"):
                continue
            if path.name == "testing.py":
                continue
            yield rel, path.read_text(encoding="utf-8")

    def imports(self, source):
        """Absoluta importer (from .email import ... är utskicks eget paket,
        inte standardbibliotekets email)."""
        names = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        return names

    def test_django_mail_only_in_alerts(self):
        for rel, source in self.modules():
            if rel == "alerts.py":
                continue
            with self.subTest(module=rel):
                self.assertNotIn("django.core.mail", self.imports(source))

    def test_ses_send_email_only_in_the_transport(self):
        for rel, source in self.modules():
            if rel == "email/transport.py":
                continue
            with self.subTest(module=rel):
                self.assertNotRegex(source, r"\bsend_email\s*\(")

    def test_stdlib_email_message_only_in_mime(self):
        for rel, source in self.modules():
            if rel == "email/mime.py":
                continue
            with self.subTest(module=rel):
                imported = {n for n in self.imports(source) if n.startswith("email.")}
                self.assertEqual(imported, set())

    def test_no_customer_mail_from_the_agency_side(self):
        """Byråns sida mejlar aldrig kunden (README D12): inget i
        manage_views eller demot anropar mejl eller transporten."""
        for rel in ("manage_views.py", "demo.py", "retention.py", "sending/tick.py"):
            source = (APP / rel).read_text(encoding="utf-8")
            with self.subTest(module=rel):
                self.assertNotIn("transport.send(", source)
                self.assertNotRegex(source, r"\bsend_mail\(")


#: De senaste migreringarna per app före S1 (HEAD 3cbdae0). Allt efter dem
#: följer B.0; en ny app har baslinjen 0.
BASELINE = {
    "aidocs": 1,
    "analytics": 3,
    "areas": 5,
    "assistant": 5,
    "cloud": 2,
    "faq": 2,
    "flamingo": 15,
    "inquiries": 4,
    "monitor": 2,
    "offers": 8,
    "projects": 11,
    "services": 9,
    "sms": 5,
    "tools": 1,
    "website": 19,
}


class DeployGuardTests(SimpleTestCase):
    def test_the_tick_lock_is_held_through_the_health_check(self):
        """C.5: en rollback (git reset, uv sync, migrate) körs också under
        tick-låset, så låset släpps först efter hälsogrinden."""
        script = (BASE / "server" / "deploy.sh").read_text("utf-8")
        body = script[script.index("deploy_one() {") :]
        release = body.index("flock -u 9")
        self.assertGreater(release, body.index('"http://localhost/healthz/"'))
        self.assertGreater(release, body.index('rollback "$before" "healthz svarade inte 200"'))
        self.assertLess(body.index("flock -w 90 9"), body.index("git pull --ff-only"))


class MigrationRuleTests(SimpleTestCase):
    """B.0: förra versionen kör vidare mellan migrate och reload, och för
    gott efter en automatisk tillbakarullning. Ett nytt fält på en tabell
    som en tidigare migrering skapat måste därför vara null=True eller ha
    db_default. Ingen undantagslista."""

    def local_labels(self):
        return {
            config.label for config in apps.get_app_configs() if config.name.startswith("apps.")
        }

    def in_scope(self, label, name):
        match = re.match(r"(\d{4})_", name)
        return bool(match) and int(match.group(1)) > BASELINE.get(label, 0)

    def violations(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        local = self.local_labels()
        found = []
        for (label, name), migration in sorted(loader.disk_migrations.items()):
            if label not in local or not self.in_scope(label, name):
                continue
            created = {
                op.name.lower() for op in migration.operations if isinstance(op, CreateModel)
            }
            for op in migration.operations:
                if not isinstance(op, AddField) or op.model_name.lower() in created:
                    continue
                field = op.field
                if isinstance(field, models.ManyToManyField):
                    continue
                if field.null or field.db_default is not models.NOT_PROVIDED:
                    continue
                found.append(f"{label}.{name}: {op.model_name}.{op.name}")
        return found

    def test_new_fields_on_old_tables_are_null_or_db_default(self):
        self.assertEqual(self.violations(), [])

    def test_the_s1_migrations_are_in_scope(self):
        self.assertTrue(self.in_scope("utskick", "0001_kontakter"))
        self.assertTrue(self.in_scope("flamingo", "0016_forfragans_kontakt"))
        self.assertFalse(self.in_scope("flamingo", "0015_x"))

    def test_the_rule_catches_a_plain_add_field(self):
        class Fake:
            operations = [
                AddField("lead", "attribution", models.JSONField(default=dict)),
                AddField("lead", "activity_at", models.DateTimeField(null=True)),
            ]

        loader = mock.Mock(disk_migrations={("flamingo", "0099_test"): Fake()})
        with mock.patch("apps.utskick.test_s1_guards.MigrationLoader", return_value=loader):
            self.assertEqual(self.violations(), ["flamingo.0099_test: lead.attribution"])


class ProductionKeyTests(SimpleTestCase):
    MODULE = "config.settings.production"

    def load(self, environ):
        saved = sys.modules.pop(self.MODULE, None)
        try:
            with mock.patch.dict(os.environ, environ):
                return importlib.import_module(self.MODULE)
        finally:
            sys.modules.pop(self.MODULE, None)
            if saved is not None:
                sys.modules[self.MODULE] = saved

    def test_production_refuses_to_start_without_the_keys(self):
        for missing in ("UTSKICK_HASH_KEY", "UTSKICK_LINK_KEY"):
            environ = {
                "UTSKICK_HASH_KEY": "h" * 64,
                "UTSKICK_LINK_KEY": "l" * 64,
                "ALLOWED_HOSTS": "adx.se",
                missing: "",
            }
            with self.subTest(missing=missing):
                with self.assertRaisesMessage(ImproperlyConfigured, missing):
                    self.load(environ)

    def test_production_starts_with_both_keys(self):
        module = self.load(
            {
                "UTSKICK_HASH_KEY": "h" * 64,
                "UTSKICK_LINK_KEY": "l" * 64,
                "ALLOWED_HOSTS": "adx.se",
                "SENTRY_DSN": "",
            }
        )
        self.assertFalse(module.DEBUG)


class NoNetworkRunnerTests(SimpleTestCase):
    def test_the_runner_is_the_no_network_runner(self):
        self.assertEqual(settings.TEST_RUNNER, "config.test_runner.NoNetworkRunner")

    def test_sending_is_off_whatever_env_says(self):
        self.assertFalse(settings.SMS_SEND_LIVE)
        self.assertFalse(settings.UTSKICK_EMAIL_LIVE)
        self.assertEqual(settings.ADX_AWS_PROFILE, "__test__")
        self.assertEqual(settings.UTSKICK_TICK_MAX_MB, 0)

    def test_a_connection_outside_loopback_is_refused(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("192.0.2.10", 443), timeout=1)

    def test_a_name_lookup_is_refused(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(ConnectionRefusedError):
                socket.getaddrinfo("adx.se", 443)

    def test_loopback_still_works(self):
        self.assertTrue(socket.getaddrinfo("localhost", 5432))
