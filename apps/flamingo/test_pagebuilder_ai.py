"""ADX Flamingo: AI i sidbyggaren ("Bygg sidan åt mig", "Skriv om"),
Konverteringskollen och priset i annonsen (pagebuilder/ai.py, koll.py,
principles.py, app_views/page_ai.py, generator.price_texts).

Modellen anropas aldrig på riktigt: llm.call byts mot en attrapp (eller ett
fel), llm.client och botocores inloggning fäller testet om något ändå
försöker nå AWS."""

import io
import json
import shutil
import tempfile
import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.common.security import AI_TYPOGRAPHY_CHARS
from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import checks, generator, limits, pagebuilder
from .models import Fact, FlamingoAccount, LandingPage, MediaAsset, Service
from .pagebuilder import ai, koll, principles, registry

User = get_user_model()
PHONE = "08-000 00 00"
PRICE = "Utryckning från 995 kr"
_MEDIA = tempfile.mkdtemp(prefix="flamingo-pb-ai-")


def _png(size=(320, 240)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 90, 200)).save(buffer, "PNG")
    return ContentFile(buffer.getvalue(), name="bild.png")


def tool_response(name, data):
    block = SimpleNamespace(type="tool_use", name=name, input=data)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="Här"), block])


def no_aws():
    """Patchers som fäller testet om något försöker nå modellen eller AWS."""
    return [
        mock.patch("apps.assistant.llm.client", side_effect=AssertionError("AWS ska inte nås")),
        mock.patch(
            "botocore.credentials.CredentialResolver.load_credentials",
            side_effect=AssertionError("AWS-inloggning ska inte sökas"),
        ),
    ]


class AIFixture:
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(name="Lindqvist Rör AB")
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.customer.users.add(cls.anna)
        reviews = [
            {
                "id": f"places/x/reviews/{n}",
                "author": author,
                "author_uri": "",
                "rating": 5,
                "text": text,
                "time": "2026-09-01T10:00:00Z",
                "relative": "",
            }
            for n, (author, text) in enumerate(
                [
                    ("Anna L.", "Tydligt och noggrant."),
                    ("Johan S.", "Bra jobb."),
                    ("Eva K.", "Bra."),
                ],
                start=1,
            )
        ]
        cls.account = FlamingoAccount.objects.create(
            customer=cls.customer,
            is_enabled=True,
            google_place_id="ChIJexempel",
            google_rating=Decimal("4.8"),
            google_review_count=37,
            google_reviews=reviews,
            google_reviews_selected=[r["id"] for r in reviews],
        )
        for key, label, value, confirmed in (
            ("telefon", "Telefon", PHONE, True),
            ("adress", "Adress", "Exempelvägen 4, Nacka", True),
            ("oppettider", "Öppettider", "Vardagar 7-16", True),
            ("omrade", "Område", "Nacka, Värmdö och Tyresö", True),
            ("pris-rorjour", "Pris, Rörjour", PRICE, True),
            ("pris-filmning-av-avlopp", "Pris, Filmning av avlopp", "Från 1 900 kr", False),
            ("behorighet", "Behörighet", "Säker Vatten-auktoriserade", True),
            ("garanti", "Garanti", "Två års garanti på arbetet", True),
            ("kontaktperson", "Kontaktperson", "Lisa Lindqvist", True),
        ):
            Fact.objects.create(
                account=cls.account, key=key, label=label, value=value, confirmed=confirmed
            )
        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL, order=1
        )
        cls.badrum = Service.objects.create(
            account=cls.account, name="Badrumsrenovering", sales_mode=Service.SALES_QUOTE, order=2
        )
        cls.film = Service.objects.create(
            account=cls.account, name="Filmning av avlopp", sales_mode=Service.SALES_BOOK, order=3
        )
        from .models import Campaign

        cls.campaign = Campaign.objects.create(
            account=cls.account, service=cls.jour, name="Rörjour Nacka", area="Nacka + 15 km"
        )
        with mock.patch("apps.assistant.llm.is_configured", return_value=False):
            cls.page = pagebuilder.create_page_for_campaign(cls.campaign)

        cls.other_customer = Customer.objects.create(name="Hemlig Bygg AB")
        cls.other_account = FlamingoAccount.objects.create(
            customer=cls.other_customer, is_enabled=True
        )
        cls.other_service = Service.objects.create(account=cls.other_account, name="Takbyte")
        cls.other_page = LandingPage.objects.create(account=cls.other_account, name="Hemlig")

    def setUp(self):
        super().setUp()
        cache.clear()
        self.page.refresh_from_db()
        self.account.refresh_from_db()
        patchers = [
            mock.patch("apps.assistant.llm.is_configured", return_value=False),
            mock.patch(
                "apps.assistant.llm.call", side_effect=AssertionError("AI ska inte anropas")
            ),
            *no_aws(),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def ai_on(self, response=None, side_effect=None):
        """AI påslagen med en attrapp som svar. Returnerar attrappen."""
        patcher = mock.patch("apps.assistant.llm.is_configured", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        call = mock.Mock(return_value=response, side_effect=side_effect)
        patcher = mock.patch("apps.assistant.llm.call", call)
        patcher.start()
        self.addCleanup(patcher.stop)
        return call

    def client_for(self, user, view_as=None):
        client = Client()
        client.force_login(user)
        if view_as is not None:
            session = client.session
            session[VIEW_AS_KEY] = view_as.pk
            session.save()
        return client

    def media(self, **fields):
        asset = MediaAsset(account=self.account, alt="Ett badrum", **fields)
        asset.file.save("bild.png", _png(), save=False)
        asset.save()
        return asset

    def block(self, type_key, variant=None, **fields):
        # Sidans kampanj ger tjänsten och dess pris (ett pris bara för sin tjänst).
        ctx = pagebuilder.build_ctx(self.campaign)
        block = pagebuilder.new_block(type_key, variant, self.account, ctx=ctx)
        if fields:
            pagebuilder.add_version(
                block, dict(pagebuilder.active_fields(block), **fields), "customer", self.anna
            )
        return block

    def plan_numbers(self, goal="call", service=None):
        base = ai.make_base(self.page, self.account, service=service or self.jour, goal=goal)
        return {p.type: nr for nr, p in enumerate(ai.plan(base), start=1)}

    def texts(self, blocks):
        """Bara fälten i versionerna: id, tider och signaturer innehåller
        siffror som inte är text på sidan (en tid med ,995117 är inget pris)."""
        fields = [v.get("fields") for b in blocks for v in b.get("versions") or []]
        return json.dumps(fields, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Principerna
# ---------------------------------------------------------------------------


class PrincipleTests(AIFixture, TestCase):
    def test_every_principle_has_a_label_a_line_and_a_source(self):
        keys = {p.key for p in principles.PRINCIPLES}
        for key in (
            "socialt_bevis",
            "auktoritet",
            "sympati",
            "omsesidighet",
            "engagemang",
            "gemenskap",
            "samma_budskap",
            "pris_tidigt",
            "en_handling",
            "farre_falt",
            "klarhet",
            "riskomvandning",
            "konkret",
            "nasta_steg",
            "ansikte_namn",
        ):
            self.assertIn(key, keys)
        for principle in principles.PRINCIPLES:
            with self.subTest(principle=principle.key):
                self.assertTrue(principle.label and principle.text and principle.source)
                # Inga påhittade siffror: ingen procentsats i någon text.
                self.assertNotIn("%", principle.text + principle.source)
        self.assertEqual(set(principles.BLOCK_PRINCIPLE), set(registry.TYPES))
        self.assertTrue(set(principles.BLOCK_PRINCIPLE.values()) <= keys)

    def test_scarcity_is_never_an_ai_principle(self):
        self.assertIn("knapphet", principles.BY_KEY)
        self.assertNotIn("knapphet", principles.AI_KEYS)
        enum = ai.REWRITE_TOOL["input_schema"]["properties"]["forslag"]["items"]["properties"]
        self.assertNotIn("knapphet", enum["princip"]["enum"])


# ---------------------------------------------------------------------------
# Vakten
# ---------------------------------------------------------------------------


class GuardTests(AIFixture, TestCase):
    def guard(self):
        return ai.make_base(self.page, self.account, service=self.jour).guard

    def test_fake_urgency_numbers_ratings_and_time_promises_are_stopped(self):
        guard = self.guard()
        for text in (
            "Bara 2 tider kvar i veckan",
            "Bara några tider kvar",
            "Passa på innan det är för sent",
            "Erbjudandet gäller bara i dag",
            "Vi är på plats inom en timme",
            "Vi kommer direkt",
            "Snabb utryckning i Nacka",
            "Från 499 kr per timme",
            "4,9 i betyg",
            "Certifierade proffs med lång erfarenhet",
            "Billigast i Nacka",
            'Kunderna skriver "bästa rörmokaren"',
            "Gratis offert",
            "Se mer på www.example.com",
        ):
            with self.subTest(text=text):
                self.assertTrue(guard.problems(text))
        for text in (
            "Rörjour i Nacka",
            "Utryckning från 995 kr",
            "Från 995 kr",
            "Säker Vatten-auktoriserad rörjour i Nacka",
            "Två års garanti på arbetet",
            "Ring Lindqvist Rör på 08-000 00 00",
            "Vad kunderna säger",
        ):
            with self.subTest(text=text):
                self.assertEqual(guard.problems(text), [])

    def test_reviews_words_need_a_google_profile(self):
        base = ai.make_base(self.other_page, self.other_account)
        self.assertTrue(base.guard.problems("Omdömen från våra kunder"))

    def test_an_insurance_heading_needs_a_confirmed_insurance(self):
        base = ai.make_base(self.page, self.account, service=self.jour)
        certificates = next(p for p in ai.plan(base) if p.type == "certificates")
        self.assertNotIn("försäkring", certificates.fields["title"].lower())


# ---------------------------------------------------------------------------
# Bygg sidan åt mig
# ---------------------------------------------------------------------------


class BuildTests(AIFixture, TestCase):
    def ai_build_response(self):
        nr = self.plan_numbers()
        return tool_response(
            ai.BUILD_TOOL["name"],
            {
                "block": [
                    {
                        "nr": nr["hero"],
                        "title": "Rörjour i Nacka, ring oss",
                        "lead": "Bara 2 tider kvar i veckan, så ring nu.",
                        "points": [
                            "Från 499 kr per timme",
                            "4,9 i betyg på Google",
                            "Säker Vatten-auktoriserade",
                            "Vi är på plats inom en timme",
                        ],
                    },
                    {"nr": nr["price"], "text": "Fast pris från 499 kr."},
                    {"nr": nr["reviews_google"], "title": "Kunderna ger oss 4,9 av 5"},
                    {"nr": nr["certificates"], "title": "Certifierade proffs"},
                    {
                        "nr": nr["steps"],
                        "title": "Så här går det till",
                        "steps": [
                            {"title": "Du ringer", "text": "Berätta vad som hänt."},
                            {"title": "Vi kommer överens", "text": "Om en tid som passar dig."},
                            {"title": "Jobbet görs", "text": "Du vet vad som görs innan."},
                        ],
                    },
                    {"nr": nr["area"], "text": "Vi jobbar i Nacka, Värmdö och Tyresö sedan 1998."},
                    {"nr": nr["callbar"], "title": "Ring nu, vi kommer direkt"},
                    {"nr": 99, "title": "Ett block som inte finns"},
                ]
            },
        )

    def test_the_proposal_is_valid_and_fact_only(self):
        call = self.ai_on(self.ai_build_response())
        result = ai.build(self.page, self.account, goal="call", service=self.jour, tone="varm")
        self.assertEqual(result["source"], "ai")
        blocks = result["blocks"]
        self.assertEqual(pagebuilder.validate_blocks(blocks, account=self.account), blocks)
        context = pagebuilder.page_context(self.page)
        self.assertEqual(pagebuilder.page_problems(self.page, context, blocks=blocks), [])
        self.assertEqual(result["problems"], [])
        self.assertEqual(
            [b["type"] for b in blocks],
            [
                "hero",
                "price",
                "reviews_google",
                "steps",
                "certificates",
                "guarantee",
                "person",
                "faq",
                "area",
                "form",
                "callbar",
            ],
        )
        variants = {b["type"]: b["variant"] for b in blocks}
        self.assertEqual(variants["hero"], "call")
        self.assertEqual(variants["form"], "short")
        self.assertEqual(variants["reviews_google"], "cards")
        # Det AI hittade på är borta; det som klarade vakten står kvar.
        text = self.texts(blocks)
        for bad in ("499", "4,9", "tider kvar", "inom en timme", "proffs", "1998", "direkt"):
            self.assertNotIn(bad, text)
        hero = pagebuilder.active_fields(blocks[0])
        self.assertEqual(hero["title"], "Rörjour i Nacka, ring oss")
        self.assertEqual(hero["points"][0], PRICE)
        self.assertIn("Säker Vatten-auktoriserade", hero["points"])
        self.assertEqual(hero["phone"], PHONE)
        self.assertNotIn("Bara", hero["lead"])
        self.assertEqual(blocks[0]["versions"][0]["source"], "ai")
        price = pagebuilder.active_fields(blocks[1])
        self.assertEqual(price["price"], PRICE)
        self.assertEqual(blocks[1]["versions"][0]["source"], "template")
        steps = pagebuilder.active_fields(blocks[3])
        self.assertEqual(steps["steps"][0]["title"], "Du ringer")
        for block in blocks:
            for version in block["versions"]:
                self.assertIn(version["source"], ("ai", "template"))
        self.assertFalse(any(ch in text for ch in AI_TYPOGRAPHY_CHARS))

        # Förklaringarna: en per block, med principen.
        self.assertEqual([e["block_id"] for e in result["explanations"]], [b["id"] for b in blocks])
        for item in result["explanations"]:
            self.assertEqual(item["principle_label"], principles.label(item["principle_key"]))
            self.assertTrue(item["title"] and item["text"])
        self.assertEqual(result["explanations"][0]["principle_key"], "samma_budskap")
        self.assertEqual(result["explanations"][1]["principle_key"], "pris_tidigt")
        self.assertIn(
            {"label": "Pris, Rörjour", "value": PRICE, "source": "Du"}, result["used_facts"]
        )
        self.assertIn("Google-profilen", [f["label"] for f in result["used_facts"]])

        # Ett anrop, ett verktyg, bara bekräftade uppgifter (inte det
        # obekräftade priset för filmningen).
        call.assert_called_once()
        kwargs = call.call_args.kwargs
        self.assertEqual([t["name"] for t in kwargs["tools"]], [ai.BUILD_TOOL["name"]])
        payload = json.loads(kwargs["messages"][0]["content"])
        self.assertEqual(payload["från_pris"], "från 995 kr")
        self.assertNotIn("1 900", kwargs["messages"][0]["content"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.ai_count, 1)
        # Inget sparas.
        page = LandingPage.objects.get(pk=self.page.pk)
        self.assertEqual(page.rev, self.page.rev)
        self.assertEqual(page.draft, self.page.draft)

    def test_the_kicker_keeps_the_service_and_place_and_the_title_is_a_benefit(self):
        """Överrubriken (mallens) säger tjänsten och orten; AI:s rubrik säger
        vad kunden får och behöver inte upprepa dem. En för lång rubrik
        kastas och mallens står kvar."""
        nr = self.plan_numbers()
        self.ai_on(
            tool_response(
                ai.BUILD_TOOL["name"], {"block": [{"nr": nr["hero"], "title": "Vi lagar allt"}]}
            )
        )
        result = ai.build(self.page, self.account, goal="call", service=self.jour)
        hero = pagebuilder.active_fields(result["blocks"][0])
        self.assertEqual(hero["kicker"], "Rörjour i Nacka")
        self.assertEqual(hero["title"], "Vi lagar allt")
        self.assertEqual(result["source"], "ai")

        long_title = "Vi lagar allt " + "och lite till " * 6
        self.ai_on(
            tool_response(
                ai.BUILD_TOOL["name"], {"block": [{"nr": nr["hero"], "title": long_title}]}
            )
        )
        result = ai.build(self.page, self.account, goal="call", service=self.jour)
        hero = pagebuilder.active_fields(result["blocks"][0])
        self.assertEqual(hero["title"], registry.HERO_TITLES[Service.SALES_CALL])
        self.assertEqual(result["source"], "mallar")
        self.assertEqual(result["note"], ai.NOTE_EMPTY)

    def test_quote_and_book_put_the_form_beside_the_hero_and_skip_the_callbar(self):
        result = ai.build(self.page, self.account, goal="quote", service=self.badrum)
        types = [b["type"] for b in result["blocks"]]
        self.assertEqual(types[:2], ["hero", "form"])
        self.assertEqual(result["blocks"][0]["variant"], "form")
        self.assertEqual(result["blocks"][1]["variant"], "questions")
        self.assertNotIn("callbar", types)
        # Badrummet har inget bekräftat pris: inget prisblock, och det saknas.
        self.assertNotIn("price", types)
        self.assertNotIn("995", self.texts(result["blocks"]))
        self.assertIn("Pris", [m["label"] for m in result["missing"]])
        questions = pagebuilder.active_fields(result["blocks"][1])["questions"]
        self.assertLessEqual(len(questions) + 2, koll.FORM_MAX_FIELDS)
        hero = pagebuilder.active_fields(result["blocks"][0])
        self.assertEqual(hero["kicker"], "Badrumsrenovering i Nacka")
        self.assertEqual(hero["title"], registry.HERO_TITLES[Service.SALES_QUOTE])

        book = ai.build(self.page, self.account, goal="book", service=self.film)
        self.assertEqual([b["variant"] for b in book["blocks"][:2]], ["form", "booking"])
        book_hero = pagebuilder.active_fields(book["blocks"][0])
        self.assertEqual(book_hero["kicker"], "Filmning av avlopp i Nacka")
        self.assertEqual(book_hero["title"], registry.HERO_TITLES[Service.SALES_BOOK])
        # Filmningens pris är inte bekräftat.
        self.assertNotIn("1 900", self.texts(book["blocks"]))

    def test_templates_take_over_when_ai_is_off(self):
        result = ai.build(self.page, self.account, goal="call", service=self.jour, tone="kort")
        self.assertEqual(result["source"], "mallar")
        self.assertEqual(result["note"], ai.NOTE_OFF)
        blocks = result["blocks"]
        self.assertEqual(pagebuilder.validate_blocks(blocks, account=self.account), blocks)
        context = pagebuilder.page_context(self.page)
        self.assertEqual(pagebuilder.page_problems(self.page, context, blocks=blocks), [])
        self.assertTrue(all(v["source"] == "template" for b in blocks for v in b["versions"]))
        self.assertEqual(len(result["explanations"]), len(blocks))
        self.assertEqual(pagebuilder.active_fields(blocks[0])["lead"], "Ring Lindqvist Rör.")
        self.account.refresh_from_db()
        self.assertEqual(self.account.ai_count, 0)

    def test_templates_take_over_when_the_budget_is_spent(self):
        call = self.ai_on(tool_response(ai.BUILD_TOOL["name"], {"block": []}))
        with mock.patch(
            "apps.assistant.llm.check_budget", side_effect=ai.llm.BudgetExceeded("slut")
        ):
            result = ai.build(self.page, self.account, goal="call", service=self.jour)
        self.assertEqual((result["source"], result["note"]), ("mallar", ai.NOTE_BUDGET))
        call.assert_not_called()

    def test_the_daily_ai_limit_is_enforced(self):
        call = self.ai_on(tool_response(ai.BUILD_TOOL["name"], {"block": []}))
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            ai_day=limits.stockholm_today(), ai_count=limits.AI_DAILY_MAX
        )
        result = ai.build(self.page, self.account, goal="call", service=self.jour)
        self.assertEqual((result["source"], result["note"]), ("mallar", ai.NOTE_LIMIT))
        rewrite = ai.rewrite(
            self.page, self.account, block_id=self.page.draft_blocks[0]["id"], field="title"
        )
        self.assertEqual(rewrite["note"], ai.NOTE_LIMIT)
        self.assertEqual(len(rewrite["suggestions"]), 3)
        call.assert_not_called()

    def test_a_slow_model_falls_back_to_templates(self):
        release = threading.Event()

        def slow(**kwargs):
            release.wait(5)
            return tool_response(ai.BUILD_TOOL["name"], {"block": []})

        self.ai_on(side_effect=slow)
        with mock.patch.object(ai, "BUILD_TIMEOUT", 0.05):
            result = ai.build(self.page, self.account, goal="call", service=self.jour)
        release.set()
        self.assertEqual((result["source"], result["note"]), ("mallar", ai.NOTE_TIMEOUT))

    def test_a_model_error_falls_back_to_templates(self):
        self.ai_on(side_effect=ai.llm.ModelUnavailable("nej"))
        result = ai.build(self.page, self.account, goal="call", service=self.jour)
        self.assertEqual((result["source"], result["note"]), ("mallar", ai.NOTE_ERROR))

    def test_an_existing_before_and_after_with_images_is_kept(self):
        with override_settings(MEDIA_ROOT=_MEDIA):
            before, after = self.media(), self.media()
            block = self.block("before_after", "pair", before=before.pk, after=after.pk)
            blocks = [*self.page.draft_blocks, block]
            pagebuilder.save_draft(self.page, blocks, rev=self.page.rev)
            result = ai.build(self.page, self.account, goal="call", service=self.jour)
        kept = [b for b in result["blocks"] if b["type"] == "before_after"]
        self.assertEqual([b["id"] for b in kept], [block["id"]])
        self.assertNotIn("Bilder från jobb", [m["label"] for m in result["missing"]])


# ---------------------------------------------------------------------------
# Skriv om
# ---------------------------------------------------------------------------


class RewriteTests(AIFixture, TestCase):
    def hero_id(self):
        return self.page.draft_blocks[0]["id"]

    def test_three_suggestions_and_time_promises_are_dropped(self):
        call = self.ai_on(
            tool_response(
                ai.REWRITE_TOOL["name"],
                {
                    "forslag": [
                        {"text": "Rörjour i Nacka, på plats inom en timme", "princip": "klarhet"},
                        {"text": "Rörjour i Nacka från 499 kr", "princip": "pris_tidigt"},
                        {
                            "text": "Rörjour i Nacka från 995 kr",
                            "princip": "pris_tidigt",
                            "varfor": "Priset sållar bort dem som inte vill betala.",
                        },
                        {"text": "Bara 2 tider kvar för rörjour i Nacka", "princip": "klarhet"},
                        {"text": "Rörjour i Nacka", "princip": "knapphet"},
                        {"text": "Läcker det i Nacka? Ring för rörjour", "princip": "klarhet"},
                        {
                            "text": "Säker Vatten-auktoriserad rörjour i Nacka",
                            "princip": "auktoritet",
                            "varfor": "Höjer konverteringen med 40 %.",
                        },
                        {"text": "Ännu en rörjour i Nacka", "princip": "konkret"},
                    ]
                },
            )
        )
        result = ai.rewrite(self.page, self.account, block_id=self.hero_id(), field="title")
        self.assertEqual(result["source"], "ai")
        texts = [s["text"] for s in result["suggestions"]]
        self.assertEqual(
            texts,
            [
                "Rörjour i Nacka från 995 kr",
                "Läcker det i Nacka? Ring för rörjour",
                "Säker Vatten-auktoriserad rörjour i Nacka",
            ],
        )
        keys = [s["principle_key"] for s in result["suggestions"]]
        self.assertEqual(keys, ["pris_tidigt", "klarhet", "auktoritet"])
        for suggestion in result["suggestions"]:
            self.assertEqual(
                suggestion["principle_label"], principles.label(suggestion["principle_key"])
            )
            self.assertTrue(suggestion["why"])
        self.assertEqual(
            result["suggestions"][0]["why"], "Priset sållar bort dem som inte vill betala."
        )
        # Ett påhittat påstående om effekten ersätts av principens egen rad.
        self.assertEqual(result["suggestions"][2]["why"], principles.get("auktoritet").text)
        payload = json.loads(call.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(payload["nu"], registry.HERO_TITLES[Service.SALES_CALL])
        self.assertNotIn("knapphet", [p["nyckel"] for p in payload["principer"]])

    def test_templates_give_three_suggestions_without_ai(self):
        result = ai.rewrite(self.page, self.account, block_id=self.hero_id(), field="title")
        self.assertEqual(result["source"], "mallar")
        self.assertEqual(len(result["suggestions"]), 3)
        context = pagebuilder.page_context(self.page)
        # Överrubriken säger tjänsten och orten: rubrikens förslag säger vad
        # kunden får.
        for suggestion in result["suggestions"]:
            self.assertEqual(checks.text_problems(suggestion["text"], context), [])
            self.assertNotEqual(suggestion["text"], result["current"])
            self.assertLessEqual(len(suggestion["text"]), ai.HERO_TITLE_MAX)
        kicker = ai.rewrite(self.page, self.account, block_id=self.hero_id(), field="kicker")
        self.assertEqual(len(kicker["suggestions"]), 3)
        for suggestion in kicker["suggestions"]:
            self.assertIn("Nacka", suggestion["text"])
            self.assertEqual(checks.text_problems(suggestion["text"], context), [])
            self.assertNotEqual(suggestion["text"], "Rörjour i Nacka")
        texts = [s["text"] for s in kicker["suggestions"]]
        self.assertIn("Rörjour i Nacka från 995 kr", texts)
        # En behörighet som ett ord framför tjänsten, inte "Rörjour i Nacka.
        # Säker Vatten-auktoriserade".
        self.assertIn("Säker Vatten-auktoriserad rörjour i Nacka", texts)

    def test_fields_of_every_kind(self):
        steps = self.block("steps", "three")
        faq = self.block("faq", "three")
        price = self.block("price", "from")
        blocks = [*self.page.draft_blocks, steps, faq, price]
        pagebuilder.save_draft(self.page, blocks, rev=self.page.rev)
        guard = ai.make_base(self.page, self.account, service=self.jour).guard
        for block, field in (
            (self.page.draft_blocks[0], "lead"),
            (self.page.draft_blocks[0], "points.0"),
            (steps, "title"),
            (steps, "steps.1.text"),
            (steps, "steps.0.title"),
            (faq, "items.0.a"),
            (faq, "items.0.q"),
            (price, "price"),
            (price, "text"),
        ):
            with self.subTest(block=block["type"], field=field):
                result = ai.rewrite(self.page, self.account, block_id=block["id"], field=field)
                self.assertTrue(result["suggestions"])
                for suggestion in result["suggestions"]:
                    self.assertEqual(guard.problems(suggestion["text"]), [])
                    self.assertNotEqual(suggestion["text"], result["current"])
        prices = ai.rewrite(self.page, self.account, block_id=price["id"], field="price")
        self.assertTrue(all("995" in s["text"] for s in prices["suggestions"]))

    def test_an_unsaved_block_can_be_sent_along(self):
        result = ai.rewrite(
            self.page,
            self.account,
            block_id="b_osparat00001",
            field="title",
            block_type="callbar",
            variant="call",
            fields={"title": "Ring oss", "phone": PHONE},
        )
        self.assertEqual(len(result["suggestions"]), 3)

    def test_unknown_fields_and_phone_numbers_are_refused(self):
        for field in ("phone", "image", "okant", "points.9", "title.0.x", ""):
            with self.subTest(field=field), self.assertRaises(ai.AIError):
                ai.rewrite(self.page, self.account, block_id=self.hero_id(), field=field)
        with self.assertRaises(ai.AIError):
            ai.rewrite(self.page, self.account, block_id="b_finnsinte0001", field="title")


# ---------------------------------------------------------------------------
# Konverteringskollen
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class KollTests(AIFixture, TestCase):
    def items(self, blocks, page=None, account=None):
        result = koll.koll(page or self.page, account or self.account, blocks=blocks)
        self.assertEqual(result["score"], sum(1 for i in result["items"] if i["ok"]))
        self.assertEqual(result["total"], len(result["items"]))
        oks = [i["ok"] for i in result["items"]]
        self.assertEqual(oks, sorted(oks))  # det som inte är ok först
        return {i["key"]: i for i in result["items"]}

    def hero(self, variant="call", **fields):
        return self.block("hero", variant, **fields)

    def test_the_headline_matches_the_ad(self):
        good = self.items(self.page.draft_blocks)["rubrik"]
        self.assertTrue(good["ok"])
        self.assertIn('"rörjour"', good["text"])
        # Tjänsten och orten i överrubriken räcker; rubriken säger vad kunden får.
        benefit = self.hero(kicker="Rörjour i Nacka", title="Läcker det? Vi lagar det.")
        self.assertTrue(self.items([benefit])["rubrik"]["ok"])
        hero = self.hero(kicker="", title="Vi lagar rör")
        bad = self.items([hero])["rubrik"]
        self.assertFalse(bad["ok"])
        self.assertEqual(
            bad["action"],
            {"kind": "select_block", "block_id": hero["id"], "label": "Ändra överrubriken"},
        )
        self.assertEqual(bad["principle_key"], "samma_budskap")

    def test_a_call_button_or_form_in_the_first_block(self):
        self.assertTrue(self.items([self.hero("call")])["forsta_blocket"]["ok"])
        form_hero = self.hero("form")
        self.assertTrue(
            self.items([form_hero, self.block("form", "questions")])["forsta_blocket"]["ok"]
        )
        steps = self.block("steps", "three")
        bad = self.items([steps, self.hero("call")])["forsta_blocket"]
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["action"]["kind"], "select_block")
        # Bara text och bild har ringknappen som huvudhandling, men bara med
        # ett nummer.
        self.assertTrue(self.items([self.hero("text")])["forsta_blocket"]["ok"])
        text_hero = self.hero("text", phone="")
        self.assertFalse(self.items([text_hero])["forsta_blocket"]["ok"])
        # Formuläret syns direkt bara när det står direkt efter Toppen.
        later = self.items([form_hero, steps, self.block("form", "questions")])["forsta_blocket"]
        self.assertFalse(later["ok"])
        self.assertEqual(later["action"]["kind"], "select_block")
        missing = self.items([steps])["forsta_blocket"]
        self.assertEqual(missing["action"]["kind"], "add_block")
        self.assertEqual(missing["action"]["type"], "hero")

    def test_the_price_early(self):
        self.assertTrue(self.items([self.hero(points=[PRICE])])["pris"]["ok"])
        hero = self.hero(points=[])
        missing = self.items([hero])["pris"]
        self.assertFalse(missing["ok"])
        self.assertEqual(
            missing["action"],
            {
                "kind": "add_block",
                "type": "price",
                "variant": "from",
                "after_id": hero["id"],
                "label": "Lägg till prisblock",
            },
        )
        self.assertIn(PRICE, missing["text"])
        self.assertTrue(self.items([hero, self.block("price", "from")])["pris"]["ok"])
        late = [hero, *(self.block("steps", "three") for _ in range(3)), self.block("price")]
        far = self.items(late)["pris"]
        self.assertFalse(far["ok"])
        self.assertEqual(far["action"]["block_id"], late[-1]["id"])
        # Utan bekräftat pris finns punkten inte.
        other = self.items(
            [self.block("hero", "text")], page=self.other_page, account=self.other_account
        )
        self.assertNotIn("pris", other)

    def test_social_proof(self):
        hero = self.hero()
        missing = self.items([hero])["omdomen"]
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["action"]["kind"], "add_block")
        self.assertEqual(missing["action"]["type"], "reviews_google")
        self.assertIn("4,8", missing["text"])
        self.assertTrue(self.items([hero, self.block("reviews_google", "cards")])["omdomen"]["ok"])
        # Utan Google-profil: länken till omdömena (om den finns).
        Fact.objects.create(
            account=self.other_account, key="telefon", label="Telefon", value=PHONE, confirmed=True
        )
        other_hero = pagebuilder.new_block("hero", "call", self.other_account)
        item = self.items([other_hero], page=self.other_page, account=self.other_account)
        url = ai.safe_reverse("flamingo:app_reviews")
        self.assertFalse(item["omdomen"]["ok"])
        if url:
            self.assertEqual(item["omdomen"]["action"]["url"], url)
        else:
            self.assertIsNone(item["omdomen"]["action"])

    def test_the_form_has_at_most_four_fields(self):
        short = self.items([self.hero("form"), self.block("form", "short")])["formular"]
        self.assertTrue(short["ok"])
        self.assertEqual(short["title"], "Formuläret har två fält")
        questions = [{"key": f"q{n}", "label": f"Fråga {n}", "kind": "text"} for n in range(3)]
        form = self.block("form", "questions", questions=questions)
        long = self.items([self.hero("form"), form])["formular"]
        self.assertFalse(long["ok"])
        # Som besökaren ser det: tre frågor, namn, telefon, meddelandet och
        # e-posten.
        self.assertEqual(long["title"], "Formuläret har sju fält")
        # Mallens offertformulär: en längre text (inget meddelande till),
        # en fråga, namn, telefon och e-post.
        default = self.items([self.hero("form"), self.block("form", "questions")])["formular"]
        self.assertTrue(default["ok"])
        self.assertEqual(default["title"], "Formuläret har fem fält")
        self.assertEqual(long["action"]["block_id"], form["id"])
        self.assertNotIn("formular", self.items([self.hero()]))

    def test_one_main_action(self):
        ok = self.items([self.hero("call"), self.block("form", "short")])["en_handling"]
        self.assertTrue(ok["ok"])
        form = self.block("form", "questions")
        competing = self.items([self.hero("call"), form])["en_handling"]
        self.assertFalse(competing["ok"])
        self.assertEqual(competing["action"]["block_id"], form["id"])
        callbar = self.block("callbar", "call")
        bar = self.items([self.hero("form"), self.block("form"), callbar])["en_handling"]
        self.assertFalse(bar["ok"])
        self.assertEqual(bar["action"]["block_id"], callbar["id"])
        both = self.block("callbar", "call_write")
        self.assertTrue(
            self.items([self.hero("form"), self.block("form"), both])["en_handling"]["ok"]
        )

    def test_real_images(self):
        none = self.items([self.hero()])["bilder"]
        self.assertFalse(none["ok"])
        self.assertEqual(none["action"]["kind"], "open_panel")
        self.assertEqual(none["action"]["panel"], "media")
        # Sidan har inga bilder: texten påstår inget om bilder från nätet.
        self.assertNotIn("nätet", none["text"])
        asset = self.media()
        with_image = self.items([self.hero("image", image=asset.pk)])["bilder"]
        self.assertTrue(with_image["ok"])
        empty = self.block("before_after", "pair")
        waiting = self.items([self.hero("image", image=asset.pk), empty])["bilder"]
        self.assertFalse(waiting["ok"])
        self.assertEqual(waiting["action"]["block_id"], empty["id"])
        # En bild från ett annat konto räknas inte.
        foreign = MediaAsset(account=self.other_account, alt="Hemlig")
        foreign.file.save("bild.png", _png(), save=False)
        foreign.save()
        hero = self.hero("image")
        hero["versions"][-1]["fields"]["image"] = foreign.pk
        foreign_item = self.items([hero])["bilder"]
        self.assertFalse(foreign_item["ok"])
        self.assertEqual(foreign_item["action"]["block_id"], hero["id"])

    def test_the_number_can_be_tapped(self):
        item = self.items([self.hero("call")])["numret"]
        self.assertTrue(item["ok"])
        self.assertEqual(item["title"], "Numret går att trycka på")
        no_phone = self.hero("form", phone="")
        self.assertNotIn("numret", self.items([no_phone, self.block("form")]))

    def test_the_page_ends_with_an_action(self):
        ends = self.items([self.hero("call"), self.block("form", "short")])["slutet"]
        self.assertTrue(ends["ok"])
        self.assertTrue(
            self.items([self.hero("call"), self.block("callbar", "call")])["slutet"]["ok"]
        )
        steps = self.block("steps", "three")
        bare = self.items([self.hero("call"), steps])["slutet"]
        self.assertFalse(bare["ok"])
        self.assertEqual(
            bare["action"],
            {
                "kind": "add_block",
                "type": "callbar",
                "variant": "call",
                "after_id": steps["id"],
                "label": "Lägg till ringremsan sist",
            },
        )
        form = self.block("form", "questions")
        with_form = self.items([self.hero("form", phone=""), form, steps])["slutet"]
        self.assertFalse(with_form["ok"])
        self.assertEqual(with_form["action"]["kind"], "select_block")
        self.assertEqual(with_form["action"]["block_id"], form["id"])

    def test_labels_are_plain(self):
        result = koll.koll(self.page, self.account)
        for item in result["items"]:
            self.assertNotIn("Cialdini", item["principle_label"])
            self.assertNotIn("Hero", item["title"] + item["text"])
            self.assertNotIn("Sållar", item["principle_label"])

    def test_the_page_is_light(self):
        asset = self.media()
        blocks = [self.hero("image", image=asset.pk)]
        self.assertTrue(self.items(blocks)["latt"]["ok"])
        with mock.patch.object(koll, "LIGHT_MAX_BYTES", 10):
            heavy = self.items(blocks)["latt"]
        self.assertFalse(heavy["ok"])
        self.assertEqual(heavy["action"]["block_id"], blocks[0]["id"])
        self.assertNotIn("latt", self.items([self.hero()]))

    def test_the_whole_page_and_an_empty_one(self):
        result = koll.koll(self.page, self.account)
        self.assertGreater(result["total"], 5)
        self.assertIn("av", f"{result['score']} av {result['total']}")
        empty = koll.koll(self.other_page, self.other_account)
        self.assertEqual((empty["score"], empty["total"], empty["items"]), (0, 0, []))


# ---------------------------------------------------------------------------
# Priset i annonsen (generatorn)
# ---------------------------------------------------------------------------


class PriceInAdTests(AIFixture, TestCase):
    def proposal(self, service, area="Nacka + 15 km"):
        from .models import Campaign

        campaign = Campaign(account=self.account, service=service, name="X", area=area)
        return generator.build_proposal(campaign, save=False)

    def assert_clean(self, proposal, service):
        context = checks.build_context(
            self.account.confirmed_facts().values(),
            extra=(service.name, "Nacka + 15 km", self.customer.name),
        )
        for text in proposal.headlines:
            self.assertLessEqual(len(text), 30)
            self.assertEqual(checks.text_problems(text, context), [])
        for text in proposal.descriptions:
            self.assertLessEqual(len(text), 90)
            self.assertEqual(checks.text_problems(text, context), [])

    def test_a_confirmed_price_goes_into_the_ad_and_early_on_the_page(self):
        proposal = self.proposal(self.jour)
        self.assertEqual(proposal.headlines[1], "Rörjour från 995 kr")
        self.assertTrue(any("från 995 kr" in d for d in proposal.descriptions))
        self.assertEqual(proposal.page["points"][0], PRICE)
        self.assert_clean(proposal, self.jour)
        headlines, _ = generator.example_texts(self.account, self.jour, "Nacka + 15 km")
        self.assertIn("Rörjour från 995 kr", headlines)

    def test_no_price_without_a_confirmed_price_for_the_service(self):
        for service in (self.badrum, self.film):
            with self.subTest(service=service.name):
                proposal = self.proposal(service)
                text = " ".join(
                    proposal.headlines + proposal.descriptions + proposal.page["points"]
                )
                self.assertNotIn(" kr", text)
                self.assertNotIn("995", text)  # aldrig en annan tjänsts pris
                self.assertNotIn("1 900", text)  # inte ett obekräftat pris
                self.assert_clean(proposal, service)

    def test_the_price_is_added_when_ai_wrote_the_texts(self):
        response = tool_response(
            generator.TOOL_NAME,
            {
                "rubriker": ["Rörjour i Nacka", "Ring Lindqvist Rör", "Jour i Nacka"],
                "beskrivningar": ["Ring Lindqvist Rör om rörjour i Nacka.", "Vi jobbar i Nacka."],
            },
        )
        call = self.ai_on(response)
        proposal = self.proposal(self.jour)
        self.assertEqual(proposal.source, generator.SOURCE_AI)
        self.assertEqual(proposal.headlines[:2], ["Rörjour i Nacka", "Rörjour från 995 kr"])
        self.assertTrue(
            generator.has_price_from(
                proposal.descriptions,
                generator.info_for(
                    SimpleNamespace(account=self.account, service=self.jour, area="Nacka + 15 km")
                ),
            )
        )
        payload = json.loads(call.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(payload["från_pris"], "från 995 kr")
        self.assertIn("från_pris", call.call_args.kwargs["system"])
        self.assert_clean(proposal, self.jour)

    def test_amounts_are_read_as_written(self):
        self.assertEqual(generator.price_amount("Utryckning från 995 kr"), "995")
        self.assertEqual(generator.price_amount("Från 12 900 kr med montering"), "12 900")
        self.assertEqual(generator.price_amount("1 900:-"), "1 900")
        self.assertEqual(generator.price_amount("Pris på förfrågan"), "")
        self.assertEqual(
            generator.price_fact_key("Byte av varmvattenberedare"),
            "pris-byte-av-varmvattenberedare",
        )


# ---------------------------------------------------------------------------
# Vyerna: JSON, behörighet, byrån i kundvyn, demot
# ---------------------------------------------------------------------------


class ViewTests(AIFixture, TestCase):
    def urls(self, page):
        return (
            reverse("flamingo:app_page_ai_build", args=[page.pk]),
            reverse("flamingo:app_page_ai_rewrite", args=[page.pk]),
            reverse("flamingo:app_page_koll", args=[page.pk]),
        )

    def post(self, client, url, data):
        return client.post(url, json.dumps(data), content_type="application/json")

    def test_the_customer_gets_json(self):
        client = self.client_for(self.anna)
        build, rewrite, koll_url = self.urls(self.page)
        state = client.get(build).json()
        self.assertEqual(state["service_id"], self.jour.pk)
        self.assertEqual(state["goal"], "call")
        self.assertEqual([s["name"] for s in state["services"]][:1], ["Rörjour"])
        self.assertIn(PRICE, [f["value"] for f in state["used_facts"]])
        self.assertEqual(state["guard"], ai.GUARD_LINE)
        self.assertIn("hero", state["fields"])
        self.assertFalse(state["ai"]["available"])
        result = self.post(
            client, build, {"goal": "call", "service_id": self.jour.pk, "tone": "saklig"}
        ).json()
        self.assertEqual(
            set(result),
            {
                "blocks",
                "explanations",
                "used_facts",
                "missing",
                "source",
                "note",
                "problems",
                "goal",
                "tone",
                "service_id",
            },
        )
        self.assertEqual(result["source"], "mallar")
        suggestions = self.post(
            client, rewrite, {"block_id": self.page.draft_blocks[0]["id"], "field": "lead"}
        ).json()["suggestions"]
        self.assertEqual(len(suggestions), 3)
        self.assertEqual(set(suggestions[0]), {"text", "principle_key", "principle_label", "why"})
        koll_result = client.get(koll_url + "?which=draft").json()
        self.assertEqual(set(koll_result), {"score", "total", "summary", "items", "which"})
        for item in koll_result["items"]:
            self.assertEqual(
                set(item),
                {"key", "ok", "title", "text", "principle_key", "principle_label", "action"},
            )
        # Inget sparades.
        page = LandingPage.objects.get(pk=self.page.pk)
        self.assertEqual(page.rev, self.page.rev)

    def test_bad_requests(self):
        client = self.client_for(self.anna)
        build, rewrite, koll_url = self.urls(self.page)
        self.assertEqual(
            client.post(build, "inte json", content_type="application/json").status_code, 400
        )
        self.assertEqual(self.post(client, build, {"goal": "call"}).status_code, 400)
        response = self.post(client, rewrite, {"block_id": "b_finnsinte0001", "field": "title"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        self.assertEqual(self.post(client, rewrite, {"field": "title"}).status_code, 400)
        self.assertEqual(client.get(rewrite).status_code, 405)
        self.assertEqual(self.post(client, koll_url, {}).status_code, 405)
        too_big = {"block_id": "x", "field": "title", "fields": {"title": "x" * ai.MAX_BODY}}
        self.assertEqual(self.post(client, rewrite, too_big).status_code, 400)

    def test_another_accounts_page_or_service_is_404(self):
        client = self.client_for(self.anna)
        build, rewrite, koll_url = self.urls(self.other_page)
        self.assertEqual(client.get(build).status_code, 404)
        self.assertEqual(self.post(client, build, {"goal": "call"}).status_code, 404)
        self.assertEqual(
            self.post(client, rewrite, {"block_id": "b", "field": "title"}).status_code, 404
        )
        self.assertEqual(client.get(koll_url).status_code, 404)
        own_build = self.urls(self.page)[0]
        response = self.post(
            client, own_build, {"goal": "call", "service_id": self.other_service.pk}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            client.get(own_build + f"?service_id={self.other_service.pk}").status_code, 404
        )
        # Någon utan Flamingo får samma 404.
        stranger = User.objects.create_user("x@example.com", password="x")
        self.assertEqual(self.client_for(stranger).get(self.urls(self.page)[2]).status_code, 404)

    def test_staff_viewing_as_the_customer_can_use_it(self):
        client = self.client_for(self.staff, view_as=self.customer)
        build, rewrite, koll_url = self.urls(self.page)
        result = self.post(client, build, {"goal": "call", "service_id": self.jour.pk})
        self.assertEqual(result.status_code, 200)
        staff_id = result.json()["blocks"][0]["versions"][0]["by"]
        self.assertEqual(staff_id, self.staff.pk)
        response = self.post(
            client, rewrite, {"block_id": self.page.draft_blocks[0]["id"], "field": "title"}
        )
        self.assertEqual(len(response.json()["suggestions"]), 3)
        self.assertEqual(client.get(koll_url).status_code, 200)
        # Byrån i kundvyn på en annan kund når inte sidan.
        other = self.client_for(self.staff, view_as=self.other_customer)
        self.assertEqual(other.get(koll_url).status_code, 404)


DEMO_MEDIA = tempfile.mkdtemp(prefix="flamingo-pb-ai-demo-")


@override_settings(DEBUG=True, MEDIA_ROOT=DEMO_MEDIA)
class DemoTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(DEMO_MEDIA, ignore_errors=True)

    def setUp(self):
        # AI "påslagen": demot ska ändå aldrig anropa modellen.
        for patcher in (
            mock.patch("apps.assistant.llm.is_configured", return_value=True),
            mock.patch(
                "apps.assistant.llm.call", side_effect=AssertionError("AI ska inte anropas")
            ),
            *no_aws(),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_demo_uses_templates_and_calls_nothing(self):
        call_command("flamingo_demo", stdout=io.StringIO())
        account = FlamingoAccount.objects.get(is_demo=True)
        staff = User.objects.create_user("demo-byra", password="x12345678", is_staff=True)
        client = Client()
        client.force_login(staff)
        session = client.session
        session[VIEW_AS_KEY] = account.customer_id
        session.save()
        for page in LandingPage.objects.filter(account=account):
            hero = next(b for b in page.draft_blocks if b["type"] == "hero")
            with self.subTest(page=page.name):
                build = reverse("flamingo:app_page_ai_build", args=[page.pk])
                state = client.get(build).json()
                self.assertEqual(state["ai"], {"available": False, "note": ai.NOTE_DEMO})
                result = client.post(
                    build,
                    json.dumps({"goal": state["goal"], "service_id": state["service_id"]}),
                    content_type="application/json",
                ).json()
                self.assertEqual((result["source"], result["note"]), ("mallar", ai.NOTE_DEMO))
                self.assertEqual(
                    pagebuilder.validate_blocks(result["blocks"], account=account),
                    result["blocks"],
                )
                self.assertEqual(result["problems"], [])
                rewrite = client.post(
                    reverse("flamingo:app_page_ai_rewrite", args=[page.pk]),
                    json.dumps({"block_id": hero["id"], "field": "title"}),
                    content_type="application/json",
                ).json()
                self.assertEqual(rewrite["source"], "mallar")
                self.assertTrue(rewrite["suggestions"])
                koll_result = client.get(reverse("flamingo:app_page_koll", args=[page.pk])).json()
                self.assertGreater(koll_result["total"], 0)
        account.refresh_from_db()
        self.assertEqual(account.ai_count, 0)
