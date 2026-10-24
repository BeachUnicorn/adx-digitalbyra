"""
Kundkortets flikar (card_tabs.py, ?flik=).

Giovanni 2026-10-24: "när man är inne på /manage/kunder/{id}/ så får man
scrolla i en evighet för att hitta saker". Kortet är nu en flik i taget. Här
prövas att varje flik visar sin panel och inte de andras, att varje panels
formulär landar på panelens flik, att menyn märker fliken man står på och
att inget som leder till kortet pekar på ett ankare som inte finns där.
"""

import re
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.flamingo.models import FlamingoAccount
from apps.monitor.models import MonitoredDomain

from . import card_tabs
from .card_tabs import TABS, card_url
from .models import CustomerLogEntry
from .tests import PortalFixtureMixin

BASE = Path(settings.BASE_DIR)

#: Det som bara ska finnas på en flik: flik -> markörer i HTML:en.
PANELS = {
    card_tabs.OVERVIEW: ('<div class="tv-sec-title">Uppgifter</div>', ">Läget</div>"),
    card_tabs.LOG: ('id="logg"',),
    card_tabs.ISSUES: ('<div class="tv-sec-title">Ärenden</div>',),
    card_tabs.CONTACTS: ('<div class="tv-sec-title">Portalkontakter</div>',),
    card_tabs.OPERATIONS: ('id="overvakning"', 'id="aws"'),
    card_tabs.FLAMINGO: ('id="flamingo"', 'id="utskick"', 'id="flamingo-google"'),
    card_tabs.SMS: ('id="sms"',),
}

#: Tabellerna som bara de tunga flikarna får fråga.
HEAVY_TABLES = {
    card_tabs.OPERATIONS: (
        "cloud_awsaccount",
        "monitor_monitorsettings",
        "monitor_monitoreddomain",
    ),
    card_tabs.FLAMINGO: ("flamingo_flamingoaccount", "utskick_utskicksettings"),
    card_tabs.SMS: ("sms_smsaccount",),
}

#: Övre gräns för frågor per flik. Kortet gjorde 37 som en enda sida.
MAX_QUERIES = 26

#: Tecken som avslutar en adress i en mall eller en f-sträng.
_URL_TAIL = r"(?:\?flik=([\w-]*))?(?:#([\w-]+))?"


def _tab_html(client, customer, tab):
    response = client.get(card_url(customer.pk, tab))
    assert response.status_code == 200, (tab, response.status_code)
    return response.content.decode()


@override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@adx.example")
class CardTabTests(PortalFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Flamingo på, så att Google- och utskicksdelen ritas.
        FlamingoAccount.objects.create(customer=cls.acme, is_enabled=True)

    def test_each_tab_shows_its_panels_and_none_of_the_others(self):
        client = self.as_staff()
        for tab, _label in TABS:
            with self.subTest(tab=tab or "översikt"):
                html = _tab_html(client, self.acme, tab)
                for marker in PANELS[tab]:
                    self.assertIn(marker, html)
                for other, markers in PANELS.items():
                    if other == tab:
                        continue
                    for marker in markers:
                        self.assertNotIn(marker, html, f"{marker} hör till {other or 'översikt'}")
                # Huvudet står på alla flikar.
                self.assertIn("Öppna tavlan", html)
                self.assertIn("Visa portalen som kunden", html)

    def test_an_unknown_tab_is_the_overview(self):
        client = self.as_staff()
        html = client.get(reverse("manage:customer_detail", args=[self.acme.pk]) + "?flik=nej")
        self.assertEqual(html.status_code, 200)
        self.assertEqual(html.context["tab"], card_tabs.OVERVIEW)
        self.assertContains(html, PANELS[card_tabs.OVERVIEW][0])

    def test_the_menu_marks_the_current_tab(self):
        client = self.as_staff()
        for tab, label in TABS:
            with self.subTest(tab=tab or "översikt"):
                html = _tab_html(client, self.acme, tab)
                nav = re.search(r'<nav class="m-subnav tv-cardtabs"[^>]*>(.*?)</nav>', html, re.S)
                self.assertIsNotNone(nav)
                links = re.findall(r'<a href="([^"]+)" class="m-subnav__item[^"]*"([^>]*)>', nav[1])
                self.assertEqual(
                    [href for href, _ in links],
                    [card_url(self.acme.pk, key) for key, _ in TABS],
                )
                current = [href for href, attrs in links if 'aria-current="page"' in attrs]
                self.assertEqual(current, [card_url(self.acme.pk, tab)])
                self.assertIn(f'is-active" aria-current="page">{label}', nav[1])

    def test_the_menu_counts_unsent_log_rows_and_open_issues(self):
        CustomerLogEntry.objects.create(customer=self.acme, text="A")
        html = _tab_html(self.as_staff(), self.acme, card_tabs.OVERVIEW)
        self.assertIn("1 osänd loggrad", html)
        self.assertIn("2 öppna ärenden", html)

    def test_each_tab_asks_only_for_what_it_shows(self):
        client = self.as_staff()
        for tab, _label in TABS:
            client.get(card_url(self.acme.pk, tab))  # sessionen och cacharna värms
            with self.subTest(tab=tab or "översikt"):
                with CaptureQueriesContext(connection) as queries:
                    _tab_html(client, self.acme, tab)
                self.assertLessEqual(len(queries.captured_queries), MAX_QUERIES)
                sql = " ".join(q["sql"] for q in queries.captured_queries)
                for other, tables in HEAVY_TABLES.items():
                    if other == tab:
                        continue
                    for table in tables:
                        self.assertNotIn(f'"{table}"', sql, f"{table} på {tab or 'översikt'}")

    def test_saving_the_details_lands_on_the_overview(self):
        response = self.as_staff().post(
            reverse("manage:customer_detail", args=[self.acme.pk]),
            {"name": "Acme AB", "is_active": "on"},
        )
        self.assertEqual(response["Location"], card_url(self.acme.pk))

    def test_every_panel_form_lands_on_its_tab(self):
        entry = CustomerLogEntry.objects.create(customer=self.acme, text="Ta bort mig")
        domain = MonitoredDomain.objects.create(customer=self.acme, name="acme.se")
        pk = self.acme.pk
        cases = [
            ("customer_log_add", [pk], {"text": "Ny rad"}, card_tabs.LOG, ""),
            ("customer_log_delete", [entry.pk], {}, card_tabs.LOG, ""),
            ("customer_log_send", [pk], {"entries": ""}, card_tabs.LOG, ""),
            ("customer_invite", [pk], {"email": "inte en adress"}, card_tabs.CONTACTS, ""),
            ("customer_remove_contact", [pk, self.contact.pk], {}, card_tabs.CONTACTS, ""),
            ("monitor_update", [pk], {"note": "Bra"}, card_tabs.OPERATIONS, ""),
            ("monitor_domain_add", [pk], {"name": "inte-en-doman"}, card_tabs.OPERATIONS, ""),
            ("monitor_domain_update", [domain.pk], {"action": "toggle"}, card_tabs.OPERATIONS, ""),
            ("aws_account_add", [pk], {"account_id": "123"}, card_tabs.OPERATIONS, "aws"),
            ("sms_customer_update", [pk], {}, card_tabs.SMS, ""),
            ("flamingo_customer_update", [pk], {"is_enabled": "on"}, card_tabs.FLAMINGO, ""),
            (
                "flamingo_google_update",
                [pk],
                {"google_status": "none", "google_ads_customer_id": ""},
                card_tabs.FLAMINGO,
                "",
            ),
            ("utskick_customer_update", [pk], {}, card_tabs.FLAMINGO, "utskick"),
        ]
        client = self.as_staff()
        for name, args, data, tab, anchor in cases:
            with self.subTest(view=name):
                response = client.post(reverse(f"manage:{name}", args=args), data)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], card_url(self.acme.pk, tab, anchor))

    def test_no_link_to_the_card_points_at_an_anchor_that_is_not_there(self):
        """Allt som leder till kortet: mallarnas {% url %} med ?flik= och #,
        och Pythons card_url/card_redirect. Varje flik ska finnas och varje
        ankare ska finnas på sin flik. Ett ankare utan flik vore översikten,
        och där finns inga."""
        used = set()
        url_tag = re.compile(r"\{% url 'manage:customer_detail' [^%]+%\}" + _URL_TAIL)
        for path in (BASE / "templates").rglob("*.html"):
            for flik, anchor in url_tag.findall(path.read_text("utf-8")):
                self.assertIn(flik, card_tabs.TAB_KEYS, f"{path}: ?flik={flik}")
                used.add((flik, anchor, str(path.relative_to(BASE))))

        constants = {name: getattr(card_tabs, name) for name in dir(card_tabs) if name.isupper()}
        call = re.compile(
            r"card_(?:url|redirect)\(\s*[^,()]+,\s*(?:card_tabs\.)?([A-Z_]+)\b(?:,\s*\"([\w-]*)\")?"
        )
        for path in (BASE / "apps").rglob("*.py"):
            if path.name.startswith("test") or "migrations" in path.parts:
                continue
            source = path.read_text("utf-8")
            where = str(path.relative_to(BASE))
            # Adressen byggs bara med card_url: inget reverse(...) + "#..."
            # och ingen handskriven /manage/kunder/<pk>/#... kvar.
            self.assertNotRegex(source, r"customer_detail\"[^\n]*\)\s*\+", where)
            self.assertNotRegex(source, r"/manage/kunder/\{[^}]+\}/[#?]", where)
            for name, anchor in call.findall(source):
                if path.name == "card_tabs.py":
                    continue
                self.assertIn(name, constants, f"{where}: {name}")
                used.add((constants[name], anchor, where))

        self.assertTrue(any(anchor for _, anchor, _ in used), "vakten hittar inga ankare")
        client = self.as_staff()
        pages = {tab: _tab_html(client, self.acme, tab) for tab, _ in TABS}
        for flik, anchor, where in sorted(used):
            if anchor:
                with self.subTest(where=where, flik=flik, anchor=anchor):
                    self.assertTrue(
                        f'id="{anchor}"' in pages[flik],
                        f"{where}: #{anchor} finns inte på fliken {flik or 'översikt'}",
                    )
