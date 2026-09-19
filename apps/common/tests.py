"""
Vakttest: inga AI-typografiska tecken någonstans (beslut 2026-08-29).

Teckenmängden ägs av apps/common/security.py (_NORMALIZE_MAP) - samma karta
som redigeringsvägens sanerare kör vid spara. Vakten täcker allt som INTE
passerar sanerarna:

  * mallar (templates/)          - renderas rakt ut
  * seedfiler (seed_data/)       - skrivs rått till databasen av seed_site
  * static + src (css/js)        - kommentarer OCH renderade strängar
  * python-källkod (apps/)       - labels, help-texter, placeholders, __str__

samt att normalize_typography-kommandot faktiskt städar databasrader som
smugit förbi (pg_dump-kloner, loaddata). Undantag och varför:

  * security.py        - definierar själva teckenkartan
  * */migrations/*     - historik; ändras aldrig retroaktivt
  * *tests*            - testdata FÅR innehålla tecknen
  * static/js/dist/    - byggartefakt med vendor-kod (tiptap)
"""

import re
from pathlib import Path

from django.conf import settings as django_settings
from django.core.management import call_command
from django.test import TestCase

from apps.common.security import AI_TYPOGRAPHY_CHARS, normalize_typography

BASE = Path(django_settings.BASE_DIR)

# Tecknen kan smyga in förklädda. Två förklädnader hittades 2026-09-19, båda
# i kod som passerat vakten i veckor:
#   * \u-escapes  - "\u2014" i en python-sträng eller JSON renderas som tecknet
#   * HTML-entiteter - &mdash; i en mall renderas som tecknet
# Mönstren HÄRLEDS ur teckenkartan, så ett nytt tecken i security.py täcks
# automatiskt i alla tre former.
_CODEPOINTS = sorted({ord(ch) for ch in AI_TYPOGRAPHY_CHARS})
_ESCAPED = re.compile(r"\\u(" + "|".join(f"{cp:04x}" for cp in _CODEPOINTS) + r")", re.IGNORECASE)
_NAMED_ENTITIES = {
    0x2013: "ndash",
    0x2014: "mdash",
    0x2018: "lsquo",
    0x2019: "rsquo",
    0x201A: "sbquo",
    0x201C: "ldquo",
    0x201D: "rdquo",
    0x201E: "bdquo",
    0x2026: "hellip",
}
_ENTITY = re.compile(
    "&(?:"
    + "|".join(
        [name for cp, name in _NAMED_ENTITIES.items() if cp in _CODEPOINTS]
        + [f"#0*{cp}" for cp in _CODEPOINTS]
        + [f"#x0*{cp:x}" for cp in _CODEPOINTS]
    )
    + ");",
    re.IGNORECASE,
)


def _offenses_in(path: Path) -> list[str]:
    """Alla förekomster i en fil som 'rad: tecken', för ett läsbart testfel."""
    offenses = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        where = f"{path.relative_to(BASE) if path.is_relative_to(BASE) else path}:{lineno}"
        for ch in AI_TYPOGRAPHY_CHARS:
            if ch in line:
                offenses.append(f"{where}: {ch!r}")
        escaped = _ESCAPED.search(line)
        if escaped:
            offenses.append(f"{where}: escapat AI-tecken ({escaped.group(0)})")
        entity = _ENTITY.search(line)
        if entity:
            offenses.append(f"{where}: AI-tecken som HTML-entitet ({entity.group(0)})")
    return offenses


class AiTypographyFileGuardTests(TestCase):
    """Tecknen får inte finnas i filer som renderas eller seedas."""

    def test_the_guard_sees_through_escapes_and_entities(self):
        """Vakten vaktar sig själv: varje förklädnad ska ge utslag."""
        import tempfile

        disguises = {
            "python-escape": 'label = "360' + "\\" + 'u2013430 px"',
            "namngiven entitet": "<p>Egen insamling &" + "mdash; ingen data</p>",
            "decimal entitet": "<p>citat &" + "#8221;</p>",
            "hex-entitet": "<p>mer &" + "#x2026;</p>",
        }
        for name, text in disguises.items():
            with self.subTest(disguise=name), tempfile.TemporaryDirectory() as tmp:
                f = Path(tmp) / "prov.html"
                f.write_text(text)
                self.assertTrue(_offenses_in(f), f"vakten missade: {name}")

    def test_harmless_entities_and_escapes_pass(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "ok.html"
            f.write_text(
                '<p>&rarr; &middot; &times; &quot;rakt&quot; &#10495; och "' + "\\" + 'u00e5"</p>'
            )
            self.assertEqual(_offenses_in(f), [])

    def _assert_clean(self, files):
        offenses = []
        for f in sorted(files):
            offenses.extend(_offenses_in(f))
        self.assertEqual(
            offenses,
            [],
            "AI-typografiska tecken hittade (ersätt med -, raka citattecken "
            "eller ...):\n" + "\n".join(offenses),
        )

    def test_templates_are_clean(self):
        tpl = BASE / "templates"
        self._assert_clean(list(tpl.rglob("*.html")) + list(tpl.rglob("*.txt")))

    def test_seed_data_is_clean(self):
        self._assert_clean((BASE / "seed_data").glob("*.json"))

    def test_static_and_src_are_clean(self):
        files = [
            f
            for f in (BASE / "static").rglob("*")
            if f.suffix in (".css", ".js") and "dist" not in f.parts
        ] + list((BASE / "src").rglob("*.js"))
        self._assert_clean(files)

    def test_python_sources_are_clean(self):
        files = [
            f
            for f in (BASE / "apps").rglob("*.py")
            if f.name != "security.py"
            and "migrations" not in f.relative_to(BASE).parts
            and "tests" not in f.name
        ]
        self._assert_clean(files)


class NormalizeTypographyCommandTests(TestCase):
    """Kommandot städar rader som kommit in förbi sanerarna (dump/loaddata)."""

    def test_normalizes_text_and_json_fields(self):
        from apps.website.models import Block, BlockPage, BlockType

        page = BlockPage.objects.create(
            slug="vakttest",
            title="Rubrik — med tankstreck",
            meta_description="Citat: ”fint” och en ellips …",
        )
        block = Block.objects.create(
            page=page,
            block_type=BlockType.choices[0][0],
            data={"heading": "A – B", "items": [{"text": "punkt ’ett’"}]},
        )

        call_command("normalize_typography", verbosity=0)

        page.refresh_from_db()
        block.refresh_from_db()
        for value in (
            page.title,
            page.meta_description,
            block.data["heading"],
            block.data["items"][0]["text"],
        ):
            for ch in AI_TYPOGRAPHY_CHARS:
                self.assertNotIn(ch, value)
        self.assertEqual(page.title, "Rubrik - med tankstreck")
        self.assertEqual(block.data["heading"], "A - B")

    def test_map_covers_expected_characters(self):
        self.assertEqual(normalize_typography("— – “ ” ‘ ’ …"), "- - \" \" ' ' ...")


class MultilineFieldTests(TestCase):
    """
    `plain` och `text` är två olika fälttyper och ska bete sig olika.

    Båda gick genom sanitize_plain_text, som kollapsar radbrytningar till
    mellanslag. Alltså plattades varje flerradigt blockfält tyst - trots att
    schemats hjälptext lovar "Radbrytningar behålls" och hero-mallen renderar
    dem med linebreaksbr. Designens tvåradiga rubriker gick inte att sätta
    vare sig via AI-verktygen eller via en seed.
    """

    def test_a_text_field_keeps_its_line_breaks(self):
        from apps.manage.block_schema import clean_block_values

        data = clean_block_values("hero", {}, {"title": "Rad ett\nRad två"})
        self.assertEqual(data["title"], "Rad ett\nRad två")

    def test_a_plain_field_still_collapses_them(self):
        """Enradiga fält ska förbli enradiga - rubriker och etiketter."""
        from apps.manage.block_schema import clean_block_values

        data = clean_block_values("hero", {}, {"kicker": "Rad ett\nRad två"})
        self.assertEqual(data["kicker"], "Rad ett Rad två")

    def test_rows_in_lists_keep_line_breaks_too(self):
        from apps.manage.block_schema import clean_block_rows

        data = clean_block_rows("steps", {}, {"steps": [{"title": "Steg", "text": "Först\nSedan"}]})
        self.assertEqual(data["steps"][0]["text"], "Först\nSedan")

    def test_blank_line_runs_are_capped(self):
        from apps.common.security import sanitize_multiline_text

        self.assertEqual(sanitize_multiline_text("A\n\n\n\n\nB"), "A\n\nB")

    def test_markup_is_still_stripped(self):
        """Radbrytningar bevaras - taggar gör det inte."""
        from apps.common.security import sanitize_multiline_text

        cleaned = sanitize_multiline_text("<script>alert(1)</script>Rad\n<b>Två</b>")
        self.assertNotIn("<", cleaned)
        self.assertIn("\n", cleaned)

    def test_ai_typography_is_normalised_here_too(self):
        from apps.common.security import sanitize_multiline_text

        self.assertEqual(sanitize_multiline_text("A — B\nC"), "A - B\nC")
