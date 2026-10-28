"""ADX Flamingo, steg 2 (README, Giovanni 2026-10-28): Inkorgen visar
förfrågans källa som chip, med samma texter och färger som sidans Statistik.

    LeadSourceTests   sources.lead_source för varje källa: Annons med och utan
                      kampanj, Sms, E-post, QR-kod, Instagram, Annat, Direkt;
                      gamla rader utan sparad källa läses som Statistik; svar,
                      samtal och förfrågningar lagda för hand har inget chip
    InboxChipTests    listan och förfrågan visar chipet, sidan och kampanjen,
                      utskicket eller länken; kanalen står kvar för resten
    QueryCountTests   listan och förfrågan gör lika många frågor hur många
                      förfrågningar det än är
"""

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.utskick.models import TrackedLink, Utskick

from . import sources
from .models import Lead
from .test_lp_sources import LINK_SETTINGS, SourcesFixture


class LeadsFixture(SourcesFixture):
    def lead(self, **fields):
        data = {
            "account": self.account,
            "source": Lead.SOURCE_FORM,
            "name": "Mikael Ström",
            "phone": "070-174 06 50",
        }
        data.update(fields)
        return Lead.objects.create(**data)

    def named(self, label, medium):
        self.links_made = getattr(self, "links_made", 0) + 1
        return TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            landing_page=self.page,
            destination=self.page.landing_url,
            slug=f"lank-{self.links_made}",
            label=label,
            medium=medium,
        )

    def every_source(self):
        """En förfrågan av varje sort, med sidan Avloppsspolning."""
        sms = self.utskick(name="Höstservice")
        email = Utskick.objects.create(
            account=self.account,
            name="Vinterbrevet",
            channel_mode=Utskick.ChannelMode.EMAIL_ONLY,
            status=Utskick.Status.SENT,
        )
        page = {"landing_page": self.page}
        return {
            "ad": self.lead(name="Ada Annons", campaign=self.campaign, page_source="ad", **page),
            "ad_unknown": self.lead(name="Ulla Okänd", page_source="ad", gclid="Cj0", **page),
            "sms": self.lead(name="Sam Sms", page_source="sms", utskick=sms, **page),
            "email": self.lead(name="Eva Epost", page_source="email", utskick=email, **page),
            "qr": self.lead(
                name="Kalle Qr",
                page_source="named",
                named_link=self.named("Affisch i verkstaden", "qr"),
                **page,
            ),
            "instagram": self.lead(
                name="Ina Insta",
                page_source="named",
                named_link=self.named("Profilen", "instagram"),
                **page,
            ),
            "other": self.lead(
                name="Otto Annat",
                page_source="named",
                named_link=self.named("Mässan", ""),
                **page,
            ),
            "direct": self.lead(name="Dora Direkt", page_source="direct", **page),
        }


@override_settings(**LINK_SETTINGS)
class LeadSourceTests(LeadsFixture, TestCase):
    def test_every_source_has_the_stats_pages_chip_and_text(self):
        found = {key: sources.lead_source(lead) for key, lead in self.every_source().items()}
        expected = {
            "ad": ("Annons", "ad", "Avloppsspolning Nacka"),
            "ad_unknown": ("Annons", "ad", "Google Ads"),
            "sms": ("Sms", "sms", "Höstservice"),
            "email": ("E-post", "mail", "Vinterbrevet"),
            "qr": ("QR-kod", "qr", "Affisch i verkstaden"),
            "instagram": ("Instagram", "social", "Profilen"),
            "other": ("Annat", "other", "Mässan"),
            "direct": ("Direkt", "direct", ""),
        }
        for key, (chip, tone, text) in expected.items():
            with self.subTest(source=key):
                self.assertEqual((found[key].chip, found[key].tone), (chip, tone))
                self.assertEqual(found[key].text, text)
                self.assertEqual(found[key].page_name, "Avloppsspolning")
        # Samma texter som Statistik.
        chips = {chip for chip, _tone in sources.SOURCE_CHIPS.values()}
        chips |= {chip for chip, _tone in sources.MEDIUM_CHIPS.values()}
        self.assertEqual({f.chip for f in found.values()}, chips)

    def test_an_old_lead_without_a_stored_source_reads_like_the_stats(self):
        campaign_lead = self.lead(campaign=self.campaign)
        found = sources.lead_source(campaign_lead)
        self.assertEqual(
            (found.chip, found.text, found.page_name),
            ("Annons", self.campaign.name, "Avloppsspolning"),
        )
        utskick = self.utskick(name="Gammalt sms")
        sms_lead = self.lead(
            campaign=self.campaign,
            utskick=utskick,
            attribution={"channel": "sms", "name": "Gammalt sms"},
        )
        self.assertEqual(sources.lead_source(sms_lead).chip, "Sms")
        named = self.lead(
            campaign=self.campaign, attribution={"channel": "named", "label": "Affischen"}
        )
        found = sources.lead_source(named)
        self.assertEqual((found.chip, found.text), ("Annat", "Affischen"))
        # Statistikens SQL säger samma sak om samma rader.
        values = dict(
            Lead.objects.filter(pk__in=[campaign_lead.pk, sms_lead.pk, named.pk])
            .annotate(src=sources.lead_source_expr())
            .values_list("pk", "src")
        )
        self.assertEqual(values, {campaign_lead.pk: "ad", sms_lead.pk: "sms", named.pk: "named"})

    def test_manual_calls_and_replies_have_no_chip(self):
        for source in (Lead.SOURCE_MANUAL, Lead.SOURCE_CALL, Lead.SOURCE_REPLY):
            with self.subTest(source=source):
                self.assertIsNone(sources.lead_source(self.lead(source=source)))
        self.assertIsNone(sources.lead_source(self.lead()))


@override_settings(**LINK_SETTINGS)
class InboxChipTests(LeadsFixture, TestCase):
    def test_the_list_shows_the_chip_the_page_and_the_detail(self):
        self.every_source()
        manual = self.lead(name="Hans Hand", source=Lead.SOURCE_MANUAL)
        html = self.client_for(self.anna).get(reverse("flamingo:app_inbox")).content.decode()
        for tone, chip in (
            ("ad", "Annons"),
            ("sms", "Sms"),
            ("mail", "E-post"),
            ("qr", "QR-kod"),
            ("social", "Instagram"),
            ("other", "Annat"),
            ("direct", "Direkt"),
        ):
            with self.subTest(chip=chip):
                self.assertIn(f'<span class="fl-lpsrc fl-lpsrc--{tone}">{chip}</span>', html)
        for text in (
            "Avloppsspolning · Avloppsspolning Nacka",
            "Avloppsspolning · Google Ads",
            "Avloppsspolning · Höstservice",
            "Avloppsspolning · Vinterbrevet",
            "Avloppsspolning · Affisch i verkstaden",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn("Lagd för hand", html)
        self.assertIn(manual.name, html)

    def test_the_detail_shows_the_source_and_links_the_page(self):
        lead = self.every_source()["sms"]
        response = self.client_for(self.anna).get(reverse("flamingo:app_lead", args=[lead.pk]))
        self.assertContains(response, "<dt>Källa</dt>", html=False)
        self.assertContains(response, '<span class="fl-lpsrc fl-lpsrc--sms">Sms</span>', html=False)
        self.assertContains(response, "Höstservice")
        stats = reverse("flamingo:app_page_stats", args=[self.page.pk])
        self.assertContains(response, f'<a href="{stats}">Avloppsspolning</a>', html=False)

    def test_a_manual_lead_keeps_its_channel(self):
        lead = self.lead(source=Lead.SOURCE_MANUAL)
        response = self.client_for(self.anna).get(reverse("flamingo:app_lead", args=[lead.pk]))
        self.assertNotContains(response, "<dt>Källa</dt>", html=False)
        self.assertContains(response, "Lagd för hand")

    def test_the_chip_style_is_shared_with_the_stats_page(self):
        from django.conf import settings

        app = (settings.BASE_DIR / "static/css/flamingo-app.css").read_text()
        pages = (settings.BASE_DIR / "static/css/flamingo-app-pages.css").read_text()
        for tone in ("ad", "sms", "mail", "qr", "social"):
            self.assertIn(f".fl-lpsrc--{tone}{{", app)
        self.assertNotIn(".fl-lpsrc{", pages)


@override_settings(**LINK_SETTINGS)
class QueryCountTests(LeadsFixture, TestCase):
    def count(self, url):
        client = self.client_for(self.anna)
        client.get(url)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(client.get(url).status_code, 200)
        return len(queries)

    def test_the_list_is_flat(self):
        self.every_source()
        few = self.count(reverse("flamingo:app_inbox"))
        self.every_source()
        self.every_source()
        self.assertEqual(self.count(reverse("flamingo:app_inbox")), few)

    def test_the_detail_is_flat(self):
        lead = self.every_source()["qr"]
        url = reverse("flamingo:app_lead", args=[lead.pk])
        few = self.count(url)
        self.every_source()
        self.every_source()
        self.assertEqual(self.count(url), few)
