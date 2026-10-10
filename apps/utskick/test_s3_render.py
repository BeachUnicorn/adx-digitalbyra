"""
Brev som HTML och text (README F.4, F.3, H.5, J S3 test_s3_render):

    BrevFixture         Exempelrör med uppgifter, bilder, en logga, omdömen och ett
                        utskick med alla 24 element (används av test_s3_media och
                        test_s3_registry också)
    TokenTests          .t-brev ur mockups/flamingo-epost-brev.html mot style.TOKENS,
                        geometrin 560/28/20/30 och typografin
    StructureTests      doctype, ljust läge, förhandstexten, mso-ramen, 560 px, bara
                        inline-stilar, absoluta adresser, bilder med mått och alt,
                        alla 22 block, sidhuvudets loggval och sidfoten
    LinkTests           frysningens platser och renderingens länkar stämmer;
                        per mottagare genom klick.adx.se/m/, aldrig mailto:, tel: och
                        våra egna; testmejlet
    PixelTests          pixeln bara med open_tracking och tracking_ok
    MergeTests          reservtexterna, escapat i HTML och rått i texten, en rad i
                        ämnesraden, ett värde blir aldrig en länk
    TextVersionTests    textversionen ur blocken, kundens egen, sidfoten
    SizeTests           102 kB med de längsta värdena
    AccentTests         ljus och mörk färg
    ModeTests           redigeraren (pb-attributen, CSP), webbversionen, kalendern
    MimeTests           List-Unsubscribe och Post kodas aldrig, text före HTML
    TemplateGuardTests  mallarna: inga skript, inga externa typsnitt, inga relativa
                        adresser, ingen layout som bara hänger på class
"""

import io
import re
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from apps.flamingo import media
from apps.flamingo.models import Fact, FlamingoAccount, MediaAsset
from apps.flamingo.pagebuilder import blocks as pb

from . import keys, links
from .email import blocks, checks, mime, registry, render, style, text
from .email.transport import OutgoingMail
from .models import CHANNEL_EMAIL, Recipient, TrackedLink, Utskick
from .test_s3_foundation import LINK_SETTINGS, make_utskick
from .testing import UtskickFixture, make_contact

User = get_user_model()
BASE = Path(settings.BASE_DIR)
MOCKUP = BASE.parent / "mockups" / "flamingo-epost-brev.html"
TEMPLATES = BASE / "templates" / "utskick" / "brev"

_MEDIA = tempfile.mkdtemp(prefix="utskick-brev-")
BREV_SETTINGS = {
    **LINK_SETTINGS,
    "MEDIA_ROOT": _MEDIA,
    "FLAMINGO_LANDING_BASE_URL": "https://adx.se",
    "SITE_BASE_URL": "https://adx.se",
}


def image_bytes(size=(800, 600), color=(30, 90, 200), mode="RGB", fmt="PNG"):
    buffer = io.BytesIO()
    fill = (*color, 0) if mode == "RGBA" else color
    image = Image.new(mode, size, fill)
    if mode == "RGBA":
        # En logga: genomskinlig bakgrund med en mörk ruta i mitten.
        for x in range(size[0] // 4, size[0] * 3 // 4):
            for y in range(size[1] // 4, size[1] * 3 // 4):
                image.putpixel((x, y), (29, 43, 79, 255))
    image.save(buffer, fmt)
    return buffer.getvalue()


def make_asset(account, *, size=(800, 600), color=(30, 90, 200), mode="RGB", alt="", **fields):
    processed = media.process_image(image_bytes(size=size, color=color, mode=mode))
    asset = media.store_image(account, processed, alt=alt)
    if fields:
        MediaAsset.objects.filter(pk=asset.pk).update(**fields)
        asset.refresh_from_db()
    return asset


def blk(type_key, **fields):
    """Ett block som redigeraren skickar det (kundens version, osignerad)."""
    version_id = pb.new_version_id()
    return {
        "id": pb.new_block_id(),
        "type": type_key,
        "variant": registry.VARIANT,
        "active": version_id,
        "versions": [
            {
                "id": version_id,
                "fields": fields,
                "source": "customer",
                "by": None,
                "at": timezone.now().isoformat(),
            }
        ],
    }


def all_blocks(photo, other):
    """Alla 22 block i bibliotekets ordning, med innehållet ur mockupen."""
    site = "https://exempelror.example"
    return [
        blk(
            "hero",
            image=photo.pk,
            kicker="Höstservice",
            title="Hej {förnamn|du}, dags för service av värmepumpen",
            lead="En timme hemma hos dig, så går den tyst och snålt hela vintern.",
            button_text="Boka service",
            button_url=f"{site}/boka?gclid=abc",
        ),
        blk("heading", text="Det här ingår i servicen", size="h2"),
        blk(
            "text",
            body=(
                "Servicen tar ungefär en timme. Du behöver **inte vara hemma** om vi kommer åt "
                f"utedelen, och vi skickar ett sms *när vi är klara*. Mer finns på [vår sida om "
                f"service]({site}/service).\n\n- Rengöring av filter\n- Kontroll av tryck\n"
                "- Genomgång av inställningarna"
            ),
        ),
        blk(
            "button",
            primary_text="Boka service",
            primary_url=f"{site}/boka",
            secondary_text="Se lediga tider",
            secondary_url=f"{site}/tider",
            align="left",
        ),
        blk("image", image=other.pk, caption="Johan på ett servicebesök i Bromma."),
        blk(
            "image_text",
            image=photo.pk,
            title="Byt filter i tid",
            body="Ett igensatt filter får pumpen att jobba hårdare.",
            link_text="Läs mer om filter",
            link_url=f"{site}/filter",
            side="right",
        ),
        blk(
            "columns",
            items=[
                {"number": "01", "title": "Lägre elräkning", "text": "Ren pump drar mindre."},
                {"number": "02", "title": "Håller längre", "text": "Små fel upptäcks i tid."},
                {"number": "03", "title": "Inget krångel", "text": "Ett sms när vi är klara."},
            ],
        ),
        blk("divider"),
        blk(
            "offer",
            valid_until="2026-10-31",
            title="Filterbytet ingår",
            text="när du bokar service och anger koden",
            code="varme26",
        ),
        blk(
            "prices",
            title="Priser",
            items=[
                {"name": "Service, luft-luft", "price": "1 495 kr"},
                {"name": "Filterbyte", "price": "395 kr"},
            ],
        ),
        blk("reviews", source="google", count="2", show_summary="yes"),
        blk(
            "steps",
            title="Så går det till",
            items=[
                {"text": "**Boka** via knappen eller ring oss."},
                {"text": "**Vi kommer** på avtalad tid."},
                {"text": "**Klart.** Du får ett sms med en kort rapport."},
            ],
        ),
        blk(
            "event",
            date="2026-10-23",
            start="15:00",
            end="18:00",
            title="Öppet hus i butiken",
            place="Mossvägen 12",
            calendar="yes",
        ),
        blk(
            "person",
            photo=photo.pk,
            name="Johan Lind",
            role="Servicetekniker",
            phone="08-123 456 78",
            email="johan@exempelror.example",
        ),
        blk(
            "video",
            thumbnail=other.pk,
            title="Så rengör du filtret själv",
            url="https://www.youtube.com/watch?v=abc123",
        ),
        blk(
            "gallery",
            items=[
                {"image": photo.pk, "alt": "Utedelen"},
                {"image": other.pk, "alt": "Innedelen"},
                {"image": photo.pk, "alt": "Filtret"},
            ],
        ),
        blk(
            "faq",
            title="Vanliga frågor",
            items=[{"q": "Måste jag vara hemma?", "a": "Nej, om vi kommer åt utedelen."}],
        ),
        blk("hours", show_hours="yes", show_address="yes", map_text="Hitta hit"),
        blk("callout", text="**PS.** Har du en luft-vatten-pump? Då kontrollerar vi den också."),
        blk(
            "signature",
            greeting="Vänliga hälsningar,",
            script_name="Johan",
            name="Johan Lind",
            line="Exempelrör AB",
            phone="08-123 456 78",
        ),
        blk("spacer", size="m"),
        blk(
            "social",
            items=[
                {"network": "facebook", "url": "https://www.facebook.com/exempelror"},
                {"network": "instagram", "url": "https://www.instagram.com/exempelror"},
            ],
        ),
    ]


class BrevFixture(UtskickFixture):
    @classmethod
    def setUpClass(cls):
        cls._brev_settings = override_settings(**BREV_SETTINGS)
        cls._brev_settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._brev_settings.disable()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key, label, value in (
            ("adress", "Adress", "Mossvägen 12, 167 33 Bromma"),
            ("telefon", "Telefon", "08-123 456 78"),
            ("oppettider", "Öppettider", "Mån till fre 07 till 16; Lör och sön stängt"),
            ("pris-service", "Service, luft-luft", "1 495 kr"),
        ):
            Fact.objects.create(
                account=cls.account, key=key, label=label, value=value, confirmed=True
            )
        FlamingoAccount.objects.filter(pk=cls.account.pk).update(
            google_rating="4.8",
            google_review_count=162,
            google_reviews=[
                {"id": "r1", "author": "Maria", "rating": 5, "text": "Kom samma vecka."},
                {"id": "r2", "author": "Erik", "rating": 5, "text": "Bra pris och trevligt."},
            ],
            google_reviews_selected=["r1", "r2"],
        )
        cls.account.refresh_from_db()

    def setUp(self):
        super().setUp()
        # Varje test får egna filer (MEDIA_ROOT töms inte mellan testerna).
        self.photo = make_asset(self.account, alt="Värmepumpen utanför huset")
        self.other = make_asset(self.account, color=(200, 120, 40), alt="Montören")
        self.logo = make_asset(self.account, size=(400, 120), mode="RGBA", alt="Exempelrör")
        media.set_logo(self.logo)
        self.utskick = make_utskick(
            self.account,
            subject="Dags för service, {förnamn|du}",
            preheader="Boka före den 31 oktober",
            created_by=self.anna,
        )
        blocks.save(self.utskick, all_blocks(self.photo, self.other), rev=0, user=self.anna)
        self.utskick.refresh_from_db()

    def contact(self, **data):
        self._contacts = getattr(self, "_contacts", 0) + 1
        data.setdefault("first_name", "Anna")
        default = (
            "anna.k@kund.example" if self._contacts == 1 else f"k{self._contacts}@kund.example"
        )
        data.setdefault("email", default)
        return make_contact(self.account, **data)

    def recipient(self, *, tracking_ok=False, **merge):
        contact = self.contact()
        values = {"förnamn": "Anna", **merge}
        return Recipient.objects.create(
            utskick=self.utskick,
            contact=contact,
            channel=CHANNEL_EMAIL,
            address=contact.email,
            merge=values,
            basis="consent",
            tracking_ok=tracking_ok,
        )

    def freeze(self):
        """Som frysningen (byggare C): underlaget och en TrackedLink per plats."""
        snap = render.snapshot(self.utskick)
        table = {}
        for spot in render.collect_links(self.utskick, data=snap):
            link = TrackedLink.objects.create(
                account=self.account,
                utskick=self.utskick,
                kind=TrackedLink.Kind.EXTERNAL,
                destination=spot.url,
                label=spot.label,
                block_id=spot.block_id,
                position=spot.position,
            )
            table[f"{spot.block_id}:{spot.position}"] = link.pk
        snap["links"] = table
        Utskick.objects.filter(pk=self.utskick.pk).update(email_snapshot=snap)
        self.utskick.refresh_from_db()
        return snap

    def send_html(self, recipient):
        ctx = render.context_for(
            self.utskick,
            mode=render.SEND,
            recipient=recipient,
            snapshot=self.utskick.email_snapshot,
        )
        return render.render_html(self.utskick, ctx), ctx


def hrefs(html):
    return re.findall(r'href="([^"]*)"', html)


def srcs(html):
    return re.findall(r'src="([^"]*)"', html)


# ---------------------------------------------------------------------------
# Mockupens tokens
# ---------------------------------------------------------------------------


def _mockup_block(name):
    text = MOCKUP.read_text(encoding="utf-8")
    match = re.search(r"\.t-brev\{(.*?)\}", text, flags=re.S)
    return text, dict(re.findall(r"--([\w-]+):([^;]+);", match.group(1)))


class TokenTests(TestCase):
    def setUp(self):
        if not MOCKUP.is_file():
            self.skipTest("mockups/flamingo-epost-brev.html finns inte här")
        self.text, self.vars = _mockup_block("t-brev")

    def test_the_colour_tokens_are_the_mockups(self):
        for name, value in style.TOKENS.items():
            with self.subTest(token=name):
                self.assertIn(name, self.vars)
                self.assertEqual(self.vars[name].strip().upper(), value.upper())

    def test_the_geometry_is_560_28_20_30(self):
        self.assertEqual(self.vars["width"].strip(), f"{style.WIDTH}px")
        self.assertEqual(self.vars["pad"].strip(), f"{style.PAD}px")
        self.assertRegex(self.text, r"\.t-brev \.blk\{padding:0 var\(--pad\) 30px\}")
        self.assertEqual(style.GAP, 30)
        self.assertIn(".t-brev .blk.hdr{padding-top:28px", self.text)
        self.assertEqual(style.HEADER_TOP, 28)
        mobile = re.search(r"@media \(max-width:(\d+)px\)\{(.*?)\n\}", self.text, flags=re.S)
        self.assertEqual(int(mobile.group(1)), style.MOBILE_BREAK)
        self.assertIn(f"padding-left:{style.PAD_MOBILE}px", mobile.group(2))
        self.assertIn(f"padding-left:{style.PAD_MOBILE}px", style.mobile_css())
        S = style.brev_styles(style.palette_for_accent(style.DEFAULT_ACCENT))
        self.assertIn("padding:0 28px 30px", S["blk"])
        self.assertIn("padding:28px 28px 30px", S["hdr"])
        self.assertIn("max-width:560px", S["main"])

    def test_the_typography_is_the_mockups(self):
        S = style.brev_styles(style.palette_for_accent(style.DEFAULT_ACCENT))
        rules = {
            "h1": ("font-size:28px", "font-weight:700"),
            "h2": ("font-size:24px", "font-weight:700", "line-height:1.2"),
            "h3": ("font-size:17px", "font-weight:700", "line-height:1.3"),
            "lead": ("font-size:18px", "line-height:1.6"),
            "p": ("font-size:16px", "line-height:1.65"),
            "btn_a": (
                "font-size:16px",
                "font-weight:600",
                "padding:15px 24px",
                "border-radius:6px",
            ),
            "img": ("border-radius:6px",),
            "stars": ("color:#E8A400",),
        }
        for key, parts in rules.items():
            for part in parts:
                with self.subTest(style=key, part=part):
                    self.assertIn(part, S[key])
        # Mockupens egna regler säger samma sak.
        self.assertIn(".h2{font:var(--h1w) 24px/1.2", self.text)
        self.assertIn(".h3{font:700 17px/1.3", self.text)
        self.assertIn(".p{font:400 16px/1.65", self.text)
        self.assertIn(".lead{font:400 18px/1.6", self.text)
        self.assertIn("font:600 16px/1 var(--body);padding:15px 24px", self.text)
        self.assertEqual(self.vars["h1"].strip(), "28px")
        self.assertEqual(self.vars["h1w"].strip(), "700")

    def test_the_font_stack_is_the_mockups_without_web_fonts(self):
        stack = self.vars["body"].replace('"', "'").replace(" ", "")
        self.assertEqual(stack, style.FONT.replace(" ", ""))
        self.assertEqual(self.vars["head"], self.vars["body"])

    def test_the_swatches_are_the_mockups(self):
        for _name, value in style.SWATCHES:
            with self.subTest(value=value):
                self.assertIn(f'data-c="{value}"', self.text)


# ---------------------------------------------------------------------------
# Strukturen
# ---------------------------------------------------------------------------


class StructureTests(BrevFixture, TestCase):
    def preview(self, **kwargs):
        ctx = render.context_for(self.utskick, mode=render.PREVIEW, **kwargs)
        return render.render_html(self.utskick, ctx)

    def test_the_document(self):
        html = self.preview()
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn('<meta name="color-scheme" content="light">', html)
        self.assertIn('<meta name="supported-color-schemes" content="light">', html)
        self.assertIn("Boka före den 31 oktober", html)
        self.assertRegex(html, r'<div style="display:none;[^"]*">Boka före den 31 oktober')
        self.assertIn('<!--[if mso]><table role="presentation" width="560"', html)
        self.assertRegex(html, r'<table role="presentation" class="br-main" width="560"')
        self.assertIn("max-width:560px", html)
        self.assertIn("@media (max-width:620px)", html)
        self.assertNotIn("<script", html.lower())
        self.assertNotIn("fonts.googleapis", html)
        self.assertNotIn("@import", html)
        self.assertNotIn("<link", html)

    def test_all_22_blocks_are_drawn(self):
        html = self.preview()
        for needle in (
            "dags för service av värmepumpen",
            "Det här ingår i servicen",
            "<strong>inte vara hemma</strong>",
            "<em>när vi är klara</em>",
            "Se lediga tider",
            "Johan på ett servicebesök i Bromma.",
            "Byt filter i tid",
            "Lägre elräkning",
            "border-top:1px solid #E3E6EB;font-size:0",
            "VARME26",
            "Till 31 oktober",
            "1 495 kr",
            "4,8 av 5",
            "162 omdömen på Google",
            "Kom samma vecka.",
            "<strong>Boka</strong> via knappen",
            "Fredag 23 oktober kl. 15 till 18",
            "Lägg till i kalendern",
            "Servicetekniker",
            "Så rengör du filtret själv",
            'alt="Innedelen"',
            "Måste jag vara hemma?",
            "Mån till fre",
            "Hitta hit",
            "<strong>PS.</strong>",
            "Vänliga hälsningar,",
            "Facebook",
            "Instagram",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)
        self.assertEqual(html.count("data-pb-block"), 0)

    def test_the_mobile_columns_hold_without_the_media_query(self):
        html = self.preview()
        columns = re.findall(r'<div class="br-col"[^>]*style="([^"]*)"', html)
        self.assertGreaterEqual(len(columns), 5)
        for value in columns:
            with self.subTest(style=value):
                self.assertIn("display:inline-block", value)
                self.assertRegex(value, r"max-width:\d+px")
        # Utan <style> (Gmail-appen) är varje class bara en förfining: allt
        # som bär layouten står inline.
        for match in re.finditer(r'<(\w+)[^>]*class="([^"]+)"([^>]*)>', html):
            with self.subTest(tag=match.group(0)[:80]):
                self.assertIn("style=", match.group(0))

    def test_only_absolute_addresses(self):
        html = self.preview(contact=self.contact())
        for value in hrefs(html) + srcs(html):
            with self.subTest(value=value):
                self.assertRegex(value, r"^(https://|mailto:|tel:)")

    def test_every_image_has_size_alt_and_display_block(self):
        html = self.preview()
        images = re.findall(r"<img\b[^>]*>", html)
        self.assertGreaterEqual(len(images), 9)
        for tag in images:
            with self.subTest(tag=tag[:90]):
                self.assertRegex(tag, r'\swidth="\d+"')
                self.assertRegex(tag, r'\sheight="\d+"')
                self.assertRegex(tag, r'\salt="')
                self.assertIn("display:block", tag)
                src = re.search(r'src="([^"]+)"', tag).group(1)
                self.assertTrue(src.startswith("https://adx.se/media/utskick-img/"), src)
                self.assertRegex(src, r"\.(jpg|png)$")

    def test_every_cell_has_a_white_background(self):
        html = self.preview()
        for tag in re.findall(r'<td class="br-pad"[^>]*>', html):
            with self.subTest(tag=tag[:80]):
                self.assertIn('bgcolor="#FFFFFF"', tag)
                self.assertIn("background-color:#FFFFFF", tag)

    def test_the_logo_left_center_or_none(self):
        left = self.preview()
        self.assertRegex(left, r'text-align:right;"><a href="https://klick\.adx\.se/w/')
        self.assertIn('height="40"', left)
        Utskick.objects.filter(pk=self.utskick.pk).update(logo_position="center")
        self.utskick.refresh_from_db()
        center = self.preview()
        self.assertIn("text-align:center;", center)
        self.assertIn("margin:0 auto;", center)
        Utskick.objects.filter(pk=self.utskick.pk).update(logo_position="none")
        self.utskick.refresh_from_db()
        none = self.preview()
        self.assertNotIn('height="40"', none)
        self.assertRegex(none, r'text-align:left;"><a href="https://klick\.adx\.se/w/')

    def test_no_logo_uploaded_means_none(self):
        media.unset_logo(self.logo)
        html = self.preview()
        self.assertNotIn('height="40"', html)
        state = registry.logo_state(self.account)
        self.assertEqual(state, (False, "Ladda upp en logotyp under Media."))

    def test_the_footer(self):
        html = self.preview(contact=self.contact())
        # En uppgift per rad och ingen rad om varför (Giovanni 2026-10-10).
        self.assertIn("Exempelrör AB</b><br>Mossvägen 12<br>167 33 Bromma<br>08-123 456 78", html)
        self.assertNotIn("Du får det här", html)
        footer = html[html.index("data-brev-element") if "data-brev-element" in html else 0 :]
        for label in ("Ändra vad du får", "Avregistrera dig", "Visa i webbläsaren"):
            self.assertIn(label, footer)
        value_hash = keys.value_hash("email", "anna.k@kund.example")
        self.assertIn(links.unsubscribe_url(self.account.pk, value_hash), html)
        self.assertIn(links.email_preferences_url(self.account.pk, value_hash), html)

    def test_no_reason_line_for_any_basis_or_information(self):
        """Sidfoten säger inte varför mottagaren får mejlet, oavsett grund
        eller information (Giovanni 2026-10-10: "ta bort det")."""
        for basis in ("consent", "existing_customer", "company"):
            ctx = render.context_for(
                self.utskick,
                mode=render.SEND,
                recipient=SimpleNamespace(
                    pk=None, merge={}, basis=basis, tracking_ok=False, address="a@b.example"
                ),
            )
            with self.subTest(basis=basis):
                html = render.render_html(self.utskick, ctx)
                self.assertNotIn("Du får det här", html)
                self.assertIn("Avregistrera dig", html)
        Utskick.objects.filter(pk=self.utskick.pk).update(purpose="information")
        self.utskick.refresh_from_db()
        self.assertNotIn("Det här är information om ditt ärende", self.preview())

    def test_the_text_version_has_one_detail_per_line(self):
        from .email import text

        body = text.render_text(self.utskick, render.context_for(self.utskick, mode=render.PREVIEW))
        self.assertIn("Exempelrör AB\nMossvägen 12\n167 33 Bromma\n08-123 456 78\n\n", body)
        self.assertNotIn("Du får det här", body)

    def test_the_privacy_link_names_the_company(self):
        from .models import UtskickSettings

        UtskickSettings.objects.filter(pk=self.settings.pk).update(
            privacy_url="https://exempelror.example/integritet"
        )
        html = self.preview()
        self.assertIn(
            '<a href="https://exempelror.example/integritet" style="color:#9AA0A6;'
            'text-decoration:underline;">Så hanterar Exempelrör dina uppgifter</a>',
            html,
        )

    def test_a_block_without_content_is_not_drawn(self):
        doc = [blk("video", title="Ingen bild", url="https://www.youtube.com/watch?v=x")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        html = self.preview()
        self.assertNotIn("Ingen bild", html)


# ---------------------------------------------------------------------------
# Länkarna
# ---------------------------------------------------------------------------


class LinkTests(BrevFixture, TestCase):
    def test_every_spot_is_frozen_and_used(self):
        snap = self.freeze()
        spots = render.collect_links(self.utskick, data=snap)
        self.assertEqual(len(spots), TrackedLink.objects.filter(utskick=self.utskick).count())
        urls = [spot.url for spot in spots]
        # Klick-id bort (F.1), adresserna rensade vid sparningen.
        self.assertIn("https://exempelror.example/boka", urls)
        self.assertNotIn("gclid", " ".join(urls))
        self.assertIn("https://www.youtube.com/watch?v=abc123", urls)
        self.assertTrue(any(u.startswith("https://www.google.com/maps/search/") for u in urls))
        recipient = self.recipient()
        html, _ctx = self.send_html(recipient)
        used = [h for h in hrefs(html) if "/m/" in h]
        self.assertEqual(len(set(used)), len(spots))
        for href in hrefs(html):
            with self.subTest(href=href):
                self.assertFalse(href.startswith("https://exempelror.example"), href)
                self.assertFalse(href.startswith("https://www.youtube.com"), href)
                self.assertRegex(
                    href,
                    r"^(https://klick\.adx\.se/(m|a|v|w|c)/|tel:|mailto:|https://adx\.se/utskick/)",
                )

    def test_the_click_token_carries_the_recipient_and_the_link(self):
        from . import tokens

        self.freeze()
        recipient = self.recipient()
        html, _ctx = self.send_html(recipient)
        token = re.search(r"https://klick\.adx\.se/m/([A-Za-z0-9.]+)", html).group(1)
        ref = tokens.read_email_click(token)
        self.assertEqual(ref.recipient_id, recipient.pk)
        link = TrackedLink.objects.get(pk=ref.link_id)
        self.assertEqual(link.utskick_id, self.utskick.pk)

    def test_mailto_tel_and_our_own_links_are_never_tracked(self):
        self.freeze()
        html, _ctx = self.send_html(self.recipient())
        self.assertIn('href="tel:+46812345678"', html)
        self.assertIn('href="mailto:johan@exempelror.example"', html)
        self.assertRegex(html, r'href="https://klick\.adx\.se/c/[^"]+\.ics"')
        self.assertRegex(html, r'href="https://klick\.adx\.se/a/[^"]+"')
        self.assertRegex(html, r'href="https://klick\.adx\.se/w/[^"]+"')

    def test_preview_links_go_straight_to_the_page(self):
        ctx = render.context_for(self.utskick, mode=render.PREVIEW)
        html = render.render_html(self.utskick, ctx)
        self.assertIn('href="https://exempelror.example/boka"', html)
        self.assertNotIn("/m/", html)

    def test_a_test_mail_with_links_uses_recipient_zero(self):
        from . import tokens

        snap = self.freeze()
        ctx = render.context_for(
            self.utskick,
            mode=render.SEND,
            contact=self.contact(),
            test=True,
            snapshot={"links": snap["links"]},
        )
        html = render.render_html(self.utskick, ctx)
        token = re.search(r"https://klick\.adx\.se/m/([A-Za-z0-9.]+)", html).group(1)
        self.assertIsNone(tokens.read_email_click(token).recipient_id)

    def test_a_test_mail_never_unsubscribes_the_previewed_contact(self):
        contact = self.contact()
        ctx = render.context_for(self.utskick, mode=render.PREVIEW, contact=contact, test=True)
        html = render.render_html(self.utskick, ctx)
        value_hash = keys.value_hash("email", contact.email)
        self.assertNotIn(links.unsubscribe_url(self.account.pk, value_hash), html)
        self.assertIn('href="https://klick.adx.se/" style="color:#9AA0A6;', html)
        self.assertIn("Hej Anna, dags", html)

    def test_the_text_version_uses_the_same_links(self):
        self.freeze()
        recipient = self.recipient()
        html, ctx = self.send_html(recipient)
        plain = text.render_text(self.utskick, ctx)
        tracked_html = set(re.findall(r"https://klick\.adx\.se/m/[A-Za-z0-9.]+", html))
        tracked_text = set(re.findall(r"https://klick\.adx\.se/m/[A-Za-z0-9.]+", plain))
        self.assertEqual(tracked_text, tracked_html)

    def test_blocks_urls_follow_the_render_order(self):
        doc = self.utskick.email_doc["blocks"]
        rows = blocks.urls(doc)
        hero = doc[0]["id"]
        self.assertEqual(rows[0], (hero, 0, "https://exempelror.example/boka", "Boka service"))


# ---------------------------------------------------------------------------
# Pixeln (H.5)
# ---------------------------------------------------------------------------


class PixelTests(BrevFixture, TestCase):
    def pixel_in(self, *, open_tracking, tracking_ok):
        Utskick.objects.filter(pk=self.utskick.pk).update(open_tracking=open_tracking)
        self.utskick.refresh_from_db()
        self.freeze()
        html, _ctx = self.send_html(self.recipient(tracking_ok=tracking_ok))
        return "/o/" in html

    def test_only_with_open_tracking_and_tracking_ok(self):
        self.assertTrue(self.pixel_in(open_tracking=True, tracking_ok=True))

    def test_not_without_tracking_ok(self):
        self.assertFalse(self.pixel_in(open_tracking=True, tracking_ok=False))

    def test_not_without_open_tracking(self):
        self.assertFalse(self.pixel_in(open_tracking=False, tracking_ok=True))

    def test_never_in_preview_or_web_view(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(open_tracking=True)
        self.utskick.refresh_from_db()
        self.freeze()
        recipient = self.recipient(tracking_ok=True)
        self.assertNotIn("/o/", render.web_view(self.utskick, recipient))
        ctx = render.context_for(self.utskick, mode=render.PREVIEW, contact=self.contact())
        self.assertNotIn("/o/", render.render_html(self.utskick, ctx))


# ---------------------------------------------------------------------------
# Sammanfogningen (F.3)
# ---------------------------------------------------------------------------


class MergeTests(BrevFixture, TestCase):
    def test_the_inline_fallback(self):
        recipient = self.recipient()
        recipient.merge = {}
        recipient.save()
        html, ctx = self.send_html(recipient)
        self.assertIn("Hej du, dags för service", html)
        self.assertEqual(render.subject_for(self.utskick, ctx), "Dags för service, du")

    def test_the_utskick_fallback(self):
        doc = [blk("heading", text="Hej {förnamn}")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        Utskick.objects.filter(pk=self.utskick.pk).update(merge_fallbacks={"förnamn": "kund"})
        self.utskick.refresh_from_db()
        ctx = render.context_for(self.utskick, mode=render.PREVIEW)
        self.assertIn("Hej kund", render.render_html(self.utskick, ctx))

    def test_values_are_escaped_in_html_and_raw_in_text(self):
        recipient = self.recipient(**{"förnamn": "<b>Anna</b> & Co"})
        html, ctx = self.send_html(recipient)
        self.assertIn("Hej &lt;b&gt;Anna&lt;/b&gt; &amp; Co, dags", html)
        self.assertNotIn("<b>Anna</b>", html)
        self.assertIn("Hej <b>Anna</b> & Co, dags", text.render_text(self.utskick, ctx))

    def test_a_value_never_becomes_a_link_or_bold(self):
        doc = [blk("text", body="Hej {förnamn}, välkommen.")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        recipient = self.recipient(**{"förnamn": "[klicka](https://evil.example) **x**"})
        ctx = render.context_for(self.utskick, mode=render.SEND, recipient=recipient)
        html = render.render_html(self.utskick, ctx)
        self.assertNotIn('href="https://evil.example"', html)
        self.assertIn("[klicka](https://evil.example) **x**", html)

    def test_subject_and_preheader_are_one_line_and_capped(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(
            subject="Hej {förnamn}", preheader="Till {företag}"
        )
        self.utskick.refresh_from_db()
        recipient = SimpleNamespace(
            pk=None,
            merge={"förnamn": "Anna\r\nBcc: x@y.example", "företag": "W" * 90},
            basis="",
            tracking_ok=False,
            address="",
        )
        ctx = render.context_for(self.utskick, mode=render.PREVIEW, recipient=recipient)
        self.assertEqual(render.subject_for(self.utskick, ctx), "Hej Anna Bcc: x@y.example")
        self.assertEqual(render.preheader_for(self.utskick, ctx), "Till " + "W" * 60)

    def test_the_editor_shows_the_tags(self):
        ctx = render.context_for(self.utskick, mode=render.EDITOR)
        html = render.render_html(self.utskick, ctx)
        self.assertIn("Hej {förnamn|du}, dags", html)


# ---------------------------------------------------------------------------
# Textversionen
# ---------------------------------------------------------------------------


class TextVersionTests(BrevFixture, TestCase):
    def test_built_from_the_blocks(self):
        self.freeze()
        _html, ctx = self.send_html(self.recipient())
        plain = text.render_text(self.utskick, ctx)
        self.assertIn("HÖSTSERVICE\nHej Anna, dags för service av värmepumpen", plain)
        self.assertRegex(plain, r"Boka service: https://klick\.adx\.se/m/")
        self.assertIn("- Rengöring av filter", plain)
        self.assertIn("1. Boka via knappen eller ring oss.", plain)
        self.assertIn("Kod: VARME26", plain)
        self.assertIn("Fredag 23 oktober kl. 15 till 18", plain)
        self.assertRegex(plain, r"Avregistrera dig: https://klick\.adx\.se/a/")
        self.assertRegex(plain, r"^Visa i webbläsaren: https://klick\.adx\.se/w/")
        self.assertNotIn("<", plain.replace("<b>", ""))
        self.assertNotIn("**", plain)
        self.assertTrue(plain.endswith("\n"))
        self.assertNotIn("\n\n\n", plain)

    def test_the_customers_own_text_wins_with_the_footer(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(
            text_override="Hej {förnamn|du}.\n\nVälkommen till öppet hus."
        )
        self.utskick.refresh_from_db()
        ctx = render.context_for(self.utskick, mode=render.SEND, recipient=self.recipient())
        plain = text.render_text(self.utskick, ctx)
        self.assertIn("Hej Anna.\n\nVälkommen till öppet hus.", plain)
        self.assertIn("Avregistrera dig: https://klick.adx.se/a/", plain)
        self.assertNotIn("HÖSTSERVICE", plain)
        self.assertIn("HÖSTSERVICE", text.default_text(self.utskick, ctx))


# ---------------------------------------------------------------------------
# Storleken (F.5)
# ---------------------------------------------------------------------------


class SizeTests(BrevFixture, TestCase):
    def test_the_full_brev_is_under_102_kb(self):
        size = render.html_size(self.utskick)
        self.assertGreater(size, 20_000)
        self.assertLess(size, checks.MAX_HTML_BYTES)

    def test_the_longest_values_count(self):
        doc = [blk("heading", text="Hej {förnamn}")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        with_tag = render.html_size(self.utskick)
        doc = [blk("heading", text="Hej")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        self.assertGreaterEqual(with_tag - render.html_size(self.utskick), 60)

    def test_a_long_mail_blocks(self):
        body = ("Ett långt stycke med [en länk](https://exempelror.example/a) och mer text. " * 37)[
            :2990
        ]
        doc = [blk("text", body=body) for _ in range(registry.MAX_BLOCKS)]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        self.assertGreater(render.html_size(self.utskick), checks.MAX_HTML_BYTES)
        items = checks.email_checks(self.utskick, check_links=False)
        size = next(i for i in items if i.key == "size")
        self.assertEqual(size.level, checks.BLOCKS)
        self.assertIn("Gmail kapar mejl över 102 kB, och då försvinner avregistreringen", size.text)


# ---------------------------------------------------------------------------
# Accentfärgen (F.2)
# ---------------------------------------------------------------------------


class AccentTests(BrevFixture, TestCase):
    def test_a_dark_colour(self):
        palette = style.palette_for_accent("#1a57d6")
        self.assertEqual(palette.button_bg, "#1A57D6")
        self.assertEqual(palette.button_text, "#FFFFFF")
        self.assertEqual(palette.accent_text, "#1A57D6")
        self.assertFalse(palette.light)

    def test_a_light_colour(self):
        from apps.flamingo.pagebuilder.render import contrast

        palette = style.palette_for_accent("#F2C94C")
        self.assertTrue(palette.light)
        self.assertEqual(palette.button_bg, "#F2C94C")
        self.assertEqual(palette.button_text, "#111111")
        self.assertNotEqual(palette.accent_text, "#F2C94C")
        self.assertGreaterEqual(contrast(palette.accent_text, "#FFFFFF"), 4.5)

    def test_the_mail_uses_the_palette(self):
        Utskick.objects.filter(pk=self.utskick.pk).update(accent="#F2C94C")
        self.utskick.refresh_from_db()
        ctx = render.context_for(self.utskick, mode=render.PREVIEW)
        html = render.render_html(self.utskick, ctx)
        dark = ctx.palette.accent_text
        self.assertIn('bgcolor="#F2C94C"', html)
        self.assertIn("color:#111111;text-decoration:none", html)
        self.assertIn(f"color:{dark};text-decoration:underline", html)
        self.assertIn(f"border-left:3px solid {dark}", html)

    def test_the_default_is_the_logos_colour_or_blue(self):
        with mock.patch.object(media, "logo_colors_for_account", return_value={}):
            self.assertEqual(style.default_accent(self.account), "#1A57D6")
        with mock.patch.object(
            media, "logo_colors_for_account", return_value={"primary": "#1f7a4d"}
        ):
            self.assertEqual(style.default_accent(self.account), "#1F7A4D")
            Utskick.objects.filter(pk=self.utskick.pk).update(accent="")
            self.utskick.refresh_from_db()
            self.assertEqual(style.accent_for(self.utskick), "#1F7A4D")

    def test_the_picker(self):
        with mock.patch.object(
            media,
            "logo_colors_for_account",
            return_value={"primary": "#1D2B4F", "colors": ["#1D2B4F", "#F2C94C", "#1A57D6"]},
        ):
            picker = style.picker(self.account)
        self.assertEqual([c["value"] for c in picker["logo"]], ["#1D2B4F", "#F2C94C", "#1A57D6"])
        self.assertEqual(
            [c["value"] for c in picker["swatches"]], ["#1F7A4D", "#B42318", "#6D28D9", "#111111"]
        )
        self.assertTrue(picker["logo"][1]["light"])
        self.assertEqual(picker["light_text"], style.LIGHT_TEXT)


# ---------------------------------------------------------------------------
# Lägena: redigeraren, webbversionen, kalendern
# ---------------------------------------------------------------------------


class ModeTests(BrevFixture, TestCase):
    def test_the_editor_has_the_pb_attributes_and_no_scripts(self):
        ctx = render.context_for(self.utskick, mode=render.EDITOR)
        html = render.render_html(self.utskick, ctx)
        self.assertIn(render.EDITING_CSP, html)
        self.assertEqual(html.count("data-pb-block="), 22)
        self.assertIn('data-pb-field="title"', html)
        self.assertIn('data-pb-field="items.0.title"', html)
        self.assertIn("data-brev-canvas", html)
        first = self.utskick.email_doc["blocks"][0]
        self.assertIn(f'data-pb-block="{first["id"]}"', html)
        self.assertIn(f'data-pb-version="{first["active"]}"', html)

    def test_an_unsaved_block_never_gets_a_script_link(self):
        raw = blk("text", body="Se [här](javascript:void) och [där](//x.example).")
        html = render.render_block(self.utskick, raw)
        self.assertNotIn("javascript:", html)
        self.assertNotIn('href="//', html)
        self.assertIn('href="https://klick.adx.se/"', html)

    def test_render_block_for_the_editor(self):
        block = self.utskick.email_doc["blocks"][1]
        html = render.render_block(self.utskick, block)
        self.assertTrue(html.startswith("<tr data-pb-block="))
        self.assertIn("Det här ingår i servicen", html)
        empty = blk("hero")
        html = render.render_block(self.utskick, empty)
        self.assertIn("data-pb-empty", html)
        self.assertIn('data-pb-field="image" data-pb-media', html)

    def test_the_web_view_is_the_frozen_mail_without_the_web_link(self):
        self.freeze()
        recipient = self.recipient()
        html = render.web_view(self.utskick, recipient)
        self.assertNotIn(">Visa i webbläsaren<", html)
        self.assertIn("Hej Anna, dags", html)
        self.assertIn("https://klick.adx.se/m/", html)
        # Företaget ändras efter frysningen: webbversionen är som mejlet.
        Fact.objects.filter(account=self.account, key="adress").update(value="Ny väg 1")
        self.assertIn("Mossvägen 12", render.web_view(self.utskick, recipient))
        self.assertNotIn("Mossvägen 12", render.web_view(make_utskick(self.account)))

    def test_the_calendar(self):
        event = next(b for b in self.utskick.email_doc["blocks"] if b["type"] == "event")
        ics = render.calendar_ics(self.utskick, event["id"])
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("DTSTART:20261023T130000Z", ics)
        self.assertIn("DTEND:20261023T160000Z", ics)
        self.assertIn("SUMMARY:Öppet hus i butiken", ics)
        self.assertIn("LOCATION:Mossvägen 12", ics)
        self.assertIn(f"UID:{self.utskick.pk}-{event['id']}@klick.adx.se", ics)
        self.assertTrue(all(len(line.encode()) <= 75 for line in ics.split("\r\n")))
        self.assertIsNone(render.calendar_ics(self.utskick, "b_" + "x" * 12))

    def test_a_whole_day_event(self):
        doc = [blk("event", date="2026-11-02", title="Stängt", calendar="yes")]
        blocks.save(self.utskick, doc, rev=self.utskick.email_rev, user=self.anna)
        self.utskick.refresh_from_db()
        block_id = self.utskick.email_doc["blocks"][0]["id"]
        ics = render.calendar_ics(self.utskick, block_id)
        self.assertIn("DTSTART;VALUE=DATE:20261102", ics)
        self.assertIn("DTEND;VALUE=DATE:20261103", ics)
        ctx = render.context_for(self.utskick, mode=render.PREVIEW)
        self.assertIn("Måndag 2 november", render.render_html(self.utskick, ctx))

    def test_the_date_helpers(self):
        self.assertEqual(render.date_long("2026-10-24"), "24 oktober")
        self.assertEqual(render.date_long("2027-01-02", year=2026), "2 januari 2027")
        self.assertEqual(render.time_text("09:30"), "9.30")
        line = render.event_line({"date": "2026-10-23", "start": "15:00"}, year=2026)
        self.assertEqual(line, "Fredag 23 oktober kl. 15")
        self.assertEqual(render.event_line({"date": "2026-10-23"}, year=2026), "Fredag 23 oktober")


# ---------------------------------------------------------------------------
# MIME (D.6)
# ---------------------------------------------------------------------------


@override_settings(**LINK_SETTINGS)
class MimeTests(TestCase):
    def build(self, **headers):
        mail = OutgoingMail(
            to="anna@kund.example",
            from_name="Exempelrör",
            from_addr="exempelror@utskick.adx.se",
            subject="Dags för service av värmepumpen innan vintern, Anna Lindqvist",
            text="Hej Anna\n",
            html="<!doctype html><p>Hej Anna</p>",
            headers=headers,
        )
        return mime.build(mail)

    def test_list_unsubscribe_is_never_encoded(self):
        https = links.unsubscribe_url(12345, "ab" * 32)
        mailto = links.mailto_unsubscribe(12345, 99999999)
        self.assertGreater(len(https), 78)
        raw = self.build(**mime.unsubscribe_headers(https, mailto))
        head = raw.split(b"\r\n\r\n", 1)[0].decode("ascii")
        self.assertNotIn("=?utf-8?q?=3Chttps", head)
        self.assertIn(f"List-Unsubscribe: <{https}>,", head)
        self.assertIn("List-Unsubscribe-Post: List-Unsubscribe=One-Click", head)
        parsed = mime.parse(raw)
        self.assertEqual(parsed["List-Unsubscribe"], f"<{https}>, <{mailto}>")
        self.assertTrue(all(len(line) <= 998 for line in head.split("\r\n")))

    def test_text_before_html_and_quoted_printable(self):
        parsed = mime.parse(self.build())
        self.assertEqual(parsed.get_content_type(), "multipart/alternative")
        parts = [p.get_content_type() for p in parsed.iter_parts()]
        self.assertEqual(parts, ["text/plain", "text/html"])
        for part in parsed.iter_parts():
            self.assertEqual(part["Content-Transfer-Encoding"], "quoted-printable")
        self.assertIn("=?utf-8?", parsed.as_string().split("\n\n", 1)[0] + "=?utf-8?")
        self.assertEqual(
            parsed["Subject"], "Dags för service av värmepumpen innan vintern, Anna Lindqvist"
        )

    def test_in_reply_to_keeps_the_message_id(self):
        long_id = "<0102018c" + "a" * 70 + "@eu-west-1.amazonses.com>"
        parsed = mime.parse(self.build(**{"In-Reply-To": long_id, "References": long_id}))
        self.assertEqual(parsed["In-Reply-To"], long_id)


# ---------------------------------------------------------------------------
# Mallarnas egen vakt (F.4)
# ---------------------------------------------------------------------------


class TemplateGuardTests(TestCase):
    def templates(self):
        return sorted(TEMPLATES.rglob("*.html"))

    def test_every_block_has_a_template(self):
        names = {p.stem for p in (TEMPLATES / "blocks").glob("*.html")}
        for key in registry.BLOCK_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, names)

    def test_no_scripts_fonts_or_relative_addresses(self):
        for path in self.templates():
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotRegex(source, r"<script")
                self.assertNotIn("fonts.googleapis", source)
                self.assertNotIn("@import", source)
                self.assertNotIn("<link", source)
                self.assertNotRegex(source, r'(href|src)="(/|#|\.)')
                self.assertNotRegex(source, r"\bon\w+=")

    def test_every_class_comes_with_inline_style(self):
        for path in self.templates():
            source = path.read_text(encoding="utf-8")
            for match in re.finditer(r"<\w+\b[^>]*\bclass=\"[^\"]*\"[^>]*>", source):
                with self.subTest(path=path.name, tag=match.group(0)[:80]):
                    self.assertIn("style=", match.group(0))

    def test_the_copy_rules(self):
        for path in self.templates():
            source = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
            visible = re.sub(r"{%.*?%}|{{.*?}}|<[^>]+>", "", source, flags=re.S)
            with self.subTest(path=path.name):
                self.assertNotIn("!", visible)
                self.assertNotRegex(visible, r"\[\s*\]")
                for char in (chr(0x2013), chr(0x2014), chr(0x2026), chr(0x201C), chr(0x201D)):
                    self.assertNotIn(char, source)

    def test_the_rendered_mail_follows_the_rules_too(self):
        # Hela mejlet (inte bara mallarna): inga skript, inga händelser.
        html = Path(TEMPLATES / "layout.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="color-scheme" content="light">', html)
