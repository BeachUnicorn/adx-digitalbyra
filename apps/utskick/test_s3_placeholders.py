"""
Tomma fält i Brevs redigerare (templates/utskick/brev/, brev_tags
pb_empty_attrs, static/css/flamingo-app-brev.css; Giovannis beställning
2026-10-10): ett tomt "Rubrik och bild" visade "ÖverrubrikRubrikIngress" på
en rad. Platshållarna ska stå på egna rader i samma layout som ifyllda fält.

    EmptyFieldTests     alla 22 block: varje tomt fält ritas i samma element
                        (tagg och inline-stil) som när det är ifyllt, med
                        fältets etikett som platshållare; Rubrik och bild
                        har överrubriken, rubriken och ingressen i var sitt
                        block-element; förhandsvisningen och utskicket ritar
                        inga tomma element
    CanvasCssTests      bara inline-element blir inline-block när de är
                        tomma; tomma fält i block som inte är valda förblir
                        dolda (med !important: rabattkodens inline-stil har
                        display); ett tomt textfält som skrivs i panelen
                        visar sin etikett
    PageBuilderTests    sidbyggarens pb_empty och stilmall är som förut
"""

from html.parser import HTMLParser
from pathlib import Path

from django.conf import settings
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase

from apps.flamingo.templatetags import flamingo_pb

from .email import registry, render
from .test_s3_render import BrevFixture, blk

BASE = Path(settings.BASE_DIR)
BREV_CSS = BASE / "static" / "css" / "flamingo-app-brev.css"
PB_CSS = BASE / "static" / "css" / "flamingo-pb.css"
#: Fält som är listor: deras tomma platshållare är en ruta (data-pb-list).
LISTS = {"items"}
BLOCK_TAGS = {"p", "h1", "h2", "h3", "div"}


class _Fields(HTMLParser):
    """Elementen med data-pb-field: [(tagg, attribut)] i dokumentets ordning."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._open = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if "data-pb-field" in data:
            self.rows.append({"tag": tag, "attrs": data, "text": ""})
            self._open.append(self.rows[-1])
        else:
            self._open.append(None)

    def handle_endtag(self, tag):
        if self._open:
            self._open.pop()

    def handle_data(self, data):
        for row in self._open:
            if row is not None:
                row["text"] += data


def fields_in(html):
    parser = _Fields()
    parser.feed(html)
    return parser.rows


def sample(field):
    if field.kind == registry.CODE:
        return "PROV10"
    return "Prov"


class EmptyFieldTests(BrevFixture, TestCase):
    def empty_fields(self, html):
        return [
            row
            for row in fields_in(html)
            if "data-pb-empty" in row["attrs"] and row["attrs"]["data-pb-field"] not in LISTS
        ]

    def test_every_empty_field_is_drawn_like_the_filled_field(self):
        checked = 0
        for key in registry.BLOCK_KEYS:
            block_type = registry.EMAIL_TYPES[key]
            labels = {f.key: f.label for f in block_type.fields}
            kinds = {f.key: f for f in block_type.fields}
            html = render.render_block(self.utskick, blk(key))
            for row in self.empty_fields(html):
                name = row["attrs"]["data-pb-field"]
                with self.subTest(block=key, field=name):
                    self.assertEqual(row["attrs"]["data-pb-placeholder"], labels[name])
                    self.assertEqual(row["text"], "", "ett tomt fält är helt tomt (:empty)")
                    filled_html = render.render_block(
                        self.utskick, blk(key, **{name: sample(kinds[name])})
                    )
                    filled = [
                        r
                        for r in fields_in(filled_html)
                        if r["attrs"].get("data-pb-field") == name
                        and "data-pb-empty" not in r["attrs"]
                    ]
                    self.assertEqual(len(filled), 1, filled_html)
                    self.assertEqual(row["tag"], filled[0]["tag"])
                    self.assertEqual(row["attrs"].get("style"), filled[0]["attrs"].get("style"))
                    self.assertEqual(row["attrs"].get("class"), filled[0]["attrs"].get("class"))
                    if row["tag"] != "span":
                        self.assertIn(row["tag"], BLOCK_TAGS)
                    checked += 1
        # Rubrik och bild (3), Rubrik, Text, Bild, Bild och text (2), Erbjudande
        # (3), Prislista (2), Steg, Händelse (2), Person (2), Video, Vanliga
        # frågor, Ruta, Underskrift (3).
        self.assertEqual(checked, 24)

    def test_the_hero_placeholders_are_three_lines(self):
        html = render.render_block(self.utskick, blk("hero"))
        rows = self.empty_fields(html)
        self.assertEqual(
            [(r["tag"], r["attrs"]["data-pb-placeholder"]) for r in rows],
            [("p", "Överrubrik"), ("h1", "Rubrik"), ("p", "Ingress")],
        )
        S = render.context_for(self.utskick, mode=render.EDITOR).S
        styles = [r["attrs"]["style"] for r in rows]
        self.assertTrue(styles[0].startswith(S["kick"]))
        self.assertTrue(styles[1].startswith(S["h1"]))
        self.assertEqual(styles[2], S["lead"])
        self.assertEqual(rows[1]["attrs"]["class"], "br-h1")

    def test_a_filled_field_keeps_its_attributes(self):
        html = render.render_block(self.utskick, blk("hero", kicker="Höstservice", title="Dags"))
        rows = {r["attrs"]["data-pb-field"]: r for r in fields_in(html)}
        self.assertNotIn("data-pb-empty", rows["kicker"]["attrs"])
        self.assertNotIn("data-pb-placeholder", rows["kicker"]["attrs"])
        self.assertEqual(rows["kicker"]["text"], "Höstservice")
        self.assertIn("data-pb-empty", rows["lead"]["attrs"])

    def test_an_empty_code_is_hidden_although_its_style_has_display(self):
        """Rabattkodens tomma span har kodens inline-stil, med
        display:inline-block, som vinner över en vanlig regel: båda reglarna
        som döljer tomma fält (före och efter redigerarens skript) har
        !important."""
        S = render.context_for(self.utskick, mode=render.EDITOR).S
        self.assertIn("display:inline-block", S["code"])
        html = render.render_block(self.utskick, blk("offer"))
        (code,) = [r for r in fields_in(html) if r["attrs"]["data-pb-field"] == "code"]
        self.assertIn("data-pb-empty", code["attrs"])
        self.assertEqual(code["attrs"]["style"], S["code"])
        page = render.render_html(
            self.utskick, render.context_for(self.utskick, mode=render.EDITOR)
        )
        self.assertIn("body[data-brev-editing] [data-pb-empty]{display:none !important}", page)

    def test_preview_and_the_mail_draw_no_empty_elements(self):
        from .email import blocks

        S = render.context_for(self.utskick, mode=render.PREVIEW).S
        doc = [blk("hero", title="Dags för service"), blk("offer", title="Rabatt")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        html = render.render_html(
            self.utskick, render.context_for(self.utskick, mode=render.PREVIEW)
        )
        self.assertIn("Dags för service", html)
        self.assertNotIn("data-pb", html)
        self.assertNotIn(f'<p style="{S["kick"]}', html)
        self.assertNotIn(f'<p style="{S["lead"]}"></p>', html)
        self.assertNotIn(f'<span style="{S["code"]}"></span>', html)


class CanvasCssTests(SimpleTestCase):
    def test_only_inline_elements_become_inline_block(self):
        css = BREV_CSS.read_text("utf-8")
        self.assertNotIn(
            "[data-brev-canvas] [data-pb-field][contenteditable]:empty{display:inline-block", css
        )
        self.assertIn(
            "[data-brev-canvas] :where(span,a,strong,b,em)[data-pb-field][contenteditable]:empty"
            "{display:inline-block;min-width:4ch}",
            css,
        )

    def test_blank_fields_outside_the_selected_block_stay_hidden(self):
        css = BREV_CSS.read_text("utf-8")
        hidden = (
            "[data-brev-canvas] [data-pb-block]:not(.pb-is-sel) [data-pb-blank]"
            "{display:none !important}"
        )
        self.assertIn(hidden, css)
        # Samma specificitet som regeln för inline-element (:where räknas inte)
        # och senare i filen: den vinner.
        self.assertGreater(css.index(hidden), css.index(":where(span,a,strong,b,em)"))

    def test_an_empty_panel_field_shows_its_label(self):
        css = BREV_CSS.read_text("utf-8")
        self.assertIn("[data-brev-canvas] [data-pb-panel-field]:empty::before", css)


class PageBuilderTests(SimpleTestCase):
    def test_pb_empty_is_unchanged(self):
        html = Template('{% load flamingo_pb %}{% pb_empty "lead" "p" "rn-lead" %}').render(
            Context({"editing": True, "b": {"fields": {}}})
        )
        self.assertEqual(
            html,
            '<p class="rn-lead" data-pb-field="lead" data-pb-empty data-pb-placeholder="lead"></p>',
        )
        self.assertFalse(hasattr(flamingo_pb, "pb_empty_attrs"))

    def test_the_page_builder_css_is_unchanged(self):
        css = PB_CSS.read_text("utf-8")
        self.assertIn(
            ".rn--editing [data-pb-field][contenteditable]:empty"
            "{display:inline-block;min-width:4ch}",
            css,
        )
