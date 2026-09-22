"""
Vakter för smala skärmar.

Panelen och kundportalen byggdes i en bred webbläsare, och varje ny lista
eller menypost föll utanför telefonens skärm utan att någon märkte det
förrän Giovanni gjorde det (2026-09-22). Ett riktigt layouttest kräver en
webbläsare, som inte finns i testsviten. Det här är i stället de
strukturella villkor som gör layouten möjlig - de fångar precis de misstag
som faktiskt gjordes.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

TEMPLATES = Path(settings.BASE_DIR) / "templates"
CSS = Path(settings.BASE_DIR) / "static" / "css"


def _templates_with(needle):
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if needle in text:
            yield path, text


class TableCardGuardTests(TestCase):
    """
    Under 760 px blir varje tabellrad ett kort, och cellen får sin etikett
    ur <thead> (static/js/manage-tables.js). En tabell utan <thead> ger
    etikettlösa kort - alltså siffror utan att man vet vad de betyder.
    """

    def test_every_table_has_a_header_row_or_opts_out(self):
        missing = []
        for path, text in _templates_with('class="m-table'):
            for match in re.finditer(r'<table[^>]*class="([^"]*m-table[^"]*)"[^>]*>', text):
                classes = match.group(1)
                if "m-table--plain" in classes:
                    continue
                rest = text[match.end() : match.end() + 4000]
                table = rest.split("</table>")[0]
                if "<thead" not in table and "<th" not in table:
                    missing.append(f"{path.relative_to(TEMPLATES)}: {classes}")
        self.assertEqual(
            missing,
            [],
            "Tabeller utan rubrikrad blir etikettlösa kort på telefon. "
            "Lägg till <thead>, eller m-table--plain om tabellen redan är "
            "etikett + värde:\n" + "\n".join(missing),
        )

    def test_the_card_mode_is_actually_wired_up(self):
        skin = (CSS / "manage-skin.css").read_text(encoding="utf-8")
        self.assertIn(".m-table[data-cards]", skin)
        script = (Path(settings.BASE_DIR) / "static" / "js" / "manage-tables.js").read_text()
        self.assertIn("data-label", script)
        for base in ("manage/base.html", "portal/base.html"):
            self.assertIn("manage-tables.js", (TEMPLATES / base).read_text(encoding="utf-8"))


class NavWrapGuardTests(TestCase):
    """
    Panelens menyrad har nowrap för att tio länkar ska rymmas på surfplatta.
    Portalen ärvde den regeln, och när Fakturor lades till sköts Logg och
    Fakturor utanför skärmen i telefonbredd. Portalens meny måste radbryta.
    """

    def test_portal_has_a_hamburger_menu_on_narrow_screens(self):
        """
        Portalens länkrad göms i telefonbredd och ersätts av en hamburgare som
        öppnar en helskärmsmeny. Utan den regeln hamnar länkarna utanför
        skärmen igen så fort en ny läggs till.
        """
        css = (CSS / "tavla.css").read_text(encoding="utf-8")
        blocks = re.findall(r"@media[^{]*max-width\s*:\s*(\d+)px[^{]*\{(.*?)\n\}", css, re.S)
        narrow = "".join(body for width, body in blocks if int(width) <= 780).replace(" ", "")
        skin = (CSS / "manage-skin.css").read_text(encoding="utf-8").replace(" ", "")
        self.assertIn(".mob-burger{display:flex}", skin)
        self.assertIn(".pt-nav.m-nav__right{display:none}", narrow)
        base = (TEMPLATES / "portal/base.html").read_text(encoding="utf-8")
        self.assertIn('id="mob-menu"', base)
        self.assertIn("data-menu-open", base)
        # Varje länk i raden ska också finnas i mobilmenyn.
        row = base.split('class="m-nav__links"')[1].split("</ul>")[0]
        menu = base.split('id="mob-menu"')[1]
        for name in re.findall(r"url 'portal:(\w+)'", row):
            if name in ("landing", "issue_create"):
                continue
            self.assertIn(f"url 'portal:{name}'", menu, name)

    def test_portal_nav_link_count_is_still_within_two_rows(self):
        """Fler än sju länkar ryms inte i datorbredd heller."""
        base = (TEMPLATES / "portal/base.html").read_text(encoding="utf-8")
        nav = base.split('class="m-nav__links"')[1].split("</ul>")[0]
        links = re.findall(r"m-nav__link", nav)
        self.assertLessEqual(
            len(links),
            7,
            "Portalens meny har blivit för lång för en telefon. Gruppera "
            "något under en undermeny i stället för att lägga till en länk.",
        )


class ViewportGuardTests(TestCase):
    """Utan viewport-taggen zoomar telefonen ut hela sidan i stället."""

    def test_every_base_template_declares_the_viewport(self):
        for base in (
            "manage/base.html",
            "portal/base.html",
            "website/base.html",
            "errors/_standalone.html",
        ):
            text = (TEMPLATES / base).read_text(encoding="utf-8")
            self.assertIn("width=device-width", text, base)


class PanelMenuGuardTests(TestCase):
    """Panelen har samma helskärmsmeny: varje sida i menyraden ska gå att nå från den."""

    def test_every_panel_link_is_in_the_mobile_menu(self):
        base = (TEMPLATES / "manage/base.html").read_text(encoding="utf-8")
        row = base.split('class="m-nav__links"')[1].split('id="mob-menu"')[0]
        menu = base.split('id="mob-menu"')[1]
        self.assertIn("data-menu-open", row)
        for name in set(re.findall(r"url '(manage:\w+)'", row)):
            self.assertIn(f"url '{name}'", menu, name)
        self.assertIn("menu.js", base)
