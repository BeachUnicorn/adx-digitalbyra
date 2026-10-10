"""ADX Flamingo: redigeraren i sidbyggaren (app_views/pages.py,
static/js/flamingo-pb.js): spara med rev, schemat, ritningen i
redigeringsläget, nya block, namn och färg, publiceringen som JSON,
sidlistan (ny, kopiera, ta bort) och kampanjens val av sida. Allt via
kundens konto: en annan kunds sida eller bild ger 404 eller 400."""

import io
import json
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer

from . import pagebuilder
from .app_views import pages as page_views
from .models import Campaign, Fact, FlamingoAccount, LandingPage, MediaAsset, Service

User = get_user_model()
PHONE = "08-000 00 00"
ALERTS = {"INQUIRY_NOTIFICATION_EMAIL": "larm@adx.example"}
_MEDIA = tempfile.mkdtemp(prefix="flamingo-pb-editor-")


def _png(size=(320, 240)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 90, 200)).save(buffer, "PNG")
    return ContentFile(buffer.getvalue(), name="bild.png")


def _asset(account, alt="Ett badrum"):
    asset = MediaAsset(account=account, alt=alt)
    asset.file.save("bild.png", _png(), save=False)
    asset.thumb.save("tumme.png", _png((160, 120)), save=False)
    asset.save()
    return asset


class EditorFixture:
    """Två kunder: Lindqvist Rör med en delad sida som är live (två
    kampanjer) och en sida utan kampanj, och Hemlig Bygg med en egen sida,
    tjänst och kontakt. Bilderna sparas i en egen tillfällig mapp."""

    @classmethod
    def setUpClass(cls):
        cls._media = override_settings(MEDIA_ROOT=_MEDIA)
        cls._media.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media.disable()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(name="Lindqvist Rör AB")
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.customer.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(customer=cls.customer, is_enabled=True)
        for key, label, value in (
            ("telefon", "Telefon", PHONE),
            ("adress", "Adress", "Exempelvägen 4, Nacka"),
            ("omrade", "Område", "Nacka, Värmdö och Tyresö"),
            ("behorighet", "Behörighet", "Säker Vatten-auktoriserade"),
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
        # En delad sida som är live (två kampanjer) och en sida utan kampanj.
        blocks = page_views.starter_blocks(cls.account, cls.badrum)
        cls.shared = LandingPage.objects.create(
            account=cls.account,
            name="Badrum",
            draft={"blocks": blocks},
            published={"blocks": blocks},
            published_at=timezone.now(),
        )
        cls.unused = LandingPage.objects.create(
            account=cls.account,
            name="Rörjour",
            draft={"blocks": page_views.starter_blocks(cls.account, cls.jour)},
        )
        cls.live = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum Nacka",
            area="Nacka + 15 km",
            status=Campaign.STATUS_LIVE,
            landing_page=cls.shared,
        )
        cls.second = Campaign.objects.create(
            account=cls.account,
            service=cls.badrum,
            name="Badrum Värmdö",
            area="Värmdö + 10 km",
            landing_page=cls.shared,
        )

        cls.other_customer = Customer.objects.create(name="Hemlig Bygg AB")
        cls.olle = User.objects.create_user("olle@hemlig.se", email="olle@hemlig.se", password="x")
        cls.other_customer.users.add(cls.olle)
        cls.other_account = FlamingoAccount.objects.create(
            customer=cls.other_customer, is_enabled=True
        )
        cls.other_page = LandingPage.objects.create(account=cls.other_account, name="Hemlig")
        cls.other_service = Service.objects.create(
            account=cls.other_account, name="Takbyte", sales_mode=Service.SALES_QUOTE
        )

    def setUp(self):
        super().setUp()
        cache.clear()
        for target, kwargs in (
            ("apps.assistant.llm.is_configured", {"return_value": False}),
            ("apps.assistant.llm.call", {"side_effect": AssertionError("AI ska inte anropas")}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- hjälpare -----------------------------------------------------------

    def client_for(self, user, view_as=None, **kwargs):
        client = Client(**kwargs)
        client.force_login(user)
        if view_as is not None:
            session = client.session
            session[VIEW_AS_KEY] = view_as.pk
            session.save()
        return client

    def url(self, name, page=None):
        page = page or self.unused
        return reverse(f"flamingo:{name}", args=[page.pk])

    def post_json(self, client, url, data, **extra):
        return client.post(
            url,
            data=json.dumps(data),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            **extra,
        )

    def draft(self, page=None):
        page = page or self.unused
        page.refresh_from_db()
        return page.draft_blocks

    def hero(self, blocks):
        return next(b for b in blocks if b["type"] == "hero")


# ---------------------------------------------------------------------------
# Redigeraren och behörigheten
# ---------------------------------------------------------------------------


class EditorPageTests(EditorFixture, TestCase):
    def test_the_editor_renders_the_page_in_editing_mode(self):
        response = self.client_for(self.anna).get(self.url("app_page", self.shared))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "flamingo/app/pages/editor.html")
        html = response.content.decode()
        self.assertIn('id="pb-frame"', html)
        self.assertIn("srcdoc=", html)
        # Sidan i ramen är escapad i srcdoc och har redigeringsattributen.
        self.assertIn("data-pb-block=&quot;b_", html)
        self.assertIn("data-pb-field=&quot;title&quot;", html)
        self.assertIn("js/flamingo-pb.js", html)
        self.assertIn("js/flamingo-pb-ai.js", html)
        self.assertIn("css/flamingo-pb-ai.css", html)
        self.assertIn('id="pb-panel-ai"', html)
        self.assertIn('id="pb-panel-koll"', html)
        self.assertIn('id="pb-panel-media"', html)
        # Publicerad och oförändrad: knappen säger "Publicera ändringarna"
        # men det finns inget att publicera.
        self.assertIn("Publicera ändringarna", html)
        self.assertIn("Sidan delas av", html)
        self.assertIn("Badrum Värmdö", html)
        config = response.context["config"]
        self.assertEqual(config["pageId"], self.shared.pk)
        self.assertEqual(config["rev"], self.shared.rev)
        self.assertEqual(config["me"]["source"], pagebuilder.SOURCE_CUSTOMER)
        for key in ("save", "renderBlock", "newBlock", "publish", "settings"):
            self.assertTrue(config["urls"][key], key)
        for key in ("ai_build", "ai_rewrite", "koll", "media_json", "media_upload", "media"):
            self.assertIn(key, config["urls"])
        self.assertFalse(config["available"]["price"]["ok"])
        self.assertIn("pris", config["available"]["price"]["reason"])
        self.assertNotIn("[ ", html)

    def test_the_page_profile_is_unchanged(self):
        """flamingo-pb.js monteras också för mejlen i Brev (profilen brev,
        README för utskick F.6). Sidornas profil är exakt de värden som
        stod fast i skriptet innan profilerna fanns."""
        config = self.client_for(self.anna).get(self.url("app_page")).context["config"]
        self.assertEqual(config["profile"], "page")
        self.assertEqual(config["canvasRoot"], "main.rn-main")
        self.assertEqual(config["chromeSelectors"], {"top": ".rn-top", "foot": ".rn-foot"})
        self.assertEqual(config["paletteMarker"], "--rn-primary")
        self.assertEqual(config["devices"], {"desktop": 1024, "phone": 390, "fit": "fill"})
        self.assertEqual(
            config["placement"], {"pairs": [["hero", "form", "form"]], "endGroup": "end"}
        )
        self.assertEqual(config["panelKinds"], [])
        self.assertNotIn("texts", config)
        self.assertEqual(
            config["addWords"],
            {
                "hero.points": "punkt",
                "area.places": "ort",
                "guarantee.terms": "villkor",
                "steps.steps": "steg",
                "faq.items": "fråga",
                "certificates.items": "certifikat",
                "price.items": "prisexempel",
                "form.questions": "fråga",
            },
        )
        self.assertEqual(
            config["wireframes"]["hero"],
            {"call": "h l b", "form": "row:h l|l l b", "image": "img h b", "text": "h l l"},
        )
        self.assertEqual(
            config["wireframes"]["callbar"], {"call": "bar:b", "call_write": "bar:b b2"}
        )
        self.assertEqual(
            sorted(config["wireframes"]),
            sorted(
                [
                    "hero",
                    "price",
                    "reviews_google",
                    "reviews_reco",
                    "certificates",
                    "guarantee",
                    "person",
                    "steps",
                    "before_after",
                    "area",
                    "faq",
                    "form",
                    "callbar",
                ]
            ),
        )
        # Varje variant i sidornas register har sin skiss.
        for block_type in pagebuilder.registry.TYPES_LIST:
            for variant in block_type.variants:
                if block_type.key in config["wireframes"]:
                    with self.subTest(type=block_type.key, variant=variant.key):
                        self.assertIn(variant.key, config["wireframes"][block_type.key])
        # Skriptets egna standardvärden är sidornas, och inget är kvar fast i koden.
        script = (settings.BASE_DIR / "static/js/flamingo-pb.js").read_text(encoding="utf-8")
        self.assertIn('config.canvasRoot || "main.rn-main"', script)
        self.assertIn('config.paletteMarker || "--rn-primary"', script)
        self.assertNotIn('("main.rn-main', script)
        self.assertNotIn('"main.rn-main >', script)
        self.assertNotIn('indexOf("--rn-primary")', script)
        self.assertIn('{ top: ".rn-top", foot: ".rn-foot" }', script)
        self.assertIn('{ pairs: [["hero", "form", "form"]], endGroup: "end" }', script)
        self.assertIn("var DESKTOP_MIN = DEVICES.desktop || 1024;", script)
        self.assertIn("var PHONE_WIDTH = DEVICES.phone || 390;", script)

    def test_a_page_that_was_never_published_says_publicera(self):
        html = self.client_for(self.anna).get(self.url("app_page")).content.decode()
        self.assertIn(">Publicera</button>", html)
        self.assertNotIn("Sidan delas av", html)

    def test_optional_urls_are_none_when_missing(self):
        with mock.patch.object(page_views, "reverse", side_effect=page_views.NoReverseMatch):
            self.assertIsNone(page_views._optional_url("flamingo:finns_inte", 1))

    def test_anonymous_and_other_customers_get_404(self):
        urls = [
            ("get", self.url("app_page")),
            ("get", reverse("flamingo:app_pages")),
            ("post", self.url("app_page_save")),
            ("post", self.url("app_page_render_block")),
            ("post", self.url("app_page_block_new")),
            ("post", self.url("app_page_settings")),
            ("post", self.url("app_page_copy")),
            ("post", self.url("app_page_delete")),
            ("post", self.url("app_page_publish")),
        ]
        anonymous = Client()
        olle = self.client_for(self.olle)
        for method, url in urls:
            with self.subTest(url=url, who="anonym"):
                self.assertEqual(getattr(anonymous, method)(url).status_code, 404)
            if url == reverse("flamingo:app_pages"):
                continue
            with self.subTest(url=url, who="en annan kunds kontakt"):
                self.assertEqual(getattr(olle, method)(url).status_code, 404)
        self.assertTrue(LandingPage.objects.filter(pk=self.unused.pk).exists())
        # Anna når inte den andra kundens sida, varken att läsa eller spara.
        anna = self.client_for(self.anna)
        self.assertEqual(anna.get(self.url("app_page", self.other_page)).status_code, 404)
        response = self.post_json(
            anna, self.url("app_page_save", self.other_page), {"rev": 1, "blocks": []}
        )
        self.assertEqual(response.status_code, 404)
        self.other_page.refresh_from_db()
        self.assertEqual(self.other_page.rev, 1)


# ---------------------------------------------------------------------------
# Spara
# ---------------------------------------------------------------------------


class SaveTests(EditorFixture, TestCase):
    def test_save_bumps_rev_and_stores_the_draft(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        version = pagebuilder.active_version(self.hero(blocks))
        version["fields"]["title"] = "Rörjour i Nacka och Värmdö"
        response = self.post_json(client, self.url("app_page_save"), {"rev": 1, "blocks": blocks})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["rev"], 2)
        self.assertRegex(data["saved_text"], r"^\d\d:\d\d$")
        self.assertIn("problems", data)
        self.assertEqual(data["state"]["kind"], page_views.STATE_DRAFT)
        saved = self.draft()
        self.assertEqual(
            pagebuilder.active_fields(self.hero(saved))["title"], "Rörjour i Nacka och Värmdö"
        )
        self.unused.refresh_from_db()
        self.assertEqual(self.unused.rev, 2)

    def test_a_stale_rev_is_409_and_nothing_is_overwritten(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        self.hero(blocks)["versions"][0]["fields"]["title"] = "Första fliken"
        self.assertEqual(
            self.post_json(
                client, self.url("app_page_save"), {"rev": 1, "blocks": blocks}
            ).status_code,
            200,
        )
        self.hero(blocks)["versions"][0]["fields"]["title"] = "Andra fliken"
        response = self.post_json(client, self.url("app_page_save"), {"rev": 1, "blocks": blocks})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["rev"], 2)
        self.assertIn("ändrats på ett annat ställe", response.json()["error"])
        self.assertEqual(
            pagebuilder.active_fields(self.hero(self.draft()))["title"], "Första fliken"
        )

    def test_html_is_stripped_and_unknown_types_are_rejected(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        version = pagebuilder.active_version(self.hero(blocks))
        version["fields"]["title"] = "Rörjour <script>alert(1)</script><b>nu</b>"
        version["fields"]["lead"] = '<img src=x onerror="alert(1)">Ring oss'
        response = self.post_json(client, self.url("app_page_save"), {"rev": 1, "blocks": blocks})
        self.assertEqual(response.status_code, 200, response.content)
        fields = pagebuilder.active_fields(self.hero(self.draft()))
        self.assertNotIn("<", fields["title"] + fields["lead"])
        self.assertNotIn("script", fields["title"])
        self.assertEqual(fields["lead"], "Ring oss")

        rev = 2
        for broken in (
            {**blocks[0], "type": "karusell"},
            {**blocks[0], "variant": "fyrverkeri"},
            {**blocks[0], "extra": 1},
        ):
            with self.subTest(broken=sorted(broken)):
                response = self.post_json(
                    client, self.url("app_page_save"), {"rev": rev, "blocks": [broken]}
                )
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()["errors"])
        long_title = [dict(b) for b in blocks]
        hero = self.hero(long_title)
        hero["versions"] = [dict(v, fields=dict(v["fields"])) for v in hero["versions"]]
        pagebuilder.active_version(hero)["fields"]["title"] = "x" * 500
        response = self.post_json(
            client, self.url("app_page_save"), {"rev": rev, "blocks": long_title}
        )
        self.assertEqual(response.status_code, 400)
        self.unused.refresh_from_db()
        self.assertEqual(self.unused.rev, 2)

    def test_media_must_belong_to_the_account(self):
        client = self.client_for(self.anna)
        mine = _asset(self.account)
        theirs = _asset(self.other_account, alt="Hemlig bild")
        blocks = self.draft()
        hero = self.hero(blocks)
        hero["variant"] = "image"
        pagebuilder.active_version(hero)["fields"]["image"] = theirs.pk
        response = self.post_json(client, self.url("app_page_save"), {"rev": 1, "blocks": blocks})
        self.assertEqual(response.status_code, 400)
        self.assertIn("mediaarkiv", response.json()["error"])
        pagebuilder.active_version(hero)["fields"]["image"] = mine.pk
        response = self.post_json(client, self.url("app_page_save"), {"rev": 1, "blocks": blocks})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(pagebuilder.active_fields(self.hero(self.draft()))["image"], mine.pk)

    def test_limits_methods_and_shape(self):
        client = self.client_for(self.anna)
        url = self.url("app_page_save")
        self.assertEqual(client.get(url).status_code, 405)
        too_many = [pagebuilder.new_block("steps", "three", self.account) for _ in range(41)]
        response = self.post_json(client, url, {"rev": 1, "blocks": too_many})
        self.assertEqual(response.status_code, 400)
        self.assertIn("40", response.json()["error"])
        huge = {"rev": 1, "blocks": [], "fill": "x" * (page_views.MAX_BODY + 10)}
        self.assertEqual(self.post_json(client, url, huge).status_code, 413)
        self.assertEqual(self.post_json(client, url, {"blocks": []}).status_code, 400)
        self.assertEqual(self.post_json(client, url, {"rev": 1}).status_code, 400)
        self.assertEqual(self.post_json(client, url, {"rev": 1, "blocks": "x"}).status_code, 400)
        response = client.post(url, data="inte json", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.unused.refresh_from_db()
        self.assertEqual(self.unused.rev, 1)

    def test_csrf_is_required(self):
        client = self.client_for(self.anna, enforce_csrf_checks=True)
        body = {"rev": 1, "blocks": self.draft()}
        self.assertEqual(self.post_json(client, self.url("app_page_save"), body).status_code, 403)
        client.get(self.url("app_page"))
        token = client.cookies["csrftoken"].value
        response = self.post_json(client, self.url("app_page_save"), body, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_the_server_stamps_who_wrote_a_version(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        hero = self.hero(blocks)
        template = pagebuilder.active_version(hero)
        # Klienten påstår att byrån skrev den nya versionen och att mallens
        # version skrevs av någon annan: servern litar inte på det.
        template_by = template["by"]
        template["by"] = self.staff.pk
        new = {
            "id": "v_AAAAAAAAAAAA",
            "fields": {**template["fields"], "title": "Kundens rubrik"},
            "source": "adx",
            "by": self.staff.pk,
            "at": "2020-01-01T00:00:00+00:00",
        }
        hero["versions"].append(new)
        hero["active"] = new["id"]
        response = self.post_json(client, self.url("app_page_save"), {"rev": 1, "blocks": blocks})
        self.assertEqual(response.status_code, 200, response.content)
        saved = self.hero(self.draft())
        versions = {v["id"]: v for v in saved["versions"]}
        self.assertEqual(versions[new["id"]]["source"], pagebuilder.SOURCE_CUSTOMER)
        self.assertEqual(versions[new["id"]]["by"], self.anna.pk)
        self.assertNotEqual(versions[new["id"]]["at"], new["at"])
        self.assertEqual(versions[template["id"]]["by"], template_by)
        self.assertEqual(versions[template["id"]]["source"], pagebuilder.SOURCE_TEMPLATE)

    def test_staff_viewing_as_the_customer_can_save_in_their_own_name(self):
        client = self.client_for(self.staff, view_as=self.customer)
        page = client.get(self.url("app_page"))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["config"]["me"]["source"], pagebuilder.SOURCE_ADX)
        blocks = self.draft()
        hero = self.hero(blocks)
        current = pagebuilder.active_version(hero)
        hero["versions"].append(
            {
                "id": "v_BBBBBBBBBBBB",
                "fields": {**current["fields"], "title": "ADX skrev det här"},
                "source": "customer",
                "by": None,
                "at": "2026-10-03T10:00:00+00:00",
            }
        )
        hero["active"] = "v_BBBBBBBBBBBB"
        response = self.post_json(client, self.url("app_page_save"), {"rev": 1, "blocks": blocks})
        self.assertEqual(response.status_code, 200, response.content)
        version = pagebuilder.active_version(self.hero(self.draft()))
        self.assertEqual(version["fields"]["title"], "ADX skrev det här")
        self.assertEqual(version["source"], pagebuilder.SOURCE_ADX)
        self.assertEqual(version["by"], self.staff.pk)


# ---------------------------------------------------------------------------
# Rita i redigeringsläget och nya block
# ---------------------------------------------------------------------------


class RenderTests(EditorFixture, TestCase):
    def test_the_whole_page_comes_back_with_editing_attributes(self):
        client = self.client_for(self.anna)
        response = self.post_json(
            client, self.url("app_page_render_block"), {"blocks": self.draft(), "palette": "green"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        html = data["html"]
        self.assertIn("rn--editing", html)
        self.assertIn('data-pb-block="b_', html)
        self.assertIn('data-pb-field="title"', html)
        self.assertIn("css/flamingo-pb.css", html)
        self.assertIn("#137333", data["palette_style"].upper())
        # Ritningen sparar ingenting.
        self.unused.refresh_from_db()
        self.assertEqual(self.unused.rev, 1)
        self.assertEqual(self.unused.palette, LandingPage.PALETTE_BLUE)

    def test_one_block_with_ids(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        hero = self.hero(blocks)
        response = self.post_json(
            client, self.url("app_page_render_block"), {"blocks": blocks, "ids": [hero["id"]]}
        )
        self.assertEqual(response.status_code, 200, response.content)
        html = response.json()["blocks"][hero["id"]]
        self.assertIn(f'data-pb-block="{hero["id"]}"', html)
        self.assertIn(f'data-pb-version="{hero["active"]}"', html)
        self.assertIn('data-pb-field="title"', html)
        self.assertEqual(list(response.json()["blocks"]), [hero["id"]])

    def test_an_empty_row_being_written_is_drawn(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        hero = self.hero(blocks)
        pagebuilder.active_version(hero)["fields"]["points"] = ["Jour dygnet runt", ""]
        response = self.post_json(
            client, self.url("app_page_render_block"), {"blocks": blocks, "ids": [hero["id"]]}
        )
        self.assertIn('data-pb-field="points.1"', response.json()["blocks"][hero["id"]])

    def test_unknown_types_and_foreign_media_are_refused(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        url = self.url("app_page_render_block")
        response = self.post_json(client, url, {"blocks": [{**blocks[0], "type": "karusell"}]})
        self.assertEqual(response.status_code, 400)
        theirs = _asset(self.other_account)
        hero = self.hero(blocks)
        hero["variant"] = "image"
        pagebuilder.active_version(hero)["fields"]["image"] = theirs.pk
        response = self.post_json(client, url, {"blocks": blocks})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(theirs.file.url, response.content.decode())
        self.assertEqual(self.post_json(client, url, {"palette": "rosa"}).status_code, 400)
        self.assertEqual(self.post_json(client, url, {"ids": "b_x"}).status_code, 400)


class NewBlockTests(EditorFixture, TestCase):
    def test_a_new_block_comes_from_the_templates(self):
        client = self.client_for(self.anna)
        response = self.post_json(
            client, self.url("app_page_block_new"), {"type": "steps", "variant": "four"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        block = response.json()["block"]
        self.assertEqual((block["type"], block["variant"]), ("steps", "four"))
        self.assertRegex(block["id"], r"^b_[A-Za-z0-9]{12}$")
        version = pagebuilder.active_version(block)
        self.assertEqual(version["source"], pagebuilder.SOURCE_TEMPLATE)
        self.assertEqual(len(version["fields"]["steps"]), 4)
        # Bara ett förslag: sidan ändras först när redigeraren sparar.
        self.assertEqual(len(self.draft()), len(self.unused.draft_blocks))

    def test_a_block_that_needs_a_fact_is_refused_with_the_reason(self):
        client = self.client_for(self.anna)
        response = self.post_json(client, self.url("app_page_block_new"), {"type": "price"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("bekräftat pris", response.json()["error"])
        response = self.post_json(client, self.url("app_page_block_new"), {"type": "karusell"})
        self.assertEqual(response.status_code, 400)
        response = self.post_json(
            client, self.url("app_page_block_new"), {"type": "faq", "variant": "tolv"}
        )
        self.assertEqual(response.status_code, 400)

    def test_fields_from_the_editor_become_the_active_version(self):
        """Fälten från redigeraren (också ett AI-förslag) blir en version i
        den inloggades namn: en källa "ai" från klienten litas inte på."""
        client = self.client_for(self.anna)
        response = self.post_json(
            client,
            self.url("app_page_block_new"),
            {
                "type": "faq",
                "variant": "three",
                "fields": {"title": "Frågor om <b>jouren</b>"},
                "source": "ai",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        block = response.json()["block"]
        self.assertEqual(len(block["versions"]), 2)
        version = pagebuilder.active_version(block)
        self.assertEqual(version["source"], pagebuilder.SOURCE_CUSTOMER)
        self.assertEqual(version["by"], self.anna.pk)
        self.assertEqual(version["fields"]["title"], "Frågor om jouren")
        self.assertTrue(version["fields"]["items"])
        response = self.post_json(
            client,
            self.url("app_page_block_new"),
            {"type": "faq", "fields": {"okänt": "x"}},
        )
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Namn, färg och publicering
# ---------------------------------------------------------------------------


class SettingsTests(EditorFixture, TestCase):
    def test_rename_and_palette(self):
        client = self.client_for(self.anna)
        url = self.url("app_page_settings")
        response = self.post_json(client, url, {"name": "  Rörjour   Nacka "})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["name"], "Rörjour Nacka")
        response = self.post_json(client, url, {"palette": "green"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["palette_label"], "Grön")
        self.assertIn("--rn-primary:#137333", response.json()["palette_style"])
        self.unused.refresh_from_db()
        self.assertEqual((self.unused.name, self.unused.palette), ("Rörjour Nacka", "green"))

    def test_bad_names_and_palettes_are_refused(self):
        client = self.client_for(self.anna)
        url = self.url("app_page_settings")
        for data, field in (
            ({"name": "   "}, "name"),
            ({"name": "<b></b>"}, "name"),
            ({"name": "Badrum"}, "name"),
            ({"name": "x" * 121}, "name"),
            ({"palette": "rosa"}, "palette"),
            ({"palette": "logo"}, "palette"),
            ({}, None),
        ):
            with self.subTest(data=data):
                response = self.post_json(client, url, data)
                self.assertEqual(response.status_code, 400)
                if field:
                    self.assertEqual(response.json()["field"], field)
        LandingPage.objects.filter(pk=self.unused.pk).update(logo_colors={"primary": "#E8505B"})
        self.assertEqual(self.post_json(client, url, {"palette": "logo"}).status_code, 200)
        self.unused.refresh_from_db()
        self.assertEqual((self.unused.name, self.unused.palette), ("Rörjour", "logo"))


@override_settings(**ALERTS)
class PublishTests(EditorFixture, TestCase):
    def test_problems_come_back_as_json_with_the_block(self):
        client = self.client_for(self.anna)
        blocks = self.draft()
        hero = self.hero(blocks)
        pagebuilder.active_version(hero)["fields"]["title"] = "Rörjour med 24 års erfarenhet"
        pagebuilder.save_draft(self.unused, blocks, rev=1)
        response = client.post(self.url("app_page_publish"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["problems"])
        self.assertEqual(data["problems"][0]["block"], hero["id"])
        self.assertEqual(data["problems"][0]["part"], "title")
        self.assertTrue(data["problems"][0]["where"].startswith("Toppen"))
        self.unused.refresh_from_db()
        self.assertFalse(self.unused.is_published)

    def test_publishing_returns_the_new_state_and_never_mails_the_customer(self):
        client = self.client_for(self.anna)
        response = client.post(self.url("app_page_publish"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["state"]["kind"], page_views.STATE_PUBLISHED)
        self.unused.refresh_from_db()
        self.assertTrue(self.unused.is_published)
        # En ändring på den delade live-sidan: byrån larmas, aldrig kunden.
        blocks = self.draft(self.shared)
        pagebuilder.active_version(self.hero(blocks))["fields"]["title"] = "Badrum i Nacka"
        save = self.post_json(
            client,
            self.url("app_page_save", self.shared),
            {"rev": self.shared.rev, "blocks": blocks},
        )
        self.assertEqual(save.json()["state"]["kind"], page_views.STATE_CHANGED)
        response = client.post(
            self.url("app_page_publish", self.shared), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Badrum Nacka", response.json()["message"])
        self.assertEqual(response.json()["state"]["kind"], page_views.STATE_LIVE)
        for message in mail.outbox:
            self.assertNotIn("anna@ror.se", message.to + message.cc + message.bcc)
        self.assertTrue(any("larm@adx.example" in m.to for m in mail.outbox))

    def test_a_form_post_still_redirects(self):
        response = self.client_for(self.anna).post(self.url("app_page_publish"))
        self.assertRedirects(response, self.url("app_page"), fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# Sidlistan: ny sida, kopiera, ta bort
# ---------------------------------------------------------------------------


class PageListTests(EditorFixture, TestCase):
    def test_cards_show_usage_state_and_a_small_picture(self):
        html = self.client_for(self.anna).get(reverse("flamingo:app_pages")).content.decode()
        self.assertIn("Används av 2 kampanjer", html)
        self.assertIn("Ingen kampanj använder sidan än", html)
        self.assertIn(">Live<", html)
        self.assertIn(">Utkast<", html)
        self.assertIn("pb-thumb__row--hero", html)
        self.assertIn(reverse("flamingo:app_page_new"), html)
        self.assertNotIn("Hemlig", html)
        LandingPage.objects.filter(pk=self.shared.pk).update(draft={"blocks": []})
        html = self.client_for(self.anna).get(reverse("flamingo:app_pages")).content.decode()
        self.assertIn("Ändringar ej publicerade", html)

    def test_a_new_page_from_the_templates_of_a_service(self):
        client = self.client_for(self.anna)
        response = client.post(reverse("flamingo:app_page_new"), {"service": self.jour.pk})
        page = LandingPage.objects.filter(account=self.account).order_by("-pk").first()
        self.assertRedirects(response, self.url("app_page", page), fetch_redirect_response=False)
        self.assertEqual(page.name, "Rörjour (2)")
        types = [b["type"] for b in page.draft_blocks]
        self.assertEqual(types[0], "hero")
        self.assertIn("callbar", types)
        self.assertNotIn("price", types)  # inget bekräftat pris
        self.assertFalse(page.is_published)
        self.assertFalse(page.campaigns.exists())
        self.assertEqual(pagebuilder.validate_blocks(page.draft_blocks), page.draft_blocks)
        self.assertEqual(pagebuilder.page_problems(page), [])
        foreign = client.post(reverse("flamingo:app_page_new"), {"service": self.other_service.pk})
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(client.get(reverse("flamingo:app_page_new")).status_code, 405)

    def test_copy(self):
        client = self.client_for(self.anna)
        response = client.post(self.url("app_page_copy", self.shared))
        page = LandingPage.objects.get(account=self.account, name="Kopia av Badrum")
        self.assertRedirects(response, self.url("app_page", page), fetch_redirect_response=False)
        self.assertEqual(page.draft_blocks, self.shared.draft_blocks)
        self.assertFalse(page.is_published)
        self.assertFalse(page.campaigns.exists())
        self.assertEqual(client.post(self.url("app_page_copy", self.other_page)).status_code, 404)

    def test_a_page_used_by_campaigns_cannot_be_deleted(self):
        client = self.client_for(self.anna)
        response = client.post(self.url("app_page_delete", self.shared), follow=True)
        self.assertContains(response, "kan inte tas bort")
        self.assertContains(response, "Badrum Nacka")
        self.assertTrue(LandingPage.objects.filter(pk=self.shared.pk).exists())
        response = client.post(self.url("app_page_delete", self.unused), follow=True)
        self.assertContains(response, "är borttagen")
        self.assertFalse(LandingPage.objects.filter(pk=self.unused.pk).exists())
        self.assertEqual(client.post(self.url("app_page_delete", self.other_page)).status_code, 404)
        self.assertTrue(LandingPage.objects.filter(pk=self.other_page.pk).exists())


# ---------------------------------------------------------------------------
# Kampanjen: egen eller delad sida
# ---------------------------------------------------------------------------


class CampaignPageChoiceTests(EditorFixture, TestCase):
    def create(self, client, **data):
        payload = {
            "service": str(self.jour.pk),
            "sales_mode": Service.SALES_CALL,
            "place": "Nacka",
            "radius_km": "15",
            "budget": "200",
            "budget_own": "",
        }
        payload.update(data)
        return client.post(reverse("flamingo:app_campaign_new"), payload)

    def test_the_form_offers_own_or_existing_page(self):
        html = self.client_for(self.anna).get(reverse("flamingo:app_campaign_new")).content.decode()
        self.assertIn("Egen sida för den här kampanjen (rekommenderas)", html)
        self.assertIn("Använd en befintlig sida", html)
        self.assertIn(f'<option value="{self.shared.pk}"', html)
        self.assertNotIn(f'<option value="{self.other_page.pk}"', html)
        self.assertIn("Badrum</b> visas i Badrum Nacka och Badrum Värmdö", html)

    def test_a_new_campaign_gets_its_own_page_by_default(self):
        client = self.client_for(self.anna)
        response = self.create(client)
        campaign = Campaign.objects.get(account=self.account, service=self.jour)
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(campaign.landing_page)
        self.assertNotIn(campaign.landing_page_id, (self.shared.pk, self.unused.pk))
        self.assertEqual(list(campaign.landing_page.campaigns.all()), [campaign])

    def test_a_new_campaign_can_use_an_existing_page(self):
        client = self.client_for(self.anna)
        self.create(client, page_choice="shared", shared_page=str(self.shared.pk))
        campaign = Campaign.objects.get(account=self.account, service=self.jour)
        self.assertEqual(campaign.landing_page, self.shared)
        # Sidan är delad och redigerad av ingen: den byggs inte om av förslaget.
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.campaigns.count(), 3)

    def test_the_existing_page_must_be_on_the_same_account(self):
        client = self.client_for(self.anna)
        for value in (str(self.other_page.pk), "", "x"):
            with self.subTest(value=value):
                response = self.create(client, page_choice="shared", shared_page=value)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "fl-field__error")
        self.assertFalse(Campaign.objects.filter(service=self.jour).exists())
        self.assertFalse(self.other_page.campaigns.exists())

    def test_a_live_campaign_can_only_switch_to_a_published_page(self):
        client = self.client_for(self.anna)
        url = reverse("flamingo:app_campaign", args=[self.live.pk])
        response = client.post(
            url, {"section": "landing", "page": str(self.unused.pk)}, follow=True
        )
        self.assertContains(response, "Publicera sidan först")
        self.live.refresh_from_db()
        self.assertEqual(self.live.landing_page, self.shared)
        response = client.post(url, {"section": "landing", "page": str(self.other_page.pk)})
        self.assertEqual(response.status_code, 404)

    def test_the_page_tab_names_the_other_campaigns(self):
        client = self.client_for(self.anna)
        url = reverse("flamingo:app_campaign", args=[self.live.pk]) + "?flik=sidan"
        response = client.get(url)
        self.assertContains(response, "Delad sida.")
        self.assertContains(response, "Badrum Värmdö")
        self.assertContains(response, "syns i båda kampanjerna")
        self.assertContains(response, "Redigera sidan")
        self.assertContains(response, self.url("app_page", self.shared))
        self.assertContains(response, "Byt sida")
        self.assertNotContains(response, "[ ")
        # Förhandsvisningen ligger i en ram med sandbox utan skript: sidans
        # skript tas inte med (annars "Blocked script execution").
        self.assertContains(response, 'class="fl-camp-lpframe"')
        self.assertNotContains(response, "flamingo-lp.js")
        # En egen sida säger det.
        own = pagebuilder.ensure_own_page(self.second)
        self.assertNotEqual(own, self.shared)
        response = client.get(
            reverse("flamingo:app_campaign", args=[self.second.pk]) + "?flik=sidan"
        )
        self.assertContains(response, "Kampanjens egen sida")
