"""Brevs länkfält med valet av landningssida (Flamingos README Steg 2,
Giovanni 2026-10-28).

    PickerConfigTests   redigeraren får kontots publicerade sidor med sidans
                        egen adress (inte utkast, inte ett annat kontos), och
                        fälten valet gäller finns i Brevs schema som länkfält
    PickedLinkFlowTests en sida som valts i en knapp blir en spårad länk till
                        sidan när utskicket fryses (TrackedLink med
                        landing_page, personlig per mottagare), och klicket
                        landar som E-post på sidans statistik: besöket och
                        förfrågan; fri text och sparade mejl fungerar som förut
"""

from urllib.parse import parse_qs, urlsplit

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.exports import landing_page_url
from apps.flamingo.models import LandingPage, Lead, PageVisitDay

from . import tokens
from .app_views import brev
from .email import registry
from .models import Click, TrackedLink
from .test_s3_flow import CLICK_RE, IPHONE, KLICK, S3FlowFixture, path_of


class PickerConfigTests(S3FlowFixture, TestCase):
    def config(self):
        utskick = self.draft_with_mail()
        response = self.customer_client.get(reverse("flamingo:app_brev", args=[utskick.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context["br_config"]["pageLinks"]

    def test_the_editor_lists_the_published_pages_with_their_own_address(self):
        page = self.quote_page.landing_page
        draft = LandingPage.objects.create(account=self.account, name="Inte publicerad")
        other = LandingPage.objects.create(
            account=self.other_account,
            name="Annans sida",
            published={"blocks": []},
            published_at=timezone.now(),
        )
        links = self.config()
        listed = {item["id"]: item for item in links["pages"]}
        self.assertIn(page.pk, listed)
        self.assertEqual(listed[page.pk]["url"], landing_page_url(page))
        self.assertEqual(listed[page.pk]["name"], page.name)
        self.assertTrue(listed[page.pk]["url"].startswith("https://adx.se/lp/exempelror/"))
        self.assertNotIn(draft.pk, listed)
        self.assertNotIn(other.pk, listed)
        self.assertEqual(links["fields"], {k: list(v) for k, v in brev.PAGE_LINK_FIELDS.items()})

    def test_the_picker_fields_are_link_fields_in_brev(self):
        for type_key, keys in brev.PAGE_LINK_FIELDS.items():
            fields = {f.key: f for f in registry.EMAIL_TYPES[type_key].fields}
            for key in keys:
                with self.subTest(block=type_key, field=key):
                    self.assertEqual(fields[key].kind, registry.URL)
        # Videon och sociala medier går till annat än en landningssida.
        self.assertNotIn("video", brev.PAGE_LINK_FIELDS)
        self.assertNotIn("social", brev.PAGE_LINK_FIELDS)

    def test_the_editor_script_keeps_the_picker_in_the_brev_profile(self):
        from django.conf import settings

        script = (settings.BASE_DIR / "static/js/flamingo-app-brev.js").read_text()
        self.assertIn("config.pageLinks", script)
        self.assertIn('kind === "url" && pagePickable(blockId, path)', script)
        # Sidbyggarens skript har inget av valet.
        builder = (settings.BASE_DIR / "static/js/flamingo-pb.js").read_text()
        self.assertNotIn("pageLinks", builder)


class PickedLinkFlowTests(S3FlowFixture, TestCase):
    """Mejlet byggs som i S3-flödet, men knappen går till sidans egen adress
    som valet skriver den (config.pageLinks), inte kampanjens adress."""

    def setUp(self):
        super().setUp()
        self.page = self.quote_page.landing_page
        self.picked = None

    def block(self, utskick, type_key, **fields):
        if type_key == "hero":
            if self.picked is None:
                response = self.customer_client.get(reverse("flamingo:app_brev", args=[utskick.pk]))
                pages = response.context["br_config"]["pageLinks"]["pages"]
                self.picked = next(p["url"] for p in pages if p["id"] == self.page.pk)
            fields["button_url"] = self.picked
        return super().block(utskick, type_key, **fields)

    def test_a_picked_page_becomes_a_tracked_link_and_the_click_lands_as_email(self):
        utskick, mails = self.sent_utskick()
        self.assertEqual(
            utskick.email_doc["blocks"][0]["versions"][-1]["fields"]["button_url"], self.picked
        )
        link = TrackedLink.objects.get(utskick=utskick, kind=TrackedLink.Kind.LP)
        self.assertEqual(link.landing_page_id, self.page.pk)
        self.assertIsNone(link.campaign_id)
        self.assertEqual(link.destination, landing_page_url(self.page))
        # Den fria texten (webbplatsen i texten) är en vanlig extern länk.
        self.assertTrue(
            TrackedLink.objects.filter(utskick=utskick, kind=TrackedLink.Kind.EXTERNAL).exists()
        )

        # Personlig per mottagare: Annas och Bos länkar till sidan skiljer sig.
        def page_url(first):
            recipient, message = mails[first]
            for url in CLICK_RE.findall(self.html(message)):
                ref = tokens.read_email_click(path_of(url).rsplit("/", 1)[1])
                if ref.link_id == link.pk:
                    self.assertEqual(ref.recipient_id, recipient.pk)
                    return url
            self.fail(f"Ingen länk till sidan i {first}s mejl")

        self.assertNotEqual(page_url("Anna"), page_url("Bo"))

        response = Client().get(path_of(page_url("Anna")), HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(response.status_code, 302)
        location = urlsplit(response["Location"])
        self.assertEqual(location.path, self.page.landing_url)
        query = parse_qs(location.query)
        click = Click.objects.get()
        self.assertEqual(click.channel, Click.Channel.EMAIL)
        self.assertEqual(query["ut"], [tokens.ut_token(click.pk)])

        visitor = Client(HTTP_USER_AGENT=IPHONE, HTTP_X_REAL_IP="203.0.113.40")
        page = visitor.get(location.path, {"ut": query["ut"][0]})
        self.assertEqual(page.status_code, 200)
        row = PageVisitDay.objects.get(page=self.page)
        self.assertEqual((row.source, row.key), ("email", f"email:u{utskick.pk}"))
        self.assertEqual(row.utskick_id, utskick.pk)
        visitor.post(
            location.path,
            {
                "name": "Anna Ek",
                "phone": "0701740699",
                "email": "anna.ek@hemma.example",
                "ut": query["ut"][0],
            },
        )
        lead = Lead.objects.get(account=self.account, utskick=utskick)
        self.assertEqual((lead.landing_page_id, lead.page_source), (self.page.pk, "email"))
        self.assertIsNone(lead.campaign_id)
        self.assertFalse(lead.can_send_to_google)
        # Statistiken: en rad E-post med utskickets namn, ett besök och en förfrågan.
        from apps.flamingo import sources

        stats = sources.page_stats(self.page)
        email = [r for r in stats.rows if r.source == "email"]
        self.assertEqual(len(email), 1)
        self.assertEqual((email[0].chip, email[0].text), ("E-post", utskick.name))
        self.assertEqual((email[0].visits, email[0].leads), (1, 1))
