"""E-postredigeraren i Brev och e-posten i guidens steg (README F.2, F.4 till
F.8, H.1, I.4, I.6, I.8, D.8; app_views/brev.py, ai.py och de märkta S3-
blocken i app_views/utskick.py).

    EditorPageTests   sidan: profilen brev, ramen, biblioteket, färgerna
    TenancyTests      varje app_brev*-adress är kontots, 404 annars
    SaveTests         spara med rev, 409, 400 för främmande id, utkast igen
    RenderTests       hela mejlet, ett block, ett nytt block, låsningar
    ImageTests        bilden för e-post, främmande bild och fel syfte
    ChecksTests       kontrollerna, länkarna bara på begäran
    PreviewTests      förhandsvisningen: CSP, kontakten, mörkt läge
    AiTests           AI för ett block och sms: mallen, vakten, villkoren
    TestSendTests     testmejlet (F.8): vart, byråns kryssruta, aldrig demot
    GuideTests        Kanal, Innehåll och Granska med e-post
    ScriptTests       flamingo-pb.js profilen brev, mallarna och copy-reglerna

Inget når nätet: AI är av eller mockad, testmejlet går genom en mockad
sending.email.send_test, och e-posten är av (UTSKICK_EMAIL_LIVE) om inte ett
test slår på den.
"""

import io
import json
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.flamingo.models import Fact, MediaAsset
from apps.flamingo.pagebuilder.render import EDITING_CSP

from . import ai
from . import consent as consents
from .app_views import brev
from .email import transport
from .models import (
    CHANNEL_EMAIL,
    INFORMATION,
    Consent,
    ContactList,
    EmailImage,
    SenderDomain,
    Switchboard,
    Utskick,
    UtskickSettings,
)
from .testing import UtskickFixture, make_contact

_MEDIA = tempfile.mkdtemp(prefix="utskick-brev-editor-")

#: (namn, metoden). Alla tar utskickets pk.
ROUTES = (
    ("flamingo:app_brev", "get"),
    ("flamingo:app_brev_save", "post"),
    ("flamingo:app_brev_render_block", "post"),
    ("flamingo:app_brev_image", "post"),
    ("flamingo:app_brev_checks", "get"),
    ("flamingo:app_brev_ai", "post"),
    ("flamingo:app_brev_preview", "get"),
)


def _png(size=(320, 240)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 90, 200)).save(buffer, "PNG")
    return ContentFile(buffer.getvalue(), name="bild.png")


def make_asset(account, alt="Ett badrum"):
    asset = MediaAsset(account=account, alt=alt)
    asset.file.save("bild.png", _png(), save=False)
    asset.thumb.save("tumme.png", _png((160, 120)), save=False)
    asset.save()
    return asset


class BrevFixture(UtskickFixture):
    """Exempelrör med listan Kunder och adressen under Företaget. Bilderna
    sparas i en egen tillfällig mapp."""

    @classmethod
    def setUpClass(cls):
        cls._media = override_settings(MEDIA_ROOT=_MEDIA)
        cls._media.enable()
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kunder = ContactList.objects.create(account=cls.account, name="Kunder")
        for key, label, value in (
            ("telefon", "Telefon", "08-123 456 78"),
            ("adress", "Adress", "Exempelvägen 4, 123 45 Exempelstad"),
        ):
            Fact.objects.create(
                account=cls.account, key=key, label=label, value=value, confirmed=True
            )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media.disable()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.anna)
        self.n = 0
        for target, kwargs in (
            ("apps.assistant.llm.is_configured", {"return_value": False}),
            ("apps.assistant.llm.call", {"side_effect": AssertionError("AI ska inte anropas")}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- hjälpare -----------------------------------------------------------

    def person(self, account=None, **data):
        account = account or self.account
        self.n += 1
        data.setdefault("first_name", f"Person{self.n}")
        data.setdefault("email", f"person{self.n}@exempel.example")
        kontakt = make_contact(account, **data)
        consents.set_status(
            kontakt, CHANNEL_EMAIL, consents.YES, source=Consent.Source.MANUAL, evidence="kassan"
        )
        if account == self.account:
            self.kunder.memberships.create(contact=kontakt)
        return kontakt

    def utskick(self, account=None, **kwargs):
        account = account or self.account
        data = {
            "name": "Höstservice värmepump",
            "channel_mode": Utskick.ChannelMode.EMAIL_ONLY,
            "subject": "Dags för service, {förnamn|du}",
            "audience": {"lists": [self.kunder.pk]} if account == self.account else {},
        }
        data.update(kwargs)
        return Utskick.objects.create(account=account, **data)

    def url(self, name, utskick):
        return reverse(name, args=[utskick.pk])

    def post_json(self, url, data, client=None, **extra):
        return (client or self.client).post(
            url,
            data=json.dumps(data),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            **extra,
        )

    def new_block(self, utskick, type_key="text", client=None, **extra):
        response = self.post_json(
            self.url("flamingo:app_brev_render_block", utskick),
            {"type": type_key, **extra},
            client=client,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["block"]

    def save(self, utskick, client=None, **data):
        utskick.refresh_from_db()
        data.setdefault("rev", utskick.email_rev)
        return self.post_json(self.url("flamingo:app_brev_save", utskick), data, client=client)

    def doc(self, utskick):
        utskick.refresh_from_db()
        return (utskick.email_doc or {}).get("blocks") or []

    def email_on(self):
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK,
            defaults={"email_enabled": True, "email_ready_at": timezone.now()},
        )
        patcher = override_settings(UTSKICK_EMAIL_LIVE=True)
        patcher.enable()
        self.addCleanup(patcher.disable)


# ---------------------------------------------------------------------------
# Sidan
# ---------------------------------------------------------------------------


class EditorPageTests(BrevFixture, TestCase):
    def test_the_editor_mounts_the_page_builder_with_the_brev_profile(self):
        utskick = self.utskick()
        response = self.client.get(self.url("flamingo:app_brev", utskick))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "flamingo/app/utskick/brev_editor.html")
        html = response.content.decode()
        self.assertIn('id="pb-frame"', html)
        self.assertIn("data-brev-canvas", html)
        self.assertIn("js/flamingo-pb.js", html)
        self.assertIn("js/flamingo-app-brev.js", html)
        self.assertNotIn("js/flamingo-pb-ai.js", html)
        self.assertIn('id="pb-config"', html)
        self.assertIn('id="br-config"', html)
        self.assertIn("Enklast på dator.", html)
        config = response.context["config"]
        self.assertEqual(config["profile"], "brev")
        self.assertEqual(config["canvasRoot"], "[data-brev-canvas]")
        self.assertEqual(config["canvasEnd"], '[data-brev-element="footer"]')
        self.assertEqual(config["devices"], {"desktop": 680, "phone": 375, "fit": "fixed"})
        self.assertEqual(config["maxBlocks"], 30)
        self.assertEqual(config["rev"], utskick.email_rev)
        self.assertEqual(config["me"]["source"], "customer")
        self.assertIsNone(config["urls"]["publish"])
        self.assertEqual(config["urls"]["newBlock"], config["urls"]["renderBlock"])
        self.assertIn("rich_basic", config["panelKinds"])
        self.assertIn("url", config["panelKinds"])
        self.assertNotIn("text", config["panelKinds"])
        keys = [entry["key"] for entry in config["schema"]]
        self.assertEqual(len(keys), 22)
        self.assertTrue(all(entry["icon"] == f"brev-{entry['key']}" for entry in config["schema"]))
        # Biblioteket: varje block har sin ikon i redigerarens sprite.
        for key in keys:
            self.assertIn(f'id="pb-i-brev-{key}"', html)
        self.assertIn('data-pb-add="hero"', html)
        # Färgerna: mockupens fem efter loggans, och ingen färg i en style.
        picker = response.context["picker"]
        values = [s["value"] for s in picker["swatches"]]
        for value in ("#1A57D6", "#1F7A4D", "#B42318", "#6D28D9", "#111111"):
            self.assertIn(value, values)
        self.assertNotIn("[ ", html)

    def test_canvas_has_no_scripts_and_the_editing_stylesheet(self):
        utskick = self.utskick()
        self.new_block(utskick, "heading")
        response = self.client.get(self.url("flamingo:app_brev", utskick))
        canvas = response.context["canvas_html"]
        self.assertTrue(canvas.lstrip().startswith("<!doctype html>"))
        self.assertEqual(canvas.count(EDITING_CSP), 1)
        self.assertIn("css/flamingo-app-brev.css", canvas)
        self.assertNotIn("<script", canvas.lower())

    def test_an_sms_only_utskick_goes_to_the_channel_step(self):
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.SMS_ONLY)
        response = self.client.get(self.url("flamingo:app_brev", utskick))
        self.assertRedirects(
            response, reverse("flamingo:app_utskick_step", args=[utskick.pk, "kanal"])
        )

    def test_a_sent_utskick_goes_to_the_report(self):
        utskick = self.utskick(status=Utskick.Status.SENT)
        response = self.client.get(self.url("flamingo:app_brev", utskick))
        self.assertRedirects(response, reverse("flamingo:app_utskick", args=[utskick.pk]))

    def test_staff_in_view_as_writes_as_adx(self):
        utskick = self.utskick()
        response = self.client_for(self.staff).get(self.url("flamingo:app_brev", utskick))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["config"]["me"]["source"], "adx")
        self.assertTrue(response.context["br_config"]["staff"])

    def test_without_a_logo_only_none_can_be_chosen(self):
        utskick = self.utskick(logo_position=Utskick.LogoPosition.NONE)
        html = self.client.get(self.url("flamingo:app_brev", utskick)).content.decode()
        self.assertIn("Ladda upp en logotyp under", html)
        self.assertIn('data-br-logo="left" aria-pressed="false" disabled', html)
        self.assertIn('data-br-logo="none" aria-pressed="true">', html)


# ---------------------------------------------------------------------------
# Behörighet (H.1)
# ---------------------------------------------------------------------------


class TenancyTests(BrevFixture, TestCase):
    def test_another_accounts_utskick_is_404_on_every_route(self):
        foreign = self.utskick(account=self.other_account)
        for name, method in ROUTES:
            with self.subTest(name=name):
                url = self.url(name, foreign)
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.email_rev, 0)

    def test_everything_is_404_when_utskick_is_off_also_for_staff(self):
        utskick = self.utskick()
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        for client in (self.client, self.client_for(self.staff)):
            for name, method in ROUTES:
                with self.subTest(name=name):
                    response = getattr(client, method)(self.url(name, utskick))
                    self.assertEqual(response.status_code, 404)

    def test_post_routes_refuse_get(self):
        utskick = self.utskick()
        for name, method in ROUTES:
            if method == "post":
                with self.subTest(name=name):
                    self.assertEqual(self.client.get(self.url(name, utskick)).status_code, 405)

    def test_the_test_mail_route_is_404_for_a_foreign_utskick(self):
        foreign = self.utskick(account=self.other_account)
        response = self.client.post(
            reverse("flamingo:app_utskick_test", args=[foreign.pk]),
            {"kanal": "epost", "till": "mig"},
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Spara (F.2)
# ---------------------------------------------------------------------------


class SaveTests(BrevFixture, TestCase):
    def test_blocks_and_the_mail_fields_are_saved_with_rev(self):
        utskick = self.utskick()
        block = self.new_block(utskick, "heading")
        response = self.save(
            utskick,
            blocks=[block],
            subject="Hej {förnamn|du}, dags för service",
            preheader="Boka en tid som passar dig",
            accent="#1f7a4d",
            logo_position="center",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["rev"], 1)
        self.assertEqual(data["subject_errors"], [])
        utskick.refresh_from_db()
        self.assertEqual(utskick.email_rev, 1)
        self.assertEqual([b["id"] for b in self.doc(utskick)], [block["id"]])
        self.assertEqual(utskick.subject, "Hej {förnamn|du}, dags för service")
        self.assertEqual(utskick.preheader, "Boka en tid som passar dig")
        self.assertEqual(utskick.accent, "#1F7A4D")
        self.assertEqual(utskick.logo_position, "center")

    def test_nothing_changed_keeps_rev(self):
        utskick = self.utskick()
        block = self.new_block(utskick, "heading")
        self.save(utskick, blocks=[block])
        stored = self.doc(utskick)
        response = self.save(utskick, blocks=stored, subject=utskick.subject)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rev"], 1)

    def test_a_stale_rev_is_409_and_nothing_is_overwritten(self):
        utskick = self.utskick()
        block = self.new_block(utskick, "heading")
        self.save(utskick, blocks=[block])
        response = self.save(utskick, rev=0, blocks=[], subject="Annat")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Inget har skrivits över", response.json()["error"])
        utskick.refresh_from_db()
        self.assertEqual(len(self.doc(utskick)), 1)
        self.assertEqual(utskick.subject, "Dags för service, {förnamn|du}")

    def test_a_foreign_image_is_400(self):
        utskick = self.utskick()
        foreign = make_asset(self.other_account)
        block = self.new_block(utskick, "image")
        block["versions"][-1]["fields"]["image"] = foreign.pk
        block["versions"][-1].pop("sig", None)
        response = self.save(utskick, blocks=[block])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.doc(utskick), [])

    def test_sender_domain_must_be_the_accounts_and_verified(self):
        utskick = self.utskick()
        foreign = SenderDomain.objects.create(
            account=self.other_account,
            domain="annanfirma.example",
            from_name="Annanfirma",
            status=SenderDomain.Status.VERIFIED,
        )
        pending = SenderDomain.objects.create(
            account=self.account, domain="exempelror.example", from_name="Exempelrör"
        )
        self.assertEqual(self.save(utskick, sender_domain=foreign.pk).status_code, 400)
        response = self.save(utskick, sender_domain=pending.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("inte verifierad", response.json()["error"])
        SenderDomain.objects.filter(pk=pending.pk).update(status=SenderDomain.Status.VERIFIED)
        response = self.save(utskick, sender_domain=pending.pk, from_name="Anna på Exempelrör")
        self.assertEqual(response.status_code, 200)
        utskick.refresh_from_db()
        self.assertEqual(utskick.sender_domain_id, pending.pk)
        self.assertEqual(utskick.from_name, "Anna på Exempelrör")
        # Tillbaka till ADX-domänen: avsändarnamnet följer inte med.
        self.save(utskick, sender_domain=None, from_name="Kvar")
        utskick.refresh_from_db()
        self.assertIsNone(utskick.sender_domain_id)
        self.assertEqual(utskick.from_name, "")

    def test_bad_values_are_400(self):
        utskick = self.utskick()
        self.assertEqual(self.save(utskick, accent="blå").status_code, 400)
        self.assertEqual(self.save(utskick, logo_position="right").status_code, 400)
        response = self.post_json(self.url("flamingo:app_brev_save", utskick), {"blocks": []})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            self.url("flamingo:app_brev_save", utskick), data="nej", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_placeholders_in_the_subject_are_saved_with_the_error(self):
        utskick = self.utskick()
        response = self.save(utskick, subject="Hej {fornamn}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["subject_errors"])
        utskick.refresh_from_db()
        self.assertEqual(utskick.subject, "Hej {fornamn}")

    def test_typography_and_html_are_cleaned(self):
        utskick = self.utskick()
        curly = "Hej " + chr(0x201C) + "du" + chr(0x201D) + " <b>nu</b> " + chr(0x2013) + " boka"
        self.save(utskick, subject=curly)
        utskick.refresh_from_db()
        self.assertNotIn(chr(0x201C), utskick.subject)
        self.assertNotIn(chr(0x2013), utskick.subject)
        self.assertNotIn("<b>", utskick.subject)

    def test_a_scheduled_utskick_becomes_a_draft_when_the_mail_changes(self):
        utskick = self.utskick(
            status=Utskick.Status.SCHEDULED,
            confirmed_at=timezone.now(),
            confirm_summary={"email": 3},
            scheduled_at=timezone.now() + timezone.timedelta(days=1),
        )
        response = self.save(utskick, subject=utskick.subject)
        self.assertEqual(response.status_code, 200)
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.SCHEDULED)
        response = self.save(utskick, subject="Ny ämnesrad")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "draft")
        self.assertIn("utkast igen", response.json()["message"])
        utskick.refresh_from_db()
        self.assertEqual(utskick.status, Utskick.Status.DRAFT)
        self.assertIsNone(utskick.confirmed_at)
        self.assertEqual(utskick.confirm_summary, {})

    def test_a_sending_utskick_is_409(self):
        utskick = self.utskick(status=Utskick.Status.SENDING)
        response = self.save(utskick, subject="Nej")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["locked"])
        utskick.refresh_from_db()
        self.assertEqual(utskick.subject, "Dags för service, {förnamn|du}")

    def test_staff_in_view_as_is_stamped_as_adx(self):
        utskick = self.utskick()
        staff = self.client_for(self.staff)
        block = self.new_block(utskick, "heading", client=staff)
        version = dict(block["versions"][-1])
        version.pop("sig", None)
        version.update(id="v_Ab12Cd34Ef56", fields={**version["fields"], "text": "Hej hej"})
        block["versions"].append(version)
        block["active"] = version["id"]
        response = self.save(utskick, client=staff, blocks=[block])
        self.assertEqual(response.status_code, 200, response.content)
        stored = self.doc(utskick)[0]["versions"][-1]
        self.assertEqual(stored["source"], "adx")
        self.assertEqual(stored["by"], self.staff.pk)

    def test_fallbacks_are_merged_with_the_sms_ones(self):
        utskick = self.utskick(merge_fallbacks={"efternamn": "kund"})
        self.save(utskick, merge_fallbacks={"förnamn": "du", "okänd": "x", "efternamn": ""})
        utskick.refresh_from_db()
        self.assertEqual(utskick.merge_fallbacks, {"förnamn": "du"})

    def test_terms_are_confirmed_only_as_the_customer_saw_them(self):
        utskick = self.utskick()
        terms = [{"label": "Erbjudandet gäller", "value": "till 31 oktober"}]
        Utskick.objects.filter(pk=utskick.pk).update(confirmed_terms=terms)
        seen = [{"label": "Erbjudandet gäller", "value": "till 30 oktober"}]
        response = self.save(utskick, terms_ok=True, terms=seen)
        self.assertFalse(response.json()["terms_confirmed"])
        response = self.save(utskick, terms_ok=True, terms=terms)
        self.assertTrue(response.json()["terms_confirmed"])
        utskick.refresh_from_db()
        self.assertEqual(utskick.terms_confirmed_by, self.anna)
        self.assertIsNotNone(utskick.terms_confirmed_at)


# ---------------------------------------------------------------------------
# Rita och nya block
# ---------------------------------------------------------------------------


class RenderTests(BrevFixture, TestCase):
    def test_the_whole_mail_comes_back_in_editing_mode(self):
        utskick = self.utskick()
        block = self.new_block(
            utskick,
            "button",
            fields={"primary_text": "Boka", "primary_url": "https://exempelror.example/boka"},
        )
        response = self.post_json(
            self.url("flamingo:app_brev_render_block", utskick),
            {"blocks": [block], "accent": "#B42318", "logo_position": "none"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        html = response.json()["html"]
        self.assertIn("data-brev-canvas", html)
        self.assertIn(f'data-pb-block="{block["id"]}"', html)
        self.assertIn('data-brev-element="footer"', html)
        self.assertEqual(html.count(EDITING_CSP), 1)
        self.assertIn("#B42318".lower(), html.lower())
        # Ritningen sparar ingenting.
        utskick.refresh_from_db()
        self.assertEqual(utskick.email_rev, 0)
        self.assertEqual(utskick.accent, "")

    def test_one_block_with_ids(self):
        utskick = self.utskick()
        block = self.new_block(utskick, "heading")
        response = self.post_json(
            self.url("flamingo:app_brev_render_block", utskick),
            {"blocks": [block], "ids": [block["id"]]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'data-pb-block="{block["id"]}"', response.json()["blocks"][block["id"]])

    def test_unknown_types_bad_accents_and_foreign_images_are_refused(self):
        utskick = self.utskick()
        url = self.url("flamingo:app_brev_render_block", utskick)
        self.assertEqual(self.post_json(url, {"type": "nope"}).status_code, 400)
        self.assertEqual(self.post_json(url, {"blocks": [], "accent": "red"}).status_code, 400)
        block = self.new_block(utskick, "image")
        block["versions"][-1]["fields"]["image"] = make_asset(self.other_account).pk
        self.assertEqual(self.post_json(url, {"blocks": [block]}).status_code, 400)
        self.assertEqual(self.post_json(url, {"blocks": "x"}).status_code, 400)

    def test_offers_are_locked_for_information(self):
        utskick = self.utskick(purpose=INFORMATION, info_reason="oppettider")
        response = self.post_json(
            self.url("flamingo:app_brev_render_block", utskick), {"type": "offer"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Erbjudanden hör inte hemma i information.", response.json()["error"])

    def test_fields_from_the_ai_panel_become_the_users_version(self):
        utskick = self.utskick()
        block = self.new_block(utskick, "heading", fields={"text": "Dags för service"}, source="ai")
        version = block["versions"][-1]
        self.assertEqual(block["active"], version["id"])
        self.assertEqual(version["fields"]["text"], "Dags för service")
        self.assertEqual(version["source"], "customer")
        self.assertEqual(version["by"], self.anna.pk)


# ---------------------------------------------------------------------------
# Bilderna
# ---------------------------------------------------------------------------


class ImageTests(BrevFixture, TestCase):
    def test_an_own_image_gets_an_email_rendition(self):
        utskick = self.utskick()
        asset = make_asset(self.account)
        response = self.post_json(
            self.url("flamingo:app_brev_image", utskick), {"asset": asset.pk, "purpose": "content"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["url"].startswith(("http://", "https://")))
        self.assertEqual(data["alt"], "Ett badrum")
        self.assertTrue(EmailImage.objects.filter(pk=data["id"], account=self.account).exists())

    def test_foreign_images_and_unknown_purposes_are_400(self):
        utskick = self.utskick()
        url = self.url("flamingo:app_brev_image", utskick)
        foreign = make_asset(self.other_account)
        self.assertEqual(self.post_json(url, {"asset": foreign.pk}).status_code, 400)
        own = make_asset(self.account)
        self.assertEqual(self.post_json(url, {"asset": own.pk, "purpose": "logo"}).status_code, 400)
        self.assertEqual(self.post_json(url, {"asset": own.pk, "purpose": "x"}).status_code, 400)
        self.assertEqual(self.post_json(url, {"asset": "abc"}).status_code, 400)
        self.assertFalse(EmailImage.objects.filter(asset=foreign).exists())


# ---------------------------------------------------------------------------
# Kontrollerna (F.5)
# ---------------------------------------------------------------------------


class ChecksTests(BrevFixture, TestCase):
    def test_checks_come_back_as_json_without_link_checks(self):
        utskick = self.utskick(subject="")
        with mock.patch("apps.utskick.links.check_destinations") as checker:
            response = self.client.get(self.url("flamingo:app_brev_checks", utskick))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["blocking"])
        keys = [item["key"] for item in data["items"]]
        self.assertIn("subject", keys)
        self.assertIn("unsubscribe", keys)
        checker.assert_not_called()

    def test_links_are_checked_on_request(self):
        utskick = self.utskick()
        block = self.new_block(utskick, "button")
        block["versions"][-1]["fields"].update(
            primary_text="Boka", primary_url="https://exempelror.example/boka"
        )
        block["versions"][-1].pop("sig", None)
        self.save(utskick, blocks=[block])
        with mock.patch(
            "apps.utskick.links.check_destinations",
            return_value={"https://exempelror.example/boka": True},
        ) as checker:
            response = self.client.get(self.url("flamingo:app_brev_checks", utskick) + "?lankar=1")
        self.assertEqual(response.status_code, 200)
        checker.assert_called()

    def test_a_foreign_contact_is_400(self):
        utskick = self.utskick()
        foreign = self.person(account=self.other_account)
        url = self.url("flamingo:app_brev_checks", utskick) + f"?kontakt={foreign.pk}"
        self.assertEqual(self.client.get(url).status_code, 400)


# ---------------------------------------------------------------------------
# Förhandsvisningen (F.4)
# ---------------------------------------------------------------------------


class PreviewTests(BrevFixture, TestCase):
    def test_the_preview_is_html_without_scripts(self):
        self.person(first_name="Anna")
        utskick = self.utskick()
        self.new_block(utskick, "heading")
        response = self.client.get(self.url("flamingo:app_brev_preview", utskick))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("<script", response.content.decode().lower())

    def test_json_names_the_contact_and_merges_the_subject(self):
        anna = self.person(first_name="Anna")
        self.person(first_name="Bo")
        utskick = self.utskick()
        response = self.client.get(
            self.url("flamingo:app_brev_preview", utskick) + f"?kontakt={anna.pk}",
            HTTP_ACCEPT="application/json",
        )
        data = response.json()
        self.assertEqual(data["subject"], "Dags för service, Anna")
        self.assertEqual(data["kontakt"]["pk"], anna.pk)
        self.assertIsNotNone(data["next"])
        self.assertNotEqual(data["next"], anna.pk)
        self.assertIn("<html", data["html"])

    def test_dark_mode_and_foreign_contacts(self):
        utskick = self.utskick()
        dark = self.client.get(self.url("flamingo:app_brev_preview", utskick) + "?lage=morkt")
        self.assertIn("invert(1)", dark.content.decode())
        foreign = self.person(account=self.other_account)
        url = self.url("flamingo:app_brev_preview", utskick) + f"?kontakt={foreign.pk}"
        self.assertEqual(self.client.get(url).status_code, 400)


# ---------------------------------------------------------------------------
# AI (F.7)
# ---------------------------------------------------------------------------


def _tool_response(name, data):
    block = mock.Mock(type="tool_use", input=data)
    block.name = name
    return mock.Mock(content=[block])


class AiTests(BrevFixture, TestCase):
    def ai_on(self, data, name=None):
        name = name or ai.WRITE_BLOCK_TOOL["name"]
        patches = (
            mock.patch("apps.assistant.llm.is_configured", return_value=True),
            mock.patch("apps.assistant.llm.check_budget", return_value=None),
            mock.patch("apps.assistant.llm.call", return_value=_tool_response(name, data)),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_without_ai_the_template_is_the_suggestion(self):
        utskick = self.utskick()
        response = self.post_json(
            self.url("flamingo:app_brev_ai", utskick), {"type": "steps", "brief": "Höstservice"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual(data["source"], "mallar")
        self.assertIn("AI är inte inkopplad", data["note"])
        self.assertEqual(data["fields"]["title"], "Så går det till")
        # Ett block utan mallens text: kunden skriver själv.
        response = self.post_json(self.url("flamingo:app_brev_ai", utskick), {"type": "hero"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            "AI är inte inkopplad. Mallen har ingen text för blocket, så skriv texten själv.",
        )

    def test_ai_text_that_fails_the_guard_is_not_used(self):
        utskick = self.utskick()
        self.ai_on(
            {"falt": {"title": "Vi är billigast i stan", "lead": "Boka en tid som passar dig."}}
        )
        result = ai.write_block(utskick, "hero", user=self.anna, brief="")
        self.assertTrue(result.ok)
        self.assertEqual(result.source, "ai")
        self.assertNotIn("title", result.fields)
        self.assertEqual(result.fields["lead"], "Boka en tid som passar dig.")
        self.assertTrue(any("Rubrik" in w for w in result.warnings))

    def test_ai_never_writes_offers_in_information(self):
        utskick = self.utskick(purpose=INFORMATION, info_reason="oppettider")
        self.assertFalse(ai.write_block(utskick, "offer", user=self.anna).ok)
        self.ai_on({"falt": {"text": "Nu 20 % rabatt på service."}})
        result = ai.write_block(utskick, "heading", user=self.anna)
        self.assertNotIn("text", result.fields)
        self.assertTrue(result.warnings)

    def test_offer_words_need_confirmed_terms(self):
        utskick = self.utskick()
        text = "Erbjudandet gäller till 31 oktober."
        guard = ai.make_utskick_guard(self.account, utskick)
        self.assertTrue(guard.problems(text))
        terms = [{"label": "Erbjudandet gäller", "value": "till 31 oktober"}]
        Utskick.objects.filter(pk=utskick.pk).update(confirmed_terms=terms)
        utskick.refresh_from_db()
        self.assertTrue(ai.make_utskick_guard(self.account, utskick).problems(text))
        Utskick.objects.filter(pk=utskick.pk).update(terms_confirmed_at=timezone.now())
        utskick.refresh_from_db()
        self.assertEqual(ai.make_utskick_guard(self.account, utskick).problems(text), [])

    def test_the_demo_never_calls_the_model(self):
        self.account.is_demo = True
        self.account.save(update_fields=["is_demo"])
        utskick = self.utskick()
        with mock.patch("apps.assistant.llm.call") as call:
            result = ai.write_block(utskick, "steps", user=self.anna)
            sms = ai.write_sms(utskick, user=self.anna)
        call.assert_not_called()
        self.assertEqual(result.source, "mallar")
        self.assertEqual(sms.source, "mallar")

    def test_sms_from_ai_must_fit_one_part_and_name_the_company(self):
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.SMS_ONLY)
        self.ai_on(
            {"text": "Hej {förnamn|du}, dags för service hos Exempelrör. Svara JA om du vill."},
            name=ai.WRITE_SMS_TOOL["name"],
        )
        result = ai.write_sms(utskick, user=self.anna)
        self.assertEqual(result.source, "ai")
        self.assertIn("Exempelrör", result.text)

    def test_sms_without_the_company_falls_back_to_the_template(self):
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.SMS_ONLY)
        self.ai_on({"text": "Hej, dags för service."}, name=ai.WRITE_SMS_TOOL["name"])
        result = ai.write_sms(utskick, user=self.anna)
        self.assertEqual(result.source, "mallar")
        self.assertTrue(result.warnings)
        self.assertNotIn("{länk:", result.text)

    def test_the_view_refuses_unknown_blocks(self):
        utskick = self.utskick()
        response = self.post_json(self.url("flamingo:app_brev_ai", utskick), {"type": "x"})
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# Testmejlet (F.8)
# ---------------------------------------------------------------------------


class TestSendTests(BrevFixture, TestCase):
    def send(self, utskick, client=None, **data):
        data.setdefault("kanal", "epost")
        return (client or self.client).post(
            reverse("flamingo:app_utskick_test", args=[utskick.pk]),
            data,
            HTTP_ACCEPT="application/json",
        )

    def test_the_customer_gets_the_test_at_their_own_address(self):
        utskick = self.utskick()
        with mock.patch(
            "apps.utskick.sending.email.send_test", return_value=transport.Sent(ok=True)
        ) as send:
            response = self.send(utskick, till="mig")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["text"], "Testet är skickat till anna@exempelror.example.")
        self.assertEqual(send.call_args.kwargs["address"], "anna@exempelror.example")
        self.assertFalse(send.call_args.kwargs["actor"].staff)
        # En kund kan inte skriva en annan adress.
        with mock.patch("apps.utskick.sending.email.send_test") as send:
            response = self.send(utskick, till="eget", adress="annan@exempel.example")
        self.assertEqual(response.status_code, 400)
        send.assert_not_called()

    def test_staffs_typed_address_never_gets_a_contacts_details(self):
        # Säkerhetsgranskningen: byrån skriver bara sin egen adress (när
        # inloggningen saknar en), aldrig kundens eller en kontakts, och testet
        # visas då utan kontakt.
        utskick = self.utskick()
        kontakt = make_contact(self.account, first_name="Greta", email="greta@kund.example")
        staff = self.client_for(self.staff)
        with mock.patch(
            "apps.utskick.sending.email.send_test", return_value=transport.Sent(ok=True)
        ) as send:
            response = self.send(
                utskick, client=staff, till="eget", adress="byra@adx.example", kontakt=kontakt.pk
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(send.call_args.kwargs["address"], "byra@adx.example")
            self.assertIsNone(send.call_args.kwargs["contact"])
            send.reset_mock()
            for address in (self.anna.email, "Greta@Kund.Example"):
                response = self.send(utskick, client=staff, till="eget", adress=address)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["text"], brev.TEST_NOT_OWN_TEXT)
            send.assert_not_called()
            # Med en adress på inloggningen går testet alltid dit.
            self.staff.email = "byra@adx.se"
            self.staff.save(update_fields=["email"])
            response = self.send(utskick, client=staff, till="eget", adress="nagon@annan.example")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(send.call_args.kwargs["address"], "byra@adx.se")

    def test_email_off_is_said_in_plain_swedish(self):
        utskick = self.utskick()
        response = self.send(utskick, till="mig")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["text"], "E-post är inte påslaget än.")

    def test_staff_needs_the_checkbox_to_mail_the_customer(self):
        utskick = self.utskick()
        staff = self.client_for(self.staff)
        with mock.patch(
            "apps.utskick.sending.email.send_test", return_value=transport.Sent(ok=True)
        ) as send:
            response = self.send(utskick, client=staff, till="kunden", kund_adress=self.anna.email)
            self.assertEqual(response.status_code, 400)
            self.assertIn("Kryssa i", response.json()["text"])
            response = self.send(
                utskick,
                client=staff,
                till="kunden",
                kund_adress="okand@exempel.example",
                som_adx="1",
            )
            self.assertEqual(response.status_code, 400)
            send.assert_not_called()
            response = self.send(
                utskick, client=staff, till="kunden", kund_adress=self.anna.email, som_adx="1"
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(send.call_args.kwargs["actor"].staff)

    def test_never_from_the_demo_and_never_without_a_subject(self):
        utskick = self.utskick(subject="")
        with mock.patch("apps.utskick.sending.email.send_test") as send:
            response = self.send(utskick, till="mig")
            self.assertIn("ämnesrad", response.json()["text"])
            self.account.is_demo = True
            self.account.save(update_fields=["is_demo"])
            response = self.send(self.utskick(), till="mig")
            self.assertEqual(response.json()["text"], "Demokontot skickar aldrig.")
        send.assert_not_called()

    def test_a_form_post_redirects_to_the_editor(self):
        utskick = self.utskick()
        with mock.patch(
            "apps.utskick.sending.email.send_test", return_value=transport.Sent(ok=True)
        ):
            response = self.client.post(
                reverse("flamingo:app_utskick_test", args=[utskick.pk]),
                {"kanal": "epost", "till": "mig"},
            )
        self.assertRedirects(response, reverse("flamingo:app_brev", args=[utskick.pk]))


# ---------------------------------------------------------------------------
# Guiden: Kanal, Innehåll och Granska med e-post (I.6, I.8, D.8)
# ---------------------------------------------------------------------------


class GuideTests(BrevFixture, TestCase):
    def step(self, utskick, step):
        return reverse("flamingo:app_utskick_step", args=[utskick.pk, step])

    def test_email_modes_are_locked_until_email_is_on(self):
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.SMS_ONLY)
        response = self.client.get(self.step(utskick, "kanal"))
        self.assertContains(response, "Sms först, e-post till resten")
        self.assertContains(response, "Bara e-post")
        self.assertContains(response, "E-post är inte påslaget än.")
        response = self.client.post(
            self.step(utskick, "kanal"), {"syfte": "reklam", "kanal": "email_only"}
        )
        self.assertContains(response, "E-post är inte påslaget än.")
        utskick.refresh_from_db()
        self.assertEqual(utskick.channel_mode, Utskick.ChannelMode.SMS_ONLY)

    def test_with_email_on_the_channel_is_saved(self):
        self.email_on()
        self.person()
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.SMS_ONLY)
        response = self.client.get(self.step(utskick, "kanal"))
        self.assertContains(response, "1 mottagare")
        response = self.client.post(
            self.step(utskick, "kanal"), {"syfte": "reklam", "kanal": "email_only"}
        )
        self.assertRedirects(response, self.step(utskick, "innehall"))
        utskick.refresh_from_db()
        self.assertEqual(utskick.channel_mode, Utskick.ChannelMode.EMAIL_ONLY)

    def test_the_adx_cap_locks_email_without_an_own_domain(self):
        self.email_on()
        self.person()
        self.person()
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.SMS_ONLY)
        with mock.patch("apps.utskick.sending.email.adx_cap_left", return_value=1):
            response = self.client.get(self.step(utskick, "kanal"))
        self.assertContains(response, "kräver egen domän. Verifiera din domän under Inställningar.")

    def test_content_shows_the_mail_card_and_tabs(self):
        utskick = self.utskick()
        response = self.client.get(self.step(utskick, "innehall"))
        self.assertContains(response, "Skriv mejlet")
        self.assertContains(response, reverse("flamingo:app_brev", args=[utskick.pk]))
        self.assertNotContains(response, 'name="sms_body"')
        both = self.utskick(channel_mode=Utskick.ChannelMode.BOTH, sms_body="Hej från Exempelrör")
        response = self.client.get(self.step(both, "innehall"))
        self.assertContains(response, 'name="sms_body"')
        self.assertContains(response, 'name="flik" value="epost"')
        response = self.client.post(
            self.step(both, "innehall"), {"sms_body": "Hej igen från Exempelrör", "flik": "epost"}
        )
        self.assertRedirects(response, self.step(both, "innehall") + "?flik=epost")
        both.refresh_from_db()
        self.assertEqual(both.sms_body, "Hej igen från Exempelrör")
        response = self.client.get(self.step(both, "innehall") + "?flik=epost")
        self.assertContains(response, "Skicka testmejl")

    def test_saving_the_sms_keeps_the_mails_fallbacks(self):
        utskick = self.utskick(
            channel_mode=Utskick.ChannelMode.BOTH,
            sms_body="Hej {förnamn} från Exempelrör",
            merge_fallbacks={"efternamn": "kund", "förnamn": "du"},
        )
        self.client.post(
            self.step(utskick, "innehall"),
            {"sms_body": "Hej {förnamn} från Exempelrör", "reserv_förnamn": "vän"},
        )
        utskick.refresh_from_db()
        self.assertEqual(utskick.merge_fallbacks, {"efternamn": "kund", "förnamn": "vän"})

    def test_review_counts_mails_and_blocks_while_email_is_off(self):
        self.person()
        utskick = self.utskick(send_mode=Utskick.SendMode.NOW)
        self.new_block(utskick, "heading")
        response = self.client.get(self.step(utskick, "granska"))
        self.assertEqual(response.status_code, 200)
        review = response.context["review"]
        self.assertEqual(review["n_email"], 1)
        self.assertTrue(review["blocking"])
        texts = [item["text"] for item in review["items"]]
        self.assertIn("E-post är inte påslaget än.", texts)
        self.assertIn("1 får mejlet.", texts)
        self.assertEqual(review["summary"]["email"], 1)
        self.assertFalse(any("sms" in t.lower() and "STOPP" in t for t in texts))
        self.assertContains(response, "1 mejl")
        self.assertContains(response, "E-post ingår.")
        self.assertIn("brev", response.context)

    def test_ai_writes_an_sms_suggestion_that_the_customer_chooses(self):
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.SMS_ONLY, sms_body="Min text")
        url = self.step(utskick, "innehall")
        response = self.client.post(url, {"sms_body": "Min text", "ai_skriv": "1", "ai_brief": "x"})
        self.assertRedirects(response, url + "#ut-ai", fetch_redirect_response=False)
        utskick.refresh_from_db()
        self.assertEqual(utskick.sms_body, "Min text")
        page = self.client.get(url)
        suggestion = page.context["ai_suggestion"]
        self.assertEqual(suggestion["source"], "mallar")
        self.assertContains(page, "Använd förslaget")
        self.assertContains(page, "AI är inte inkopplad")
        # Förslaget visas en gång.
        self.assertIsNone(self.client.get(url).context["ai_suggestion"])
        self.client.post(
            url, {"sms_body": "Min text", "ai_anvand": "1", "ai_text": suggestion["text"]}
        )
        utskick.refresh_from_db()
        self.assertEqual(utskick.sms_body, suggestion["text"])
        self.assertIn("Exempelrör", utskick.sms_body)

    def test_the_count_names_the_missing_address(self):
        from .app_views import utskick as guide

        counted = {"sms": 3, "email": 4, "skipped": 2, "skipped_by_reason": {"no_address": 2}}
        self.assertEqual(
            guide.channels_count_text(counted, self.utskick()),
            "4 får mejlet. 2 hoppas över: 2 saknar e-post.",
        )
        both = self.utskick(channel_mode=Utskick.ChannelMode.SMS_THEN_EMAIL)
        self.assertEqual(
            guide.channels_count_text(counted, both),
            "3 får sms och 4 får mejlet. 2 hoppas över: 2 saknar nummer och e-post.",
        )

    def test_review_for_both_channels_says_both(self):
        self.email_on()
        utskick = self.utskick(channel_mode=Utskick.ChannelMode.BOTH)
        from .app_views import utskick as guide

        checked = {
            "n_sms": 3,
            "n_email": 2,
            "uses_sms": True,
            "uses_email": True,
            "cost_text": "2 kr",
        }
        self.assertEqual(
            guide._dialog_text(checked),
            "3 sms och 2 mejl skickas nu. Kostnad cirka 2 kr. E-post ingår.",
        )
        checked["uses_sms"] = False
        self.assertEqual(guide._dialog_text(checked), "2 mejl skickas nu. E-post ingår.")
        self.assertIn("visas som skickade", guide._dialog_text(checked, demo=True))
        self.assertEqual(utskick.channel_mode, "both")


# ---------------------------------------------------------------------------
# Inställningar, listan och rapporten (I.8, I.9)
# ---------------------------------------------------------------------------

SETTINGS_FORM = {
    "weekday_start": "9",
    "weekday_end": "20",
    "weekend_start": "10",
    "weekend_end": "18",
    "weekly_cap_sms": "2",
    "weekly_cap_email": "4",
    "notify_on_reply": "1",
}


class SettingsRowTests(BrevFixture, TestCase):
    def test_the_email_rows_and_open_tracking(self):
        url = reverse("flamingo:app_utskick_settings")
        response = self.client.get(url)
        self.assertContains(response, "Avsändardomän")
        self.assertContains(response, "utskick.adx.se · 0 av 2")
        self.assertContains(response, "Använd din egen domän")
        self.assertContains(response, "Svar till Inkorgen.")
        self.assertContains(response, "Av som standard. Öppningar mäts bara hos dem som sagt ja")
        response = self.client.post(url, {**SETTINGS_FORM, "open_tracking": "1"})
        self.assertEqual(response.status_code, 302)
        row = UtskickSettings.objects.get(account=self.account)
        self.assertTrue(row.open_tracking)
        self.assertTrue(row.consent_text_email.endswith("visar om de öppnas."))
        # Nya utskick tar med inställningen (B.2).
        self.client.post(reverse("flamingo:app_utskick_new"), {"namn": "Nytt"})
        self.assertTrue(Utskick.objects.get(account=self.account, name="Nytt").open_tracking)
        self.client.post(url, SETTINGS_FORM)
        row.refresh_from_db()
        self.assertFalse(row.open_tracking)
        self.assertNotIn("visar om de öppnas", row.consent_text_email)

    def test_a_long_consent_text_is_not_cut(self):
        long_text = "Ja tack, " + "x" * 180
        UtskickSettings.objects.filter(account=self.account).update(consent_text_email=long_text)
        url = reverse("flamingo:app_utskick_settings")
        response = self.client.post(url, {**SETTINGS_FORM, "open_tracking": "1"})
        self.assertContains(response, "blir för lång med meningen om öppningar")
        row = UtskickSettings.objects.get(account=self.account)
        self.assertFalse(row.open_tracking)
        self.assertEqual(row.consent_text_email, long_text)


class ListAndReportTests(BrevFixture, TestCase):
    def test_the_list_names_the_channel(self):
        self.utskick(name="Bara mejl")
        self.utskick(name="Båda", channel_mode=Utskick.ChannelMode.BOTH)
        html = self.client.get(reverse("flamingo:app_utskick_list")).content.decode()
        self.assertIn('<td data-label="Kanal">E-post</td>', html)
        self.assertIn('<td data-label="Kanal">Sms och e-post</td>', html)

    def test_the_report_has_email_tiles(self):
        from .models import Recipient, Suppression

        kontakt = self.person()
        utskick = self.utskick(status=Utskick.Status.SENT, open_tracking=True)
        now = timezone.now()
        Recipient.objects.create(
            utskick=utskick,
            contact=kontakt,
            channel=CHANNEL_EMAIL,
            address=kontakt.email,
            status=Recipient.Status.DELIVERED,
            sent_at=now,
            opened_at=now,
        )
        Recipient.objects.create(
            utskick=utskick,
            channel=CHANNEL_EMAIL,
            address="studs@exempel.example",
            status=Recipient.Status.BOUNCED,
            sent_at=now,
        )
        Suppression.objects.create(
            account=self.account,
            channel=CHANNEL_EMAIL,
            value_hash="ab" * 32,
            reason=Suppression.Reason.LINK,
            utskick=utskick,
        )
        # Studsens spärr (inbound.events) bär också utskicket, men en adress
        # som inte finns är ingen avregistrering: den står under Studsar.
        Suppression.objects.create(
            account=self.account,
            channel=CHANNEL_EMAIL,
            value_hash="cd" * 32,
            reason=Suppression.Reason.BOUNCE,
            utskick=utskick,
        )
        response = self.client.get(reverse("flamingo:app_utskick", args=[utskick.pk]))
        self.assertEqual(response.status_code, 200)
        tiles = {t["label"]: t["value"] for t in response.context["email_tiles"]}
        self.assertEqual(tiles["Levererade"], 1)
        self.assertEqual(tiles["Öppnat (indikation)"], 1)
        self.assertEqual(tiles["Studsar"], 1)
        self.assertEqual(tiles["Avregistreringar"], 1)
        self.assertTrue(response.context["header_line"].startswith("E-post"))
        labels = [t["label"] for t in response.context["tiles"]]
        self.assertEqual(labels, ["Förfrågningar"])
        Utskick.objects.filter(pk=utskick.pk).update(open_tracking=False)
        response = self.client.get(reverse("flamingo:app_utskick", args=[utskick.pk]))
        labels = [t["label"] for t in response.context["email_tiles"]]
        self.assertNotIn("Öppnat (indikation)", labels)


class PauseBannerTests(BrevFixture, TestCase):
    def paused(self, reason):
        return self.utskick(
            status=Utskick.Status.PAUSED_HEALTH
            if reason in ("bounces", "complaints")
            else Utskick.Status.PAUSED,
            pause_reason=reason,
        )

    def banner(self, utskick, client=None):
        response = (client or self.client).get(reverse("flamingo:app_utskick", args=[utskick.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context["banner"]

    def test_email_pauses_say_what_happened(self):
        from .models import Recipient

        bounced = self.paused("bounces")
        for status in (Recipient.Status.DELIVERED,) * 3 + (Recipient.Status.BOUNCED,):
            Recipient.objects.create(utskick=bounced, channel=CHANNEL_EMAIL, status=status)
        banner = self.banner(bounced)
        self.assertIn(
            "25" + chr(0xA0) + "% av de första 4 mejlen studsade. Vi pausar vid 4 %", banner["text"]
        )
        labels = [b["label"] for b in banner["buttons"]]
        self.assertIn("Ta bort studsade och fortsätt", labels)
        staff = [b["label"] for b in self.banner(bounced, self.client_for(self.staff))["buttons"]]
        self.assertEqual(staff.count("Fortsätt") + staff.count("Ta bort studsade och fortsätt"), 1)
        # Kunden får fortsätta efter studsarna (I.5).
        with mock.patch("apps.utskick.sending.state.resume") as resume:
            resume.return_value = mock.Mock(ok=True, error="")
            self.client.post(
                reverse("flamingo:app_utskick_state", args=[bounced.pk]), {"action": "fortsatt"}
            )
        resume.assert_called_once()
        cap = self.paused("adx_mail_cap")
        banner = self.banner(cap)
        self.assertIn("mejl från ADX-domänen är skickade i", banner["text"])
        self.assertEqual(banner["buttons"][0]["label"], "Verifiera domän")
        self.assertEqual(
            self.banner(self.paused("account_health"))["text"],
            "E-postutskick är spärrade tills ADX har gått igenom studsarna.",
        )
        self.assertEqual(
            self.banner(self.paused("email_disabled"))["text"], "E-post är inte påslaget än."
        )
        self.assertIn("skräppost", self.banner(self.paused("complaints"))["text"])


# ---------------------------------------------------------------------------
# Profilen i flamingo-pb.js och copy-reglerna
# ---------------------------------------------------------------------------


class ScriptTests(TestCase):
    def read(self, path):
        return (settings.BASE_DIR / path).read_text(encoding="utf-8")

    def test_the_brev_script_uses_the_public_api_only(self):
        script = self.read("static/js/flamingo-app-brev.js")
        self.assertIn('pb.profile !== "brev"', script)
        for name in ("saveExtra", "renderExtra", "setField", "touch", "rerender", "openMedia"):
            self.assertIn(f"pb.{name}(", script)
        self.assertNotIn("innerHTML = data", script)
        self.assertNotIn("eval(", script)

    def test_the_page_builder_exposes_the_profile_api(self):
        script = self.read("static/js/flamingo-pb.js")
        for name in ("setField:", "saveExtra:", "renderExtra:", "rerender:", "openMedia:"):
            self.assertIn(name, script)
        self.assertIn("config.canvasEnd", script)
        self.assertIn(
            "listeners = { change: [], select: [], saved: [], panel: [], field: [] }", script
        )

    def test_templates_have_no_inline_styles_or_scripts(self):
        for path in (
            "templates/flamingo/app/utskick/brev_editor.html",
            "templates/flamingo/app/utskick/_brev_card.html",
            "templates/flamingo/app/utskick/_brev_icons.html",
        ):
            with self.subTest(path=path):
                text = self.read(path)
                self.assertNotIn("style=", text)
                self.assertNotIn("<script>", text)
                self.assertNotIn("[ ", text)

    def test_copy_rules_in_the_new_files(self):
        banned = [chr(0x2013), chr(0x2014), chr(0x2026), chr(0x201C), chr(0x201D), chr(0x2019)]
        for path in (
            "apps/utskick/app_views/brev.py",
            "apps/utskick/ai.py",
            "static/js/flamingo-app-brev.js",
            "static/css/flamingo-app-brev.css",
            "templates/flamingo/app/utskick/brev_editor.html",
            "templates/flamingo/app/utskick/_brev_card.html",
        ):
            with self.subTest(path=path):
                text = self.read(path)
                for ch in banned:
                    self.assertNotIn(ch, text)

    def test_ui_strings_have_no_exclamation_marks(self):
        script = self.read("static/js/flamingo-app-brev.js")
        for line in script.splitlines():
            stripped = line.strip()
            if '"' in stripped and "!" in stripped:
                quoted = stripped.split('"')[1::2]
                for text in quoted:
                    with self.subTest(text=text):
                        self.assertNotIn("! ", text + " ")
