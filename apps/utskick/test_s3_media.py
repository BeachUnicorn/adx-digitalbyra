"""
Mejlens bilder och mediaarkivet (README F.4, C.2, E.7, J S3 test_s3_media):

    RenditionTests      JPEG för en vanlig bild, PNG på vitt för en med alfa, högst
                        1120 bred, loggan 80 hög, porträttet kvadratiskt, videons
                        spelknapp, samma rad igen, absoluta adresser
    DeleteTests         ett utkast hindrar borttagningen, ett skickat gör det inte,
                        renditionen överlever (asset null, filen kvar)
    PurgeTests          retentionen: öppna utskick och skickade med mottagare kvar
                        behåller bilderna, annars tas raden och filen bort
    PageBuilderTests    sidornas media_ids_in och MediaInUse som förut
"""

import io
from datetime import timedelta
from pathlib import Path

from django.test import TestCase
from django.utils import timezone
from PIL import Image

from apps.flamingo import media, pagebuilder
from apps.flamingo.models import LandingPage, MediaAsset

from .email import blocks, images, render
from .models import CHANNEL_EMAIL, EmailImage, Recipient, Utskick
from .test_s3_render import BrevFixture, blk, make_asset


def opened(image):
    with image.file.open("rb") as handle:
        return Image.open(io.BytesIO(handle.read()))


class RenditionTests(BrevFixture, TestCase):
    def test_a_photo_becomes_a_jpeg_at_most_1120_wide(self):
        big = make_asset(self.account, size=(2400, 1200))
        row = images.rendition(big, images.CONTENT)
        self.assertEqual(row.format, EmailImage.Format.JPEG)
        self.assertEqual((row.width, row.height), (1120, 560))
        picture = opened(row)
        self.assertEqual(picture.format, "JPEG")
        self.assertEqual(picture.size, (1120, 560))
        self.assertEqual(row.bytes, row.file.size)
        self.assertTrue(row.file.name.startswith("utskick-img/"))
        self.assertTrue(row.file.name.endswith(".jpg"))
        # Aldrig uppskalad.
        small = make_asset(self.account, size=(400, 300))
        self.assertEqual(images.rendition(small, images.CONTENT).width, 400)

    def test_the_same_row_again(self):
        first = images.rendition(self.photo, images.CONTENT)
        again = images.rendition(self.photo, images.CONTENT)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(EmailImage.objects.filter(asset=self.photo).count(), 1)

    def test_a_logo_is_a_png_on_white_80_high(self):
        row = images.logo_for(self.account)
        self.assertEqual(row.asset_id, self.logo.pk)
        self.assertEqual(row.purpose, images.LOGO)
        self.assertEqual(row.format, EmailImage.Format.PNG)
        self.assertEqual(row.height, 80)
        picture = opened(row)
        self.assertEqual(picture.format, "PNG")
        self.assertEqual(picture.mode, "RGB")
        # Hörnet var genomskinligt: nu vitt (mörkt läge, F.4).
        self.assertEqual(picture.getpixel((0, 0)), (255, 255, 255))
        self.assertIsNone(images.logo_for(self.other_account))

    def test_a_transparent_photo_is_a_png_on_white(self):
        alpha = make_asset(self.account, size=(600, 400), mode="RGBA")
        row = images.rendition(alpha, images.CONTENT)
        self.assertEqual(row.format, EmailImage.Format.PNG)
        self.assertEqual(opened(row).getpixel((1, 1)), (255, 255, 255))

    def test_an_avatar_is_square(self):
        row = images.rendition(self.photo, images.AVATAR)
        self.assertEqual((row.width, row.height), (images.AVATAR_SIDE, images.AVATAR_SIDE))

    def test_a_video_gets_a_play_button(self):
        row = images.rendition(self.other, images.VIDEO)
        picture = opened(row)
        radius = round(min(picture.size) * 0.11)
        # Inne i den vita cirkeln, till vänster om den mörka triangeln.
        center = picture.getpixel((picture.width // 2 - round(radius * 0.7), picture.height // 2))
        self.assertTrue(all(c > 230 for c in center), center)
        self.assertEqual(row.format, EmailImage.Format.JPEG)

    def test_the_absolute_address(self):
        row = images.rendition(self.photo, images.CONTENT)
        self.assertEqual(images.absolute_url(row), "https://adx.se" + row.file.url)

    def test_a_broken_file_is_a_media_error(self):
        broken = make_asset(self.account)
        Path(broken.file.path).write_bytes(b"inte en bild")
        with self.assertRaises(media.MediaError):
            images.rendition(broken, images.CONTENT)

    def test_the_snapshot_points_at_renditions(self):
        snap = render.snapshot(self.utskick)
        self.assertTrue(snap["image_ids"])
        for key, info in snap["images"].items():
            with self.subTest(key=key):
                self.assertTrue(info["url"].startswith("https://adx.se/media/utskick-img/"))
                self.assertIn(info["id"], snap["image_ids"])
        self.assertTrue(snap["logo"]["url"].endswith(".png"))
        self.assertEqual(snap["logo"]["height"], 40)


class DeleteTests(BrevFixture, TestCase):
    def test_a_draft_blocks_the_delete(self):
        with self.assertRaises(media.MediaInUse) as caught:
            media.delete_asset(self.photo)
        self.assertEqual(caught.exception.utskick, ["Höstservice värmepump"])
        self.assertEqual(
            caught.exception.message,
            "Bilden används i utskicket Höstservice värmepump. Byt bilden där först, sedan "
            "går den att ta bort.",
        )
        self.assertTrue(MediaAsset.objects.filter(pk=self.photo.pk).exists())

    def test_every_open_state_blocks_and_an_old_version_too(self):
        for status in ("scheduled", "freezing", "sending", "paused", "paused_cap"):
            Utskick.objects.filter(pk=self.utskick.pk).update(status=status)
            with self.subTest(status=status), self.assertRaises(media.MediaInUse):
                media.delete_asset(self.other)
        Utskick.objects.filter(pk=self.utskick.pk).update(status="draft")
        # Bara i en äldre version (den kan väljas igen): fortfarande nej.
        lone = make_asset(self.account)
        block = blk("image", image=self.photo.pk)
        old = dict(block["versions"][0], id="v_" + "a" * 12, fields={"image": lone.pk})
        block["versions"].insert(0, old)
        blocks.save(self.utskick, [block], rev=self.utskick.email_rev, user=self.anna)
        with self.assertRaises(media.MediaInUse):
            media.delete_asset(lone)

    def test_a_page_and_an_utskick_are_both_named(self):
        hero = pagebuilder.new_block("hero", "image", self.account)
        hero["versions"][0]["fields"]["image"] = self.photo.pk
        LandingPage.objects.create(account=self.account, name="Badrum", draft={"blocks": [hero]})
        with self.assertRaises(media.MediaInUse) as caught:
            media.delete_asset(self.photo)
        self.assertEqual(
            caught.exception.message,
            "Bilden används på sidan Badrum och i utskicket Höstservice värmepump. Byt bilden "
            "där först, sedan går den att ta bort.",
        )

    def test_allowed_after_send_and_the_rendition_survives(self):
        snap = render.snapshot(self.utskick)
        Utskick.objects.filter(pk=self.utskick.pk).update(status="sent", email_snapshot=snap)
        row = EmailImage.objects.get(asset=self.photo, purpose=images.CONTENT)
        path = Path(row.file.path)
        self.assertTrue(path.is_file())
        with self.captureOnCommitCallbacks(execute=True):
            media.delete_asset(self.photo)
        self.assertFalse(MediaAsset.objects.filter(pk=self.photo.pk).exists())
        row.refresh_from_db()
        self.assertIsNone(row.asset_id)
        self.assertTrue(path.is_file())
        # Det skickade mejlet ritas fortfarande med bilden.
        self.utskick.refresh_from_db()
        self.assertIn(row.file.url, render.web_view(self.utskick))

    def test_another_accounts_utskick_never_counts(self):
        mine = make_asset(self.other_account)
        self.assertEqual(images.uses(self.other_account.pk), set())
        with self.captureOnCommitCallbacks(execute=True):
            media.delete_asset(mine)
        self.assertEqual(images.uses(self.account.pk), {self.photo.pk, self.other.pk})


class PurgeTests(BrevFixture, TestCase):
    def age(self, days=8):
        EmailImage.objects.update(created_at=timezone.now() - timedelta(days=days))

    def test_open_utskick_keep_their_images(self):
        render.snapshot(self.utskick)
        self.age()
        with self.captureOnCommitCallbacks(execute=True):
            result = images.purge_unused()
        kept = set(EmailImage.objects.values_list("asset_id", "purpose"))
        self.assertIn((self.photo.pk, images.CONTENT), kept)
        self.assertIn((self.logo.pk, images.LOGO), kept)
        self.assertEqual(result["deleted"], 0)

    def test_a_sent_mail_keeps_them_while_recipients_remain(self):
        snap = render.snapshot(self.utskick)
        Utskick.objects.filter(pk=self.utskick.pk).update(status="sent", email_snapshot=snap)
        recipient = Recipient.objects.create(
            utskick=self.utskick, channel=CHANNEL_EMAIL, address="a@kund.example"
        )
        rows = list(EmailImage.objects.filter(pk__in=snap["image_ids"]))
        files = [Path(r.file.path) for r in rows]
        with self.captureOnCommitCallbacks(execute=True):
            media.delete_asset(self.photo)
        MediaAsset.objects.filter(account=self.account).update(is_logo=False)
        self.age()
        with self.captureOnCommitCallbacks(execute=True):
            images.purge_unused()
        self.assertEqual(EmailImage.objects.filter(pk__in=snap["image_ids"]).count(), len(rows))
        recipient.delete()
        with self.captureOnCommitCallbacks(execute=True):
            result = images.purge_unused()
        self.assertEqual(result["deleted"], len(rows))
        self.assertFalse(EmailImage.objects.filter(pk__in=snap["image_ids"]).exists())
        self.assertFalse(any(f.is_file() for f in files))

    def test_young_rows_wait(self):
        images.rendition(make_asset(self.account), images.CONTENT)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(images.purge_unused()["deleted"], 0)
        self.age()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(images.purge_unused()["deleted"], 1)


class PageBuilderTests(BrevFixture, TestCase):
    def test_media_ids_in_for_pages_is_unchanged(self):
        page = pagebuilder.new_block("before_after", None, self.account)
        page["versions"][0]["fields"]["before"] = self.photo.pk
        self.assertEqual(media.media_ids_in([page]), {self.photo.pk})
        # Ett Brev-block är okänt för sidorna och tvärtom.
        email = blk("image", image=self.other.pk)
        self.assertEqual(media.media_ids_in([email]), set())
        from .email.registry import EMAIL_TYPES

        self.assertEqual(media.media_ids_in([email], types=EMAIL_TYPES), {self.other.pk})
        self.assertEqual(media.media_ids_in([page], types=EMAIL_TYPES), set())

    def test_media_in_use_for_a_page_reads_as_before(self):
        exc = media.MediaInUse([media.Use(page=LandingPage(name="Badrum"), where="utkastet")])
        self.assertEqual(
            exc.message,
            "Bilden används på sidan Badrum. Byt bilden där först, sedan går den att ta bort.",
        )
        self.assertEqual(exc.utskick, [])
