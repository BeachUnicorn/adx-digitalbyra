"""ADX Flamingo: sidbyggaren (apps/flamingo/pagebuilder/): blocken, schemat,
kontrollerna, renderaren för Ren, delade sidor, publiceringen och
migreringen av de gamla sidorna."""

import copy
import io
import re
import shutil
import tempfile
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.common.security import AI_TYPOGRAPHY_CHARS
from apps.projects.models import Customer

from . import checks, generator, pagebuilder
from .models import (
    Campaign,
    Fact,
    FlamingoAccount,
    LandingPage,
    Lead,
    MediaAsset,
    Service,
)
from .pagebuilder import facts, principles, registry, render
from .templatetags import flamingo_pb
from .testing import migration, pages_from_campaigns

User = get_user_model()
PHONE = "08-000 00 00"
#: AI-typografin, byggd ur kodpunkterna (vakten i apps/common/tests.py fäller
#: tecknen och deras escapes i källkoden).
DASH, LQ, RQ, ELLIPSIS = chr(0x2014), chr(0x201C), chr(0x201D), chr(0x2026)
#: Blockets klass i Ren (lp/ren/blocks/<typ>.html).
CSS = {
    "hero": "rn-hero",
    "price": "rn-price",
    "reviews_google": "rn-reviews",
    "reviews_reco": "rn-reco",
    "certificates": "rn-certs",
    "guarantee": "rn-guarantee",
    "person": "rn-person",
    "steps": "rn-steps",
    "before_after": "rn-ba",
    "area": "rn-area",
    "faq": "rn-faq",
    "form": "rn-form",
    "callbar": "rn-callbar",
}
ALERTS = {"INQUIRY_NOTIFICATION_EMAIL": "larm@adx.example"}
_MEDIA = tempfile.mkdtemp(prefix="flamingo-pb-")


def _png(color=(30, 90, 200), size=(800, 600)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return ContentFile(buffer.getvalue(), name="bild.png")


class PageFixture:
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
        cls.account = FlamingoAccount.objects.create(
            customer=cls.customer,
            is_enabled=True,
            google_place_id="ChIJexempel",
            google_place_name="Lindqvist Rör",
            google_maps_uri="https://maps.google.com/?cid=1",
            google_rating=Decimal("4.8"),
            google_review_count=37,
            google_reviews=[
                {
                    "id": "places/x/reviews/1",
                    "author": "Anna L.",
                    "author_uri": "https://www.google.com/maps/contrib/1",
                    "rating": 5,
                    "text": "Tydligt och noggrant.",
                    "time": "2026-09-01T10:00:00Z",
                    "relative": "för en månad sedan",
                },
                {
                    "id": "places/x/reviews/2",
                    "author": "Johan S.",
                    "author_uri": "",
                    "rating": 4,
                    "text": "Bra jobb.",
                    "time": "2026-08-01T10:00:00Z",
                    "relative": "",
                },
            ],
            google_reviews_selected=["places/x/reviews/2", "places/x/reviews/1"],
            # En intygad profil på Reco (blocket Omdömen från Reco, reco.py).
            reco_venue_id="5998572",
            reco_url="https://www.reco.se/lindqvist-ror-ab",
            reco_name="Lindqvist Rör AB",
        )
        for key, label, value in (
            ("telefon", "Telefon", PHONE),
            ("adress", "Adress", "Exempelvägen 4, Nacka"),
            ("oppettider", "Öppettider", "Vardagar 7-16"),
            ("omrade", "Område", "Nacka, Värmdö och Tyresö"),
            ("pris-rorjour", "Pris, Rörjour", "Utryckning från 995 kr"),
            ("behorighet", "Behörighet", "Säker Vatten-auktoriserade"),
            ("garanti", "Garanti", "Två års garanti på arbetet"),
            ("kontaktperson", "Kontaktperson", "Lisa Lindqvist"),
        ):
            Fact.objects.create(
                account=cls.account, key=key, label=label, value=value, confirmed=True
            )
        cls.jour = Service.objects.create(
            account=cls.account, name="Rörjour", sales_mode=Service.SALES_CALL
        )
        cls.badrum = Service.objects.create(
            account=cls.account, name="Badrumsrenovering", sales_mode=Service.SALES_QUOTE
        )
        cls.film = Service.objects.create(
            account=cls.account, name="Filmning av avlopp", sales_mode=Service.SALES_BOOK
        )
        cls.live = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum Nacka",
            area="Nacka + 15 km",
            status=Campaign.STATUS_LIVE,
            page={
                "title": "Badrumsrenovering i Nacka",
                "lead": "Berätta om ditt badrum.",
                "points": ["Nacka, Värmdö och Tyresö"],
                "phone": PHONE,
                "form_title": "Berätta om ditt badrum",
                "questions": [
                    {"key": "storlek", "label": "Ungefär hur stort? (m2)", "kind": "text"},
                    {"key": "nar", "label": "När passar det?", "kind": "date"},
                ],
                "note": "",
            },
        )
        cls.second = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum Värmdö",
            area="Värmdö + 10 km",
            status=Campaign.STATUS_LIVE,
            page={"title": "Eget"},
        )
        cls.draft = Campaign.objects.create(
            account=cls.account,
            service=cls.jour,
            name="Rörjour utkast",
            area="Nacka + 15 km",
            page={"title": "Rörjour i Nacka", "phone": PHONE},
        )
        pages_from_campaigns(cls.live, cls.second, cls.draft)
        cls.page = cls.live.landing_page

        cls.other_customer = Customer.objects.create(name="Hemlig Bygg AB")
        cls.other_account = FlamingoAccount.objects.create(
            customer=cls.other_customer, is_enabled=True
        )
        cls.other_page = LandingPage.objects.create(account=cls.other_account, name="Hemlig")

    def setUp(self):
        super().setUp()
        cache.clear()
        # AI avstängd: generatorn skriver med mallar och anropar aldrig modellen.
        for target, kwargs in (
            ("apps.assistant.llm.is_configured", {"return_value": False}),
            ("apps.assistant.llm.call", {"side_effect": AssertionError("AI ska inte anropas")}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def media(self, **fields):
        fields.setdefault("alt", "Ett badrum")
        asset = MediaAsset(account=self.account, **fields)
        asset.file.save("bild.png", _png(), save=False)
        asset.thumb.save("tumme.png", _png(size=(320, 240)), save=False)
        asset.save()
        return asset

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def every_block(self):
        """Ett block av varje typ och variant, med bilder där det behövs."""
        image, before, after = self.media(), self.media(), self.media()
        blocks = []
        for block_type in registry.TYPES_LIST:
            for variant in block_type.variants:
                block = pagebuilder.new_block(
                    block_type.key,
                    variant.key,
                    self.account,
                    ctx=pagebuilder.BuildContext(
                        service="Rörjour", places=["Nacka", "Värmdö"], mode=Service.SALES_QUOTE
                    ),
                )
                version = pagebuilder.active_version(block)
                if "image" in version["fields"]:
                    version["fields"]["image"] = image.pk
                if block_type.key == "before_after":
                    version["fields"]["before"] = before.pk
                    version["fields"]["after"] = after.pk
                if block_type.key == "person":
                    version["fields"]["text"] = "Jag svarar själv i telefon."
                blocks.append(block)
        return blocks


# ---------------------------------------------------------------------------
# Blocken och schemat
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class BlockSchemaTests(PageFixture, TestCase):
    def test_new_block_has_the_documented_shape(self):
        block = pagebuilder.new_block("hero", "call", self.account)
        self.assertEqual(set(block), {"id", "type", "variant", "active", "versions"})
        self.assertRegex(block["id"], r"^b_[A-Za-z0-9]{12}$")
        version = block["versions"][0]
        self.assertEqual(set(version), {"id", "fields", "source", "by", "at", "sig"})
        self.assertRegex(version["id"], r"^v_[A-Za-z0-9]{12}$")
        # Serverns signatur (pagebuilder.is_signed): källan går inte att ändra.
        self.assertTrue(pagebuilder.is_signed(version))
        self.assertFalse(pagebuilder.is_signed(dict(version, source="customer")))
        self.assertEqual(block["active"], version["id"])
        self.assertEqual(version["source"], "template")
        self.assertIsNone(version["by"])
        self.assertEqual(version["fields"]["phone"], PHONE)
        self.assertEqual(pagebuilder.validate_blocks([block]), [block])

    def test_the_registry_is_complete(self):
        """Varje typ har namn, ikon (ritad i pagebuilder/icons.html), varför,
        princip och de varianter Giovanni beslutade."""
        from django.template.loader import render_to_string

        expected = {
            "hero": ["call", "form", "image", "text"],
            "price": ["from", "examples", "fixed"],
            "reviews_google": ["cards", "quote", "line"],
            "reviews_reco": ["stor", "medel", "liten", "staende"],
            "certificates": ["badges", "icons"],
            "guarantee": ["short", "terms"],
            "person": ["image", "noimage"],
            "steps": ["three", "four"],
            "before_after": ["slider", "pair"],
            "area": ["list", "map"],
            "faq": ["three", "six"],
            "form": ["short", "questions", "booking"],
            "callbar": ["call", "call_write"],
        }
        self.assertEqual({k: list(t.variant_keys) for k, t in registry.TYPES.items()}, expected)
        icons = render_to_string("flamingo/pagebuilder/icons.html")
        for block_type in registry.TYPES_LIST:
            with self.subTest(type=block_type.key):
                self.assertTrue(block_type.name and block_type.why and block_type.principle)
                self.assertIn(f'id="pb-i-{block_type.icon}"', icons)
                self.assertTrue(all(v.name for v in block_type.variants))
        self.assertEqual(len(pagebuilder.schema()), 13)

    def test_templates_use_only_confirmed_facts(self):
        bare = FlamingoAccount.objects.create(customer=Customer.objects.create(name="Tom AB"))
        for kind in ("price", "certificates", "guarantee", "callbar"):
            with self.subTest(kind=kind), self.assertRaises(pagebuilder.BlockUnavailable):
                pagebuilder.new_block(kind, None, bare)
        available = registry.available(bare)
        self.assertFalse(available["price"][0])
        self.assertIn("bekräftat pris", available["price"][1])
        self.assertTrue(available["steps"][0])
        hero = pagebuilder.active_fields(pagebuilder.new_block("hero", "text", bare))
        self.assertEqual(hero["phone"], "")
        self.assertEqual(hero["points"], [])
        # Med uppgifterna: priset, certifikatet och garantin kommer därifrån.
        # Priset bara för sin egen tjänst: utan tjänst finns inget pris att visa.
        ctx = pagebuilder.BuildContext(service="Rörjour")
        price = pagebuilder.new_block("price", "from", self.account, ctx=ctx)
        self.assertEqual(pagebuilder.active_fields(price)["price"], "Utryckning från 995 kr")
        with self.assertRaises(pagebuilder.BlockUnavailable):
            pagebuilder.new_block("price", "from", self.account)
        certs = pagebuilder.active_fields(pagebuilder.new_block("certificates", None, self.account))
        self.assertEqual(certs["items"][0]["name"], "Säker Vatten-auktoriserade")
        guarantee = pagebuilder.active_fields(
            pagebuilder.new_block("guarantee", None, self.account)
        )
        # Garantin är rubriken, inte bara ordet "Garanti" ovanför den.
        self.assertEqual(guarantee["title"], "Två års garanti på arbetet")
        self.assertEqual(guarantee["text"], "")
        # Prisblockets rubrik är frågan som priset svarar på.
        self.assertEqual(pagebuilder.active_fields(price)["title"], "Vad kostar rörjour?")

    def test_versions(self):
        block = pagebuilder.new_block("hero", "call", self.account)
        first = block["active"]
        fields = dict(pagebuilder.active_fields(block), title="Rörjour på Värmdö")
        version = pagebuilder.add_version(block, fields, "customer", self.anna)
        self.assertEqual(block["active"], version["id"])
        self.assertEqual(version["by"], self.anna.pk)
        self.assertEqual(pagebuilder.active_fields(block)["title"], "Rörjour på Värmdö")
        pagebuilder.activate_version(block, first)
        self.assertNotEqual(pagebuilder.active_fields(block)["title"], "Rörjour på Värmdö")
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.activate_version(block, "v_finnsinte123")
        for n in range(pagebuilder.MAX_VERSIONS + 3):
            pagebuilder.add_version(block, dict(fields, title=f"Rubrik {n}"), "ai", None)
        self.assertEqual(len(block["versions"]), pagebuilder.MAX_VERSIONS)
        self.assertEqual(pagebuilder.validate_blocks([block])[0]["active"], block["active"])

    def test_validation_rejects_unknown_types_fields_and_long_text(self):
        good = pagebuilder.new_block("hero", "call", self.account)

        def broken(change):
            block = copy.deepcopy(good)
            change(block)
            return block

        cases = {
            "okänd typ": lambda b: b.update(type="slideshow"),
            "okänd variant": lambda b: b.update(variant="video"),
            "okänt fält": lambda b: b["versions"][0]["fields"].update(color="red"),
            "för lång rubrik": lambda b: b["versions"][0]["fields"].update(title="x" * 200),
            "fel id": lambda b: b.update(id="block-1"),
            "aktiv saknas": lambda b: b.update(active="v_000000000000"),
            "okänd källa": lambda b: b["versions"][0].update(source="robot"),
            "extra nyckel": lambda b: b.update(html="<b>"),
            "bild som text": lambda b: b["versions"][0]["fields"].update(image="bild.png"),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(pagebuilder.BlockError):
                pagebuilder.validate_blocks([broken(change)])
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.validate_blocks([good, copy.deepcopy(good)])
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.validate_blocks({"blocks": []})

    def test_validation_strips_html_and_ai_typography(self):
        block = pagebuilder.new_block("form", "questions", self.account)
        fields = block["versions"][0]["fields"]
        fields["title"] = "<b>Berätta</b> om <script>alert(1)</script>jobbet"
        fields["note"] = f"Rad ett {DASH} och {LQ}rad{RQ} två{ELLIPSIS}"
        fields["questions"] = [{"label": "<i>Hur stort?</i>", "kind": "text"}]
        clean = pagebuilder.validate_blocks([block])[0]["versions"][0]["fields"]
        self.assertEqual(clean["title"], "Berätta om jobbet")
        self.assertFalse(set(clean["note"]) & AI_TYPOGRAPHY_CHARS)
        self.assertEqual(
            clean["questions"], [{"key": "hur-stort", "label": "Hur stort?", "kind": "text"}]
        )

    def test_media_must_belong_to_the_account(self):
        foreign = MediaAsset(account=self.other_account, alt="Främmande")
        foreign.file.save("x.png", _png(), save=False)
        foreign.save()
        block = pagebuilder.new_block("hero", "image", self.account)
        block["versions"][0]["fields"]["image"] = foreign.pk
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.validate_blocks([block], account=self.account)
        mine = self.media()
        block["versions"][0]["fields"]["image"] = mine.pk
        pagebuilder.validate_blocks([block], account=self.account)
        self.assertTrue(mine.file.name.startswith("flamingo/"))
        self.assertNotIn("bild", mine.file.name)

    def test_save_draft_needs_the_current_rev(self):
        page = LandingPage.objects.create(account=self.account, name="Rev")
        block = pagebuilder.new_block("hero", "call", self.account)
        rev = pagebuilder.save_draft(page, [block], rev=page.rev)
        self.assertEqual(rev, 2)
        with self.assertRaises(pagebuilder.StaleRevision):
            pagebuilder.save_draft(page, [block], rev=1)
        with self.assertRaises(pagebuilder.BlockError):
            pagebuilder.save_draft(page, [{"type": "nope"}], rev=2)
        page.refresh_from_db()
        self.assertEqual(page.rev, 2)
        self.assertEqual(len(page.draft_blocks), 1)


# ---------------------------------------------------------------------------
# Kontrollerna
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class PageProblemTests(PageFixture, TestCase):
    def blocks(self, *pairs):
        out = []
        for kind, variant in pairs:
            out.append(pagebuilder.new_block(kind, variant, self.account))
        return out

    def messages(self, blocks):
        return [p.message for p in pagebuilder.page_problems(self.page, blocks=blocks)]

    def test_a_template_page_passes(self):
        blocks = self.blocks(("hero", "call"), ("steps", "three"), ("form", "short"))
        self.assertEqual(self.messages(blocks), [])

    def test_texts_go_through_the_campaign_checks(self):
        blocks = self.blocks(("hero", "call"), ("form", "short"))
        hero = blocks[0]["versions"][0]["fields"]
        hero["title"] = "Billigast i stan, inom 2 timmar"
        hero["points"] = ["24 års erfarenhet"]
        problems = pagebuilder.page_problems(self.page, blocks=blocks)
        text = " ".join(p.message for p in problems)
        self.assertIn("billigast", text)
        self.assertIn("Lova inga tider", text)
        self.assertIn("Siffran 24", text)
        point = next(p for p in problems if "24" in p.message)
        self.assertEqual((point.field, point.part, point.index), ("page", "points", 0))
        self.assertEqual(point.block, blocks[0]["id"])
        self.assertEqual(point.where, "Toppen, punkt 1")

    def test_phone_must_be_a_confirmed_fact(self):
        blocks = self.blocks(("hero", "call"), ("callbar", "call"))
        blocks[1]["versions"][0]["fields"]["phone"] = "070-123 45 67"
        self.assertIn(
            "Telefonnumret finns inte bland dina bekräftade uppgifter.", self.messages(blocks)
        )

    def test_a_page_needs_a_way_to_reach_the_business(self):
        blocks = self.blocks(("hero", "text"))
        blocks[0]["versions"][0]["fields"]["phone"] = ""
        self.assertIn(pagebuilder.problems.MSG_NO_CONTACT, self.messages(blocks))
        self.assertIn(
            pagebuilder.problems.MSG_NO_HERO, self.messages(self.blocks(("form", "short")))
        )
        self.assertIn(pagebuilder.problems.MSG_EMPTY, self.messages([]))

    def test_no_ai_typography(self):
        blocks = self.blocks(("hero", "call"))
        blocks[0]["versions"][0]["fields"]["lead"] = f"Ring oss {DASH} vi lyssnar"
        self.assertIn(pagebuilder.problems.MSG_TYPOGRAPHY, self.messages(blocks))

    def test_structure(self):
        blocks = self.blocks(("hero", "form"), ("hero", "image"), ("before_after", "pair"))
        messages = " ".join(self.messages(blocks))
        self.assertIn("mer än ett block av sorten Toppen", messages)
        self.assertIn("Toppen med formulär behöver ett formulärblock", messages)
        self.assertIn("Välj en bild", messages)

    def test_campaign_checks_include_the_landing_page(self):
        self.assertEqual(
            [p for p in checks.validate(self.live) if p.field == "page"],
            [],
            checks.validate(self.live),
        )
        page = self.live.landing_page
        blocks = page.published_blocks
        blocks[0]["versions"][0]["fields"]["title"] = "Billigast i Nacka"
        page.published = {"blocks": blocks}
        page.save()
        self.live.refresh_from_db()
        self.assertTrue([p for p in checks.validate(self.live) if p.field == "page"])
        orphan = Campaign.objects.create(account=self.account, service=self.jour, name="Utan sida")
        self.assertIn(
            "Kampanjen har ingen landningssida än.", [p.message for p in checks.validate(orphan)]
        )


# ---------------------------------------------------------------------------
# Renderaren
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class RenderTests(PageFixture, TestCase):
    def page_with(self, blocks, **fields):
        return LandingPage.objects.create(
            account=self.account,
            name=f"Prov {LandingPage.objects.count()}",
            draft={"blocks": blocks},
            **fields,
        )

    def test_every_block_type_and_variant_renders_in_both_modes(self):
        blocks = self.every_block()
        page = self.page_with(blocks)
        for block in blocks:
            for editing in (False, True):
                with self.subTest(type=block["type"], variant=block["variant"], editing=editing):
                    html = pagebuilder.render_block_html(page, block, self.account, editing=editing)
                    self.assertIn(f'class="rn-block {CSS[block["type"]]} ', html)
                    self.assertEqual("data-pb-" in html, editing)
                    if editing:
                        self.assertIn(f'data-pb-block="{block["id"]}"', html)
                        self.assertIn(f'data-pb-variant="{block["variant"]}"', html)
        html = pagebuilder.render_page_html(page, self.account, self.live)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertNotIn("data-pb-", html)
        editing = pagebuilder.render_page_html(page, self.account, self.live, editing=True)
        self.assertIn('data-pb-field="title"', editing)
        self.assertIn('data-pb-field="steps.0.title"', editing)
        self.assertIn('data-pb-field="image" data-pb-media', editing)
        # Samma struktur: redigeringsattributen är det enda som skiljer.
        stripped = re.sub(r' data-pb-[a-z]+(="[^"]*")?', "", editing)
        stripped = re.sub(r"<(p|span|h2|div)[^>]*></\1>", "", stripped)
        self.assertEqual(
            re.sub(r"\s+", " ", stripped).count("<section"),
            re.sub(r"\s+", " ", html).count("<section"),
        )

    def test_every_tel_link_is_counted(self):
        page = self.page_with(self.every_block())
        html = pagebuilder.render_page_html(page, self.account, self.live)
        links = re.findall(r'<a [^>]*href="tel:[^"]*"[^>]*>', html)
        self.assertGreaterEqual(len(links), 5)
        for link in links:
            self.assertIn("data-fl-call", link)

    def test_fields_are_escaped(self):
        block = pagebuilder.new_block("hero", "call", self.account)
        # Förbi schemat, rakt i databasen: mallen ska ändå escapa.
        block["versions"][0]["fields"]["title"] = '<script>alert("x")</script>'
        block["versions"][0]["fields"]["points"] = ["<img src=x onerror=alert(1)>"]
        page = self.page_with([block])
        html = pagebuilder.render_page_html(page, self.account, self.live, editing=True)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html)

    def test_google_reviews_with_attribution_and_hidden_without(self):
        block = pagebuilder.new_block("reviews_google", "cards", self.account)
        page = self.page_with([block])
        html = pagebuilder.render_page_html(page, self.account, self.live)
        self.assertIn("Omdömen från Google", html)
        self.assertIn("Anna L.", html)
        self.assertIn('href="https://www.google.com/maps/contrib/1"', html)
        self.assertIn('href="https://maps.google.com/?cid=1"', html)
        self.assertLess(html.index("Johan S."), html.index("Anna L."))  # kundens ordning
        self.assertNotIn("googleusercontent", html)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(google_reviews_selected=[])
        self.account.refresh_from_db()
        html = pagebuilder.render_page_html(page, self.account, self.live)
        self.assertNotIn("rn-reviews", html)
        editing = pagebuilder.render_page_html(page, self.account, self.live, editing=True)
        self.assertIn("data-pb-empty", editing)

    def test_images_have_alt_size_and_lazy_loading_except_the_hero(self):
        image = self.media(alt="Nytt badrum")
        hero = pagebuilder.new_block("hero", "image", self.account)
        hero["versions"][0]["fields"]["image"] = image.pk
        person = pagebuilder.new_block("person", "image", self.account)
        person["versions"][0]["fields"]["image"] = image.pk
        html = pagebuilder.render_page_html(self.page_with([hero, person]), self.account, self.live)
        imgs = re.findall(r"<img [^>]*>", html)
        self.assertEqual(len(imgs), 2)
        self.assertIn('fetchpriority="high"', imgs[0])
        self.assertNotIn("loading=", imgs[0])
        self.assertIn('loading="lazy"', imgs[1])
        for img in imgs:
            self.assertIn('alt="Nytt badrum"', img)
            self.assertIn('width="800"', img)
            self.assertIn('height="600"', img)
            self.assertIn("srcset=", img)

    def test_palettes_reach_wcag_aa(self):
        page = LandingPage(account=self.account, name="Färg")
        for key, _label in LandingPage.PALETTE_CHOICES:
            page.palette = key
            page.logo_colors = {"primary": "#FFE45C"} if key == LandingPage.PALETTE_LOGO else {}
            with self.subTest(palette=key):
                colors = render.palette_vars(page)
                # Knapparnas text på huvudfärgen, och färgen som text på vitt
                # (länkar, överrubriken) och på den ljusa tonen.
                self.assertGreaterEqual(
                    render.contrast(colors["--rn-primary"], colors["--rn-on-primary"]), 4.5
                )
                self.assertGreaterEqual(render.contrast(colors["--rn-primary-ink"], "#FFFFFF"), 4.5)
                self.assertGreaterEqual(
                    render.contrast(colors["--rn-primary-ink"], colors["--rn-primary-soft"]), 4.5
                )
                # Den röda paletten är inte formulärets felfärg.
                self.assertNotEqual(colors["--rn-primary"], render.ERROR)
        # En ljus logotypfärg står kvar på knapparna, med mörk text på dem.
        page.palette = LandingPage.PALETTE_LOGO
        page.logo_colors = {"primary": "#FFE45C"}
        colors = render.palette_vars(page)
        self.assertEqual(colors["--rn-primary"], "#FFE45C")
        self.assertEqual(colors["--rn-on-primary"], render.INK)
        self.assertNotEqual(colors["--rn-primary-ink"], "#FFE45C")
        # En mörk logotypfärg bär vit text som förut.
        page.logo_colors = {"primary": "#0B6E4F"}
        self.assertEqual(render.palette_vars(page)["--rn-on-primary"], render.WHITE)
        # Grafit får en accent: de ljusa tonerna är inte grått på grått.
        page.palette = LandingPage.PALETTE_GRAPHITE
        colors = render.palette_vars(page)
        self.assertEqual(colors["--rn-primary"], render.PALETTES[LandingPage.PALETTE_GRAPHITE])
        self.assertNotEqual(colors["--rn-primary-ink"], colors["--rn-primary"])
        self.assertNotEqual(colors["--rn-primary-soft"], "#ECECEC")
        page.palette = LandingPage.PALETTE_LOGO
        page.logo_colors = {"primary": "red; } body { display:none"}
        self.assertEqual(render.palette_vars(page)["--rn-primary"], render.PALETTES["blue"])

    def test_no_external_fonts_or_analytics(self):
        html = pagebuilder.render_page_html(
            self.page_with(self.every_block()), self.account, self.live
        )
        self.assertNotIn("fonts.googleapis", html)
        self.assertNotIn("fonts.gstatic", html)
        self.assertNotIn("analytics", html)
        self.assertIn("figtree-latin.woff2", html)
        self.assertIn('name="robots" content="noindex', html)


# ---------------------------------------------------------------------------
# /lp/: formuläret, delade sidor, utkast och demo
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class LandingViewTests(PageFixture, TestCase):
    def test_the_form_creates_a_lead_with_answers(self):
        response = Client().post(
            self.live.landing_url,
            {
                "name": "Anna Lind",
                "phone": "070-111 22 33",
                "message": "Hela badrummet.",
                "q_storlek": "6",
                "q_nar": "2026-10-20",
                "gclid": "Cj0abc",
            },
            REMOTE_ADDR="10.0.0.7",
        )
        self.assertRedirects(
            response, reverse("flamingo_public:thanks", args=[self.live.page_slug])
        )
        lead = Lead.objects.get(campaign=self.live)
        self.assertEqual(
            lead.answers, {"Ungefär hur stort? (m2)": "6", "När passar det?": "2026-10-20"}
        )
        self.assertEqual(lead.gclid, "Cj0abc")
        self.assertEqual(mail.outbox, [])

    def test_a_shared_page_keeps_each_campaigns_leads(self):
        pagebuilder.use_shared_page(self.second, self.page)
        self.second.refresh_from_db()
        self.assertEqual(self.second.landing_page, self.page)
        self.assertEqual(set(pagebuilder.campaigns_using(self.page)), {self.live, self.second})
        for campaign, ip, click_ip in (
            (self.live, "10.0.1.1", "10.0.2.1"),
            (self.second, "10.0.1.2", "10.0.2.2"),
        ):
            html = Client().get(campaign.landing_url).content.decode()
            self.assertIn("Badrumsrenovering i Nacka", html)
            self.assertIn(reverse("flamingo_public:call_click", args=[campaign.page_slug]), html)
            Client().post(
                campaign.landing_url,
                {"name": "Ola", "phone": "070-222 33 44", "q_storlek": "4"},
                REMOTE_ADDR=ip,
            )
            Client().post(
                reverse("flamingo_public:call_click", args=[campaign.page_slug]),
                REMOTE_ADDR=click_ip,
            )
        for campaign in (self.live, self.second):
            sources = sorted(
                Lead.objects.filter(campaign=campaign).values_list("source", flat=True)
            )
            self.assertEqual(sources, [Lead.SOURCE_CALL_CLICK, Lead.SOURCE_FORM], campaign.name)
        with self.assertRaises(pagebuilder.PageError):
            pagebuilder.use_shared_page(self.second, self.other_page)
        own = pagebuilder.ensure_own_page(self.second)
        self.assertNotEqual(own.pk, self.page.pk)
        self.assertTrue(
            own.is_published
        )  # live: kopian är publicerad, besökarna ser ingen skillnad

    def test_visitors_see_the_published_version_and_staff_the_draft(self):
        blocks = self.page.draft_blocks
        blocks[0]["versions"][0]["fields"]["title"] = "Nytt utkast till rubrik"
        pagebuilder.save_draft(self.page, blocks, rev=self.page.rev)
        public = Client().get(self.live.landing_url).content.decode()
        self.assertNotIn("Nytt utkast till rubrik", public)
        self.assertNotIn(
            "Nytt utkast", Client().get(self.live.landing_url + "?utkast=1").content.decode()
        )
        staff = self.client_for(self.staff)
        self.assertIn(
            "Nytt utkast till rubrik",
            staff.get(self.live.landing_url + "?utkast=1").content.decode(),
        )
        self.assertIn("Visa utkastet", staff.get(self.live.landing_url).content.decode())

    def test_a_live_campaign_without_a_published_page_is_404(self):
        LandingPage.objects.filter(pk=self.page.pk).update(published_at=None)
        self.assertEqual(Client().get(self.live.landing_url).status_code, 404)
        self.assertEqual(self.client_for(self.staff).get(self.live.landing_url).status_code, 200)

    def test_draft_and_paused_campaigns_are_404_for_the_public(self):
        self.assertEqual(Client().get(self.draft.landing_url).status_code, 404)
        Campaign.objects.filter(pk=self.live.pk).update(status=Campaign.STATUS_PAUSED)
        self.assertEqual(Client().get(self.live.landing_url).status_code, 404)
        response = self.client_for(self.staff).get(self.draft.landing_url)
        self.assertContains(response, "Förhandsvisning")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_a_call_only_page_still_sends_csrf_for_the_click(self):
        page = LandingPage.objects.create(
            account=self.account,
            name="Bara ring",
            draft={"blocks": [pagebuilder.new_block("hero", "call", self.account)]},
        )
        pagebuilder.publish_page(page)
        Campaign.objects.filter(pk=self.live.pk).update(landing_page=page)
        html = Client().get(self.live.landing_url).content.decode()
        self.assertIn('name="csrfmiddlewaretoken"', html)
        self.assertNotIn('id="formular"', html)


# ---------------------------------------------------------------------------
# Publiceringen och kampanjerna
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA, **ALERTS)
class PublishTests(PageFixture, TestCase):
    def test_publish_alerts_adx_and_never_the_customer(self):
        pagebuilder.use_shared_page(self.second, self.page)
        mail.outbox.clear()  # bytet av sida larmar också (test_pagebuilder_fixes)
        blocks = self.page.draft_blocks
        blocks[0]["versions"][0]["fields"]["title"] = "Badrumsrenovering i Nacka och Värmdö"
        pagebuilder.save_draft(self.page, blocks, rev=self.page.rev)
        result = pagebuilder.publish_page(self.page, self.anna)
        self.assertTrue(result.alerted)
        self.assertEqual({c.pk for c in result.live_campaigns}, {self.live.pk, self.second.pk})
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["larm@adx.example"])
        self.assertNotIn("anna@ror.se", message.to + message.cc + message.bcc)
        self.assertIn("Kunden har inte mejlats", message.body)
        self.page.refresh_from_db()
        self.assertEqual(self.page.published_blocks, self.page.draft_blocks)
        self.assertIn("Nacka och Värmdö", Client().get(self.second.landing_url).content.decode())

    def test_publish_refuses_problems_and_changes_nothing(self):
        blocks = self.page.draft_blocks
        blocks[0]["versions"][0]["fields"]["title"] = "Billigast i Nacka"
        pagebuilder.save_draft(self.page, blocks, rev=self.page.rev)
        before = self.page.published_blocks
        with self.assertRaises(pagebuilder.PageProblems) as raised:
            pagebuilder.publish_page(self.page)
        self.assertTrue(raised.exception.problems)
        self.page.refresh_from_db()
        self.assertEqual(self.page.published_blocks, before)
        self.assertEqual(mail.outbox, [])

    def test_no_alert_without_a_live_campaign_or_for_a_demo(self):
        draft_page = self.draft.landing_page
        result = pagebuilder.publish_page(draft_page)
        self.assertFalse(result.alerted)
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.page.refresh_from_db()
        self.assertFalse(pagebuilder.publish_page(self.page).alerted)
        self.assertEqual(mail.outbox, [])

    def test_going_live_publishes_a_page_that_was_never_published(self):
        page = self.draft.landing_page
        self.assertFalse(page.is_published)
        Campaign.objects.filter(pk=self.draft.pk).update(
            status=Campaign.STATUS_NEEDS_CUSTOMER, approved_at="2026-10-03T10:00:00Z"
        )
        FlamingoAccount.objects.filter(pk=self.account.pk).update(
            google_status=FlamingoAccount.GOOGLE_LINKED, google_ads_customer_id="123-456-7890"
        )
        staff = self.client_for(self.staff)
        url = reverse("manage:flamingo_publish", args=[self.draft.pk])
        # Ett problem på sidan stoppar publiceringen.
        blocks = page.draft_blocks
        blocks[0]["versions"][0]["fields"]["title"] = "Rörjour inom 1 timme"
        pagebuilder.save_draft(page, blocks, rev=page.rev)
        staff.post(url, {"action": "publish", "manual": "1"})
        self.draft.refresh_from_db()
        self.assertNotEqual(self.draft.status, Campaign.STATUS_LIVE)
        page.refresh_from_db()
        self.assertFalse(page.is_published)
        blocks[0]["versions"][0]["fields"]["title"] = "Rörjour i Nacka"
        pagebuilder.save_draft(page, blocks, rev=page.rev)
        staff.post(url, {"action": "publish", "manual": "1"})
        self.draft.refresh_from_db()
        page.refresh_from_db()
        self.assertEqual(self.draft.status, Campaign.STATUS_LIVE)
        self.assertTrue(page.is_published)
        self.assertEqual(Client().get(self.draft.landing_url).status_code, 200)

    def test_the_generator_builds_the_campaigns_own_page(self):
        cases = (
            (self.jour, ["hero:call", "form:short", "callbar:call"]),
            (self.badrum, ["hero:form", "form:questions"]),
            (self.film, ["hero:form", "form:booking"]),
        )
        for service, expected in cases:
            with self.subTest(mode=service.sales_mode):
                campaign = Campaign.objects.create(
                    account=self.account, service=service, name=service.name, area="Nacka + 15 km"
                )
                generator.build_proposal(campaign)
                campaign.refresh_from_db()
                page = campaign.landing_page
                self.assertEqual(
                    [f"{b['type']}:{b['variant']}" for b in page.draft_blocks], expected
                )
                self.assertFalse(page.is_published)
                self.assertEqual(campaign.page, {})
                self.assertEqual(pagebuilder.page_problems(page), [])
                form = pagebuilder.form_spec(page.draft_blocks)
                if service == self.badrum:
                    self.assertEqual([q["key"] for q in form.questions], ["jobbet", "storlek"])
                if service == self.film:
                    self.assertIn("date", [q["kind"] for q in form.questions])

    def test_a_new_proposal_only_rebuilds_an_untouched_page(self):
        campaign = Campaign.objects.create(
            account=self.account, service=self.badrum, name="Ny", area="Nacka + 15 km"
        )
        generator.build_proposal(campaign)
        campaign.refresh_from_db()
        page = campaign.landing_page
        self.assertTrue(pagebuilder.is_untouched(page))
        blocks = page.draft_blocks
        pagebuilder.add_version(
            blocks[0],
            dict(pagebuilder.active_fields(blocks[0]), title="Mitt"),
            "customer",
            self.anna,
        )
        pagebuilder.save_draft(page, blocks, rev=page.rev)
        proposal = generator.build_proposal(campaign, save=False)
        self.assertFalse(pagebuilder.refresh_from_proposal(campaign, proposal.page))
        page.refresh_from_db()
        self.assertEqual(pagebuilder.active_fields(page.draft_blocks[0])["title"], "Mitt")

    def test_the_customer_and_staff_views(self):
        client = self.client_for(self.anna)
        self.assertContains(client.get(reverse("flamingo:app_pages")), self.page.name)
        response = client.get(reverse("flamingo:app_page", args=[self.page.pk]))
        self.assertContains(response, "Publicera ändringarna")
        self.assertContains(response, "srcdoc=")
        other = client.get(reverse("flamingo:app_page", args=[self.other_page.pk]))
        self.assertEqual(other.status_code, 404)
        tab = client.get(reverse("flamingo:app_campaign", args=[self.live.pk]) + "?flik=sidan")
        self.assertContains(tab, "Öppna sidan i sidbyggaren")
        response = client.post(reverse("flamingo:app_page_publish", args=[self.page.pk]))
        self.assertEqual(response.status_code, 302)
        staff = self.client_for(self.staff)
        review = staff.get(reverse("manage:flamingo_review", args=[self.live.pk]))
        self.assertContains(review, "Öppna sidan i sidbyggaren som kunden")
        self.assertContains(review, reverse("flamingo:app_page", args=[self.page.pk]))
        self.assertNotContains(review, 'name="page_title"')
        # Medan ADX granskar ligger knappen i granskningsformuläret och hör
        # till ett eget formulär utanför det (formulär i formulär fungerar inte).
        Campaign.objects.filter(pk=self.second.pk).update(status=Campaign.STATUS_IN_REVIEW)
        html = staff.get(reverse("manage:flamingo_review", args=[self.second.pk])).content.decode()
        review_form = html.split('id="granskning"', 1)[1].split("</form>", 1)[0]
        self.assertNotIn("<form", review_form)
        self.assertIn('form="mf-landing-view-as"', review_form)
        self.assertIn('<form id="mf-landing-view-as"', html)


# ---------------------------------------------------------------------------
# Migreringen och demot
# ---------------------------------------------------------------------------


class MigrationTests(TestCase):
    def test_the_mapping_keeps_content_and_question_keys(self):
        blocks_for = migration().blocks_for
        page = {
            "title": "Badrumsrenovering i Nacka",
            "lead": "Berätta om ditt badrum.",
            "points": ["Nacka", "", "Värmdö"],
            "phone": "",
            "form_title": "",
            "questions": [
                {"key": "storlek", "label": "Ungefär hur stort?", "kind": "text"},
                {"key": "storlek", "label": "Dubblett", "kind": "text"},
                {"key": "nar", "label": "När?", "kind": "date"},
                {"key": "x", "label": "Konstig sort", "kind": "radio"},
            ],
            "note": "Ingen stress.",
        }
        hero, form = blocks_for(
            page, "quote", "Badrum", "08-000 00 00", "2026-10-03T10:00:00+00:00"
        )
        self.assertEqual((hero["type"], hero["variant"]), ("hero", "form"))
        fields = hero["versions"][0]["fields"]
        self.assertEqual(fields["title"], "Badrumsrenovering i Nacka")
        self.assertEqual(fields["points"], ["Nacka", "Värmdö"])
        self.assertEqual(fields["phone"], "")  # "phone" på sidan vann, även tomt
        self.assertEqual((form["type"], form["variant"]), ("form", "questions"))
        form_fields = form["versions"][0]["fields"]
        self.assertEqual(form_fields["title"], "Berätta om jobbet")
        self.assertEqual(
            form_fields["questions"],
            [
                {"key": "storlek", "label": "Ungefär hur stort?", "kind": "text"},
                {"key": "nar", "label": "När?", "kind": "date"},
                {"key": "x", "label": "Konstig sort", "kind": "text"},
            ],
        )
        self.assertEqual(form_fields["note_title"], "")
        pagebuilder.validate_blocks([hero, form])

        call = blocks_for(
            {"note": "Stäng kranen."},
            "call",
            "Rörjour",
            "08-000 00 00",
            "2026-10-03T10:00:00+00:00",
        )
        self.assertEqual([b["variant"] for b in call], ["call", "short"])
        self.assertEqual(call[0]["versions"][0]["fields"]["title"], "Rörjour")
        self.assertEqual(call[0]["versions"][0]["fields"]["phone"], "08-000 00 00")
        self.assertEqual(call[1]["versions"][0]["fields"]["note_title"], "Medan du väntar")
        book = blocks_for({}, "book", "Filmning", "", "2026-10-03T10:00:00+00:00")
        self.assertEqual([b["variant"] for b in book], ["form", "booking"])

    def test_forwards_publishes_live_and_paused_only(self):
        account = FlamingoAccount.objects.create(
            customer=Customer.objects.create(name="Migrering AB"), is_enabled=True
        )
        service = Service.objects.create(account=account, name="Takbyte")
        campaigns = {}
        for status in (Campaign.STATUS_LIVE, Campaign.STATUS_PAUSED, Campaign.STATUS_DRAFT):
            campaigns[status] = Campaign.objects.create(
                account=account,
                service=service,
                name="Takbyte",
                status=status,
                page={
                    "title": f"Tak {status}",
                    "questions": [{"key": "yta", "label": "Yta?", "kind": "text"}],
                },
            )
        pages_from_campaigns(*campaigns.values())
        names = set()
        for status, campaign in campaigns.items():
            page = campaign.landing_page
            names.add(page.name)
            self.assertEqual(page.design, "ren")
            self.assertEqual(page.palette, "blue")
            self.assertEqual(page.is_published, status != Campaign.STATUS_DRAFT)
            self.assertEqual(pagebuilder.form_spec(page.draft_blocks).questions[0]["label"], "Yta?")
        self.assertEqual(len(names), 3)  # unika namn per konto
        pages_from_campaigns()  # igen: inget dubbleras
        self.assertEqual(LandingPage.objects.filter(account=account).count(), 3)


@override_settings(DEBUG=True, MEDIA_ROOT=_MEDIA)
class DemoPageTests(TestCase):
    def test_demo_pages_use_every_block_and_are_never_public(self):
        call_command("flamingo_demo", stdout=io.StringIO())
        account = FlamingoAccount.objects.get(is_demo=True)
        pages = list(LandingPage.objects.filter(account=account))
        types = {b["type"] for p in pages for b in p.draft_blocks}
        self.assertEqual(types, set(registry.TYPES))
        shared = [p for p in pages if p.campaigns.count() >= 2]
        self.assertTrue(shared)
        for page in pages:
            self.assertEqual(
                pagebuilder.validate_blocks(page.draft_blocks, account=account), page.draft_blocks
            )
        staff = User.objects.create_user("demo-byra", password="x12345678", is_staff=True)
        client = Client()
        client.force_login(staff)
        for campaign in account.campaigns.all():
            with self.subTest(campaign=campaign.name):
                self.assertEqual(Client().get(campaign.landing_url).status_code, 404)
                self.assertEqual(client.get(campaign.landing_url).status_code, 200)
        # Igen: inget dubbleras, och inga gamla bildfiler blir kvar i databasen.
        call_command("flamingo_demo", stdout=io.StringIO())
        self.assertEqual(LandingPage.objects.filter(account=account).count(), len(pages))
        self.assertEqual(
            MediaAsset.objects.filter(account=account).count(),
            len({a.pk for a in MediaAsset.objects.filter(account=account)}),
        )


# ---------------------------------------------------------------------------
# Designen Ren: tomma underfält, bilder, färger och mallarnas texter
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class RenDesignTests(PageFixture, TestCase):
    def page_with(self, blocks, **fields):
        return LandingPage.objects.create(
            account=self.account,
            name=f"Ren {LandingPage.objects.count()}",
            draft={"blocks": blocks},
            **fields,
        )

    def block(self, type_key, variant=None, **fields):
        ctx = pagebuilder.BuildContext(service="Rörjour", places=["Nacka", "Värmdö"], mode="call")
        block = pagebuilder.new_block(type_key, variant, self.account, ctx=ctx)
        block["versions"][0]["fields"].update(fields)
        return block

    def editing(self, block):
        page = self.page_with([block])
        return str(pagebuilder.render_block_html(page, block, self.account, editing=True))

    def test_an_empty_step_text_can_be_filled_in_while_editing(self):
        block = self.block("steps", "three")
        steps = block["versions"][0]["fields"]["steps"]
        steps[1]["text"] = ""
        html = self.editing(block)
        self.assertIn('data-pb-field="steps.1.text" data-pb-empty', html)
        self.assertIn('data-pb-placeholder="Text"', html)
        public = str(pagebuilder.render_block_html(self.page_with([block]), block, self.account))
        self.assertNotIn("data-pb-", public)

    def test_an_empty_certificate_text_can_be_filled_in_while_editing(self):
        block = self.block("certificates", "icons")
        self.assertEqual(block["versions"][0]["fields"]["items"][0]["text"], "")
        html = self.editing(block)
        self.assertIn('data-pb-field="items.0.text" data-pb-empty', html)

    def test_the_form_note_title_can_be_filled_in_while_editing(self):
        block = self.block("form", "short", note_title="", note="Stäng huvudkranen.")
        html = self.editing(block)
        self.assertIn('data-pb-field="note_title" data-pb-empty', html)
        self.assertIn('data-pb-field="note"', html)
        # Utan text och rubrik: rutan finns i redigeringsläget men är tom.
        empty = self.editing(self.block("form", "short", note_title="", note=""))
        self.assertIn('data-pb-field="note_title" data-pb-empty', empty)
        self.assertIn('data-pb-field="note" data-pb-empty', empty)
        # Den publika sidan visar bara texten.
        public = str(pagebuilder.render_block_html(self.page_with([block]), block, self.account))
        self.assertIn("Stäng huvudkranen.", public)
        self.assertNotIn("rn-tip__title", public)

    def test_the_booking_note_stands_under_the_date(self):
        block = self.block("form", "booking")
        html = str(pagebuilder.render_block_html(self.page_with([block]), block, self.account))
        date = html.index('type="date"')
        note = html.index(registry.FORM_NOTES["booking"])
        self.assertLess(date, note)
        self.assertLess(note, html.index('name="name"'))
        # Bokningen bekräftas i telefon: ingen e-post.
        self.assertNotIn('name="email"', html)

    def test_form_fields_are_counted_as_the_visitor_sees_them(self):
        short = pagebuilder.form_spec([self.block("form", "short")])
        self.assertEqual(short.fields, 2)
        quote = pagebuilder.form_spec([self.block("form", "questions")])
        # En längre text bland frågorna: inget meddelande till.
        self.assertFalse(quote.asks_message)
        self.assertEqual(quote.fields, len(quote.questions) + 2 + 1)
        html = str(
            pagebuilder.render_block_html(
                self.page_with([self.block("form", "questions")]),
                self.block("form", "questions"),
                self.account,
            )
        )
        self.assertNotIn('name="message"', html)
        self.assertIn("(valfritt)", html)
        self.assertIn("Skicka förfrågan", html)

    def test_short_form_asks_for_the_mobile_first(self):
        block = self.block("form", "short")
        html = str(pagebuilder.render_block_html(self.page_with([block]), block, self.account))
        self.assertLess(html.index('name="phone"'), html.index('name="name"'))

    def test_the_thumbnail_width_follows_the_longest_side(self):
        """Miniatyren ryms i 640 px på den längsta sidan: en stående bild på
        1000 x 1250 har en miniatyr som är 512 px bred, inte 640."""

        def asset(width, height):
            def file(url):
                return SimpleNamespace(url=url)

            return SimpleNamespace(
                file=file("/media/full.webp"),
                thumb=file("/media/tumme.webp"),
                width=width,
                height=height,
                alt="Bild",
            )

        portrait = flamingo_pb.rn_img(asset(1000, 1250))
        self.assertIn("/media/tumme.webp 512w", portrait)
        self.assertNotIn(" 640w", portrait)
        self.assertIn("/media/tumme.webp 640w", flamingo_pb.rn_img(asset(1600, 1000)))
        # En liten bild har ingen mindre miniatyr att erbjuda.
        self.assertNotIn("srcset", flamingo_pb.rn_img(asset(500, 300)))

    def test_phone_numbers_never_break(self):
        page = self.page_with([self.block("hero", "call"), self.block("callbar", "call")])
        html = pagebuilder.render_page_html(page, self.account, self.live)
        for link in re.findall(r'<a [^>]*href="tel:[^"]*"[^>]*>.*?</a>', html, re.S):
            self.assertIn("data-fl-call", link)
            self.assertIn('class="rn-nowrap"', link)

    def test_the_hero_has_a_kicker_with_the_service_and_place(self):
        hero = self.block("hero", "call")
        fields = pagebuilder.active_fields(hero)
        self.assertEqual(fields["kicker"], "Rörjour i Nacka")
        self.assertEqual(fields["title"], registry.HERO_TITLES[Service.SALES_CALL])
        page = self.page_with([hero])
        html = pagebuilder.render_page_html(page, self.account, self.live)
        self.assertIn('<p class="rn-hero__kicker">Rörjour i Nacka</p>', html)
        self.assertIn("<title>Rörjour i Nacka | ", html)
        # Ett äldre block utan överrubrik ritas utan den.
        hero["versions"][0]["fields"].pop("kicker")
        self.assertNotIn("rn-hero__kicker", pagebuilder.render_page_html(page, self.account))

    def test_certificates_right_after_the_hero_are_a_strip(self):
        blocks = [self.block("hero", "call"), self.block("certificates", "badges")]
        html = pagebuilder.render_page_html(self.page_with(blocks), self.account, self.live)
        self.assertIn("rn-certs--badges rn-s--strip", html)
        later = [self.block("hero", "call"), self.block("steps"), self.block("certificates")]
        html = pagebuilder.render_page_html(self.page_with(later), self.account, self.live)
        self.assertNotIn("rn-s--strip", html)

    def test_points_are_plain_swedish_and_drop_opening_hours_for_round_the_clock(self):
        self.assertEqual(
            facts.point_line("Jour", "Dygnet runt, alla dagar"), "Jour dygnet runt, alla dagar"
        )
        self.assertEqual(facts.point_line("Grundat", "2009"), "Grundat 2009")
        self.assertEqual(
            facts.point_line("Behörighet", "Säker Vatten-auktoriserade"),
            "Säker Vatten-auktoriserade",
        )
        self.assertEqual(
            facts.tidy_points(["Öppettider: Vardagar 7-16", "Jour: Dygnet runt, alla dagar"]),
            ["Jour dygnet runt, alla dagar"],
        )
        self.assertEqual(facts.tidy_points(["Öppettider: Vardagar 7-16"]), ["Öppet vardagar 7-16"])

    def test_faq_never_turns_contact_details_into_questions(self):
        faq = pagebuilder.active_fields(self.block("faq", "six"))
        questions = [item["q"] for item in faq["items"]]
        for contact in ("Hur når jag er?", "När kan jag nå er?", "Var finns ni?"):
            self.assertNotIn(contact, questions)
        self.assertIn("Vad kostar det?", questions)
        self.assertIn("Lämnar ni garanti?", questions)

    def test_certified_phrases_read_as_swedish(self):
        self.assertEqual(
            registry.certified_phrase("Ansvarsförsäkring", "Badrumsrenovering", "Nacka"),
            "Ansvarsförsäkrad badrumsrenovering i Nacka",
        )
        self.assertEqual(
            registry.certified_phrase("Säker Vatten-auktoriserade", "Rörjour", "Nacka"),
            "Säker Vatten-auktoriserad rörjour i Nacka",
        )
        self.assertEqual(registry.certified_phrase("Godkänd för F-skatt", "Rörjour"), "")

    def test_display_names_say_toppen_and_point(self):
        hero = registry.TYPES["hero"]
        self.assertEqual((hero.key, hero.name), ("hero", "Toppen"))
        points = next(f for f in registry.schema()[0]["fields"] if f["key"] == "points")
        self.assertEqual(points["item_label"], "Punkt")
        for principle in principles.PRINCIPLES:
            self.assertNotIn("Cialdini", principle.label)
        self.assertNotIn("Sållar", principles.label("pris_tidigt"))
