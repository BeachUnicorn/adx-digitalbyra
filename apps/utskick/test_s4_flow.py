"""Hela vägen genom S4 (README J S4, "Acceptance"), med de riktiga vyerna,
ticken och apps/sms. Varje del har sina egna tester (test_s4_segments,
test_s4_report, test_s4_links, test_s4_snippet, test_s4_contact_sms); här
prövas att delarna hänger ihop:

    SegmentFlowTests    segmentbyggaren (formuläret och den levande räkningen),
                        Mottagare med segmentet, Granska, bekräftelsen och
                        frysningen: samma antal hela vägen
    ReportFlowTests     rapportens tratt och länktabell leder till listor med
                        exakt lika många mottagare, och "Följ upp de som inte
                        klickade" ger segmentet och utkastet med samma antal
    NamedLinkFlowTests  Ny länk, klicket på klick.adx.se/<konto>/<slug> (också
                        med versaler), landningssidan med ut, förfrågan med
                        spåret i Inkorgen, länkens sida och översikten
    SnippetFlowTests    Spårningsskript, provlänken som gör skriptet Installerat,
                        adx= på klicket först därefter, besöksanropet och besöket
                        på kontaktkortet och i segmentregeln
    CustomerCardTests   kundkortet varnar innan adressen för anmälan byts när
                        kunden har namngivna länkar; den gamla adressen följer
                        kunden och blir aldrig en annans

Inget når nätet: 46elks är FakeElks (test_s2_flow.FlowFixture), och
klockan är den riktiga.
"""

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import Lead

from . import links, segments, site_snippet, tokens
from .models import (
    Click,
    Event,
    FieldDef,
    OldPublicSlug,
    Recipient,
    Segment,
    SiteSnippet,
    TrackedLink,
    Utskick,
    UtskickSettings,
)
from .test_s2_flow import BODY, CODE_RE, IPHONE, FlowFixture, K
from .testing import PHONE_ANNA, PHONE_BO, PHONE_CILLA

KLICK = {"HTTP_HOST": "klick.adx.se"}
SITE = "exempelror.example"


def location(response):
    return urlsplit(response["Location"])


def query(response):
    return {key: values[0] for key, values in parse_qs(location(response).query).items()}


class S4FlowFixture(FlowFixture):
    """FlowFixture (Exempelrör med sms på, Anna, Bo och Cilla i Kunder) plus
    datumfältet Senaste service: Anna och Bo fick service för länge sedan,
    Cilla nyss."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        FieldDef.objects.create(
            account=cls.account,
            key="senaste-service",
            label="Senaste service",
            kind=FieldDef.Kind.DATE,
        )
        today = timezone.localdate()
        for first, days in (("Anna", 400), ("Bo", 220), ("Cilla", 20)):
            kontakt = cls.people[first]
            kontakt.fields = {"senaste-service": (today - timedelta(days=days)).isoformat()}
            kontakt.save(update_fields=["fields"])

    def step(self, utskick, name):
        return reverse("flamingo:app_utskick_step", args=[utskick.pk, name])

    def confirm(self, utskick, client=None):
        """Kanal, Innehåll med länken till Badrum Nacka, Tid (nu), Granska
        och bekräftelsen, som kunden går dem."""
        client = client or self.customer_client
        client.post(self.step(utskick, "kanal"), {"syfte": "reklam", "avsandare": "reply"})
        client.post(
            self.step(utskick, "innehall"),
            {
                "sms_body": BODY,
                "action": "lank",
                "lank_kampanj": self.quote_page.pk,
                "lank_nyckel": "boka",
                "lank_etikett": "Boka tid",
            },
        )
        utskick.refresh_from_db()
        client.post(self.step(utskick, "innehall"), {"sms_body": utskick.sms_body, "nasta": "tid"})
        client.post(self.step(utskick, "tid"), {"nar": "now"})
        page = client.get(self.step(utskick, "granska"))
        review = page.context["review"]
        self.assertFalse(review["blocking"], review["items"])
        response = client.post(
            reverse("flamingo:app_utskick_confirm", args=[utskick.pk]),
            {"nonce": page.context["nonce"]},
        )
        self.assertRedirects(response, reverse("flamingo:app_utskick", args=[utskick.pk]))
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SCHEDULED)
        return utskick

    def click_code(self, phone, client=None):
        code = CODE_RE.search(self.sms_to(phone)["message"]).group(1)
        client = client or Client(enforce_csrf_checks=True)
        response = client.get(f"/{code}", HTTP_USER_AGENT=IPHONE, **K)
        self.assertEqual(response.status_code, 302)
        return response


# ---------------------------------------------------------------------------
# Segment -> Mottagare -> Granska -> frysningen
# ---------------------------------------------------------------------------


class SegmentFlowTests(S4FlowFixture, TestCase):
    def segment_form(self, name):
        """Acceptansens segment som formuläret skickar det: Senaste service
        äldre än 5 månader OCH listan Kunder OCH ingen förfrågan på 30 dagar."""
        return {
            "namn": name,
            "action": "save",
            "r1_g": "",
            "r1_f": "field:senaste-service",
            "r1_op": "before",
            "r1_unit": "months",
            "r1_n": "5",
            "r2_g": "",
            "r2_f": "list",
            "r2_op": "in",
            "r2_v": str(self.kunder.pk),
            "r3_g": "",
            "r3_f": "lead",
            "r3_op": "not_within_days",
            "r3_n": "30",
        }

    def test_the_segment_counts_the_same_live_saved_in_review_and_frozen(self):
        client = self.customer_client
        # Bo skickade en förfrågan förra veckan: han är inte med.
        Lead.objects.create(
            account=self.account,
            campaign=self.quote_page,
            service=self.quote_page.service,
            source=Lead.SOURCE_FORM,
            name="Bo Ek",
            phone="070-174 06 02",
            contact=self.people["Bo"],
            created_at=timezone.now() - timedelta(days=6),
        )
        form = self.segment_form("Service i höst")
        live = client.post(reverse("flamingo:app_segment_count"), form)
        self.assertEqual(live.status_code, 200)
        self.assertEqual((live.json()["total"], live.json()["sms"]), (1, 1))
        response = client.post(reverse("flamingo:app_segment_new"), form)
        segment = Segment.objects.get(account=self.account, name="Service i höst")
        self.assertRedirects(response, reverse("flamingo:app_segment", args=[segment.pk]))
        self.assertEqual((segment.cached_count, segment.cached_sms), (1, 1))
        # Samma regler som JSON (skriptets andra form) räknar lika.
        as_json = client.post(
            reverse("flamingo:app_segment_count"),
            json.dumps({"rules": segment.rules}),
            content_type="application/json",
        )
        self.assertEqual(as_json.json()["total"], 1)

        # Mottagare: bara segmentet, inga listor.
        client.post(reverse("flamingo:app_utskick_new"))
        utskick = Utskick.objects.get(account=self.account)
        response = client.post(
            self.step(utskick, "mottagare"),
            {"namn": "Service i höst", "segments": [segment.pk], "nasta": "kanal"},
        )
        self.assertRedirects(response, self.step(utskick, "kanal"))
        utskick.refresh_from_db()
        self.assertEqual(utskick.audience["segments"], [segment.pk])
        counted = client.get(reverse("flamingo:app_utskick_count", args=[utskick.pk])).json()
        self.assertEqual(counted["sms"], 1)
        utskick = self.confirm(utskick)
        self.assertEqual(utskick.confirm_summary["sms"], 1)

        self.run_tick()
        utskick.refresh_from_db()
        self.assertEqual(utskick.frozen_counts["sms"], 1)
        recipients = Recipient.objects.filter(utskick=utskick).exclude(status="skipped")
        self.assertEqual([r.contact for r in recipients], [self.people["Anna"]])
        self.assertEqual([call["to"] for call in self.fake.sends], [PHONE_ANNA])

    def test_an_excluded_segment_takes_its_contacts_out_of_the_list(self):
        client = self.customer_client
        client.post(reverse("flamingo:app_segment_new"), self.segment_form("Gammal service"))
        segment = Segment.objects.get(account=self.account, name="Gammal service")
        self.assertEqual(segment.cached_count, 2)
        client.post(reverse("flamingo:app_utskick_new"))
        utskick = Utskick.objects.get(account=self.account)
        client.post(
            self.step(utskick, "mottagare"),
            {
                "namn": "Alla utom gammal service",
                "lists": [self.kunder.pk],
                "exclude_segments": [segment.pk],
                "nasta": "kanal",
            },
        )
        utskick = self.confirm(utskick)
        self.assertEqual(utskick.confirm_summary["sms"], 1)
        self.run_tick()
        sent = Recipient.objects.filter(utskick=utskick).exclude(status="skipped")
        self.assertEqual([r.contact for r in sent], [self.people["Cilla"]])

    def test_a_foreign_segment_is_refused_in_the_guide(self):
        foreign = Segment.objects.create(account=self.other_account, name="Främmande")
        client = self.customer_client
        client.post(reverse("flamingo:app_utskick_new"))
        utskick = Utskick.objects.get(account=self.account)
        response = client.post(
            self.step(utskick, "mottagare"),
            {"namn": "X", "segments": [foreign.pk], "nasta": "kanal"},
        )
        self.assertEqual(response.status_code, 400)
        utskick.refresh_from_db()
        self.assertEqual(utskick.audience.get("segments") or [], [])


# ---------------------------------------------------------------------------
# Rapporten -> listorna -> uppföljningen
# ---------------------------------------------------------------------------


class ReportFlowTests(S4FlowFixture, TestCase):
    def sent_with_a_click_and_a_lead(self):
        utskick = self.sent_utskick()
        for call in self.fake.sends:
            self.deliver(call)
        target = location(self.click_code(PHONE_ANNA))
        ut = parse_qs(target.query)["ut"][0]
        visitor = Client()
        self.assertEqual(visitor.get(f"{target.path}?{target.query}").status_code, 200)
        beacon = reverse("flamingo_public:visit_beacon", args=[self.quote_page.page_slug])
        self.assertEqual(visitor.post(beacon, {"ut": ut, "s": "42"}).status_code, 204)
        with self.captureOnCommitCallbacks(execute=True):
            visitor.post(
                self.quote_page.landing_url,
                {"name": "Anna Ek", "phone": "070-174 06 01", "q_storlek": "6", "ut": ut},
            )
        self.assertTrue(Lead.objects.filter(utskick=utskick).exists())
        return utskick

    def listed(self, utskick, visa, kanal=""):
        params = {"visa": visa}
        if kanal:
            params["kanal"] = kanal
        page = self.customer_client.get(
            reverse("flamingo:app_utskick_recipients", args=[utskick.pk]), params
        )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["view"], visa)
        return page.context["page"].paginator.count

    def test_every_number_in_the_report_opens_a_list_of_that_length(self):
        utskick = self.sent_with_a_click_and_a_lead()
        page = self.customer_client.get(reverse("flamingo:app_utskick", args=[utskick.pk]))
        self.assertEqual(page.status_code, 200)
        full = page.context["full"]
        steps = full["funnels"][0]["steps"]
        self.assertEqual(
            [(s["key"], s["n"]) for s in steps],
            [("attempted", 3), ("delivered", 3), ("clicked", 1), ("engaged", 1), ("leads", 1)],
        )
        for step in steps:
            with self.subTest(step=step["key"]):
                self.assertTrue(step["visa"])
                self.assertEqual(self.listed(utskick, step["visa"], step["kanal"]), step["n"])
                self.assertContains(page, f"visa={step['visa']}")
        (row,) = full["links"]
        self.assertEqual((row["clicks"], row["leads"]), (1, 1))
        self.assertEqual(self.listed(utskick, row["visa"]), 1)
        self.assertEqual(self.listed(utskick, row["visa_leads"]), 1)
        self.assertEqual(full["follow_up"], 2)
        self.assertEqual(self.listed(utskick, "klickade-inte"), 2)
        lp = full["lp"]
        self.assertEqual((lp["visitors"], lp["form_leads"]), (1, 1))
        self.assertEqual(self.listed(utskick, "besokte"), lp["visitors"])
        self.assertEqual(self.listed(utskick, "formular"), lp["form_leads"])

    def test_follow_up_makes_the_segment_and_a_draft_with_the_same_people(self):
        utskick = self.sent_with_a_click_and_a_lead()
        url = reverse("flamingo:app_utskick_follow_up", args=[utskick.pk])
        response = self.customer_client.post(url)
        segment = Segment.objects.get(account=self.account)
        self.assertEqual(segment.name, "Klickade inte: Höstservice badrum")
        self.assertEqual(segment.rules, segments.follow_up_rules(utskick))
        draft = Utskick.objects.get(account=self.account, status=Utskick.Status.DRAFT)
        self.assertRedirects(response, self.step(draft, "mottagare"))
        self.assertEqual(draft.audience["segments"], [segment.pk])
        matched = set(segments.contacts(self.account, segment.rules))
        self.assertEqual(matched, {self.people["Bo"], self.people["Cilla"]})
        self.assertEqual(segment.cached_count, self.listed(utskick, "klickade-inte"))
        counted = self.customer_client.get(
            reverse("flamingo:app_utskick_count", args=[draft.pk])
        ).json()
        self.assertEqual(counted["sms"], 2)
        # En andra tryckning öppnar samma utkast.
        again = self.customer_client.post(url)
        self.assertRedirects(again, self.step(draft, "mottagare"))
        self.assertEqual(Utskick.objects.filter(status=Utskick.Status.DRAFT).count(), 1)
        self.assertEqual(Segment.objects.filter(account=self.account).count(), 1)


# ---------------------------------------------------------------------------
# Namngiven länk -> klicket -> landningssidan -> förfrågan
# ---------------------------------------------------------------------------


class NamedLinkFlowTests(S4FlowFixture, TestCase):
    def test_from_the_form_to_a_lead_in_the_inbox(self):
        client = self.customer_client
        response = client.post(
            reverse("flamingo:app_link_new"),
            {"beskrivning": "Affisch i verkstaden", "mal": "lp", "kampanj": self.quote_page.pk},
        )
        link = TrackedLink.objects.get(account=self.account, utskick__isnull=True)
        self.assertRedirects(response, reverse("flamingo:app_link", args=[link.pk]))
        self.assertEqual(link.slug, "affisch-i-verkstaden")
        self.assertEqual(
            links.named_link_url(link), "https://klick.adx.se/exempelror/affisch-i-verkstaden"
        )
        qr = client.get(reverse("flamingo:app_link_qr", args=[link.pk, "svg"]))
        self.assertEqual(qr["Content-Type"], "image/svg+xml")

        # En telefon gjorde första bokstäverna stora: 301 till gemenerna,
        # inget klick sparas på vägen.
        visitor = Client(enforce_csrf_checks=True)
        folded = visitor.get("/Exempelror/Affisch-i-verkstaden", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(folded.status_code, 301)
        self.assertEqual(folded["Location"], "/exempelror/affisch-i-verkstaden")
        self.assertFalse(Click.objects.filter(link=link).exists())
        response = visitor.get(folded["Location"], HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies, {})
        target = location(response)
        self.assertEqual(target.path, self.quote_page.landing_url)
        click = Click.objects.get(link=link)
        self.assertEqual(click.channel, Click.Channel.NAMED)
        self.assertIsNone(click.recipient_id)
        self.assertIsNone(click.contact_id)
        ut = query(response)["ut"]
        self.assertEqual(tokens.read_ut(ut), click.pk)

        page = Client().get(f"{target.path}?{target.query}")
        self.assertContains(page, f'name="ut" value="{ut}"')
        with self.captureOnCommitCallbacks(execute=True):
            Client().post(
                self.quote_page.landing_url,
                {"name": "Lisa Ekholm", "phone": "070-174 06 31", "q_storlek": "4", "ut": ut},
            )
        lead = Lead.objects.get(campaign=self.quote_page)
        self.assertIsNone(lead.utskick_id)
        self.assertEqual(lead.attribution["channel"], Click.Channel.NAMED)
        self.assertEqual(lead.attribution["link"], link.pk)
        self.assertEqual(lead.attribution["label"], "Affisch i verkstaden")
        self.assertFalse(lead.can_send_to_google)

        inbox = client.get(reverse("flamingo:app_inbox"))
        self.assertContains(inbox, "Länk: Affisch i verkstaden")
        detail = client.get(reverse("flamingo:app_link", args=[link.pk]))
        self.assertContains(detail, "Förfrågningar")
        self.assertEqual(detail.context["numbers"]["leads"], 1)
        links.rollup(timezone.now())
        link.refresh_from_db()
        self.assertEqual((link.human_clicks, link.leads), (1, 1))
        # Översikten: förfrågan via affischen är inte Googles.
        from apps.flamingo.app_views.overview import numbers_for

        numbers = numbers_for(self.account, timezone.now())
        self.assertEqual((numbers.leads, numbers.google_leads, numbers.utskick_leads), (1, 0, 1))
        # Granskningen: raden heter då "via utskick och dina länkar".
        self.assertEqual(numbers.named_leads, 1)
        overview = client.get(reverse("flamingo:app"))
        self.assertContains(overview, "Varav via utskick och dina länkar: 1 förfrågan")

    def test_upper_case_never_reaches_the_link_hosts_own_paths(self):
        TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.LP,
            campaign=self.quote_page,
            destination="https://adx.example/lp/x/",
            label="Affisch",
            slug="vinter",
        )
        visitor = Client()
        for path in (
            "/S/Ab12Cd",
            "/P/Ab12Cd",
            "/M/Ab12Cd",
            "/Annanfirma/Vinter",
            "/Exempelror/Sommar",
        ):
            with self.subTest(path=path):
                response = visitor.get(path, **KLICK)
                self.assertEqual(response.status_code, 404)
        self.assertEqual(visitor.get("/Exempelror/Vinter", **K).status_code, 404)
        self.assertEqual(visitor.get("/Exempelror/Vinter", **KLICK).status_code, 301)
        self.assertEqual(visitor.head("/EXEMPELROR/VINTER", **KLICK).status_code, 301)


# ---------------------------------------------------------------------------
# Skriptet på egen sajt -> besöket på kontaktkortet
# ---------------------------------------------------------------------------


class SnippetFlowTests(S4FlowFixture, TestCase):
    def beacon(self, site, token, origin=f"https://{SITE}", **data):
        payload = {"k": site.key, "t": token, "p": "/boka?namn=Bo", "s": 0, "v": 1}
        payload.update(data)
        response = Client(enforce_csrf_checks=True).post(
            "/v",
            data=json.dumps(payload),
            content_type="text/plain;charset=UTF-8",
            HTTP_ORIGIN=origin,
            HTTP_USER_AGENT=IPHONE,
            **KLICK,
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.cookies, {})
        site.refresh_from_db()

    def sent_to_the_website(self):
        """Ett sms-utskick till Kunder med en länk till kundens egen webbplats
        (Customer.website, ingen granskning)."""
        client = self.customer_client
        client.post(reverse("flamingo:app_utskick_new"))
        utskick = Utskick.objects.get(account=self.account)
        client.post(
            self.step(utskick, "mottagare"),
            {"namn": "Vinterservice", "lists": [self.kunder.pk], "nasta": "kanal"},
        )
        client.post(self.step(utskick, "kanal"), {"syfte": "reklam", "avsandare": "reply"})
        client.post(
            self.step(utskick, "innehall"),
            {
                "sms_body": BODY,
                "action": "lank",
                "lank_adress": f"https://{SITE}/boka",
                "lank_nyckel": "webb",
                "lank_etikett": "Boka på webbplatsen",
            },
        )
        utskick.refresh_from_db()
        self.assertEqual(TrackedLink.objects.get(utskick=utskick).kind, TrackedLink.Kind.EXTERNAL)
        client.post(self.step(utskick, "innehall"), {"sms_body": utskick.sms_body, "nasta": "tid"})
        client.post(self.step(utskick, "tid"), {"nar": "now"})
        page = client.get(self.step(utskick, "granska"))
        self.assertFalse(page.context["review"]["blocking"], page.context["review"]["items"])
        client.post(
            reverse("flamingo:app_utskick_confirm", args=[utskick.pk]),
            {"nonce": page.context["nonce"]},
        )
        self.run_tick()
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SENT)
        return utskick

    def test_install_check_then_adx_on_the_click_and_the_visit_on_the_card(self):
        client = self.customer_client
        response = client.post(
            reverse("flamingo:app_utskick_snippet"), {"action": "add", "domain": f"www.{SITE}"}
        )
        self.assertRedirects(response, reverse("flamingo:app_utskick_snippet"))
        site = SiteSnippet.objects.get(account=self.account)
        self.assertEqual(site.domain, SITE)
        self.assertFalse(site.is_installed)
        page = client.get(reverse("flamingo:app_utskick_snippet"))
        self.assertContains(page, site_snippet.NOT_SEEN_LABEL)
        self.assertContains(page, links.snippet_url())
        utskick = self.sent_to_the_website()

        # Före installationen: klicket går till sajten utan adx=.
        before = self.click_code(PHONE_ANNA)
        self.assertEqual(location(before).hostname, SITE)
        self.assertNotIn("adx", query(before))

        # Provlänken på sidan: skriptet rapporterar och blir Installerat.
        test_link = urlsplit(site_snippet.install_url(site))
        install = parse_qs(test_link.query)["adx"][0]
        self.beacon(site, install, origin=f"https://{SITE}", p="/")
        self.assertTrue(site.is_installed)
        self.assertFalse(Event.objects.filter(kind=Event.SITE_VISIT).exists())
        page = client.get(reverse("flamingo:app_utskick_snippet"))
        self.assertContains(page, site_snippet.INSTALLED_LABEL)

        # Efter: Bos klick får adx= med sitt klick, och besöket från en
        # underdomän räknas på klicket och på Bos kontaktkort.
        after = self.click_code(PHONE_BO)
        adx = query(after)["adx"]
        click = Click.objects.get(utskick=utskick, contact=self.people["Bo"])
        self.assertEqual(tokens.read_adx(adx), click.pk)
        self.beacon(site, adx, origin="https://boka.exempelror.example", s=0)
        # Besökaren stannar en stund; skriptet skickar tiden när sidan lämnas
        # (en skrivning per klick och 10 s, som på landningssidan).
        Click.objects.filter(utskick=utskick, contact=self.people["Bo"]).update(
            beacon_at=timezone.now() - timedelta(seconds=35)
        )
        self.beacon(site, adx, origin="https://boka.exempelror.example", s=35, v=0)
        click.refresh_from_db()
        self.assertEqual((click.lp_visits, click.engaged_seconds), (1, 35))
        event = Event.objects.get(kind=Event.SITE_VISIT)
        self.assertEqual(event.contact, self.people["Bo"])
        self.assertEqual(event.utskick, utskick)
        self.assertEqual(event.data["sida"], "/boka")
        card = client.get(reverse("flamingo:app_contact", args=[self.people["Bo"].pk]))
        self.assertContains(card, "Besökte webbplatsen")
        visited = segments.contacts(
            self.account, {"all": [{"f": "visited_lp", "op": "within_days", "v": 7}]}
        )
        self.assertEqual(list(visited), [self.people["Bo"]])

        # En annan Origin, en annan kunds klick eller utan Origin: ingenting.
        cilla = self.click_code(PHONE_CILLA)
        cilla_token = query(cilla)["adx"]
        self.beacon(site, cilla_token, origin="https://annan.example")
        self.beacon(site, cilla_token, origin="null")
        other_site = SiteSnippet.objects.create(account=self.other_account, domain=SITE)
        self.beacon(other_site, cilla_token)
        self.assertEqual(Event.objects.filter(kind=Event.SITE_VISIT).count(), 1)
        self.assertFalse(other_site.is_installed)


# ---------------------------------------------------------------------------
# Kundkortet: adressen bär de namngivna länkarna
# ---------------------------------------------------------------------------


class CustomerCardTests(S4FlowFixture, TestCase):
    def post(self, **data):
        client = Client()
        client.force_login(self.staff)
        values = {
            "is_enabled": "on",
            "display_name": "Exempelrör",
            "public_slug": "exempelror",
            "contact_limit": "25000",
            "email_daily_cap": "0",
        }
        values.update(data)
        return client.post(
            reverse("manage:utskick_customer_update", args=[self.customer.pk]), values
        )

    def slug(self):
        return UtskickSettings.objects.get(account=self.account).public_slug

    def test_a_new_address_without_named_links_is_saved(self):
        self.post(public_slug="exempelror-ab")
        self.assertEqual(self.slug(), "exempelror-ab")

    def test_named_links_need_the_box_before_the_address_changes(self):
        TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.EXTERNAL,
            destination=f"https://{SITE}/vinter",
            label="Affisch",
            slug="vinter",
        )
        client = Client()
        client.force_login(self.staff)
        card = client.get(reverse("manage:customer_detail", args=[self.customer.pk]))
        self.assertContains(card, "Kunden har en namngiven länk på klick.adx.se/exempelror/.")
        self.assertContains(card, 'name="slug_confirm"')
        response = self.post(public_slug="exempelror-ab")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.slug(), "exempelror")
        # Samma adress sparas utan rutan, och med rutan byts den.
        self.post(display_name="Exempelrör i Nacka")
        self.assertEqual(self.slug(), "exempelror")
        self.post(public_slug="exempelror-ab", slug_confirm="on")
        self.assertEqual(self.slug(), "exempelror-ab")

    def test_the_old_address_follows_the_customer_and_is_never_reused(self):
        # Säkerhetsgranskningen: en frigjord adress kunde bli en annan kunds,
        # och tryckta affischer och QR-koder följde då med till den kunden.
        link = TrackedLink.objects.create(
            account=self.account,
            kind=TrackedLink.Kind.EXTERNAL,
            destination=f"https://{SITE}/vinter",
            label="Affisch",
            slug="vinter",
        )
        TrackedLink.objects.create(
            account=self.other_account,
            kind=TrackedLink.Kind.EXTERNAL,
            destination="https://annanfirma.example/vinter",
            label="Deras affisch",
            slug="vinter",
        )
        self.post(public_slug="exempelror-ab", slug_confirm="on")
        self.assertEqual(self.slug(), "exempelror-ab")
        old = OldPublicSlug.objects.get(slug="exempelror")
        self.assertEqual(old.account, self.account)

        # Den tryckta adressen går fortfarande till samma kunds länk.
        visitor = Client()
        response = visitor.get("/exempelror/vinter", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(f"https://{SITE}/vinter"))
        self.assertEqual(Click.objects.get(link=link).account, self.account)
        folded = visitor.get("/Exempelror/Vinter", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual((folded.status_code, folded["Location"]), (301, "/exempelror/vinter"))
        # Anmälan, tack-sidan och integritetstexten (QR-koden till anmälan).
        from .public_views import _row_for_slug

        self.assertEqual(_row_for_slug("exempelror").account_id, self.account.pk)

        # Ingen annan kund kan få den, och förslaget hoppar över den.
        from django.core.exceptions import ValidationError

        from .access import suggest_public_slug, validate_public_slug

        with self.assertRaises(ValidationError):
            validate_public_slug("exempelror", exclude_pk=self.other_settings.pk)
        self.assertEqual(
            suggest_public_slug("Exempelror", account_id=self.other_account.pk), "exempelror-2"
        )
        client = Client()
        client.force_login(self.staff)
        card = client.get(reverse("manage:customer_detail", args=[self.other_customer.pk]))
        self.assertEqual(card.status_code, 200)
        client.post(
            reverse("manage:utskick_customer_update", args=[self.other_customer.pk]),
            {"is_enabled": "on", "display_name": "Annanfirma", "public_slug": "exempelror"},
        )
        self.other_settings.refresh_from_db()
        self.assertEqual(self.other_settings.public_slug, "annanfirma")

        # Kunden själv får ta tillbaka den, och raden går.
        self.post(public_slug="exempelror", slug_confirm="on")
        self.assertEqual(self.slug(), "exempelror")
        self.assertEqual(
            set(OldPublicSlug.objects.values_list("slug", "account")),
            {("exempelror-ab", self.account.pk)},
        )

    def test_an_old_address_of_a_deleted_account_stays_taken(self):
        OldPublicSlug.objects.create(account=self.other_account, slug="gammal-firma")
        self.other_account.delete()
        row = OldPublicSlug.objects.get(slug="gammal-firma")
        self.assertIsNone(row.account_id)
        from django.core.exceptions import ValidationError

        from .access import account_for_public_slug, validate_public_slug

        with self.assertRaises(ValidationError):
            validate_public_slug("gammal-firma", exclude_pk=self.settings.pk)
        self.assertIsNone(account_for_public_slug("gammal-firma"))
        response = Client().get("/gammal-firma/vinter", HTTP_USER_AGENT=IPHONE, **KLICK)
        self.assertEqual(response.status_code, 404)
