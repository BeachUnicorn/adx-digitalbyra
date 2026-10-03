"""ADX Flamingo, kampanjerna (kundresan 06-08): generatorn, kontrollerna och
flödet utkast -> granskning -> godkännande, med kundens gränser.

Inget test här får nå en riktig modell: AI-vägen körs med en låtsad
llm.call, och alla andra test stänger av AI (NoAI)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.assistant import llm
from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import checks, generator
from .models import (
    DESCRIPTION_COUNT,
    DESCRIPTION_MAX,
    HEADLINE_COUNT,
    HEADLINE_MAX,
    MATCH_EXACT,
    MATCH_PHRASE,
    Campaign,
    Fact,
    FlamingoAccount,
    Review,
    Service,
)

User = get_user_model()
AGENCY = "byran@example.com"
PHONE = "08-000 00 00"


class NoAI:
    """AI avstängd: generatorn skriver med mallar och anropar aldrig modellen."""

    def setUp(self):
        super().setUp()
        for target, kwargs in (
            ("apps.assistant.llm.is_configured", {"return_value": False}),
            ("apps.assistant.llm.call", {"side_effect": AssertionError("AI ska inte anropas")}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)


class CampaignFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            "byra", password="x12345678", is_staff=True, first_name="Elin"
        )
        cls.acme = Customer.objects.create(name="Lindqvist Rör AB", email="info@lindqvistror.se")
        cls.other = Customer.objects.create(name="Hemlig Bygg AB")
        cls.anna = User.objects.create_user(
            "anna@ror.se", email="anna@ror.se", password="x", first_name="Anna"
        )
        cls.acme.users.add(cls.anna)
        cls.bo = User.objects.create_user("bo@hemlig.se", email="bo@hemlig.se", password="x")
        cls.other.users.add(cls.bo)

        cls.account = FlamingoAccount.objects.create(customer=cls.acme, is_enabled=True)
        cls.other_account = FlamingoAccount.objects.create(customer=cls.other, is_enabled=True)

        facts = [
            # key, label, value, confirmed
            ("telefon", "Telefon", PHONE, True),
            ("jour", "Jour", "Dygnet runt, alla dagar", True),
            ("betyg", "Betyg på Google", "4,8", True),
            ("omrade", "Område", "Nacka, Värmdö och Tyresö", True),
            ("pris-badrum", "Pris, badrum", "Från 4 990 kr per kvadratmeter", False),
            ("garanti", "Garanti", "Fem års garanti på allt", False),
        ]
        for order, (key, label, value, confirmed) in enumerate(facts):
            Fact.objects.create(
                account=cls.account,
                key=key,
                label=label,
                value=value,
                confirmed=confirmed,
                order=order,
                # Betyget kommer från Google: bara då får annonsen säga det.
                source=Fact.SOURCE_GOOGLE if key == "betyg" else Fact.SOURCE_CUSTOMER,
            )
        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL, order=1
        )
        cls.badrum = Service.objects.create(
            account=cls.account, name="Badrumsrenovering", sales_mode=Service.SALES_QUOTE, order=2
        )
        cls.puts = Service.objects.create(
            account=cls.account, name="Fönsterputs", sales_mode=Service.SALES_BOOK, order=3
        )
        cls.secret_service = Service.objects.create(account=cls.other_account, name="Takbyte")
        cls.secret = Campaign.objects.create(
            account=cls.other_account,
            service=cls.secret_service,
            name="Hemlig kampanj",
            area="Solna + 10 km",
            headlines=["Takbyte i Solna", "Nytt tak", "Ring Hemlig Bygg"],
            descriptions=["Takbyte i Solna.", "Ring oss."],
            keywords=[{"text": "takbyte solna", "match": "phrase"}],
        )

    def client_for(self, user, view_as=None):
        client = Client()
        client.force_login(user)
        if view_as is not None:
            session = client.session
            session[VIEW_AS_KEY] = view_as.pk
            session.save()
        return client

    def make_campaign(self, service=None, area="Nacka + 15 km", **fields):
        return Campaign.objects.create(
            account=self.account,
            service=service or self.badrum,
            name=fields.pop("name", "Badrumsrenovering Nacka"),
            area=area,
            daily_budget_kr=fields.pop("daily_budget_kr", 200),
            **fields,
        )

    def proposal_for(self, service=None, area="Nacka + 15 km"):
        campaign = self.make_campaign(service=service, area=area)
        return campaign, generator.build_proposal(campaign)

    def url(self, campaign, tab=""):
        url = reverse("flamingo:app_campaign", args=[campaign.pk])
        return f"{url}?flik={tab}" if tab else url


# ---------------------------------------------------------------------------
# Generatorn, mallvägen
# ---------------------------------------------------------------------------


class GeneratorTemplateTests(NoAI, CampaignFixture, TestCase):
    def test_every_sales_mode_respects_googles_limits_and_passes_the_checks(self):
        for service in (self.jour, self.badrum, self.puts):
            with self.subTest(service=service.name):
                campaign, proposal = self.proposal_for(service)
                campaign.refresh_from_db()
                self.assertEqual(proposal.source, generator.SOURCE_TEMPLATES)
                self.assertIn("mallar", proposal.note)
                self.assertGreaterEqual(len(campaign.headlines), checks.HEADLINE_MIN)
                self.assertLessEqual(len(campaign.headlines), HEADLINE_COUNT)
                self.assertGreaterEqual(len(campaign.descriptions), checks.DESCRIPTION_MIN)
                self.assertLessEqual(len(campaign.descriptions), DESCRIPTION_COUNT)
                self.assertTrue(all(len(h) <= HEADLINE_MAX for h in campaign.headlines))
                self.assertTrue(all(len(d) <= DESCRIPTION_MAX for d in campaign.descriptions))
                lowered = [h.lower() for h in campaign.headlines]
                self.assertEqual(len(lowered), len(set(lowered)))
                self.assertEqual(checks.validate(campaign), [])

    def test_only_confirmed_facts_are_used(self):
        for service in (self.jour, self.badrum, self.puts):
            with self.subTest(service=service.name):
                campaign, _ = self.proposal_for(service)
                campaign.refresh_from_db()
                text = json.dumps(campaign.content_snapshot(), ensure_ascii=False).lower()
                self.assertNotIn("4 990", text)
                self.assertNotIn("4990", text)
                self.assertNotIn("kvadratmeter", text)
                self.assertNotIn("garanti", text)
                self.assertEqual(campaign.page["phone"], PHONE)
                self.assertIn("4,8 i betyg på google", text)

    def test_an_unconfirmed_phone_is_never_used(self):
        Fact.objects.filter(account=self.account, key="telefon").update(confirmed=False)
        campaign, _ = self.proposal_for(self.jour)
        campaign.refresh_from_db()
        self.assertEqual(campaign.page["phone"], "")
        self.assertNotIn("08-000", json.dumps(campaign.content_snapshot()))
        self.assertEqual(checks.validate(campaign), [])

    def test_keywords_are_service_variants_times_places(self):
        campaign, _ = self.proposal_for(self.badrum, area="Nacka, Värmdö + 15 km")
        keywords = campaign.keywords
        phrases = {k["text"] for k in keywords if k["match"] == MATCH_PHRASE}
        exact = {k["text"] for k in keywords if k["match"] == MATCH_EXACT}
        for text in (
            "badrumsrenovering nacka",
            "renovera badrum nacka",
            "badrumsrenovering värmdö",
            "renovera badrum värmdö",
            "badrumsrenovering offert",
        ):
            self.assertIn(text, phrases)
        self.assertEqual(exact, {"badrumsrenovering nacka", "badrumsrenovering värmdö"})

    def test_negatives_are_the_standard_list_plus_the_sales_mode(self):
        campaign, _ = self.proposal_for(self.badrum)
        for word in ("jobb", "lön", "utbildning", "kurs", "praktik", "gratis", "gör det själv"):
            self.assertIn(word, campaign.negatives)
        for word in ("diy", "begagnad", "wiki", "manual", "pdf", "blocket"):
            self.assertIn(word, campaign.negatives)
        call, _ = self.proposal_for(self.jour)
        self.assertIn("hur gör man", call.negatives)
        self.assertNotIn("blocket", call.negatives)

    def test_a_confirmed_free_offer_keeps_gratis_searches(self):
        Fact.objects.create(
            account=self.account,
            key="hembesok",
            label="Hembesök",
            value="Gratis hembesök",
            confirmed=True,
        )
        campaign, _ = self.proposal_for(self.badrum)
        self.assertNotIn("gratis", campaign.negatives)

    def test_a_negative_never_blocks_the_campaigns_own_keywords(self):
        kurs = Service.objects.create(account=self.account, name="Kurs i kakelsättning")
        campaign, _ = self.proposal_for(kurs)
        self.assertNotIn("kurs", campaign.negatives)
        self.assertFalse([p for p in checks.validate(campaign) if p.field == "keywords"])

    def test_the_page_follows_the_sales_mode(self):
        call, _ = self.proposal_for(self.jour)
        self.assertEqual(call.page["title"], "Rörjour i Nacka")
        self.assertEqual(call.page["questions"], [])
        self.assertTrue(call.page["form_title"])
        # Landningssidan visar note som "Medan du väntar" i ringläget.
        self.assertEqual(call.page["note"], "")
        self.assertIn("Jour: Dygnet runt, alla dagar", call.page["points"])
        # Betyget ritar landningssidan själv ur uppgifterna.
        self.assertFalse([p for p in call.page["points"] if "4,8" in p])

        quote, _ = self.proposal_for(self.badrum)
        self.assertEqual(quote.page["form_title"], "Beskriv jobbet")
        self.assertEqual([q["key"] for q in quote.page["questions"]], ["jobbet", "storlek"])
        self.assertNotIn("call_label", call.page)

        book, _ = self.proposal_for(self.puts)
        self.assertEqual(book.page["title"], "Boka fönsterputs i Nacka")
        self.assertIn("date", [q["kind"] for q in book.page["questions"]])
        for page in (call.page, quote.page, book.page):
            self.assertEqual(page["phone"], PHONE)

    def test_service_names_and_places(self):
        self.assertEqual(
            generator.service_variants("Badrumsrenovering"),
            ["badrumsrenovering", "renovera badrum"],
        )
        self.assertEqual(
            generator.service_variants("Byte av varmvattenberedare"),
            ["byte av varmvattenberedare", "byta varmvattenberedare"],
        )
        self.assertEqual(generator.service_variants("Takbyte"), ["takbyte", "byta tak"])
        self.assertEqual(generator.service_variants("Rörjour"), ["rörjour"])
        self.assertEqual(
            generator.places_of("Nacka, Värmdö och Tyresö + 15 km"), ["Nacka", "Värmdö", "Tyresö"]
        )
        self.assertEqual(generator.place_of("Nacka + 15 km"), "Nacka")
        self.assertEqual(
            generator.company_name(Customer(name="Lindqvist Rör AB (demo)")), "Lindqvist Rör"
        )


# ---------------------------------------------------------------------------
# Generatorn, AI-vägen (låtsad modell)
# ---------------------------------------------------------------------------


def _ai_response(headlines, descriptions, name=generator.TOOL_NAME):
    block = SimpleNamespace(
        type="tool_use", name=name, input={"rubriker": headlines, "beskrivningar": descriptions}
    )
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="Här"), block])


class GeneratorAITests(CampaignFixture, TestCase):
    def setUp(self):
        patcher = patch("apps.assistant.llm.is_configured", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ai_texts_are_used_and_filtered_by_the_checks(self):
        response = _ai_response(
            [
                "Rörjour i Nacka dygnet runt",
                "Billigast i Nacka",
                "Från 499 kr per timme",
                "En rubrik som är alldeles för lång för Google",
                "Ring Lindqvist Rör",
                "ring lindqvist rör",
                "På plats inom 30 minuter",
                "Vi garanterar nöjda kunder",
                "4,8 i betyg på Google",
            ],
            [
                "Vattenläcka i Nacka? Ring Lindqvist Rör på 08-000 00 00.",
                "Gratis offert och fast pris från 999 kr.",
                "Jouren är öppen dygnet runt, alla dagar.",
            ],
        )
        with patch("apps.assistant.llm.call", return_value=response) as call:
            campaign, proposal = self.proposal_for(self.jour)
        self.assertEqual(proposal.source, generator.SOURCE_AI)
        campaign.refresh_from_db()
        headlines, descriptions = campaign.headlines, campaign.descriptions
        self.assertEqual(
            headlines[:3],
            ["Rörjour i Nacka dygnet runt", "Ring Lindqvist Rör", "4,8 i betyg på Google"],
        )
        for bad in (
            "Billigast i Nacka",
            "Från 499 kr per timme",
            "En rubrik som är alldeles för lång för Google",
            "ring lindqvist rör",
            "På plats inom 30 minuter",
            "Vi garanterar nöjda kunder",
        ):
            self.assertNotIn(bad, headlines)
        self.assertEqual(
            descriptions[0], "Vattenläcka i Nacka? Ring Lindqvist Rör på 08-000 00 00."
        )
        self.assertNotIn("Gratis offert och fast pris från 999 kr.", descriptions)
        self.assertEqual(checks.validate(campaign), [])

        # Ett anrop, ett verktyg, och bara bekräftade uppgifter i indata.
        call.assert_called_once()
        kwargs = call.call_args.kwargs
        self.assertEqual([t["name"] for t in kwargs["tools"]], [generator.TOOL_NAME])
        self.assertIn("bara", kwargs["system"])
        sent = kwargs["messages"][0]["content"]
        payload = json.loads(sent)
        self.assertEqual(payload["tjänst"], "Rörjour")
        self.assertEqual(payload["orter"], ["Nacka"])
        self.assertIn(PHONE, sent)
        self.assertNotIn("4 990", sent)
        self.assertNotIn("garanti", sent.lower())

    def test_a_failing_model_falls_back_to_the_templates(self):
        with patch("apps.assistant.llm.call", side_effect=llm.ModelUnavailable("nere")):
            campaign, proposal = self.proposal_for(self.badrum)
        self.assertEqual(proposal.source, generator.SOURCE_TEMPLATES)
        self.assertIn("AI svarade inte", proposal.note)
        self.assertEqual(checks.validate(campaign), [])

    def test_the_daily_budget_stops_the_call_before_it_is_made(self):
        with (
            patch("apps.assistant.llm.check_budget", side_effect=llm.BudgetExceeded("slut")),
            patch("apps.assistant.llm.call") as call,
        ):
            _, proposal = self.proposal_for(self.badrum)
        call.assert_not_called()
        self.assertEqual(proposal.source, generator.SOURCE_TEMPLATES)
        self.assertIn("budget", proposal.note)

    def test_an_answer_without_the_tool_or_with_only_bad_texts_uses_templates(self):
        for response in (
            SimpleNamespace(content=[SimpleNamespace(type="text", text="Jag kan inte.")]),
            _ai_response(["Billigast i stan", "Bäst i Nacka"], ["Fast pris 999 kr."]),
            _ai_response(["Rörjour"], ["Ring oss."], name="annat_verktyg"),
        ):
            with (
                self.subTest(response=response),
                patch("apps.assistant.llm.call", return_value=response),
            ):
                campaign, proposal = self.proposal_for(self.jour)
                self.assertEqual(proposal.source, generator.SOURCE_TEMPLATES)
                self.assertTrue(proposal.note)
                self.assertEqual(checks.validate(campaign), [])

    def test_ai_typography_is_normalised(self):
        dash = chr(0x2013)
        response = _ai_response(
            [f"Rörjour {dash} Nacka", "Ring Lindqvist Rör", "Jour i Nacka"],
            [f"Vattenläcka? Ring oss {dash} jouren är öppen dygnet runt.", "Ring Lindqvist Rör."],
        )
        with patch("apps.assistant.llm.call", return_value=response):
            campaign, _ = self.proposal_for(self.jour)
        self.assertIn("Rörjour - Nacka", campaign.headlines)
        self.assertNotIn(dash, json.dumps(campaign.content_snapshot(), ensure_ascii=False))


# ---------------------------------------------------------------------------
# Kontrollerna
# ---------------------------------------------------------------------------


class ChecksTests(NoAI, CampaignFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.campaign = self.make_campaign(
            headlines=["Badrumsrenovering i Nacka", "Nytt badrum i Nacka", "Begär en offert"],
            descriptions=[
                "Berätta om jobbet så återkommer vi med en offert.",
                f"Ring Lindqvist Rör på {PHONE}. 4,8 i betyg på Google.",
            ],
            keywords=[{"text": "badrumsrenovering nacka", "match": "phrase"}],
            negatives=["jobb", "gör det själv"],
            page={"title": "Badrumsrenovering i Nacka", "phone": PHONE, "points": []},
        )

    def messages(self, field=None, **changes):
        for name, value in changes.items():
            setattr(self.campaign, name, value)
        problems = checks.validate(self.campaign)
        return [p.message for p in problems if field is None or p.field == field]

    def headline(self, text):
        return self.messages("headlines", headlines=["Badrum i Nacka", "Nytt badrum", text])

    def test_a_good_campaign_passes(self):
        self.assertEqual(self.messages(), [])

    def test_character_limits(self):
        problems = checks.validate(
            Campaign(
                **{
                    **{f: getattr(self.campaign, f) for f in ("account", "service", "area")},
                    "daily_budget_kr": 200,
                    "headlines": ["x" * 31, "Nytt badrum", "Begär en offert"],
                    "descriptions": ["y" * 91, "Berätta om jobbet."],
                    "keywords": self.campaign.keywords,
                    "page": self.campaign.page,
                }
            )
        )
        self.assertEqual(
            [(p.field, p.index) for p in problems], [("headlines", 0), ("descriptions", 0)]
        )
        self.assertIn("31 tecken", problems[0].message)
        self.assertIn("91 tecken", problems[1].message)

    def test_duplicates_and_counts(self):
        dup = self.messages("headlines", headlines=["Nytt badrum", "nytt  badrum", "Badrum"])
        self.assertTrue(any("Samma rubrik" in m for m in dup))
        self.assertTrue(
            any("Minst 3" in m for m in self.messages("headlines", headlines=["Ett", "Två"]))
        )
        self.assertTrue(
            any(
                "Högst 15" in m
                for m in self.messages(
                    "headlines", headlines=[f"Rubrik {chr(65 + i)}" for i in range(16)]
                )
            )
        )
        self.assertTrue(
            any("Minst 2" in m for m in self.messages("descriptions", descriptions=["En."]))
        )
        dup_d = self.messages("descriptions", descriptions=["Samma text.", "samma text."])
        self.assertTrue(any("Samma beskrivning" in m for m in dup_d))

    def test_numbers_must_come_from_confirmed_facts(self):
        self.assertEqual(self.headline("4,8 i betyg på Google"), [])
        self.assertEqual(self.headline("Ring 08 000 00 00"), [])
        self.assertEqual(self.headline("Ring 080000000"), [])
        self.assertEqual(self.headline("Ungefär hur stort, m2?"), [])
        self.assertIn("Siffran 499 finns inte", " ".join(self.headline("Från 499 kr")))
        self.assertTrue(self.headline("Ring 070-123 45 67"))
        self.assertTrue(self.headline("Jour 24/7"))
        # Det obekräftade priset räknas inte som en uppgift.
        self.assertTrue(self.headline("Från 4 990 kr"))

    def test_claims_that_cannot_be_backed_up_are_stopped(self):
        for text in (
            "Billigast i Nacka",
            "Billigaste badrummet",
            "Bäst i stan",
            "Bästa pris i Nacka",
            "Snabbast på plats",
            "Där inom en timme",
            "Klart inom ett par dagar",
            "Hos dig inom 2-3 dagar",
            "Svar " + "inom " + "24 timmar",
            "Vi kommer inom 24h",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.headline(text))
        for text in ("Inom Nacka kommun", "Badrum inom budget", "Renovering dagtid"):
            with self.subTest(text=text):
                self.assertEqual(self.headline(text), [])

    def test_guarantees_and_free_need_a_confirmed_fact(self):
        self.assertTrue(self.headline("Garanti på jobbet"))
        self.assertTrue(self.headline("Vi garanterar kvalitet"))
        self.assertTrue(self.headline("Gratis hembesök"))
        self.assertTrue(self.headline("Kostnadsfri offert"))
        self.assertTrue(self.headline("Vi kommer samma dag"))
        # Den obekräftade garantin räcker inte; en bekräftad gör det.
        Fact.objects.filter(account=self.account, key="garanti").update(confirmed=True)
        Fact.objects.create(
            account=self.account,
            key="hembesok",
            label="Hembesök",
            value="Gratis hembesök",
            confirmed=True,
        )
        self.assertEqual(self.headline("Garanti på jobbet"), [])
        self.assertEqual(self.headline("Gratis hembesök"), [])
        # Öppettiderna finns bland uppgifterna.
        self.assertEqual(self.headline("Jour dygnet runt"), [])

    def test_keywords(self):
        self.assertTrue(
            any("minst ett sökord" in m for m in self.messages("keywords", keywords=[]))
        )
        self.assertTrue(self.messages("keywords", keywords=[{"text": "  ", "match": "phrase"}]))
        clash = self.messages(
            "keywords",
            keywords=[{"text": "jobb badrum nacka", "match": "phrase"}],
            negatives=["jobb"],
        )
        self.assertTrue(any("krockar" in m for m in clash))
        long = self.messages("keywords", keywords=[{"text": "a " * 11, "match": "phrase"}])
        self.assertTrue(any("fler än 10 ord" in m for m in long))

    def test_budget_and_area(self):
        for budget, ok in ((49, False), (50, True), (5000, True), (5001, False), (0, False)):
            with self.subTest(budget=budget):
                found = self.messages("daily_budget_kr", daily_budget_kr=budget)
                self.assertEqual(found == [], ok)
        self.assertTrue(self.messages("area", area=""))

    def test_the_page(self):
        page = dict(self.campaign.page)
        self.assertTrue(self.messages("page", page={**page, "title": ""}))
        self.assertTrue(self.messages("page", page={**page, "phone": "070-123 45 67"}))
        self.assertEqual(self.messages(page=page), [])
        self.campaign.page = {**page, "lead": "Billigast i Nacka", "points": ["Från 499 kr"]}
        parts = {(p.part, p.index) for p in checks.validate(self.campaign)}
        self.assertEqual(parts, {("lead", None), ("points", 0)})


# ---------------------------------------------------------------------------
# Flödet i verktyget
# ---------------------------------------------------------------------------


@override_settings(INQUIRY_NOTIFICATION_EMAIL=AGENCY)
class FlowTests(NoAI, CampaignFixture, TestCase):
    def create_via_form(self, client, **data):
        payload = {
            "service": str(self.badrum.pk),
            "sales_mode": Service.SALES_QUOTE,
            "place": "Nacka",
            "radius_km": "15",
            "budget": "200",
            "budget_own": "",
        }
        payload.update(data)
        return client.post(reverse("flamingo:app_campaign_new"), payload)

    def test_draft_submit_review_approve(self):
        client = self.client_for(self.anna)
        self.assertEqual(client.get(reverse("flamingo:app_campaign_new")).status_code, 200)
        response = self.create_via_form(client)
        campaign = Campaign.objects.get(account=self.account)
        self.assertRedirects(response, self.url(campaign, "annonser"))
        self.assertEqual(campaign.status, Campaign.STATUS_DRAFT)
        self.assertEqual(campaign.name, "Badrumsrenovering Nacka")
        self.assertEqual(campaign.area, "Nacka + 15 km")
        self.assertEqual(campaign.daily_budget_kr, 200)
        self.assertEqual(campaign.created_by, self.anna)
        self.assertTrue(campaign.headlines)
        self.assertEqual(checks.validate(campaign), [])
        for tab in ("annonser", "sokord", "sidan", "granskning"):
            with self.subTest(tab=tab):
                page = client.get(self.url(campaign, tab))
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, "[ ")
        page = client.get(self.url(campaign))
        self.assertContains(page, "Skicka till granskning")
        # Granskningen är kundens val, och rutan är tom från början.
        self.assertContains(page, "Jag vill att ADX granskar kampanjen innan den publiceras")
        self.assertContains(page, '<input type="checkbox" name="review" value="1">', html=False)

        # Skicka med rutan ibockad: en runda hos ADX, ett larm till byrån och
        # inget till kunden.
        submit = reverse("flamingo:app_campaign_submit", args=[campaign.pk])
        response = client.post(submit, {"review": "1"})
        self.assertRedirects(response, self.url(campaign, "granskning"))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.STATUS_IN_REVIEW)
        self.assertTrue(campaign.review_requested)
        review = campaign.reviews.get()
        self.assertEqual((review.round, review.state), (1, Review.STATE_PENDING))
        self.assertEqual(review.submitted_by, self.anna)
        self.assertEqual(review.snapshot, json.loads(json.dumps(campaign.content_snapshot())))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [AGENCY])
        self.assertIn(reverse("manage:flamingo_review", args=[campaign.pk]), mail.outbox[0].body)

        # Låst medan ADX granskar: ingen ändring, inget nytt inskick.
        before = list(campaign.headlines)
        client.post(self.url(campaign), {"section": "ads", "headline": ["Ny", "Nyare", "Nyast"]})
        client.post(submit)
        client.post(reverse("flamingo:app_campaign_approve", args=[campaign.pk]))
        campaign.refresh_from_db()
        self.assertEqual(campaign.headlines, before)
        self.assertEqual(campaign.status, Campaign.STATUS_IN_REVIEW)
        self.assertIsNone(campaign.approved_at)
        self.assertEqual(campaign.reviews.count(), 1)
        locked = client.get(self.url(campaign, "annonser"))
        self.assertNotContains(locked, 'name="section"')
        self.assertContains(locked, "ADX granskar förslaget")

        # ADX granskar klart (byråns sida är ett annat arbete): simuleras här.
        review.state = Review.STATE_DONE
        review.reviewer = self.staff
        review.reviewed_at = timezone.now()
        review.note = "En ändring. Godkänn om den ser rätt ut."
        review.changes = [
            {
                "field": "headlines",
                "label": "Rubrik",
                "before": "Badrum Nacka dygnet runt",
                "after": "Badrumsrenovering i Nacka",
                "reason": "Jouren gäller rörjour, inte renoveringar.",
            },
            {
                "field": "negatives",
                "label": "Negativt sökord",
                "before": "",
                "after": ["badrumsmatta", "kakel billigt"],
                "reason": "",
            },
        ]
        review.save()
        Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.STATUS_NEEDS_CUSTOMER)

        page = client.get(self.url(campaign))
        self.assertContains(page, "<del>Badrum Nacka dygnet runt</del>", html=False)
        self.assertContains(page, "<ins>Badrumsrenovering i Nacka</ins>", html=False)
        self.assertContains(page, "<ins>+ badrumsmatta, kakel billigt</ins>", html=False)
        self.assertContains(page, "Jouren gäller rörjour, inte renoveringar.")
        self.assertContains(page, "Elin på ADX granskade")
        self.assertContains(page, "Det här ändrade Elin")
        self.assertContains(page, "Väntar på ditt godkännande")
        approve = reverse("flamingo:app_campaign_approve", args=[campaign.pk])
        self.assertContains(page, f'action="{approve}"')

        # Godkänn: inget publiceras, byrån publicerar.
        self.assertRedirects(client.post(approve), self.url(campaign, "granskning"))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.STATUS_NEEDS_CUSTOMER)
        self.assertEqual(campaign.approved_by, self.anna)
        approved_at = campaign.approved_at
        self.assertIsNotNone(approved_at)
        self.assertIsNone(campaign.published_at)
        self.assertContains(client.get(self.url(campaign)), "Godkänd av dig, ADX publicerar")
        client.post(approve)
        campaign.refresh_from_db()
        self.assertEqual(campaign.approved_at, approved_at)
        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertEqual(message.to, [AGENCY])
            self.assertFalse(message.cc or message.bcc)
        # Landningssidan är inte publik förrän byrån publicerat.
        self.assertEqual(Client().get(campaign.landing_url).status_code, 404)

    def test_an_edit_after_the_review_makes_a_new_draft(self):
        campaign, _ = self.proposal_for(self.badrum)
        Review.objects.create(campaign=campaign, round=1, state=Review.STATE_DONE)
        Campaign.objects.filter(pk=campaign.pk).update(
            status=Campaign.STATUS_NEEDS_CUSTOMER, approved_at=timezone.now(), approved_by=self.anna
        )
        client = self.client_for(self.anna)
        headlines = ["Badrum i Nacka", "Nytt badrum i Nacka", "Begär en offert"]
        client.post(
            self.url(campaign),
            {"section": "ads", "headline": headlines, "description": campaign.descriptions},
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.STATUS_DRAFT)
        self.assertIsNone(campaign.approved_at)
        self.assertIsNone(campaign.approved_by)
        self.assertEqual(campaign.headlines, headlines)
        # Nästa inskick med granskning blir runda 2.
        client.post(reverse("flamingo:app_campaign_submit", args=[campaign.pk]), {"review": "1"})
        self.assertEqual(campaign.reviews.order_by("-round").first().round, 2)

    def test_problems_block_the_submission(self):
        campaign = self.make_campaign(headlines=["Bara en"], descriptions=["Billigast i Nacka."])
        client = self.client_for(self.anna)
        response = client.post(reverse("flamingo:app_campaign_submit", args=[campaign.pk]))
        self.assertRedirects(response, self.url(campaign, "annonser"))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.STATUS_DRAFT)
        self.assertFalse(campaign.reviews.exists())
        self.assertEqual(mail.outbox, [])
        page = client.get(self.url(campaign, "annonser"))
        self.assertContains(page, "Rätta det här innan du skickar")
        self.assertContains(page, "Skriv inte att ni är billigast")
        self.assertNotContains(page, "Skicka till granskning")

    def test_another_customers_campaign_is_404(self):
        client = self.client_for(self.anna)
        for tab in ("", "annonser", "sokord", "sidan", "granskning"):
            with self.subTest(tab=tab):
                self.assertEqual(client.get(self.url(self.secret, tab)).status_code, 404)
        posts = [
            (self.url(self.secret), {"section": "ads", "headline": ["Hackad"]}),
            (self.url(self.secret), {"section": "regenerate"}),
            (reverse("flamingo:app_campaign_submit", args=[self.secret.pk]), {}),
            (reverse("flamingo:app_campaign_approve", args=[self.secret.pk]), {}),
        ]
        for url, data in posts:
            with self.subTest(url=url):
                self.assertEqual(client.post(url, data).status_code, 404)
        self.secret.refresh_from_db()
        self.assertEqual(self.secret.headlines[0], "Takbyte i Solna")
        self.assertFalse(self.secret.reviews.exists())
        # Någon annans tjänst går inte att välja, varken i adressen eller i formuläret.
        new = reverse("flamingo:app_campaign_new")
        self.assertEqual(client.get(f"{new}?tjanst={self.secret_service.pk}").status_code, 404)
        self.assertEqual(
            self.create_via_form(client, service=str(self.secret_service.pk)).status_code, 404
        )
        self.assertFalse(Campaign.objects.filter(account=self.account).exists())
        self.assertEqual(self.secret_service.campaigns.count(), 1)

    def test_staff_viewing_as_the_customer_reads_only(self):
        campaign, _ = self.proposal_for(self.badrum)
        client = self.client_for(self.staff, view_as=self.acme)
        page = client.get(self.url(campaign, "annonser"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="section"')
        self.assertNotContains(page, "Skicka till granskning")
        self.assertContains(page, reverse("manage:flamingo_review", args=[campaign.pk]))
        before = list(campaign.headlines)
        response = client.post(self.url(campaign), {"section": "ads", "headline": ["A", "B", "C"]})
        self.assertEqual(response.status_code, 302)
        submit = reverse("flamingo:app_campaign_submit", args=[campaign.pk])
        response = client.post(submit)
        # Grinden skickar tillbaka till adressen; en GET där visar kampanjen.
        self.assertRedirects(
            client.get(response["Location"]), self.url(campaign), fetch_redirect_response=False
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.headlines, before)
        self.assertEqual(campaign.status, Campaign.STATUS_DRAFT)
        self.assertNotContains(
            client.get(reverse("flamingo:app_campaign_new")), "Skapa förslag</button>"
        )

    def test_staff_without_view_as_gets_the_customer_list(self):
        campaign, _ = self.proposal_for(self.badrum)
        client = self.client_for(self.staff)
        for url in (
            reverse("flamingo:app_campaigns"),
            reverse("flamingo:app_campaign_new"),
            self.url(campaign),
        ):
            with self.subTest(url=url):
                self.assertTemplateUsed(client.get(url), "flamingo/app/staff_index.html")

    def test_new_service_and_sales_mode_live_on_the_service(self):
        client = self.client_for(self.anna)
        page = client.get(reverse("flamingo:app_campaign_new") + "?tjanst=ny")
        self.assertContains(page, 'name="new_service"')
        self.create_via_form(
            client, service="ny", new_service="Takläggning", sales_mode=Service.SALES_BOOK
        )
        service = Service.objects.get(account=self.account, name="Takläggning")
        self.assertEqual(service.sales_mode, Service.SALES_BOOK)
        self.assertEqual(Campaign.objects.get(service=service).sales_mode, Service.SALES_BOOK)

        self.create_via_form(client, sales_mode=Service.SALES_BOOK)
        self.badrum.refresh_from_db()
        self.assertEqual(self.badrum.sales_mode, Service.SALES_BOOK)

        # Med en kampanj hos ADX eller live byts sättet inte härifrån.
        Campaign.objects.filter(service=self.badrum).update(status=Campaign.STATUS_LIVE)
        count = Campaign.objects.count()
        response = self.create_via_form(client, sales_mode=Service.SALES_CALL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Be ADX ändra")
        self.assertEqual(Campaign.objects.count(), count)
        self.badrum.refresh_from_db()
        self.assertEqual(self.badrum.sales_mode, Service.SALES_BOOK)

    def test_the_form_preselects_the_services_sales_mode(self):
        client = self.client_for(self.anna)
        page = client.get(f"{reverse('flamingo:app_campaign_new')}?tjanst={self.jour.pk}")
        self.assertContains(page, 'value="call" checked', html=False)
        self.assertContains(page, 'value="Nacka, Värmdö och Tyresö"', html=False)
        self.assertNotContains(page, "förfrågningar i månaden")
        self.assertContains(page, "två veckor")
        self.assertContains(page, "ungefär <b>6\u00a0080\u00a0kr i månaden</b>", html=False)

    def test_budget(self):
        client = self.client_for(self.anna)
        response = self.create_via_form(client, budget="", budget_own="49")
        self.assertContains(response, "Lägsta budget är 50 kr per dag")
        response = self.create_via_form(client, budget="", budget_own="")
        self.assertContains(response, "Välj en budget per dag")
        self.assertFalse(Campaign.objects.filter(account=self.account).exists())
        self.create_via_form(client, budget="150", budget_own="300")
        self.assertEqual(Campaign.objects.get(account=self.account).daily_budget_kr, 300)

    def test_editing_keywords_page_and_settings(self):
        campaign, _ = self.proposal_for(self.badrum)
        client = self.client_for(self.anna)
        client.post(
            self.url(campaign),
            {
                "section": "keywords",
                "kw_text": ['"Rörjour Nacka"', "", "[akut rörjour]", "rörjour nacka"],
                "kw_match": ["phrase", "exact", "exact", "phrase"],
                "negatives": "jobb\nJobb\n\nlön",
            },
        )
        campaign.refresh_from_db()
        self.assertEqual(
            campaign.keywords,
            [
                {"text": "rörjour nacka", "match": "phrase"},
                {"text": "akut rörjour", "match": "exact"},
            ],
        )
        self.assertEqual(campaign.negatives, ["jobb", "lön"])

        client.post(
            self.url(campaign),
            {
                "section": "page",
                "title": "Nytt badrum i Nacka",
                "lead": "Berätta om badrummet.",
                "points": "Jour: Dygnet runt\n\nNacka och Värmdö",
                "phone": PHONE,
                "form_title": "Om badrummet",
                "q_label": ["Hur stort?", "", "Hur stort?"],
                "q_kind": ["text", "text", "date"],
                "note": "",
            },
        )
        campaign.refresh_from_db()
        self.assertEqual(campaign.page["title"], "Nytt badrum i Nacka")
        self.assertEqual(campaign.page["points"], ["Jour: Dygnet runt", "Nacka och Värmdö"])
        self.assertEqual(
            campaign.page["questions"],
            [
                {"key": "hur-stort", "label": "Hur stort?", "kind": "text"},
                {"key": "hur-stort-2", "label": "Hur stort?", "kind": "date"},
            ],
        )

        client.post(
            self.url(campaign),
            {"section": "settings", "place": "Värmdö", "radius_km": "25", "daily_budget_kr": "300"},
        )
        campaign.refresh_from_db()
        self.assertEqual(
            (campaign.area, campaign.radius_km, campaign.daily_budget_kr),
            ("Värmdö + 25 km", 25, 300),
        )

        client.post(self.url(campaign), {"section": "regenerate"})
        campaign.refresh_from_db()
        self.assertIn("badrumsrenovering värmdö", {k["text"] for k in campaign.keywords})
        self.assertEqual(campaign.page["title"], "Badrumsrenovering i Värmdö")

    def test_the_list(self):
        self.proposal_for(self.badrum)
        live = self.make_campaign(
            service=self.jour, name="Rörjour Nacka", status=Campaign.STATUS_LIVE
        )
        approved = self.make_campaign(
            service=self.puts,
            name="Fönsterputs Nacka",
            status=Campaign.STATUS_NEEDS_CUSTOMER,
            approved_at=timezone.now(),
            review_requested=True,
        )
        client = self.client_for(self.anna)
        page = client.get(reverse("flamingo:app_campaigns"))
        self.assertContains(page, "Utkast, inget publicerat")
        self.assertContains(page, "Godkänd av dig, ADX publicerar")
        # Skickad utan granskning: inskicket var godkännandet.
        Campaign.objects.filter(pk=approved.pk).update(review_requested=False)
        page = client.get(reverse("flamingo:app_campaigns"))
        self.assertContains(page, "Skickad av dig, ADX publicerar")
        self.assertContains(page, f'href="{live.landing_url}"')
        self.assertNotContains(page, approved.landing_url)
        self.assertContains(page, reverse("flamingo:app_campaign_new"))
        self.assertNotContains(page, "Hemlig")
        self.assertNotContains(page, "[ ")


@override_settings(INQUIRY_NOTIFICATION_EMAIL=AGENCY)
class ReviewAndPreviewTests(NoAI, CampaignFixture, TestCase):
    def test_changes_with_the_same_reason_are_grouped(self):
        campaign, _ = self.proposal_for(self.badrum)
        Review.objects.create(
            campaign=campaign,
            round=1,
            state=Review.STATE_DONE,
            reviewer=self.staff,
            reviewed_at=timezone.now(),
            changes=[
                {
                    "field": "negatives",
                    "part": "negatives",
                    "label": "Negativt sökord",
                    "before": "",
                    "after": "badrumsmatta",
                    "reason": "Fel sorts sökningar.",
                },
                {
                    "field": "negatives",
                    "part": "negatives",
                    "label": "Negativt sökord",
                    "before": "",
                    "after": "kakel billigt",
                    "reason": "Fel sorts sökningar.",
                },
            ],
        )
        Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.STATUS_NEEDS_CUSTOMER)
        html = self.client_for(self.anna).get(self.url(campaign, "granskning")).content.decode()
        self.assertIn("<ins>+ badrumsmatta</ins>", html)
        self.assertIn("<ins>+ kakel billigt</ins>", html)
        self.assertEqual(html.count("Fel sorts sökningar."), 1)
        self.assertIn("2 ändringar", html)

    def test_no_changes_says_so_once(self):
        """Inga ändringar: rubriken och en mening. En anteckning som bara
        upprepar det visas inte; en som säger något mer gör det."""
        campaign, _ = self.proposal_for(self.badrum)
        review = Review.objects.create(
            campaign=campaign,
            round=1,
            state=Review.STATE_DONE,
            reviewer=self.staff,
            reviewed_at=timezone.now(),
            note="Inget att ändra.",
        )
        Campaign.objects.filter(pk=campaign.pk).update(status=Campaign.STATUS_NEEDS_CUSTOMER)
        html = self.client_for(self.anna).get(self.url(campaign, "granskning")).content.decode()
        self.assertIn("Inga ändringar", html)
        self.assertIn("Elin på ADX gick igenom förslaget och ändrade inget.", html)
        self.assertNotIn("Inget att ändra.", html)
        self.assertNotIn("fl-camp-rvnote", html)
        Review.objects.filter(pk=review.pk).update(note="Bra texter, de får stå som de är.")
        html = self.client_for(self.anna).get(self.url(campaign, "granskning")).content.decode()
        self.assertIn('<p class="fl-camp-rvnote">Bra texter, de får stå som de är.</p>', html)

    def test_the_page_tab_shows_the_landing_page_template_in_a_sandbox(self):
        campaign, _ = self.proposal_for(self.jour)
        response = self.client_for(self.anna).get(self.url(campaign, "sidan"))
        self.assertTemplateUsed(response, "flamingo/lp/page.html")
        html = response.content.decode()
        self.assertIn('sandbox="allow-same-origin"', html)
        self.assertNotIn("allow-scripts", html)
        self.assertNotIn("allow-forms", html)
        # Hela sidan ligger escapad i attributet (en SafeString escapas inte
        # av sig själv: utan force_escape bröts attributet vid första ").
        self.assertIn('srcdoc="&lt;!DOCTYPE html&gt;', html)
        self.assertNotIn('srcdoc="<', html)
        srcdoc = html.split('srcdoc="', 1)[1].split('"', 1)[0]
        self.assertIn("lp-title", srcdoc)
        self.assertIn("&lt;/html&gt;", srcdoc)
        # Ringknappen med det bekräftade numret, escapad i srcdoc.
        self.assertIn(f"Ring {PHONE}", html)
        self.assertIn("Förhandsvisning: formuläret skickar inget.", html)
        # Den publika sidan finns inte förrän kampanjen är live.
        self.assertEqual(Client().get(campaign.landing_url).status_code, 404)

    def test_the_preview_falls_back_when_the_landing_page_cannot_be_drawn(self):
        campaign, _ = self.proposal_for(self.badrum)
        with patch(
            "apps.flamingo.app_views.campaigns.render_to_string", side_effect=RuntimeError("trasig")
        ):
            response = self.client_for(self.anna).get(self.url(campaign, "sidan"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "flamingo/app/campaigns/_page_preview.html")
        self.assertNotIn("srcdoc=", response.content.decode())


class TemplateGuardTests(TestCase):
    def test_no_inline_style_or_script_in_the_campaign_templates(self):
        folder = Path(settings.BASE_DIR) / "templates" / "flamingo" / "app" / "campaigns"
        files = sorted(folder.glob("*.html"))
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(template=path.name):
                self.assertNotIn("style=", text)
                self.assertNotIn("<script", text)
                self.assertNotIn("[ ", text)
