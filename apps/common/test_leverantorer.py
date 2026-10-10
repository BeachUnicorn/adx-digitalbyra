"""
Vakttest: inga leverantörsnamn där kunder, mottagare eller allmänheten ser dem.

Giovanni 2026-10-10: "What providers I am using is private. Also same with
AWS. Unless absolutely necessary, you DO NOT just hand away information about
my infrastructure partners and choices." Det hade glidit in på sju ställen i
Flamingo ("ADX behöver slå på sms-tjänsten (46elks) först.", "Paus vid 4 %
· AWS gräns 5 %", "Klagomål · SES" på kontaktkortet, leverantörens råa fel i
inkorgen och i SMS-API:t ...). I texten heter de "sms-tjänsten",
"e-posttjänsten", "e-posttjänstens gräns" och så vidare.

Vad vakten läser:

  * mallar      templates/**/*.html och *.txt, utom byråns panel
                (templates/manage/). {% comment %} och {# #} räknas inte,
                de renderas aldrig; <!-- --> räknas, den når webbläsaren.
  * python      strängar i apps/**/*.py utom tester och migrationer, och
                utom docstrings, loggrader (logger.*) och larm till byrån
                (alerts.*), som aldrig når kunden.
  * static      static/**/*.js och *.css (utom dist/) och src/**/*.js. Allt
                där går att läsa för vem som helst, kommentarer också.
  * seed_data   seed_data/*.json: den publika sajtens och Flamingos sidor.
  * aidocs      apps/aidocs/content/*.md: guiderna på /aiz/ (kräver kod).

Namnen och mönstren står i apps/common/providers.py (NAMES). Mallar, python
och seed-data prövas med strict=True: där är också ett ensamt "S3" eller
"RDS" leverantören. Static prövas utan, för "S3" är också namnet på ett
byggsteg (README J S3) i kommentarerna.

Allt som får nämna en leverantör står i ALLOWED_* med skälet. Tre slag:

  1. Byråns egna vyer, larm och kommandon (kunden ser dem aldrig).
  2. Tekniskt nödvändigt: DNS-posterna kunden lägger in för sin
     avsändardomän, och adresser och fält som bara leverantören anropar.
  3. Juridiskt nödvändigt: personuppgiftsbiträden som mottagaren har rätt
     att få veta om. Giovanni bestämmer över de texterna, inte koden.

Lägg inte till ett undantag för att få vakten grön: skriv om texten
("sms-tjänsten", "e-posttjänsten", "vår driftleverantör").
"""

import ast
import fnmatch
import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from apps.common.providers import NAMES, names_in

BASE = Path(settings.BASE_DIR)

# ---------------------------------------------------------------------------
# Undantag. Varje rad: vad, och varför kunden aldrig ser det eller varför det
# måste stå där.
# ---------------------------------------------------------------------------

#: Mallar: sökväg (glob, från templates/) -> {namn: skäl}. "*" = alla namn.
ALLOWED_TEMPLATES = {
    # 1. Byrån.
    "manage/**": {"*": "byråns panel /manage/"},
    "cloud/_customer_panel.html": {"*": "kundkortets AWS-sektion i /manage/"},
    "projects/drift.html": {"aws": "driftöversikten i /manage/"},
    "projects/customer_detail.html": {"sentry": "kundkortets övervakning i /manage/"},
    # Kundens EGET AWS-konto: fakturorna är Amazons original och utfärdaren
    # måste stå för bokföringen. Visas bara när "Kunden ser fakturorna" är
    # ikryssat på kontot. Det är kundens leverantör, inte byråns val som
    # röjs. Giovanni kan ändra ingressen till "molntjänsten".
    "portal/invoices.html": {
        "amazon": "kundens egna AWS-fakturor",
        "aws": "kundens egna AWS-fakturor",
    },
    # 3. Juridiskt: integritetssidan för mottagare (/utskick/<slug>/integritet/)
    # listar personuppgiftsbiträdena (GDPR art. 13-14). Art. 13.1 e tillåter
    # "kategorier av mottagare", så namnen är troligen inte tvingande, men
    # texten är Giovannis att ändra, inte kodens. test_s1_public läser den.
    "utskick/public/privacy.html": {
        "amazon": "biträdeslistan (juridisk text, Giovanni beslutar)",
        "46elks": "biträdeslistan (juridisk text, Giovanni beslutar)",
        "sentry": "biträdeslistan (juridisk text, Giovanni beslutar)",
    },
}

#: Python: sökväg (glob, från apps/) -> {räckvidd: skäl}. Räckvidden är
#: funktionen, klassen eller konstanten strängen står i ("SmsLog.provider_id",
#: "records"); en räckvidd täcker allt under sig. "*" = hela filen.
ALLOWED_PYTHON = {
    # 1. Byråns egna vyer, kommandon, larm och verktyg.
    "*/manage_*.py": {"*": "vyer i /manage/"},
    # Utom CHECKED_ANYWAY: kommandona som skriver det kunden ser.
    "*/management/**": {"*": "kommandon som byrån kör på servern"},
    "*/alerts.py": {"*": "larm till byrån, aldrig till kunden"},
    "manage/**": {"*": "byråns panel /manage/"},
    # Byråns AI-redaktör: MCP-servern släpper bara in byrån (asgi_app.py),
    # och stegens etiketter syns i assistenten i /manage/.
    "assistant/operations/**": {"*": "MCP-verktygen, bara för byrån"},
    "assistant/runtime.py": {"STEP_LABELS": "assistentens steg i /manage/"},
    # llm.py används också av Flamingos AI (scan.py, generator.py,
    # pagebuilder/ai.py), så bara modellens inställningar och felen.
    # _friendly blir ModelUnavailable, som bara byråns assistent visar:
    # Flamingo byter den mot fasta noter (flamingo/test_leverantorsnamn.py,
    # AiNoteTests).
    "assistant/llm.py": {
        "BEDROCK_PRICES": "modellernas id, visas aldrig",
        "DIRECT_PRICES": "modellernas id, visas aldrig",
        "assert_model_allowed": "fel vid start om inställningen är fel",
        "provider": "inställningens värde",
        "model_id": "modellens id",
        "cost_micros": "modellens id",
        "client": "klienten mot modellen",
        "is_configured": "inställningens namn",
        "_friendly": "felen till byråns assistent (se ovan)",
    },
    "common/sentry.py": {"*": "felrapporteringens inställningar"},
    "common/providers.py": {"*": "namnen som vakten och byråns varningar letar efter"},
    "monitor/checks.py": {"*": "hämtar felen till byråns övervakning"},
    "monitor/models.py": {"*": "fält och val i /manage/; portalen säger 'Fel åtgärdade'"},
    "monitor/runner.py": {"*": "dygnslarmet till byrån"},
    "monitor/status_endpoint.py": {"*": "statusadressen kräver byråns nyckel"},
    # Kundens EGET AWS-konto: kundkortet i /manage/ (card.py, manage_views.py,
    # sync.py) och klienten mot kontot.
    "cloud/aws.py": {"*": "klienten mot kundens eget AWS-konto, synken i /manage/"},
    "cloud/role_template.py": {"*": "läsrollen byrån lägger in i kundens konto (/manage/)"},
    "cloud/apps.py": {"CloudConfig.verbose_name": "appens namn i admin"},
    "cloud/models.py": {
        "invoice_pdf_path": "sökvägen i lagringen, visas aldrig",
        "AwsAccount.Meta": "modellnamn i admin",
        "AwsAccount.role_arn": "fält i /manage/",
        "AwsInvoice.Meta": "modellnamn i admin",
        # Bedömning för Giovanni, som portal/invoices.html ovan: kunden
        # laddar ner sina egna AWS-fakturor i portalen
        # (cloud/portal_views.invoice_pdf) och filen heter
        # AWS-<år>-<månad>-<id>.pdf. Det är kundens leverantör, men namnet
        # syns i kundens nedladdningar.
        "AwsInvoice.filename": "kundens egna AWS-fakturor (bedömning, se ovan)",
    },
    # Klienterna mot leverantörerna. Deras fel går till loggen, till larm och
    # till fält som kunden aldrig ser oöversatta: SmsMessage.error visas för
    # provider_error, provider_unknown och rate_limited som ERROR_TEXTS
    # (sms/api.py PROVIDER_CODES, sms/portal/dashboard.html), och
    # e-posttjänstens fel blir transport.error_text().
    "sms/elks.py": {"*": "sms-tjänstens klient"},
    "utskick/email/transport.py": {"*": "e-posttjänstens klient"},
    "utskick/inbound/queues.py": {"*": "läser e-posttjänstens köer"},
    "utskick/inbound/email.py": {"*": "läser e-posttjänstens rubriker (X-SES-*)"},
    "flamingo/sms.py": {"ELKS_URL": "sms-tjänstens API-adress"},
    "sms/service.py": {
        "apply_delivery_report": "svaret går bara till sms-tjänstens leveransrapport",
    },
    "sms/pricing.py": {
        "ReservationsPending": "månadsstängningen i /manage/sms/",
        "CloseResult.summary": "månadsstängningen i /manage/sms/",
    },
    "utskick/sending/email.py": {
        "UNKNOWN_ALERT": "larm till byrån",
        "NO_EVENTS_TEXT": "larm till byrån",
        "_email_off": "Switchboards not i /manage/utskick/",
    },
    "utskick/sending/health.py": {
        "adx_wide": "larmet om hela ADX till byrån",
        "read_ses_account": "klienten mot e-posttjänsten",
    },
    "utskick/email/domains.py": {
        "claim": "larm till byrån",
        "check": "larm till byrån",
        # 2. Tekniskt nödvändigt: posterna kunden lägger in hos sin DNS för
        # Egen domän. Värdena måste vara exakt de här; de syns ändå i DNS.
        "SPF_VALUE": "SPF-posten kunden måste lägga in",
        "records": "DKIM- och MX-posterna kunden måste lägga in",
        "check_record": "kontrollen av SPF-posten",
        "_region": "regionen i MX-posten kunden måste lägga in",
        "_client": "klienten mot e-posttjänsten",
    },
    "utskick/models.py": {
        "Switchboard.ses_max_rate": "fält i /manage/ (en ändring kräver migrering)",
        "Switchboard.ses_daily_quota": "fält i /manage/ (en ändring kräver migrering)",
        "EventReceipt.Meta": "modellnamn i admin",
    },
    "flamingo/models.py": {"SmsLog.provider_id": "fält i admin (en ändring kräver migrering)"},
    # 2. Adresser som bara leverantören anropar, och data som aldrig visas.
    "sms/api_urls.py": {"urlpatterns": "leveransrapporternas adress, bara sms-tjänsten anropar"},
    "utskick/webhook_urls.py": {"urlpatterns": "inkommande sms, bara sms-tjänsten anropar"},
    "utskick/links.py": {"SHARED_HOSTS": "delade värdar som nekas, visas aldrig"},
    "utskick/dbfk.py": {"apply": "kontroll av databasmotorn"},
    "core/errors.py": {"_PROBE_RE": "botsökningar som avvisas (/.aws)"},
    "flamingo/scan.py": {"_BAD_EMAIL": "adresser som skannern hoppar över"},
}

#: Filer som prövas fast en bred glob ovan täcker dem: bara en post med
#: exakt sökväg gäller för dem. Kommandona skriver det kunden ser.
CHECKED_ANYWAY = {
    "flamingo/management/commands/flamingo_demo.py": "demokontot, syns i Visa som kunden",
    "flamingo/management/commands/seed_flamingo.py": "de publika /flamingo/-sidorna",
    "sms/management/commands/sms_seed_demo.py": "demokontots sms i portalen",
    "offers/management/commands/seed_produkter.py": "produkterna i offerterna",
    "website/management/commands/seed_site.py": "den publika sajten",
    "website/management/commands/seed_sokordssidor.py": "den publika sajten",
    "website/management/commands/boost_conversion_content.py": "den publika sajten",
    "website/management/commands/import_site_data.py": "den publika sajten",
}

#: Seedfiler: filnamn -> {fras: skäl}. Bara frasen undantas, inte namnet i
#: resten av filen.
ALLOWED_SEED = {
    # Kundcaset Skandi VVS: firmans egen Claude-app, som når byråns
    # AI-redaktör. Det är kundens verktyg, inte byråns AI-leverantör.
    "adx_pages.json": {
        "uppdatera innehållet direkt från Claude.": "kundcase: kundens egen Claude-app",
        "innehållsredigering via Claude": "kundcase: kundens egen Claude-app",
    },
    "site_content.json": {
        "uppdatera innehållet direkt från Claude.": "kundcase: kundens egen Claude-app",
        "innehållsredigering via Claude": "kundcase: kundens egen Claude-app",
    },
    "adx_sokordssidor.json": {
        "når direkt från sin egen Claude-app.": "kundcase: kundens egen Claude-app",
    },
}

#: Guiderna på /aiz/ (apps/aidocs/content/): filnamn -> {namn: skäl}. Läsaren
#: är en AI som kopplar ett Django-projekt till övervakningen, med en kod
#: från Giovanni.
ALLOWED_AIDOCS = {
    "overvakning.md": {
        "sentry": "tekniskt nödvändigt: projektets egen felrapportering maskar nyckeln",
        "nginx": "tekniskt nödvändigt: en proxy framför projektet som tappar nyckeln",
    },
}


def _names_in(text, strict=True):
    """Mallar, python och seed-data: strict (ett ensamt "S3" räknas)."""
    return names_in(text, strict=strict)


def _allowance(table, key, name_or_scope, *, scoped=False):
    """Undantaget som täcker träffen, som (glob, post), eller None. key
    matchas som glob mot tabellens nycklar; en fil i CHECKED_ANYWAY bara
    mot sin exakta sökväg."""
    for pattern, entries in table.items():
        if key in CHECKED_ANYWAY and pattern != key:
            continue
        if not fnmatch.fnmatch(key, pattern):
            continue
        for entry in entries:
            if entry == "*" or entry == name_or_scope:
                return pattern, entry
            if scoped and name_or_scope.startswith(entry + "."):
                return pattern, entry
    return None


# ---------------------------------------------------------------- mallar

#: {% comment %} över flera rader, {# #} bara på en rad (Django renderar en
#: {# #} som går över en radbrytning).
_TEMPLATE_COMMENTS = re.compile(
    r"{%\s*comment\b.*?%}.*?{%\s*endcomment\s*%}|{#[^\n]*?#}", re.DOTALL
)


def template_hits(path, rel, used=None):
    """[(rad, namn, utdrag)] i en mall, kommentarerna borträknade. Undantagna
    träffar räknas inte men noteras i used."""
    text = path.read_text(encoding="utf-8")
    # Kommentaren byts mot lika många radbrytningar, så att radnumren stämmer.
    text = _TEMPLATE_COMMENTS.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name in _names_in(line):
            allowance = _allowance(ALLOWED_TEMPLATES, rel, name)
            if allowance is None:
                hits.append((lineno, name, line.strip()[:120]))
            elif used is not None:
                used.add(allowance)
    return hits


# ---------------------------------------------------------------- python

#: Anrop vars strängar aldrig når kunden: loggen och larmen till byrån.
_STAFF_CALLS = {"logger", "logging", "alerts"}


def _staff_call(node):
    """logger.warning(...), alerts.agency(...) och liknande."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in _STAFF_CALLS
    )


def python_hits(source, rel, used=None):
    """[(rad, namn, räckvidd, utdrag)] för strängarna i en python-fil.
    Undantagna träffar räknas inte men noteras i used."""
    tree = ast.parse(source)
    hits = []

    def visit(node, scope, staff):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = [*scope, node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node in top_level:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if names:
                scope = [*scope, names[0]]
        elif isinstance(node, ast.Call) and _staff_call(node):
            staff = True
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return  # en docstring eller en fristående sträng: ingen ser den
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and not staff:
            where = ".".join(scope)
            for name in _names_in(node.value):
                allowance = _allowance(ALLOWED_PYTHON, rel, where, scoped=True)
                if allowance is None:
                    hits.append((node.lineno, name, where, node.value.strip()[:120]))
                elif used is not None:
                    used.add(allowance)
        for child in ast.iter_child_nodes(node):
            visit(child, scope, staff)

    # Tilldelningar direkt i modulen eller i en klass ger räckvidden sitt namn
    # (konstanter och modellfält); inne i en funktion gäller funktionens.
    top_level = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef)):
            top_level.update(n for n in node.body if isinstance(n, (ast.Assign, ast.AnnAssign)))
    visit(tree, [], False)
    return hits


# ---------------------------------------------------------------- filerna


def _template_files():
    root = BASE / "templates"
    for path in sorted(root.rglob("*")):
        if path.suffix in (".html", ".txt"):
            yield path, str(path.relative_to(root))


def _python_files():
    root = BASE / "apps"
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(root))
        if "migrations" in path.parts or path.name.startswith("test") or path.name == "tests.py":
            continue
        yield path, rel


def seed_hits(used=None):
    """Träffarna i seed-data. En undantagen fras tas bort ur raden innan
    raden prövas, så att samma namn på annat ställe på raden räknas."""
    hits = []
    for path in sorted((BASE / "seed_data").glob("*.json")):
        phrases = ALLOWED_SEED.get(path.name, {})
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for phrase in phrases:
                if phrase in line:
                    line = line.replace(phrase, " ")
                    if used is not None:
                        used.add((path.name, phrase))
            for name in _names_in(line):
                hits.append(f"seed_data/{path.name}:{lineno}: {name}: {line.strip()[:120]}")
    return hits


def aidocs_hits(used=None):
    hits = []
    for path in sorted((BASE / "apps" / "aidocs" / "content").glob("*.md")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in _names_in(line):
                allowance = _allowance(ALLOWED_AIDOCS, path.name, name)
                if allowance is None:
                    hits.append(f"apps/aidocs/content/{path.name}:{lineno}: {name}: {line[:120]}")
                elif used is not None:
                    used.add(allowance)
    return hits


def _static_files():
    for path in sorted((BASE / "static").rglob("*")):
        if path.suffix in (".js", ".css") and "dist" not in path.parts:
            yield path
    yield from sorted((BASE / "src").rglob("*.js"))


class ProviderNameGuardTests(SimpleTestCase):
    """Leverantörernas namn syns inte för kunder, mottagare eller allmänheten."""

    def _fail_on(self, hits, what):
        self.assertEqual(
            hits,
            [],
            f"Leverantörsnamn i {what}. Skriv 'sms-tjänsten', 'e-posttjänsten', "
            "'vår driftleverantör' eller liknande, eller lägg ett undantag med skäl "
            "i apps/common/test_leverantorer.py om kunden verkligen aldrig ser det:\n"
            + "\n".join(hits),
        )

    def test_templates(self):
        hits = [
            f"templates/{rel}:{lineno}: {name}: {text}"
            for path, rel in _template_files()
            for lineno, name, text in template_hits(path, rel)
        ]
        self._fail_on(hits, "mallar")

    def test_python_strings(self):
        hits = [
            f"apps/{rel}:{lineno} ({scope or 'modulen'}): {name}: {text!r}"
            for path, rel in _python_files()
            for lineno, name, scope, text in python_hits(path.read_text(encoding="utf-8"), rel)
        ]
        self._fail_on(hits, "python-strängar")

    def test_static_files(self):
        hits = []
        for path in _static_files():
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for name in _names_in(line, strict=False):
                    hits.append(f"{path.relative_to(BASE)}:{lineno}: {name}: {line.strip()[:120]}")
        self._fail_on(hits, "static-filer (de går att läsa för alla)")

    def test_seed_data(self):
        self._fail_on(seed_hits(), "seed-data (den publika sajten)")

    def test_aidocs(self):
        self._fail_on(aidocs_hits(), "guiderna på /aiz/")

    def test_every_allowance_is_still_used(self):
        """Ett undantag som inte längre täcker någon träff tas bort, så att
        listan inte blir en blankofullmakt för nästa text."""
        used = set()
        for path, rel in _template_files():
            template_hits(path, rel, used)
        for path, rel in _python_files():
            python_hits(path.read_text(encoding="utf-8"), rel, used)
        seed_hits(used)
        aidocs_hits(used)
        unused = [
            f"{prefix}{pattern}: {entry}"
            for prefix, table in (
                ("templates/", ALLOWED_TEMPLATES),
                ("apps/", ALLOWED_PYTHON),
                ("seed_data/", ALLOWED_SEED),
                ("apps/aidocs/content/", ALLOWED_AIDOCS),
            )
            for pattern, entries in table.items()
            for entry in entries
            if (pattern, entry) not in used
        ]
        self.assertEqual(unused, [], "Undantag som inte längre behövs:\n" + "\n".join(unused))


class ProviderNameGuardSelfTests(SimpleTestCase):
    """Vakten vaktar sig själv: texterna Giovanni hittade 2026-10-10 ska ge
    utslag, och vanlig svenska och kod ska inte göra det."""

    QUOTED = (
        "ADX behöver slå på sms-tjänsten (46elks) först.",
        "Paus vid 4 % · AWS gräns 5 %",
        "Paus vid 0,08 % · AWS gräns 0,1 %",
        "Studsar och klagomål tas om hand direkt, och ett utskick pausas innan "
        "gränserna hos AWS nås.",
        "Vi pausar vid 4 %, före AWS gräns på 5 %.",
        "Klagomål · SES",
        "46elks svarade 429: Too many requests",
        "Sms via 46elks",
        "v=spf1 include:amazonses.com ~all",
        "Felrapporter går till Sentry.",
        "Servern körs på EC2 med PostgreSQL, DNS i Route 53.",
        "Certifikatet är utfärdat av Let's Encrypt.",
        "Texten skrivs av Claude via Bedrock (Anthropic).",
        "Bilderna ligger i en S3-bucket.",
        # Granskningen 2026-10-10: det vakten missade först.
        "Klienten använder SESv2.",
        "boto3.client('sesv2')",
        "https://sqs.eu-north-1.example/kö",
        "Bilderna går via CloudFront.",
        "Lightsail-instansen",
        "psycopg.OperationalError",
        "Servern står i eu-west-1.",
        "Server: nginx/1.24.0 (Ubuntu)",
        "Lösenorden ligger i 1Password.",
    )
    #: Bara strict (mallar, python, seed-data och byråns texter).
    STRICT = ("Bilderna lagras i S3.", "Databasen ligger i RDS.")
    HARMLESS = (
        "ADX behöver slå på sms-tjänsten först.",
        "Paus vid 4 % · gränsen 5 %",
        "Vi ses snart, och det ses över.",
        "Spärrad hos e-posttjänsten",
        "{% url 'flamingo:app_utskick' %}?visa=hoppades-over-ses_suppressed",
        "row.elks_id",
        "Gratis e-post som Gmail eller Outlook går inte.",
        "Resan längs Amazonas.",
        "Region Stockholm",
    )

    def test_the_quoted_texts_are_caught(self):
        for text in self.QUOTED:
            with self.subTest(text=text):
                self.assertTrue(names_in(text), f"vakten missade: {text}")

    def test_short_names_are_caught_in_code_but_not_in_plain_text(self):
        for text in self.STRICT:
            with self.subTest(text=text):
                self.assertTrue(names_in(text, strict=True), f"vakten missade: {text}")
                self.assertEqual(names_in(text), [])
        # Byggstegen i css-kommentarerna heter S3 (static prövas utan strict).
        self.assertEqual(names_in("S3 (redigerar-byggaren): flikarna Sms och E-post"), [])
        self.assertEqual(names_in("Service för Audi S3"), [])

    def test_swedish_and_code_pass(self):
        for text in self.HARMLESS:
            with self.subTest(text=text):
                self.assertEqual(names_in(text, strict=True), [])

    def test_every_name_has_a_pattern_once(self):
        names = [name for name, _pattern in NAMES]
        self.assertEqual(len(names), len(set(names)))

    def test_the_checked_anyway_files_exist(self):
        for rel in CHECKED_ANYWAY:
            with self.subTest(rel=rel):
                self.assertTrue((BASE / "apps" / rel).exists())

    def test_a_template_with_the_old_texts_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.html"
            path.write_text(
                "{% comment %}46elks i en kommentar renderas inte{% endcomment %}\n"
                "{# inte heller AWS här #}\n"
                '<span class="fl-kpi__note">Paus vid 4 % · AWS gräns 5 %</span>\n'
                "<!-- 46elks i en HTML-kommentar når webbläsaren -->\n"
                "{# en kommentar över två rader\nrenderas, med SES #}\n",
                encoding="utf-8",
            )
            hits = template_hits(path, "flamingo/app/utskick/health.html")
        self.assertEqual(
            [(line, name) for line, name, _ in hits], [(3, "aws"), (4, "46elks"), (6, "ses")]
        )

    def test_python_strings_are_judged_by_where_they_stand(self):
        source = (
            '"""Modulen pratar med 46elks (docstring, syns aldrig)."""\n'
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            'NOTE = "ADX behöver slå på sms-tjänsten (46elks) först."\n'
            "def send(exc):\n"
            '    logger.warning("46elks svarade %s", exc)\n'
            '    alerts.agency("SES har pausat e-posten", ["AWS svarade"])\n'
            '    return f"46elks: HTTP {exc.code}"\n'
            "class SmsLog:\n"
            '    provider_id = CharField("Id hos 46elks")\n'
        )
        hits = python_hits(source, "flamingo/prov.py")
        self.assertEqual(
            [(line, scope) for line, _name, scope, _text in hits],
            [(4, "NOTE"), (8, "send"), (10, "SmsLog.provider_id")],
        )
        # Samma fält i en fil där det är undantaget.
        hits = python_hits(source, "flamingo/models.py")
        self.assertEqual(
            [(line, scope) for line, _name, scope, _text in hits], [(4, "NOTE"), (8, "send")]
        )
