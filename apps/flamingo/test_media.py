"""ADX Flamingo: mediaarkivet (media.py, app_views/media.py): uppladdningen
och omkodningen till WebP, gränserna, logotypens färger, bilderna från
hemsidan (läsningen och hämtningen till arkivet) och bildväljarens JSON.

Inget här anropar något utanför testet: hämtningarna går genom en utbytt
analyzer.fetch, och SSRF-testerna prövar bara adresser som stoppas innan
någon anslutning görs."""

import io
import shutil
import tempfile
import threading
import time
import warnings
from unittest import mock
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from PIL import Image

from apps.projects.access import VIEW_AS_KEY
from apps.projects.models import Customer
from apps.tools.analyzer import AnalysError, Sida

from . import media, pagebuilder, scan
from .models import (
    MEDIA_MAX_PER_ACCOUNT,
    MEDIA_MAX_SIDE,
    MEDIA_THUMB_SIDE,
    Campaign,
    FlamingoAccount,
    LandingPage,
    MediaAsset,
    Service,
    SiteImageCandidate,
)
from .pagebuilder import render

User = get_user_model()
_MEDIA = tempfile.mkdtemp(prefix="flamingo-media-")


def image_bytes(fmt="PNG", size=(800, 600), color=(30, 90, 200), mode="RGB", **save):
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, fmt, **save)
    return buffer.getvalue()


def upload(name, data, content_type="image/png"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def opened(field):
    with field.open("rb") as handle:
        image = Image.open(io.BytesIO(handle.read()))
        image.load()
    return image


def site_response(url, body, content_type="image/jpeg", truncated=False):
    return Sida(
        url=url,
        status=200,
        html=body.decode("utf-8", "replace"),
        headers={"content-type": content_type},
        body=body,
        truncated=truncated,
    )


class MediaFixture:
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.customer = Customer.objects.create(
            name="Lindqvist Rör AB", website="https://lindqvistror.se"
        )
        cls.anna = User.objects.create_user("anna@ror.se", email="anna@ror.se", password="x")
        cls.customer.users.add(cls.anna)
        cls.account = FlamingoAccount.objects.create(
            customer=cls.customer, is_enabled=True, website_url="https://lindqvistror.se/"
        )
        cls.other_customer = Customer.objects.create(name="Hemlig Bygg AB")
        cls.other_account = FlamingoAccount.objects.create(
            customer=cls.other_customer, is_enabled=True
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.anna)

    def asset(self, account=None, color=(30, 90, 200), size=(800, 600), **fields):
        processed = media.process_image(image_bytes(size=size, color=color))
        asset = media.store_image(account or self.account, processed, alt=fields.pop("alt", ""))
        if fields:
            MediaAsset.objects.filter(pk=asset.pk).update(**fields)
            asset.refresh_from_db()
        return asset

    def post(self, data, client=None):
        return (client or self.client).post(reverse("flamingo:app_media"), data)


# ---------------------------------------------------------------------------
# Omkodningen: format, storlek, metadata
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class ProcessingTests(MediaFixture, TestCase):
    def test_every_accepted_format_becomes_webp_with_a_thumbnail(self):
        frames = [Image.new("RGB", (300, 200), c) for c in ((220, 30, 30), (30, 200, 30))]
        gif = io.BytesIO()
        frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
        files = {
            "bild.jpg": image_bytes("JPEG", quality=90),
            "bild.png": image_bytes("PNG"),
            "bild.webp": image_bytes("WEBP"),
            "animerad.gif": gif.getvalue(),
        }
        for name, data in files.items():
            with self.subTest(name=name):
                asset = media.add_upload(self.account, upload(name, data))
                stored = opened(asset.file)
                self.assertEqual(stored.format, "WEBP")
                self.assertTrue(asset.file.name.endswith(".webp"))
                self.assertNotIn(name.split(".")[0], asset.file.name)  # slumpat namn
                self.assertEqual(opened(asset.thumb).format, "WEBP")
                self.assertEqual((asset.width, asset.height), stored.size)
        gif_asset = MediaAsset.objects.filter(account=self.account).order_by("-pk").first()
        # Den första bildrutan (röd), inte den andra.
        red, green, _ = opened(gif_asset.file).convert("RGB").getpixel((10, 10))
        self.assertGreater(red, 150)
        self.assertLess(green, 100)

    def test_the_longest_side_is_capped_and_the_thumbnail_is_smaller(self):
        asset = media.add_upload(
            self.account, upload("stor.jpg", image_bytes("JPEG", (3600, 2400)))
        )
        self.assertEqual(max(asset.width, asset.height), MEDIA_MAX_SIDE)
        self.assertEqual((asset.width, asset.height), (2400, 1600))
        self.assertEqual(max(opened(asset.thumb).size), MEDIA_THUMB_SIDE)
        small = media.add_upload(self.account, upload("liten.png", image_bytes(size=(300, 200))))
        self.assertEqual((small.width, small.height), (300, 200))  # aldrig uppskalad

    def test_exif_is_stripped_after_the_orientation_is_applied(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # roterad 90 grader
        exif[0x010F] = "Kameramärke"
        exif[0x8825] = {1: "N"}  # GPS
        data = image_bytes("JPEG", (400, 200), exif=exif.tobytes())
        asset = media.add_upload(self.account, upload("IMG_1234.jpg", data, "image/jpeg"))
        stored = opened(asset.file)
        self.assertEqual(stored.size, (200, 400))
        self.assertEqual(dict(stored.getexif()), {})
        self.assertNotIn("exif", stored.info)
        self.assertEqual(asset.alt, "")  # kamerans namn blir ingen alt-text

    def test_transparency_is_kept_and_alt_comes_from_the_file_name(self):
        data = image_bytes("PNG", (400, 200), (10, 120, 200, 0), mode="RGBA")
        asset = media.add_upload(self.account, upload("badrum-efter_renovering.png", data))
        self.assertEqual(opened(asset.file).mode, "RGBA")
        self.assertEqual(asset.alt, "Badrum efter renovering")

    def test_svg_html_and_other_formats_are_refused(self):
        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        cases = {
            ("logo.svg", "image/svg+xml"): svg,
            ("logo.png", "image/png"): svg,
            ("bild.png", "image/png"): b"<html><body><script>alert(1)</script></body></html>",
            ("bild.gif", "image/gif"): b"GIF89a<script>alert(1)</script>",
            ("bild.jpg", "image/jpeg"): b"\xff\xd8\xff\xe0" + b"\x00" * 200,
            ("bild.bmp", "image/bmp"): image_bytes("BMP"),
            ("bild.tiff", "image/tiff"): image_bytes("TIFF"),
        }
        for (name, kind), data in cases.items():
            with self.subTest(name=name, kind=kind), self.assertRaises(media.MediaError):
                media.add_upload(self.account, upload(name, data, kind))
        self.assertFalse(MediaAsset.objects.filter(account=self.account).exists())

    def test_a_decompression_bomb_is_refused_before_it_is_decoded(self):
        bomb = io.BytesIO()
        Image.new("1", (8000, 8000)).save(bomb, "PNG")  # 64 miljoner punkter, några kB
        self.assertLess(len(bomb.getvalue()), 200_000)
        with (
            warnings.catch_warnings(),
            mock.patch.object(Image.Image, "load", side_effect=AssertionError("avkodad")),
            self.assertRaises(media.MediaError) as caught,
        ):
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            media.add_upload(self.account, upload("bomb.png", bomb.getvalue()))
        self.assertIn("för stor", caught.exception.message)

    def test_a_file_over_15_mb_is_refused_without_being_read(self):
        big = upload("stor.jpg", b"\xff\xd8" + b"\x00" * (media.MAX_UPLOAD_BYTES + 10))
        with mock.patch.object(media, "process_image") as process:
            with self.assertRaises(media.MediaError) as caught:
                media.add_upload(self.account, big)
        process.assert_not_called()
        self.assertIn("15 MB", caught.exception.message)


# ---------------------------------------------------------------------------
# Gränsen per konto
# ---------------------------------------------------------------------------


def fill_archive(account, count):
    MediaAsset.objects.bulk_create(
        MediaAsset(account=account, file=f"flamingo/fyllnad/{n}.webp", width=1, height=1)
        for n in range(count)
    )


@override_settings(MEDIA_ROOT=_MEDIA)
class LimitTests(MediaFixture, TestCase):
    def test_the_archive_stops_at_200_images(self):
        self.assertEqual(MEDIA_MAX_PER_ACCOUNT, 200)
        fill_archive(self.account, MEDIA_MAX_PER_ACCOUNT - 1)
        media.add_upload(self.account, upload("sista.png", image_bytes()))
        with self.assertRaises(media.MediaError) as caught:
            media.add_upload(self.account, upload("en-till.png", image_bytes()))
        self.assertIn("fullt", caught.exception.message)
        self.assertEqual(MediaAsset.objects.filter(account=self.account).count(), 200)
        # Det andra kontots arkiv påverkas inte.
        media.add_upload(self.other_account, upload("annan.png", image_bytes()))

    def test_the_upload_endpoint_says_why_when_full(self):
        fill_archive(self.account, MEDIA_MAX_PER_ACCOUNT)
        response = self.client.post(
            reverse("flamingo:app_media_upload"), {"file": upload("x.png", image_bytes())}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("fullt", response.json()["error"])
        listing = self.client.get(reverse("flamingo:app_media_json")).json()
        self.assertFalse(listing["can_upload"])
        self.assertEqual(listing["count"], 200)


@override_settings(MEDIA_ROOT=_MEDIA)
class ConcurrentUploadTests(TransactionTestCase):
    """Två (fyra) uppladdningar samtidigt om den sista platsen: arkivet
    låses per konto, så bara en kommer in."""

    def test_only_one_of_four_simultaneous_uploads_gets_the_last_place(self):
        customer = Customer.objects.create(name="Samtidig AB")
        account = FlamingoAccount.objects.create(customer=customer, is_enabled=True)
        fill_archive(account, MEDIA_MAX_PER_ACCOUNT - 1)
        processed = media.process_image(image_bytes())
        barrier = threading.Barrier(4)
        results = []

        def upload_one():
            try:
                barrier.wait(5)
                media.store_image(FlamingoAccount.objects.get(pk=account.pk), processed)
                results.append("ok")
            except media.MediaError as exc:
                results.append(exc.message)
            finally:
                connection.close()

        threads = [threading.Thread(target=upload_one) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(MediaAsset.objects.filter(account=account).count(), MEDIA_MAX_PER_ACCOUNT)


# ---------------------------------------------------------------------------
# Arkivet i verktyget: kontot, logotypen, ta bort
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class ArchiveViewTests(MediaFixture, TestCase):
    def test_the_page_and_the_menu(self):
        mine = self.asset(alt="Vårt badrum")
        theirs = self.asset(account=self.other_account, alt="Hemlig bild")
        response = self.client.get(reverse("flamingo:app_media"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Mediaarkivet", html)
        self.assertIn("Vårt badrum", html)
        self.assertNotIn("Hemlig bild", html)
        self.assertIn(mine.thumb.url, html)
        self.assertNotIn(theirs.thumb.url, html)
        for label in ("Alla", "Logotyp", "Uppladdat", "Från hemsidan", "Färger från logotypen"):
            self.assertIn(label, html)
        self.assertIn(f'href="{reverse("flamingo:app_media")}"', html)  # sidomenyn
        for tab in ("logotyp", "uppladdat", "hemsidan"):
            self.assertEqual(
                self.client.get(reverse("flamingo:app_media") + f"?visa={tab}").status_code, 200
            )

    def test_upload_without_script_alt_and_logo(self):
        response = self.post(
            {
                "action": "upload",
                "file": [upload("tak.png", image_bytes()), upload("fel.svg", b"<svg/>")],
            }
        )
        self.assertEqual(response.status_code, 302)
        asset = MediaAsset.objects.get(account=self.account)
        self.assertEqual(asset.alt, "Tak")
        self.post({"action": "alt", "asset": asset.pk, "alt": "Nytt tak <b>i Nacka</b>"})
        asset.refresh_from_db()
        self.assertEqual(asset.alt, "Nytt tak i Nacka")
        self.post({"action": "logo", "asset": asset.pk})
        asset.refresh_from_db()
        self.assertTrue(asset.is_logo)

    def test_one_logo_per_account(self):
        first, second = self.asset(), self.asset(color=(200, 40, 40))
        self.post({"action": "logo", "asset": first.pk})
        self.post({"action": "logo", "asset": second.pk})
        logos = list(MediaAsset.objects.filter(account=self.account, is_logo=True))
        self.assertEqual(logos, [second])
        self.post({"action": "unlogo", "asset": second.pk})
        self.assertFalse(MediaAsset.objects.filter(account=self.account, is_logo=True).exists())

    def test_another_accounts_image_is_404_everywhere(self):
        theirs = self.asset(account=self.other_account, alt="Hemlig")
        for action in ("alt", "logo", "unlogo", "delete"):
            with self.subTest(action=action):
                response = self.post({"action": action, "asset": theirs.pk, "alt": "x"})
                self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.alt, "Hemlig")
        self.assertFalse(theirs.is_logo)
        page = LandingPage.objects.create(account=self.other_account, name="Hemlig sida")
        self.assertEqual(self.post({"action": "palette", "page": page.pk}).status_code, 404)
        listing = self.client.get(reverse("flamingo:app_media_json")).json()
        self.assertNotIn(theirs.pk, [a["id"] for a in listing["assets"]])

    def test_delete_is_refused_while_a_page_uses_the_image_and_names_the_page(self):
        image = self.asset(alt="Används")
        hero = pagebuilder.new_block("hero", "image", self.account)
        hero["versions"][0]["fields"]["image"] = image.pk
        page = LandingPage.objects.create(
            account=self.account, name="Badrum Nacka", draft={"blocks": [hero]}
        )
        response = self.post({"action": "delete", "asset": image.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(MediaAsset.objects.filter(pk=image.pk).exists())
        shown = [str(m) for m in self.client.get(response.url).context["messages"]]
        self.assertTrue(any("Badrum Nacka" in m for m in shown), shown)
        # Bara i den publicerade versionen: fortfarande nej.
        LandingPage.objects.filter(pk=page.pk).update(
            draft={"blocks": []}, published={"blocks": [hero]}
        )
        with self.assertRaises(media.MediaInUse) as caught:
            media.delete_asset(image)
        self.assertEqual(caught.exception.uses[0].where, "den publicerade sidan")
        # En version som inte är aktiv räknas också (den kan väljas igen).
        old = pagebuilder.new_block("person", "image", self.account)
        old["versions"][0]["fields"]["image"] = image.pk
        pagebuilder.add_version(old, {"name": "Lisa"}, pagebuilder.SOURCE_CUSTOMER, None)
        LandingPage.objects.filter(pk=page.pk).update(
            draft={"blocks": [old]}, published={"blocks": []}
        )
        with self.assertRaises(media.MediaInUse):
            media.delete_asset(image)
        # Ingen sida använder den: borttagen.
        LandingPage.objects.filter(pk=page.pk).update(draft={"blocks": []})
        self.post({"action": "delete", "asset": image.pk})
        self.assertFalse(MediaAsset.objects.filter(pk=image.pk).exists())

    def test_staff_viewing_as_the_customer_can_use_the_endpoints(self):
        staff = Client()
        staff.force_login(self.staff)
        session = staff.session
        session[VIEW_AS_KEY] = self.customer.pk
        session.save()
        response = staff.post(
            reverse("flamingo:app_media_upload"), {"file": upload("byra.png", image_bytes())}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(staff.get(reverse("flamingo:app_media_json")).json()["count"], 1)
        self.assertEqual(staff.get(reverse("flamingo:app_media")).status_code, 200)
        # Utan kundvy och utan behörighet: inget arkiv.
        self.assertEqual(Client().get(reverse("flamingo:app_media_json")).status_code, 404)


# ---------------------------------------------------------------------------
# Bildväljarens JSON
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class JsonTests(MediaFixture, TestCase):
    def test_the_listing_has_the_agreed_shape(self):
        logo = self.asset(alt="Logga")
        media.set_logo(logo)
        self.asset(alt="Bild")
        data = self.client.get(reverse("flamingo:app_media_json")).json()
        self.assertEqual(set(data), {"assets", "can_upload", "limit", "count"})
        self.assertEqual((data["limit"], data["count"], data["can_upload"]), (200, 2, True))
        first = data["assets"][0]
        self.assertEqual(set(first), {"id", "thumb", "url", "width", "height", "alt", "is_logo"})
        self.assertEqual((first["id"], first["is_logo"]), (logo.pk, True))
        self.assertTrue(first["thumb"].startswith("/media/flamingo/"))
        self.assertEqual(self.client.post(reverse("flamingo:app_media_json")).status_code, 405)

    def test_upload_takes_several_files_and_reports_the_bad_ones(self):
        response = self.client.post(
            reverse("flamingo:app_media_upload"),
            {
                "file": [
                    upload("ett.png", image_bytes()),
                    upload("tva.jpg", image_bytes("JPEG"), "image/jpeg"),
                    upload("tre.svg", b"<svg/>", "image/svg+xml"),
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["assets"]), 2)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("tre.svg", body["errors"][0])
        only_bad = self.client.post(
            reverse("flamingo:app_media_upload"), {"file": upload("x.svg", b"<svg/>")}
        )
        self.assertEqual(only_bad.status_code, 400)
        self.assertIn("error", only_bad.json())
        self.assertEqual(self.client.get(reverse("flamingo:app_media_upload")).status_code, 405)

    def test_csrf_is_enforced_and_the_header_works(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.anna)
        url = reverse("flamingo:app_media_upload")
        refused = client.post(url, {"file": upload("a.png", image_bytes())})
        self.assertEqual(refused.status_code, 403)
        client.get(reverse("flamingo:app_media"))
        token = client.cookies["csrftoken"].value
        accepted = client.post(
            url, {"file": upload("a.png", image_bytes())}, headers={"X-CSRFToken": token}
        )
        self.assertEqual(accepted.status_code, 200)


# ---------------------------------------------------------------------------
# Logotypens färger och paletten
# ---------------------------------------------------------------------------


def logo_image(primary=(27, 102, 210), accent=(242, 153, 74)):
    """En logotyp: genomskinlig bakgrund, en vit och en svart detalj, en
    stor ruta i huvudfärgen och en mindre i accentfärgen."""
    image = Image.new("RGBA", (400, 200), (255, 255, 255, 0))
    for box, color in (
        ((0, 0, 200, 200), (*primary, 255)),
        ((200, 0, 280, 200), (*accent, 255)),
        ((280, 0, 330, 200), (255, 255, 255, 255)),
        ((330, 0, 360, 200), (10, 10, 10, 255)),
    ):
        image.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), color), box[:2])
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=_MEDIA, INQUIRY_NOTIFICATION_EMAIL="larm@adx.example")
class LogoColorTests(MediaFixture, TestCase):
    def logo(self, **kwargs):
        return media.add_upload(self.account, upload("logo.png", logo_image(**kwargs)))

    def test_colors_skip_white_black_and_transparency_and_pick_the_primary(self):
        colors = media.logo_colors_from(Image.open(io.BytesIO(logo_image())))
        self.assertEqual(len(colors["colors"]), 2)
        primary = colors["primary"]
        self.assertTrue(primary.startswith("#") and len(primary) == 7)
        r, g, b = (int(primary[i : i + 2], 16) for i in (1, 3, 5))
        self.assertLess(r, 60)
        self.assertGreater(b, 170)  # den blå, den största
        for color in colors["colors"]:
            self.assertNotIn(color, ("#FFFFFF", "#0A0A0A"))
        many = Image.new("RGB", (500, 100))
        for n, color in enumerate(
            (
                (200, 30, 30),
                (30, 160, 60),
                (30, 60, 200),
                (230, 180, 20),
                (120, 40, 160),
                (0, 150, 150),
            )
        ):
            many.paste(Image.new("RGB", (80, 100), color), (n * 80, 0))
        self.assertLessEqual(len(media.logo_colors_from(many)["colors"]), 5)
        self.assertGreaterEqual(len(media.logo_colors_from(many)["colors"]), 3)
        self.assertEqual(
            media.logo_colors_from(Image.new("RGBA", (50, 50), (255, 255, 255, 0))), {}
        )

    def test_the_logo_gives_every_page_its_colors_and_new_pages_get_them_too(self):
        page = LandingPage.objects.create(account=self.account, name="Före logotypen")
        other = LandingPage.objects.create(account=self.other_account, name="Annan")
        logo = self.logo()
        self.post({"action": "logo", "asset": logo.pk})
        page.refresh_from_db()
        self.assertEqual(page.logo_colors["asset"], logo.pk)
        self.assertTrue(page.logo_colors["primary"].startswith("#"))
        self.assertEqual(page.palette, LandingPage.PALETTE_BLUE)  # paletten ändras inte själv
        other.refresh_from_db()
        self.assertEqual(other.logo_colors, {})
        new = LandingPage.objects.create(account=self.account, name="Efter logotypen")
        self.assertEqual(new.logo_colors, page.logo_colors)
        html = self.client.get(reverse("flamingo:app_media")).content.decode()
        self.assertIn(page.logo_colors["primary"], html)
        self.assertIn("Använd som palett", html)

    def test_use_as_palette_for_all_pages_or_one(self):
        first = LandingPage.objects.create(account=self.account, name="Ett")
        second = LandingPage.objects.create(account=self.account, name="Två")
        media.set_logo(self.logo())
        self.post({"action": "palette", "page": first.pk})
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.palette, LandingPage.PALETTE_LOGO)
        self.assertEqual(second.palette, LandingPage.PALETTE_BLUE)
        self.post({"action": "palette", "page": "alla"})
        self.assertEqual(
            set(LandingPage.objects.filter(account=self.account).values_list("palette", flat=True)),
            {LandingPage.PALETTE_LOGO},
        )
        self.assertEqual(mail.outbox, [])  # ingen sida är live: inget larm

    def test_a_live_page_alerts_the_agency_never_the_customer(self):
        service = Service.objects.create(account=self.account, name="Rörjour")
        page = LandingPage.objects.create(account=self.account, name="Live")
        Campaign.objects.create(
            account=self.account,
            service=service,
            name="Rörjour Nacka",
            status=Campaign.STATUS_LIVE,
            landing_page=page,
        )
        media.set_logo(self.logo())
        media.apply_logo_palette(self.account)
        # Två larm: logotypen (sidhuvudet på live-sidan) och paletten.
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("ny logotyp", mail.outbox[0].subject)
        self.assertIn("ny palett", mail.outbox[1].subject)
        for message in mail.outbox:
            self.assertEqual(message.to, ["larm@adx.example"])
            self.assertIn("Kunden har inte mejlats", message.body)

    def test_a_light_logo_color_keeps_the_brand_with_dark_text(self):
        """En ljus logotypfärg där vit text inte klarar AA men mörk text gör
        det står kvar på knapparna med mörk text; texten i färgen (länkar,
        överrubriken) mörkas för sig tills den klarar AA på vitt."""
        logo = self.logo(primary=(250, 220, 90), accent=(255, 240, 160))
        media.set_logo(logo)
        page = LandingPage.objects.create(account=self.account, name="Gul")
        media.apply_logo_palette(self.account, page=page)
        page.refresh_from_db()
        colors = render.palette_vars(page)
        primary = colors["--rn-primary"]
        self.assertEqual(primary, page.logo_colors["primary"])
        self.assertEqual(colors["--rn-on-primary"], render.INK)
        self.assertGreaterEqual(render.contrast(primary, render.INK), render.AA)
        self.assertGreaterEqual(
            render.contrast(colors["--rn-primary-ink"], render.WHITE), render.AA
        )
        soft = colors["--rn-primary-soft"]
        ink = colors["--rn-primary-ink"]
        self.assertGreaterEqual(render.contrast(ink, soft), render.AA)

    def test_without_a_logo_the_palette_is_refused_with_a_reason(self):
        LandingPage.objects.create(account=self.account, name="Ett")
        response = self.post({"action": "palette", "page": "alla"})
        shown = [str(m) for m in self.client.get(response.url).context["messages"]]
        self.assertTrue(any("logotyp" in m for m in shown), shown)


# ---------------------------------------------------------------------------
# Bilderna från hemsidan: adresserna ur sidorna
# ---------------------------------------------------------------------------

HOME = """<!doctype html><html><head>
<meta property="og:image" content="/bilder/delning.jpg">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" href="/favicon.ico">
</head><body>
<header><a href="/"><img src="/img/huvud.png" class="site-brand" alt="Lindqvist Rör"></a></header>
<main>
<img src="/bilder/badrum-liten.jpg"
  srcset="/bilder/badrum-480.jpg 480w, /bilder/badrum-1600.jpg 1600w,
  /bilder/badrum-960.jpg 960w" alt="Nytt badrum">
<img data-src="/bilder/lat.jpg" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="Lat laddning">
<picture><source srcset="/bilder/kok.webp 1x, /bilder/kok@2x.webp 2x" type="image/webp">
<img src="/bilder/kok.jpg" alt="Kök"></picture>
<img src="/bilder/ikon.svg" alt="ikon">
<img src="/pixel.gif" width="1" height="1">
<img src="https://cdn.example/bilder/team.jpg" alt="Vi på Lindqvist">
<img src="/uploads/logo-lindqvist.png" alt="">
<img src="/bilder/badrum-liten.jpg" alt="Samma igen">
<noscript><img src="/bilder/gomd.jpg"></noscript>
</main></body></html>"""


class CandidateParsingTests(TestCase):
    def test_candidates_from_the_pages_logos_first_and_each_once(self):
        page = scan.parse_page(HOME, "https://lindqvistror.se/")
        refs = media.site_candidates([page])
        urls = [r["url"] for r in refs]
        self.assertEqual(
            urls[:3],
            [
                "https://lindqvistror.se/img/huvud.png",
                "https://lindqvistror.se/uploads/logo-lindqvist.png",
                "https://lindqvistror.se/apple-touch-icon.png",
            ],
        )
        self.assertEqual(urls[3], "https://lindqvistror.se/bilder/delning.jpg")
        self.assertIn("https://lindqvistror.se/bilder/badrum-1600.jpg", urls)  # största i srcset
        self.assertIn("https://lindqvistror.se/bilder/lat.jpg", urls)
        self.assertIn("https://lindqvistror.se/bilder/kok@2x.webp", urls)
        self.assertIn("https://cdn.example/bilder/team.jpg", urls)
        for skipped in (
            "ikon.svg",
            "pixel.gif",
            "favicon.ico",
            "data:",
            "gomd.jpg",
            "badrum-480",
        ):
            self.assertFalse(any(skipped in u for u in urls), skipped)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertTrue(all(r["logo"] for r in refs[:3]))
        nytt = next(r for r in refs if "badrum-1600" in r["url"])
        self.assertEqual(nytt["alt"], "Nytt badrum")

    def test_at_most_24_candidates_and_no_new_page_fetches(self):
        many = "".join(f'<img src="/b/{n}.jpg">' for n in range(60))
        page = scan.parse_page(f"<html><body>{many}</body></html>", "https://lindqvistror.se/")
        with mock.patch.object(media, "fetch") as fetch:
            refs = media.site_candidates([page])
        self.assertEqual(len(refs), media.SITE_CANDIDATES)
        fetch.assert_not_called()

    def test_srcset_parsing(self):
        self.assertEqual(media._srcset_largest("a.jpg 1x,b.jpg 2x"), "b.jpg")
        self.assertEqual(media._srcset_largest("a.jpg 300w, b.jpg 100w"), "a.jpg")
        self.assertEqual(
            media._srcset_largest("https://x.example/w_100,h_100/a.jpg 100w, c.jpg 900w"), "c.jpg"
        )
        self.assertEqual(media._srcset_largest("ensam.jpg"), "ensam.jpg")


# ---------------------------------------------------------------------------
# Bilderna från hemsidan: läsningen sparar miniatyrer
# ---------------------------------------------------------------------------


class FakeSite:
    """En utbytt analyzer.fetch för lindqvistror.se: sidorna som HTML,
    bilderna som riktiga bilder (eller inte), och varje anrop noterat."""

    def __init__(self, images, html=HOME, delay=None):
        self.images = images
        self.html = html
        self.delay = delay or {}
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        parts = urlsplit(url)
        if parts.hostname not in ("lindqvistror.se", "cdn.example"):
            raise AssertionError(f"hämtade en främmande adress: {url}")
        if parts.path in self.delay:
            time.sleep(self.delay[parts.path])
        if parts.path in self.images:
            body = self.images[parts.path]
            if isinstance(body, Exception):
                raise body
            return site_response(url, body)
        if parts.path in ("", "/"):
            return Sida(url=url, status=200, html=self.html, headers={"content-type": "text/html"})
        return Sida(
            url=url,
            status=200,
            html="<html><body>Sida</body></html>",
            headers={"content-type": "text/html"},
        )


@override_settings(MEDIA_ROOT=_MEDIA)
class SiteScanTests(MediaFixture, TestCase):
    def scan(self, site, budget=scan.TIME_BUDGET):
        with (
            mock.patch.object(scan, "fetch", side_effect=site),
            mock.patch.object(scan.llm, "is_configured", return_value=False),
        ):
            result = scan.scan_website(self.account, "lindqvistror.se", budget=budget)
        self.account.refresh_from_db()
        return result

    def test_the_scan_keeps_thumbnails_with_the_source_and_size(self):
        big = image_bytes("JPEG", (1600, 1000))
        site = FakeSite(
            {
                "/img/huvud.png": image_bytes("PNG", (180, 60)),  # trolig logotyp, får vara liten
                "/uploads/logo-lindqvist.png": image_bytes("PNG", (150, 150)),
                "/apple-touch-icon.png": image_bytes("PNG", (180, 180)),
                "/bilder/delning.jpg": big,
                "/bilder/badrum-1600.jpg": image_bytes("JPEG", (1600, 1200), (200, 180, 160)),
                "/bilder/lat.jpg": image_bytes("JPEG", (120, 90)),  # för liten
                "/bilder/kok@2x.webp": big,  # samma bild som delning.jpg
                "/bilder/team.jpg": AnalysError("Kunde inte hämta"),
            }
        )
        result = self.scan(site)
        self.assertTrue(result.ok)
        found = {
            urlsplit(c.source_url).path: c
            for c in SiteImageCandidate.objects.filter(account=self.account)
        }
        self.assertEqual(
            set(found),
            {
                "/img/huvud.png",
                "/uploads/logo-lindqvist.png",
                "/apple-touch-icon.png",
                "/bilder/delning.jpg",
                "/bilder/badrum-1600.jpg",
            },
        )
        self.assertEqual(result.images, 5)
        self.assertTrue(found["/img/huvud.png"].likely_logo)
        self.assertTrue(found["/uploads/logo-lindqvist.png"].likely_logo)
        self.assertFalse(found["/bilder/badrum-1600.jpg"].likely_logo)
        self.assertEqual(found["/bilder/badrum-1600.jpg"].alt, "Nytt badrum")
        self.assertEqual(
            (found["/bilder/badrum-1600.jpg"].width, found["/bilder/badrum-1600.jpg"].height),
            (1600, 1200),
        )
        thumb = opened(found["/bilder/delning.jpg"].thumb)
        self.assertEqual((thumb.format, max(thumb.size)), ("WEBP", MEDIA_THUMB_SIDE))
        self.assertFalse(MediaAsset.objects.filter(account=self.account).exists())
        # Varje bild gick genom samma (utbytta) analyzer.fetch med taket 3 MB.
        image_calls = [kw for url, kw in site.calls if "/bilder/" in url or "logo" in url]
        self.assertTrue(image_calls)
        for kwargs in image_calls:
            self.assertEqual(kwargs["max_bytes"], media.SITE_THUMB_MAX_BYTES)
            self.assertLessEqual(kwargs["time_limit"], media.SITE_IMAGE_BUDGET)
        html = self.client.get(reverse("flamingo:app_media") + "?visa=hemsidan").content.decode()
        self.assertIn("/bilder/badrum-1600.jpg", html)
        self.assertIn("Trolig logotyp", html)
        self.assertIn("1600 x 1200 px", html)
        self.assertIn("Vi äger bilderna eller har rätt att använda dem.", html)

    def test_a_second_scan_does_not_fetch_known_images_again(self):
        site = FakeSite({"/bilder/delning.jpg": image_bytes("JPEG", (900, 600))})
        self.scan(site)
        self.assertEqual(SiteImageCandidate.objects.filter(account=self.account).count(), 1)
        site.calls.clear()
        self.scan(site)
        self.assertFalse(any("delning.jpg" in url for url, _ in site.calls))
        self.assertEqual(SiteImageCandidate.objects.filter(account=self.account).count(), 1)

    def test_slow_images_never_make_the_scan_slower_than_its_budget(self):
        delays = {f"/b/{n}.jpg": 3.0 for n in range(10)}
        images = {path: image_bytes("JPEG", (800, 600)) for path in delays}
        html = "<html><body>" + "".join(f'<img src="{p}">' for p in delays) + "</body></html>"
        site = FakeSite(images, html=html, delay=delays)
        started = time.monotonic()
        with mock.patch.object(media, "SITE_IMAGE_BUDGET", 0.5):
            result = self.scan(site, budget=2.0)
        elapsed = time.monotonic() - started
        self.assertTrue(result.ok)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(result.images, 0)  # inget hann: resten hoppades över

    def test_the_demo_never_fetches_images(self):
        demo = FlamingoAccount(customer=self.customer, is_demo=True)
        page = scan.parse_page(HOME, "https://lindqvistror.se/")
        fetcher = mock.Mock()
        job = media.SiteImageFetch.start(
            demo, [page], deadline=time.monotonic() + 5, fetcher=fetcher
        )
        self.assertEqual(job.futures, {})
        fetcher.assert_not_called()


class SsrfTests(TestCase):
    """Bilderna hämtas med SSRF-skyddet i analyzer.fetch: en adress i det
    interna nätet stoppas innan någon anslutning görs."""

    def test_internal_addresses_are_refused_by_the_real_fetch(self):
        deadline = time.monotonic() + 5
        with mock.patch("socket.create_connection", side_effect=AssertionError("ansluten")):
            for url in (
                "http://127.0.0.1/bild.png",
                "http://169.254.169.254/latest/meta-data/bild.png",
                "http://10.0.0.5/bild.jpg",
                "http://[::1]/bild.jpg",
            ):
                with self.subTest(url=url):
                    self.assertIsNone(media._fetch_thumb({"url": url}, deadline))

    def test_a_huge_image_behind_a_small_file_is_not_decoded_for_a_thumbnail(self):
        big = io.BytesIO()
        Image.new("1", (5000, 4000)).save(big, "PNG")  # 20 miljoner punkter, liten fil
        self.assertLess(len(big.getvalue()), media.SITE_THUMB_MAX_BYTES)
        fetcher = mock.Mock(return_value=site_response("https://x.example/a.png", big.getvalue()))
        ref = {"url": "https://x.example/a.png"}
        self.assertIsNone(media._fetch_thumb(ref, time.monotonic() + 5, fetcher))

    def test_the_guarded_fetch_is_what_is_called(self):
        with mock.patch.object(media, "fetch", side_effect=AnalysError("nej")) as fetch:
            self.assertIsNone(
                media._fetch_thumb({"url": "https://x.example/a.jpg"}, time.monotonic() + 5)
            )
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args, ("https://x.example/a.jpg",))
        self.assertEqual(fetch.call_args.kwargs["max_bytes"], media.SITE_THUMB_MAX_BYTES)


# ---------------------------------------------------------------------------
# Bilderna från hemsidan: kundens val hämtas till arkivet
# ---------------------------------------------------------------------------


@override_settings(MEDIA_ROOT=_MEDIA)
class ImportTests(MediaFixture, TestCase):
    def candidate(self, path, account=None):
        return SiteImageCandidate.objects.create(
            account=account or self.account,
            source_url=f"https://lindqvistror.se{path}",
            width=1600,
            height=1200,
            alt="Från sidan",
        )

    def importing(self, data, images=None):
        images = images or {}

        def fake(url, **kwargs):
            body = images.get(urlsplit(url).path, image_bytes("JPEG", (3000, 2000)))
            return site_response(url, body)

        with mock.patch.object(media, "fetch", side_effect=fake) as fetch:
            response = self.post({"action": "import", **data})
        return response, fetch

    def test_the_rights_confirmation_is_required(self):
        first = self.candidate("/a.jpg")
        response, fetch = self.importing({"candidate": [first.pk]})
        fetch.assert_not_called()
        self.assertFalse(MediaAsset.objects.filter(account=self.account).exists())
        shown = [str(m) for m in self.client.get(response.url).context["messages"]]
        self.assertTrue(any("rätt att använda" in m for m in shown), shown)

    def test_import_downloads_the_original_through_the_guarded_fetch(self):
        first, second = self.candidate("/a.jpg"), self.candidate("/b.jpg")
        response, fetch = self.importing({"candidate": [first.pk, second.pk], "rights": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(fetch.call_count, 2)
        for call in fetch.call_args_list:
            self.assertEqual(call.kwargs["max_bytes"], media.MAX_UPLOAD_BYTES)
        assets = list(MediaAsset.objects.filter(account=self.account).order_by("pk"))
        self.assertEqual(len(assets), 2)
        for asset in assets:
            self.assertEqual(asset.source, MediaAsset.SOURCE_SITE)
            self.assertTrue(asset.source_url.startswith("https://lindqvistror.se/"))
            self.assertIsNotNone(asset.rights_confirmed_at)
            self.assertEqual(asset.rights_confirmed_by, self.anna)
            self.assertEqual(asset.alt, "Från sidan")
            self.assertEqual(max(asset.width, asset.height), MEDIA_MAX_SIDE)
            self.assertEqual(opened(asset.file).format, "WEBP")
        first.refresh_from_db()
        self.assertIn(first.imported_asset, assets)
        # En hämtad kandidat kan inte hämtas igen.
        _, fetch = self.importing({"candidate": [first.pk], "rights": "1"})
        fetch.assert_not_called()

    def test_another_accounts_candidates_are_never_fetched(self):
        theirs = self.candidate("/hemlig.jpg", account=self.other_account)
        _, fetch = self.importing({"candidate": [theirs.pk], "rights": "1"})
        fetch.assert_not_called()
        theirs.refresh_from_db()
        self.assertIsNone(theirs.imported_asset)

    def test_a_bad_file_on_the_site_is_reported_and_not_counted(self):
        good, bad = self.candidate("/bra.jpg"), self.candidate("/fel.jpg")
        self.importing(
            {"candidate": [good.pk, bad.pk], "rights": "1"},
            images={"/fel.jpg": b"<html>inte en bild</html>"},
        )
        self.assertEqual(MediaAsset.objects.filter(account=self.account).count(), 1)
        self.account.refresh_from_db()
        self.assertEqual(self.account.daily_usage[media.USAGE_SITE_IMPORT], 1)

    def test_imports_are_limited_per_account_and_day(self):
        candidates = [self.candidate(f"/{n}.jpg") for n in range(3)]
        with mock.patch.object(media, "SITE_IMPORT_DAILY_MAX", 2):
            _, fetch = self.importing({"candidate": [c.pk for c in candidates], "rights": "1"})
            fetch.assert_not_called()
            self.importing({"candidate": [candidates[0].pk, candidates[1].pk], "rights": "1"})
            _, fetch = self.importing({"candidate": [candidates[2].pk], "rights": "1"})
            fetch.assert_not_called()
        self.assertEqual(MediaAsset.objects.filter(account=self.account).count(), 2)

    def test_the_demo_never_downloads(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        first = self.candidate("/a.jpg")
        _, fetch = self.importing({"candidate": [first.pk], "rights": "1"})
        fetch.assert_not_called()
        self.assertFalse(MediaAsset.objects.filter(account=self.account).exists())


class LogoPaletteRenderTests(TestCase):
    """Renderaren läser logo_colors["primary"] för paletten "logo"."""

    def test_palette_logo_uses_the_stored_primary(self):
        page = LandingPage(
            palette=LandingPage.PALETTE_LOGO,
            logo_colors={"primary": "#0B6E4F", "colors": ["#0B6E4F"]},
        )
        self.assertEqual(render.palette_vars(page)["--rn-primary"], "#0B6E4F")
        page.logo_colors = {}
        self.assertEqual(
            render.palette_vars(page)["--rn-primary"], render.PALETTES[LandingPage.PALETTE_BLUE]
        )
