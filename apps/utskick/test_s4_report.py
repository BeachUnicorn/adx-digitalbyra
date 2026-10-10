"""
Hela rapporten (README I.8, I.2, H.3, J S4; S4-HANDOFF.md rapport-byggaren).

    FunnelTests        trattens tal mot ett sått utskick, och varje tal är lika
                       långt som listan det leder till
    ChartTests         klick per timme från starten, bara mänskliga klick,
                       tomma timmar på slutet bort, SVG utan style= och
                       utan decimalkomma
    LinkTests          länktabellen och listorna bakom den, aldrig ett annat
                       utskicks länk
    LandingTests       tiden på sidan, mobil, ringde och formuläret
    PageTests          rapportens delar, varje siffra leder till en lista med
                       samma vy, utkast och schemalagda utan delarna
    EmailTests         mejlens rutor med länkar, kanalens chips och en tratt per
                       kanal när utskicket har båda
    RetentionTests     efter retentionen: tratten ur stats utan länkar, inget
                       diagram, ingen export och ingen uppföljning
    ExportTests        bekräftelsen först, bara POST ger filen, safe_cell, ingen
                       adress för borttagna kontakter, ExportLog, högst tio om
                       dagen, byrån loggad, främmande utskick 404
    FollowUpTests      segmentet "Klickade inte" med rätt regler och kontakter,
                       ett utkast med segmentet, samma utkast vid ett andra tryck,
                       främmande utskick 404, ingen att följa upp

Inget når nätet.
"""

import csv
import io
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import Campaign, Lead, Service

from . import reports, segments
from .app_views import report as report_views
from .models import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    Click,
    Contact,
    ExportLog,
    Recipient,
    Segment,
    TrackedLink,
    Utskick,
)
from .test_s2_ui import UiFixture

S = Recipient.Status
BASE = Path(settings.BASE_DIR)


class ReportFixture(UiFixture):
    """Höstservice värmepump, skickat för tre timmar sedan till fem:

    anna    levererad, klickade på Flamingo-sidan efter 20 min (mobil, 112 s),
            skickade formuläret via den
    bo      levererad, klickade på den externa länken efter 70 min och på
            Flamingo-sidan efter 75 min (dator, 20 s, ringde)
    cilla   levererad, bara ett klick från en skanner (räknas inte)
    dan     gick inte fram
    eva     hoppades över (veckotaket)
    """

    def setUp(self):
        super().setUp()
        self.start = (timezone.now() - timedelta(hours=3)).replace(
            minute=5, second=0, microsecond=0
        )
        self.row = Utskick.objects.create(
            account=self.account,
            name="Höstservice värmepump",
            sms_body="Hej {förnamn|du}. Boka: {länk:boka}",
            status=Utskick.Status.SENT,
            started_at=self.start,
            finished_at=self.start + timedelta(minutes=10),
        )
        service = Service.objects.create(account=self.account, name="Värmepump")
        self.campaign = Campaign.objects.create(
            account=self.account, service=service, name="Värmepump", page={"title": "Värmepump"}
        )
        self.lp = TrackedLink.objects.create(
            account=self.account,
            utskick=self.row,
            kind=TrackedLink.Kind.LP,
            key="sida",
            campaign=self.campaign,
            destination="https://adx.example/lp/varmepump/",
            label="Boka service",
        )
        self.ext = TrackedLink.objects.create(
            account=self.account,
            utskick=self.row,
            kind=TrackedLink.Kind.EXTERNAL,
            key="boka",
            destination="https://www.exempelror.example/boka/",
        )
        people = {
            name: self.person(first_name=name.title())
            for name in ("anna", "bo", "cilla", "dan", "eva")
        }
        self.people = people
        sent = self.start + timedelta(minutes=1)

        def recipient(name, status, **extra):
            kontakt = people[name]
            return Recipient.objects.create(
                utskick=self.row,
                contact=kontakt,
                channel=CHANNEL_SMS,
                address=kontakt.phone,
                status=status,
                sent_at=sent if status != S.SKIPPED else None,
                delivered_at=sent if status == S.DELIVERED else None,
                **extra,
            )

        self.r = {
            "anna": recipient(
                "anna",
                S.DELIVERED,
                first_clicked_at=self.start + timedelta(minutes=20),
                click_count=1,
            ),
            "bo": recipient(
                "bo",
                S.DELIVERED,
                first_clicked_at=self.start + timedelta(minutes=70),
                click_count=2,
            ),
            "cilla": recipient("cilla", S.DELIVERED),
            "dan": recipient("dan", S.FAILED),
            "eva": recipient("eva", S.SKIPPED, skip_reason=Recipient.SkipReason.WEEKLY_CAP),
        }
        self.click("anna", self.lp, 20, engaged_seconds=112, lp_visits=1, device="mobile")
        self.click("bo", self.ext, 70)
        self.click(
            "bo", self.lp, 75, engaged_seconds=20, lp_visits=1, device="desktop", called=True
        )
        self.click("cilla", self.lp, 2, kind=Click.Kind.SCANNER, lp_visits=1, device="mobile")
        Lead.objects.create(
            account=self.account,
            utskick=self.row,
            utskick_recipient=self.r["anna"],
            source=Lead.SOURCE_FORM,
            name="Anna",
            attribution={"link": self.lp.pk, "late": False},
        )
        # Svarstrådens egen förfrågan räknas aldrig.
        Lead.objects.create(
            account=self.account,
            utskick=self.row,
            utskick_recipient=self.r["bo"],
            source=Lead.SOURCE_REPLY,
            attribution={"link": self.lp.pk},
        )
        self.now = self.start + timedelta(hours=3)

    def click(self, name, link, minutes, kind=Click.Kind.HUMAN, **extra):
        return Click.objects.create(
            account=self.account,
            utskick=self.row,
            recipient=self.r[name],
            contact=self.people[name],
            link=link,
            channel="sms",
            kind=kind,
            at=self.start + timedelta(minutes=minutes),
            **extra,
        )

    def report_url(self, utskick=None):
        return reverse("flamingo:app_utskick", args=[(utskick or self.row).pk])

    def list_count(self, visa, kanal=None, utskick=None):
        """Antalet på mottagarsidan för vyn, och att sidan visar just den vyn."""
        params = {"visa": visa}
        if kanal:
            params["kanal"] = kanal
        url = reverse("flamingo:app_utskick_recipients", args=[(utskick or self.row).pk])
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["view"], visa)
        return response.context["page"].paginator.count


class FunnelTests(ReportFixture, TestCase):
    def test_the_steps_against_the_seeded_utskick(self):
        steps = reports.funnel(self.row, channel=CHANNEL_SMS)
        self.assertEqual(
            [(s["label"], s["n"]) for s in steps],
            [
                ("Skickade", 4),
                ("Levererade", 3),
                ("Klickade", 2),
                ("Stannade 30 s+", 1),
                ("Förfrågan", 1),
            ],
        )
        self.assertEqual([s["pct"] for s in steps], [100.0, 75.0, 50.0, 25.0, 25.0])
        self.assertEqual([s["width"] for s in steps], [100, 75, 50, 25, 25])

    def test_every_step_is_as_long_as_its_list(self):
        for step in reports.funnel(self.row, channel=CHANNEL_SMS):
            with self.subTest(step=step["label"]):
                self.assertEqual(self.list_count(step["visa"], step["kanal"]), step["n"])
                self.assertEqual(step["kanal"], "sms")

    def test_nothing_sent_is_no_funnel(self):
        draft = Utskick.objects.create(account=self.account, name="Utkast")
        self.assertEqual(reports.funnel(draft), [])
        self.assertEqual(reports.full(draft)["funnels"], [])


class ChartTests(ReportFixture, TestCase):
    def test_human_clicks_per_hour_from_the_start(self):
        points = reports.clicks_per_hour(self.row, now=self.now)
        self.assertEqual([p["n"] for p in points], [1, 2, 0, 0])
        self.assertEqual(points[0]["start"], self.start.replace(minute=0))
        self.assertEqual(points[1]["start"] - points[0]["start"], timedelta(hours=1))

    def test_empty_hours_at_the_end_are_cut_but_twelve_stay(self):
        later = self.start + timedelta(hours=30)
        points = reports.clicks_per_hour(self.row, now=later)
        self.assertEqual(len(points), reports.CHART_MIN_HOURS)
        self.click("anna", self.lp, 60 * 20)
        self.assertEqual(len(reports.clicks_per_hour(self.row, now=later)), 21)
        self.assertEqual(len(reports.clicks_per_hour(self.row, now=later, hours=6)), 6)

    def test_no_clicks_or_no_start_is_no_chart(self):
        Click.objects.filter(kind=Click.Kind.HUMAN).delete()
        self.assertEqual(reports.clicks_per_hour(self.row, now=self.now), [])
        self.assertEqual(reports.chart([]), {})
        draft = Utskick.objects.create(account=self.account, name="Utkast")
        self.assertEqual(reports.clicks_per_hour(draft), [])

    def test_the_svg_has_dots_viewbox_and_no_style(self):
        chart = reports.chart(reports.clicks_per_hour(self.row, now=self.now))
        self.assertEqual(chart["total"], 3)
        self.assertEqual(chart["top"], 2)
        self.assertEqual([b["hi"] for b in chart["bars"]], [False, True, False, False])
        self.assertEqual(chart["bars"][1]["x"], "123")
        html = self.client.get(self.report_url()).content.decode()
        svg = re.search(r'<svg class="fl-ut-rp-chart__svg".*?</svg>', html, flags=re.S).group(0)
        self.assertIn('viewBox="0 0 480 120"', svg)
        self.assertNotIn("style=", svg)
        for value in re.findall(r'\s(?:x|y|width|height)="([^"]*)"', svg):
            self.assertRegex(value, r"^[0-9.]+$")
        self.assertIn("2 klick", html)


class LinkTests(ReportFixture, TestCase):
    def test_one_row_per_link_with_people_and_leads(self):
        rows = {row["link"].pk: row for row in reports.per_link(self.row)}
        lp, ext = rows[self.lp.pk], rows[self.ext.pk]
        self.assertEqual(
            (lp["label"], lp["destination"], lp["kind"]),
            ("Boka service", "/lp/varmepump", "Flamingo-sida"),
        )
        self.assertEqual((lp["clicks"], lp["leads"]), (2, 1))
        self.assertEqual(
            (ext["label"], ext["destination"]),
            ("exempelror.example/boka", "exempelror.example/boka"),
        )
        self.assertEqual((ext["clicks"], ext["leads"]), (1, 0))

    def test_every_number_is_as_long_as_its_list(self):
        for row in reports.per_link(self.row):
            with self.subTest(link=row["label"]):
                self.assertEqual(self.list_count(row["visa"]), row["clicks"])
                self.assertEqual(self.list_count(row["visa_leads"]), row["leads"])
        response = self.client.get(
            reverse("flamingo:app_utskick_recipients", args=[self.row.pk]),
            {"visa": f"lank-{self.lp.pk}"},
        )
        self.assertEqual(response.context["view_label"], "Klickade på Boka service")

    def test_another_utskicks_link_is_not_a_view(self):
        other = Utskick.objects.create(account=self.other_account, name="Hemligt")
        foreign = TrackedLink.objects.create(
            account=self.other_account,
            utskick=other,
            kind=TrackedLink.Kind.EXTERNAL,
            destination="https://annanfirma.example/",
        )
        self.assertIsNone(reports.recipients_for(self.row, f"lank-{foreign.pk}"))
        self.assertIsNone(reports.recipients_for(self.row, "lank-x1"))
        url = reverse("flamingo:app_utskick_recipients", args=[self.row.pk])
        response = self.client.get(url, {"visa": f"lank-{foreign.pk}"})
        self.assertEqual(response.context["view"], "alla")


class LandingTests(ReportFixture, TestCase):
    def test_time_mobile_calls_and_forms(self):
        lp = reports.lp_behaviour(self.row)
        self.assertEqual(lp["visits"], 2)
        self.assertEqual(lp["median_seconds"], 66)
        self.assertEqual(lp["mobile_pct"], 50.0)
        self.assertEqual((lp["visitors"], lp["called"], lp["form_leads"]), (2, 1, 1))
        self.assertEqual(lp["line"], "På landningssidan: mediantid 1 min 6 s · 50 % i mobil")
        self.assertEqual(self.list_count("besokte"), 2)
        self.assertEqual(self.list_count("ringde"), 1)
        self.assertEqual(self.list_count("formular"), 1)

    def test_no_visits_is_empty(self):
        Click.objects.update(lp_visits=0, called=False)
        self.assertEqual(reports.lp_behaviour(self.row), {})


class PageTests(ReportFixture, TestCase):
    def test_the_report_has_every_part(self):
        response = self.client.get(self.report_url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for text in (
            "Från utskick till förfrågan",
            "Klick per timme efter utskicket",
            "Länkar",
            "Vad de gjorde på sidan",
            "mediantid 1 min 6 s",
            "Följ upp de som inte klickade",
            ">Exportera</a>",
            "css/flamingo-app-utskick-report.css",
        ):
            self.assertIn(text, html)
        # Inget om Google (D11): utskickens förfrågningar går aldrig dit.
        self.assertNotIn("Google Ads", html)
        self.assertNotIn("konverteringar", html)

    def test_every_number_on_the_report_leads_to_a_list(self):
        html = self.client.get(self.report_url()).content.decode()
        base = reverse("flamingo:app_utskick_recipients", args=[self.row.pk])
        hrefs = set(re.findall(re.escape(base) + r'\?([^"]+)"', html))
        self.assertGreater(len(hrefs), 12)
        for query in hrefs:
            params = dict(p.split("=", 1) for p in query.replace("&amp;", "&").split("&"))
            with self.subTest(query=query):
                self.list_count(params["visa"], params.get("kanal"))

    def test_the_funnel_says_its_base_and_its_numbers_look_like_links(self):
        html = self.client.get(self.report_url()).content.decode()
        # Granskningen: "26,8 %" stod utan bas, bredvid Klick-rutan "av levererade".
        self.assertIn('<span class="fl-sr"> av skickade</span>', html)
        self.assertIn("Procenten räknas av Skickade.", html)
        css = (BASE / "static" / "css" / "flamingo-app-utskick-report.css").read_text("utf-8")
        self.assertIn(
            ".fl-ut-rp-step--link .fl-ut-rp-step__n{color:var(--fl-violet-ink);"
            "text-decoration:underline",
            css,
        )

    def test_the_chart_reads_without_hovering(self):
        html = self.client.get(self.report_url()).content.decode()
        caption = re.search(
            r'<figcaption class="fl-ut-rp-chart__caption" id="ut-rp-chart-desc">([^<]*)<', html
        ).group(1)
        self.assertIn("3 klick under", caption)
        self.assertIn("som mest 2 på en timme", caption)
        axis = re.search(r'<div class="fl-ut-rp-chart__axis"[^>]*>(.*?)</div>', html).group(1)
        self.assertEqual(axis.count("<span>"), 3)
        css = (BASE / "static" / "css" / "flamingo-app-utskick-report.css").read_text("utf-8")
        self.assertNotIn("#C9BDFA", css)
        self.assertIn(".fl-ut-rp-chart__bar{fill:var(--fl-violet)}", css)

    def test_an_utskick_without_links_has_no_click_steps_and_no_follow_up(self):
        # Granskningen: "Nya öppettider" fick "Följ upp de som inte klickade"
        # och ett segment med alla mottagare.
        Click.objects.all().delete()
        Lead.objects.all().delete()
        TrackedLink.objects.filter(utskick=self.row).delete()
        Recipient.objects.filter(utskick=self.row).update(first_clicked_at=None, click_count=0)
        response = self.client.get(self.report_url())
        full = response.context["full"]
        self.assertFalse(full["has_links"])
        self.assertEqual(full["follow_up"], 0)
        self.assertFalse(response.context["can_follow_up"])
        steps = [step["key"] for step in full["funnels"][0]["steps"]]
        self.assertEqual(steps, ["attempted", "delivered", "leads"])
        self.assertNotContains(response, "Följ upp de som inte klickade")
        self.assertNotContains(response, "Klick per timme efter utskicket")
        self.assertNotContains(response, "Inga klick än.")

    def test_drafts_and_scheduled_have_no_full_report(self):
        for status in (Utskick.Status.DRAFT, Utskick.Status.SCHEDULED):
            draft = Utskick.objects.create(account=self.account, name="Senare", status=status)
            response = self.client.get(self.report_url(draft))
            self.assertIsNone(response.context["full"])
            self.assertNotContains(response, "Från utskick till förfrågan")
            self.assertNotContains(response, "Följ upp de som inte klickade")

    def test_staff_in_view_as_sees_the_same(self):
        response = self.client_for(self.staff).get(self.report_url())
        self.assertContains(response, "Från utskick till förfrågan")

    def test_the_recipients_page_has_the_new_chips(self):
        url = reverse("flamingo:app_utskick_recipients", args=[self.row.pk])
        html = self.client.get(url).content.decode()
        for view in ("skickade", "klickade-inte", "stannade"):
            self.assertIn(f"?visa={view}", html)
        self.assertNotIn("kanal=e-post", html)
        self.assertEqual(self.list_count("klickade-inte"), 1)


class EmailTests(ReportFixture, TestCase):
    def setUp(self):
        super().setUp()
        Utskick.objects.filter(pk=self.row.pk).update(
            channel_mode=Utskick.ChannelMode.BOTH, open_tracking=True
        )
        self.row.refresh_from_db()
        sent = self.start + timedelta(minutes=2)
        for name, status, extra in (
            ("anna", S.DELIVERED, {"opened_at": sent}),
            ("bo", S.BOUNCED, {}),
            ("cilla", S.COMPLAINED, {"opened_at": sent}),
        ):
            kontakt = self.people[name]
            Recipient.objects.create(
                utskick=self.row,
                contact=kontakt,
                channel=CHANNEL_EMAIL,
                address=f"{name}@exempel.example",
                status=status,
                sent_at=sent,
                **extra,
            )

    def test_the_email_tiles_lead_to_the_email_recipients(self):
        response = self.client.get(self.report_url())
        tiles = {t["label"]: t for t in response.context["email_tiles"]}
        self.assertEqual(
            {label: t["value"] for label, t in tiles.items()},
            {
                "Levererade": 2,
                "Klick": 0,
                "Öppnat (indikation)": 2,
                "Studsar": 1,
                "Klagomål": 1,
                "Avregistreringar": 0,
            },
        )
        for label in ("Levererade", "Öppnat (indikation)", "Studsar", "Klagomål"):
            with self.subTest(tile=label):
                tile = tiles[label]
                self.assertEqual(tile["kanal"], "e-post")
                self.assertEqual(self.list_count(tile["visa"], "e-post"), tile["value"])
        self.assertContains(response, "?visa=oppnade&amp;kanal=e-post")

    def test_opens_only_when_tracking_was_on(self):
        Utskick.objects.filter(pk=self.row.pk).update(open_tracking=False)
        response = self.client.get(self.report_url())
        labels = [t["label"] for t in response.context["email_tiles"]]
        self.assertNotIn("Öppnat (indikation)", labels)

    def test_one_funnel_per_channel(self):
        full = reports.full(self.row, now=self.now)
        self.assertEqual([f["label"] for f in full["funnels"]], ["Sms", "E-post"])
        email = full["funnels"][1]["steps"]
        self.assertEqual([s["n"] for s in email], [3, 2, 0, 0, 0])
        for step in email[:2]:
            self.assertEqual(self.list_count(step["visa"], "e-post"), step["n"])

    def test_the_recipients_page_filters_on_channel(self):
        self.assertEqual(self.list_count("alla"), 7)
        self.assertEqual(self.list_count("alla", "sms"), 4)
        self.assertEqual(self.list_count("alla", "e-post"), 3)
        url = reverse("flamingo:app_utskick_recipients", args=[self.row.pk])
        html = self.client.get(url, {"visa": "alla", "kanal": "e-post"}).content.decode()
        self.assertIn("Alla kanaler", html)
        self.assertIn("?visa=studsade&amp;kanal=e-post", html)
        self.assertIn('name="kanal" value="e-post"', html)

    def test_save_as_list_keeps_the_channel(self):
        url = reverse("flamingo:app_utskick_save_list", args=[self.row.pk])
        response = self.client.post(
            url, {"visa": "studsade", "kanal": "e-post", "lista_id": "ny", "ny_lista": "Studsade"}
        )
        self.assertIn("kanal=e-post", response["Location"])
        lista = self.account.utskick_lists.get(name="Studsade")
        self.assertEqual(
            list(lista.memberships.values_list("contact", flat=True)), [self.people["bo"].pk]
        )


class RetentionTests(ReportFixture, TestCase):
    def setUp(self):
        super().setUp()
        stats = reports.final_stats(self.row)
        Utskick.objects.filter(pk=self.row.pk).update(stats=stats)
        Click.objects.all().delete()
        Recipient.objects.filter(utskick=self.row).delete()
        self.row.refresh_from_db()

    def test_the_funnel_comes_from_stats_without_links(self):
        steps = reports.funnel(self.row)
        self.assertEqual([s["n"] for s in steps], [4, 3, 2, 1, 1])
        self.assertTrue(all(s["visa"] == "" for s in steps))
        response = self.client.get(self.report_url())
        self.assertContains(response, "Från utskick till förfrågan")
        self.assertContains(response, "Inga klick än.")
        self.assertNotContains(response, "?visa=skickade")
        self.assertNotContains(response, "Följ upp de som inte klickade")
        self.assertNotContains(response, ">Exportera</a>")

    def test_the_links_show_the_rolled_up_sums(self):
        TrackedLink.objects.filter(pk=self.lp.pk).update(human_clicks=40, leads=3)
        rows = {row["link"].pk: row for row in reports.per_link(self.row)}
        self.assertEqual((rows[self.lp.pk]["clicks"], rows[self.lp.pk]["leads"]), (40, 3))
        self.assertEqual(rows[self.lp.pk]["visa"], "")

    def test_nothing_to_export(self):
        url = reverse("flamingo:app_utskick_export", args=[self.row.pk])
        self.assertContains(self.client.get(url), "Mottagarna finns inte kvar")
        response = self.client.post(url, {"bekrafta": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ExportLog.objects.exists())

    def test_an_utskick_not_yet_sent_says_so(self):
        """Ett schemalagt utskick har inga mottagare än: de är inte rensade."""
        Utskick.objects.filter(pk=self.row.pk).update(
            status=Utskick.Status.SCHEDULED, started_at=None, finished_at=None
        )
        url = reverse("flamingo:app_utskick_export", args=[self.row.pk])
        response = self.client.get(url)
        self.assertContains(response, "Utskicket har inte skickats än")
        self.assertNotContains(response, "Mottagarna finns inte kvar")


class ExportTests(ReportFixture, TestCase):
    def url(self, utskick=None):
        return reverse("flamingo:app_utskick_export", args=[(utskick or self.row).pk])

    def rows(self, response):
        text = response.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"))
        return list(csv.reader(io.StringIO(text[1:])))

    def test_get_is_the_confirmation_never_the_file(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "5 mottagare som en CSV-fil")
        self.assertContains(response, 'name="bekrafta" value="1"')
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        response = self.client.post(self.url())
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        self.assertFalse(ExportLog.objects.exists())

    def test_the_file_with_safe_cells_and_no_address_for_deleted(self):
        Contact.objects.filter(pk=self.people["anna"].pk).update(first_name="=HYPERLINK(1)")
        Recipient.objects.filter(pk=self.r["dan"].pk).update(contact=None, address="")
        response = self.client.post(self.url(), {"bekrafta": "1"})
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn(f'filename="utskick-{self.row.pk}-', response["Content-Disposition"])
        self.assertIn("no-store", response["Cache-Control"])
        rows = self.rows(response)
        self.assertEqual(tuple(rows[0]), reports.EXPORT_HEADER)
        self.assertEqual(len(rows), 6)
        anna = rows[1]
        self.assertTrue(anna[0].startswith("'="))
        self.assertEqual(anna[1], "Sms")
        self.assertEqual((anna[6], anna[7], anna[9]), ("1", "112", "Ja"))
        dan = rows[4]
        self.assertEqual((dan[0], dan[2]), ("Borttagen kontakt", ""))
        eva = rows[5]
        self.assertEqual(eva[3], "Hoppades över: veckotaket")
        log = ExportLog.objects.get()
        self.assertEqual(
            (log.kind, log.rows, log.user, log.as_staff),
            (ExportLog.Kind.UTSKICK, 5, self.anna, False),
        )

    def test_staff_is_logged_as_staff(self):
        with self.assertLogs("apps.utskick.app_views.report", "INFO") as logs:
            self.client_for(self.staff).post(self.url(), {"bekrafta": "1"})
        log = ExportLog.objects.get()
        self.assertEqual((log.user, log.as_staff), (self.staff, True))
        self.assertIn("(ADX åt kunden)", logs.output[0])

    def test_ten_a_day_shared_with_the_contacts_export(self):
        for _ in range(10):
            self.assertEqual(self.client.post(self.url(), {"bekrafta": "1"}).status_code, 200)
        response = self.client.post(self.url(), {"bekrafta": "1"})
        self.assertContains(response, "som är gränsen")
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        self.assertEqual(ExportLog.objects.count(), 10)
        response = self.client.post(reverse("flamingo:app_contacts_export"), {"bekrafta": "1"})
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))

    def test_another_accounts_utskick_is_404(self):
        foreign = Utskick.objects.create(account=self.other_account, name="Hemligt")
        self.assertEqual(self.client.get(self.url(foreign)).status_code, 404)
        self.assertEqual(self.client.post(self.url(foreign), {"bekrafta": "1"}).status_code, 404)
        self.assertFalse(ExportLog.objects.exists())

    def test_export_rows_never_reach_another_utskick(self):
        other = Utskick.objects.create(account=self.account, name="Annat")
        Recipient.objects.create(
            utskick=other, contact=self.people["anna"], channel=CHANNEL_SMS, address="+46701749999"
        )
        rows = list(reports.export_rows(self.row))
        self.assertEqual(len(rows), 5)
        self.assertNotIn("070-174 99 99", str(rows))


class FollowUpTests(ReportFixture, TestCase):
    def url(self, utskick=None):
        return reverse("flamingo:app_utskick_follow_up", args=[(utskick or self.row).pk])

    def test_the_segment_and_a_draft_with_it(self):
        response = self.client.post(self.url())
        segment = Segment.objects.get(account=self.account)
        self.assertEqual(segment.name, "Klickade inte: Höstservice värmepump")
        self.assertEqual(
            segment.rules,
            {
                "all": [
                    {"f": "got_utskick", "op": "in", "v": [self.row.pk]},
                    {"f": "clicked", "op": "not_in", "v": [self.row.pk]},
                ]
            },
        )
        # Cilla fick sms:et och klickade inte (skannern räknas inte); Dan fick
        # det aldrig, Eva hoppades över.
        found = set(segments.contacts(self.account, segment.rules).values_list("pk", flat=True))
        self.assertEqual(found, {self.people["cilla"].pk})
        self.assertEqual(reports.follow_up_count(self.row), 1)
        draft = Utskick.objects.exclude(pk=self.row.pk).get(account=self.account)
        self.assertEqual(draft.status, Utskick.Status.DRAFT)
        self.assertEqual(draft.name, "Uppföljning: Höstservice värmepump")
        self.assertEqual(draft.audience["segments"], [segment.pk])
        self.assertEqual(draft.created_by, self.anna)
        self.assertRedirects(
            response,
            reverse("flamingo:app_utskick_step", args=[draft.pk, "mottagare"]),
            fetch_redirect_response=False,
        )

    def test_a_second_press_opens_the_same_draft(self):
        self.client.post(self.url())
        response = self.client.post(self.url())
        self.assertEqual(Segment.objects.filter(account=self.account).count(), 1)
        self.assertEqual(Utskick.objects.filter(account=self.account).count(), 2)
        draft = Utskick.objects.exclude(pk=self.row.pk).get(account=self.account)
        self.assertRedirects(
            response,
            reverse("flamingo:app_utskick_step", args=[draft.pk, "mottagare"]),
            fetch_redirect_response=False,
        )

    def test_nobody_to_follow_up(self):
        Recipient.objects.filter(utskick=self.row).update(first_clicked_at=self.now)
        response = self.client.post(self.url())
        self.assertRedirects(response, self.report_url(), fetch_redirect_response=False)
        self.assertFalse(Segment.objects.exists())
        self.assertNotContains(self.client.get(self.report_url()), "Följ upp de som inte klickade")

    def test_post_only_and_another_account_is_404(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)
        foreign = Utskick.objects.create(
            account=self.other_account, name="Hemligt", status=Utskick.Status.SENT
        )
        self.assertEqual(self.client.post(self.url(foreign)).status_code, 404)
        self.assertFalse(Segment.objects.exists())

    def test_the_segment_limit_is_a_message(self):
        Segment.objects.bulk_create(
            Segment(account=self.account, name=f"S{n}") for n in range(Segment.MAX_PER_ACCOUNT)
        )
        response = self.client.post(self.url())
        self.assertRedirects(response, self.report_url(), fetch_redirect_response=False)
        self.assertEqual(Utskick.objects.filter(account=self.account).count(), 1)

    def test_texts_follow_the_copy_rules(self):
        for text in (
            report_views.FOLLOW_UP_TEXT,
            report_views.FOLLOW_UP_AGAIN_TEXT,
            report_views.NOBODY_TEXT,
            report_views.NO_ROWS_TEXT,
        ):
            self.assertNotIn(chr(0x21), text)
            self.assertNotIn(chr(0x2013), text)
            self.assertTrue(text.endswith("."))
