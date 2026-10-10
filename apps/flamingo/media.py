"""
Mediaarkivet i sidbyggaren: kundens logotyp och bilder, och bilderna som
läsningen av hemsidan hittade. Beslut 2026-10-03 (Giovanni): högst
MEDIA_MAX_PER_ACCOUNT bilder per konto, längsta sidan MEDIA_MAX_SIDE px,
sparade som WebP, och kunden intygar rätten till bilderna från hemsidan.

    process_image(data)            prövar och kodar om en bild: Processed (WebP)
    store_image(account, processed, ...)
                                   en bild blir ett MediaAsset (gränsen per konto)
    add_upload(account, uploaded, user=None)
                                   en uppladdad fil, hela vägen
    set_logo(asset, user=None) / unset_logo(asset, user=None)
                                   logotypen (en per konto) och dess färger;
                                   syns direkt på live-sidorna, så byrån larmas
    logo_colors_from(image)        {"primary": "#RRGGBB", "colors": [...]}
    apply_logo_palette(account, page=None, user=None)
                                   paletten "logo" på kontots sidor (eller en)
    media_usage(account)           {bildens id: [Use]}: sidorna som använder den
    delete_asset(asset)            MediaInUse när en sida eller ett utskick som inte
                                   är skickat använder bilden (apps/utskick, C.2)
    asset_json(asset)              bilden som JSON för redigerarens bildväljare

Bilderna från hemsidan (läsningen, scan.py):

    image_ref(tag, attrs, base_url, in_nav)
                                   en bildadress ur en tagg (sidparsern i scan.py)
    site_candidates(pages)         upp till SITE_CANDIDATES adresser, logotyper först
    SiteImageFetch.start(account, pages, deadline=...)
                                   hämtar miniatyrerna i trådar, inom tiden
    SiteImageFetch.finish(account) sparar SiteImageCandidate-raderna
    import_candidates(account, ids, user=None, rights_confirmed=False)
                                   kundens val hämtas på riktigt till arkivet

Säkerheten:

- Bara JPEG, PNG, WebP och GIF (första bildrutan). Pillow prövar filen
  (open, verify, och sedan öppnas den igen); allt annat nekas, också SVG
  (skript i en bild som nginx serverar) och HTML förklädd till en bild.
- Minnet (servern har 2 GB och delas av flera sajter): en liten fil kan
  vara en enorm bild (en förlustfri WebP på 2,4 kB med 60 miljoner
  bildpunkter tog 1,4 GB). Därför prövas storleken i filens huvud innan
  något avkodas: högst UPLOAD_MAX_PIXELS för en uppladdning eller en
  hämtning till arkivet och CANDIDATE_MAX_PIXELS för en miniatyr från
  hemsidan (en JPEG räknas i den skala den avkodas i, draft; en WebP, som
  libwebp alltid avkodar i full storlek, får en tredjedel och en bild med
  alfakanal två tredjedelar), och aldrig över MAX_PIXELS. Bilden skalas
  ner direkt efter avkodningen, innan någon kopia görs, och bara en bild
  åt gången avkodas och kodas om i processen (_DECODE). Miniatyrerna från
  hemsidan hämtas i trådar men avkodas en i taget när läsningen är klar,
  och hämtningen till arkivet håller högst några filer i minnet åt gången.
  Högst UPLOADS_PER_HOUR uppladdade filer per konto och timme
  (limits.reserve_hourly), räknat innan de avkodas. Mätt 2026-10-03 (en
  bild i taget): högst runt 350 MB för den största bild som tas emot.
- Högst MAX_UPLOAD_BYTES per fil. Image.MAX_IMAGE_PIXELS sätts till
  MAX_PIXELS, så Pillow själv också vägrar.
- Varje bild kodas om till WebP: inget av originalet följer med (EXIF med
  plats och kamera, ICC-profilen efter att färgerna räknats om till sRGB,
  kommentarer). Orienteringen ur EXIF används innan den kastas.
- Filerna får slumpade namn (models.media_upload_path); originalets namn
  blir bara en förslagen alternativtext.
- Bilderna från hemsidan hämtas med SSRF-skyddet i apps/tools/analyzer.fetch
  (bara publika adresser, varje omdirigering prövad, ett tak för storleken
  och en tidsgräns), som läsningen av sidorna.
- Ett demokonto hämtar aldrig något från en hemsida.
"""

import hashlib
import io
import logging
import os
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, transaction
from django.db.models.signals import pre_save
from django.utils import timezone
from PIL import Image, ImageCms, ImageOps

from apps.common.security import MAX_IMAGE_PIXELS, sanitize_plain_text
from apps.tools.analyzer import AnalysError, fetch

from . import limits
from .models import (
    MEDIA_FORMAT,
    MEDIA_MAX_PER_ACCOUNT,
    MEDIA_MAX_SIDE,
    MEDIA_THUMB_SIDE,
    LandingPage,
    MediaAsset,
    SiteImageCandidate,
)

logger = logging.getLogger(__name__)

#: Formaten som tas emot (Pillows namn).
ACCEPTED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
#: Största fil som tas emot (uppladdning och hämtning från hemsidan).
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
#: Bildpunkter i filens huvud som aldrig avkodas: samma gräns som resten av
#: sajten (apps.common.security, 60 miljoner), som också sätter Pillows egen.
MAX_PIXELS = MAX_IMAGE_PIXELS
#: Bildpunkter som avkodas för en uppladdning eller en bild till arkivet
#: (en JPEG i den skala den avkodas i). 24 miljoner rymmer en kamera på
#: 24 MP, och en JPEG från en telefon på 48 MP avkodas i halv skala.
UPLOAD_MAX_PIXELS = 24_000_000
#: WebP avkodas alltid i full storlek och kostar runt 17 byte per
#: bildpunkt i libwebp (mätt 2026-10-03: 16 MP gav 280 MB), mot 3-4 för
#: PNG och JPEG. En WebP får därför en tredjedel av gränsen (8 MP för en
#: uppladdning, runt 270 MB med nedskalningen), och en PNG eller GIF med
#: alfakanal två tredjedelar (nedskalningen gör en kopia med förmultiplicerad
#: alfa: 24 MP gav 350 MB, 16 MP runt 230 MB). En bild utan alfa får hela.
WEBP_SHARE = 3
ALPHA_SHARE = (2, 3)
#: Filer i en uppladdning (ett anrop).
MAX_FILES_PER_UPLOAD = 20
#: Uppladdade filer per konto och timme (räknas innan de avkodas).
UPLOADS_PER_HOUR = 60
USAGE_UPLOADS = "media_upload"
#: En bild åt gången avkodas i processen (gunicorn-arbetaren och dess
#: trådar): två samtidiga uppladdningar delar inte på minnet.
_DECODE = threading.BoundedSemaphore(1)
WEBP_QUALITY = 82
THUMB_QUALITY = 78

# Pillow vägrar också själv (DecompressionBombError över det dubbla, en
# varning däröver; vår egen kontroll nedan nekar redan vid gränsen). Samma
# värde som apps.common.security sätter, så ingen av dem ändrar den andra.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

#: Låsen per konto i Postgres: egen nyckelrymd ("FM") plus kontots id.
_LOCK_MEDIA = 0x464D << 32

TOO_BIG = "Filen är större än 15 MB."
NOT_AN_IMAGE = "Filen är inte en bild vi kan använda. Välj en JPG, PNG, WebP eller GIF."
TOO_MANY_PIXELS = (
    "Bilden är för stor (för många bildpunkter). Spara den i en mindre storlek, eller som "
    "JPG, och ladda upp den igen."
)
TOO_MANY_UPLOADS = (
    f"Högst {UPLOADS_PER_HOUR} bilder i timmen kan laddas upp. Vänta en stund och försök igen."
)
BROKEN = "Bilden gick inte att läsa. Den kan vara skadad."
EMPTY = "Filen är tom."
ARCHIVE_FULL = (
    f"Mediaarkivet är fullt ({MEDIA_MAX_PER_ACCOUNT} bilder). Ta bort bilder som inte "
    "används för att få plats med nya."
)
DEMO_REFUSED = "Det här är ett demokonto, så inga bilder hämtas från någon hemsida."
RIGHTS_REQUIRED = "Bekräfta att ni äger bilderna eller har rätt att använda dem."


class MediaError(ValueError):
    """Bilden eller ändringen togs inte emot. message är en svensk text för
    kunden (aldrig ett fel från Pillow eller hämtningen)."""

    def __init__(self, message):
        self.message = str(message)
        super().__init__(self.message)


def _named(names, one, many):
    names = list(names)
    shown = ", ".join(names[:3])
    more = f" och {len(names) - 3} till" if len(names) > 3 else ""
    return f"{one if len(names) == 1 else many} {shown}{more}"


class MediaInUse(MediaError):
    """Bilden används av en eller flera sidor (uses: [Use]) eller av utskick
    som inte är skickade (utskick: utskickens namn, apps/utskick C.2)."""

    def __init__(self, uses, utskick=()):
        self.uses = list(uses)
        self.utskick = list(utskick)
        where = []
        if self.uses:
            where.append("på " + _named((use.page.name for use in self.uses), "sidan", "sidorna"))
        if self.utskick:
            where.append("i " + _named(self.utskick, "utskicket", "utskicken"))
        super().__init__(
            f"Bilden används {' och '.join(where)}. Byt bilden där först, sedan går den "
            "att ta bort."
        )


# ---------------------------------------------------------------------------
# Bilden: prövas, vänds rätt, räknas om till sRGB och kodas om till WebP
# ---------------------------------------------------------------------------


@dataclass
class Processed:
    data: bytes
    thumb: bytes
    width: int
    height: int
    #: Miniatyren som Pillow-bild (logotypens färger räknas på den).
    preview: object = None


def _looks_like_markup(data):
    """SVG, HTML eller XML, också med en bildändelse. Pillow öppnar dem inte
    heller, men felet ska vara tydligt och komma innan Pillow ens försöker."""
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith(b"<") or b"<svg" in head or b"<html" in head or b"<script" in head


def _check_pixels(size, max_pixels=MAX_PIXELS):
    width, height = size
    if width < 1 or height < 1:
        raise MediaError(BROKEN)
    if width * height > max_pixels:
        raise MediaError(TOO_MANY_PIXELS)


def _open(data, *, draft_side=None, max_pixels=UPLOAD_MAX_PIXELS):
    """(bilden avkodad, storleken i filens huvud). Se open_image."""
    if not data:
        raise MediaError(EMPTY)
    if len(data) > MAX_UPLOAD_BYTES:
        raise MediaError(TOO_BIG)
    if _looks_like_markup(data):
        raise MediaError(NOT_AN_IMAGE)
    try:
        # Image.open läser bara filens huvud: storleken prövas innan något
        # avkodas.
        probe = Image.open(io.BytesIO(data))
        image_format = probe.format
        if image_format not in ACCEPTED_FORMATS:
            raise MediaError(NOT_AN_IMAGE)
        header_size = probe.size
        _check_pixels(header_size, MAX_PIXELS)
        if image_format != "JPEG":
            _check_pixels(header_size, _pixel_limit(probe, max_pixels))
        probe.verify()
        image = Image.open(io.BytesIO(data))
        if image.format != image_format or image.size != header_size:
            raise MediaError(NOT_AN_IMAGE)
        if getattr(image, "n_frames", 1) > 1:
            image.seek(0)
        if image_format == "JPEG" and draft_side:
            # JPEG avkodas direkt i en mindre skala (1/2, 1/4 eller 1/8) när
            # bilden ändå skalas ner; gränsen gäller den skalan. Målet har
            # bildens form, så att den längsta sidan räcker till draft_side.
            width, height = header_size
            factor = min(1.0, draft_side / max(width, height))
            image.draft("RGB", (max(1, int(width * factor)), max(1, int(height * factor))))
        _check_pixels(image.size, max_pixels)
        image.load()
    except MediaError:
        raise
    except Image.DecompressionBombError:
        raise MediaError(TOO_MANY_PIXELS) from None
    except Image.UnidentifiedImageError:
        raise MediaError(NOT_AN_IMAGE) from None
    except Exception:  # noqa: BLE001 - Pillow har många sätt att säga "trasig"
        raise MediaError(BROKEN) from None
    return image, header_size


def _pixel_limit(image, max_pixels):
    """Gränsen för en bild som inte är JPEG, efter vad avkodningen kostar
    (WEBP_SHARE, ALPHA_SHARE). Läser bara filens huvud."""
    if image.format == "WEBP":
        return max_pixels // WEBP_SHARE
    if _has_alpha(image):
        return max_pixels * ALPHA_SHARE[0] // ALPHA_SHARE[1]
    return max_pixels


def open_image(data, *, draft_side=None, max_pixels=UPLOAD_MAX_PIXELS):
    """Bilden som Pillow-bild, prövad: format, storlek i filens huvud (högst
    max_pixels bildpunkter, en JPEG i den skala den avkodas i, och aldrig
    över MAX_PIXELS), verify och sedan öppnad igen och avkodad (första
    bildrutan). Kastar MediaError."""
    return _open(data, draft_side=draft_side, max_pixels=max_pixels)[0]


def _shrink(image, max_side):
    """Bilden nerskalad på plats direkt efter avkodningen (längsta sidan
    högst max_side), innan någon kopia görs. En palettbild görs om till
    RGB(A) först, så att den skalas med LANCZOS och inte med närmsta punkt."""
    if max(image.size) <= max_side:
        return image
    if image.mode in ("P", "PA", "1"):
        image = image.convert("RGBA" if _has_alpha(image) else "RGB")
    try:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    except ValueError:
        # Ett läge som inte går att skala (16 bitar): normalize gör om det.
        image = normalize(image)
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image


def _decode(data, *, max_side, max_pixels):
    """(bilden avkodad, nerskalad och normaliserad, storleken i filens
    huvud). Anroparen håller _DECODE (process_image, _thumb_from), så att
    avkodningen och omkodningen till WebP görs för en bild åt gången."""
    image, header_size = _open(data, draft_side=max_side, max_pixels=max_pixels)
    return normalize(_shrink(image, max_side)), header_size


def _has_alpha(image):
    if image.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        return True
    return "transparency" in image.info


def _to_srgb(image, mode):
    """Färgerna räknade om från bildens ICC-profil till sRGB (en bild från
    en telefon i Display P3 blir annars blekare). Utan profil, eller om
    profilen inte går att läsa, används bilden som den är."""
    icc = image.info.get("icc_profile")
    if not icc or image.mode not in ("RGB", "RGBA", "CMYK", "L"):
        return image.convert(mode)
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        target = ImageCms.createProfile("sRGB")
        if image.mode == "L":
            image = image.convert("RGB")
        if image.mode == "RGBA" and mode == "RGBA":
            alpha = image.getchannel("A")
            rgb = ImageCms.profileToProfile(image.convert("RGB"), source, target, outputMode="RGB")
            rgb.putalpha(alpha)
            return rgb
        converted = ImageCms.profileToProfile(image, source, target, outputMode="RGB")
        return converted.convert(mode)
    except Exception:  # noqa: BLE001 - en trasig profil fäller inte bilden
        return image.convert(mode)


def normalize(image):
    """Orienteringen ur EXIF använd (på plats, ingen kopia), RGB eller RGBA
    i sRGB, utan metadata. En bild med alfakanal där allt är ogenomskinligt
    blir RGB."""
    try:
        ImageOps.exif_transpose(image, in_place=True)
    except Exception:  # noqa: BLE001 - trasig EXIF: bilden som den är
        logger.info("Flamingo: EXIF-orienteringen gick inte att läsa")
    if image.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
        image = image.point(lambda value: value * (1 / 256)).convert("L")
    mode = "RGBA" if _has_alpha(image) else "RGB"
    image = _to_srgb(image, mode)
    if mode == "RGBA":
        low, _ = image.getchannel("A").getextrema()
        if low == 255:
            image = image.convert("RGB")
    image.info = {}
    return image


def _webp(image, quality):
    buffer = io.BytesIO()
    # Inga exif=, icc_profile= eller xmp=: WebP-kodaren skriver bara det som
    # skickas med, så ingen metadata följer med.
    image.save(buffer, MEDIA_FORMAT, quality=quality, method=4)
    return buffer.getvalue()


def make_thumb(image):
    thumb = image.copy()
    thumb.thumbnail((MEDIA_THUMB_SIDE, MEDIA_THUMB_SIDE), Image.Resampling.LANCZOS)
    return thumb


def process_image(data, *, max_side=MEDIA_MAX_SIDE):
    """Bilden prövad och omkodad: WebP med längsta sidan högst max_side och
    en miniatyr (MEDIA_THUMB_SIDE). Högst UPLOAD_MAX_PIXELS avkodas, och
    bilden skalas ner direkt (_decode). Kastar MediaError."""
    with _DECODE:
        image, _size = _decode(data, max_side=max_side, max_pixels=UPLOAD_MAX_PIXELS)
        thumb = make_thumb(image)
        return Processed(
            data=_webp(image, WEBP_QUALITY),
            thumb=_webp(thumb, THUMB_QUALITY),
            width=image.width,
            height=image.height,
            preview=thumb,
        )


_CAMERA_WORDS = frozenset(
    "img image dsc dscn dscf dcim pxl mvimg photo foto bild screenshot skarmavbild "
    "skärmavbild skarmbild skärmbild scaled copy kopia edited final whatsapp unnamed".split()
)


def alt_from_filename(name):
    """En förslagen alternativtext ur filens namn ("badrum-fore_2.jpg" blir
    "Badrum fore"). Kamerans och telefonens namn (IMG_1234, PXL_2026...) och
    siffror blir ingenting: hellre tomt än skräp."""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    words = []
    for word in re.split(r"[\s_\-.+,()]+", stem):
        if not word or word.casefold() in _CAMERA_WORDS:
            continue
        if not re.search(r"[^\W\d_]{2,}", word):
            continue  # siffror, eller en bokstav och siffror
        if re.fullmatch(r"[0-9a-fA-F]{8,}", word) or re.fullmatch(r"\d+x\d+", word):
            continue
        words.append(word)
    text = sanitize_plain_text(" ".join(words), max_length=200)
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------------------
# Arkivet: spara, logotypen, ta bort
# ---------------------------------------------------------------------------


def _lock_media(account_pk):
    """Kontots arkiv ändras ett anrop i taget (resten av transaktionen), så
    två uppladdningar samtidigt inte båda kan ta den sista platsen."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK_MEDIA + int(account_pk)])


def media_count(account):
    return MediaAsset.objects.filter(account=account).count()


def store_image(
    account,
    processed,
    *,
    alt="",
    user=None,
    source=MediaAsset.SOURCE_UPLOAD,
    source_url="",
    rights_confirmed=False,
    now=None,
):
    """Spara en prövad bild (Processed) som ett MediaAsset. Gränsen per
    konto prövas med kontots arkiv låst. Kastar MediaError(ARCHIVE_FULL)."""
    now = now or timezone.now()
    who = user if getattr(user, "pk", None) else None
    with transaction.atomic():
        _lock_media(account.pk)
        if MediaAsset.objects.filter(account=account).count() >= MEDIA_MAX_PER_ACCOUNT:
            raise MediaError(ARCHIVE_FULL)
        asset = MediaAsset(
            account=account,
            alt=sanitize_plain_text(alt, max_length=200),
            source=source,
            source_url=source_url[:500] if source == MediaAsset.SOURCE_SITE else "",
            rights_confirmed_at=now if rights_confirmed else None,
            rights_confirmed_by=who if rights_confirmed else None,
            created_at=now,
        )
        saved = []
        try:
            asset.file.save("bild.webp", ContentFile(processed.data), save=False)
            saved.append(asset.file)
            asset.thumb.save("tumme.webp", ContentFile(processed.thumb), save=False)
            saved.append(asset.thumb)
            asset.width, asset.height = processed.width, processed.height
            asset.save()
        except Exception:
            for image in saved:
                try:
                    image.storage.delete(image.name)
                except Exception:  # noqa: BLE001
                    logger.warning("Flamingo: bildfilen %s kunde inte tas bort", image.name)
            raise
    return asset


def read_upload(uploaded):
    """Den uppladdade filens bytes, eller MediaError (för stor, tom)."""
    size = getattr(uploaded, "size", None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise MediaError(TOO_BIG)
    data = uploaded.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise MediaError(TOO_BIG)
    return data


def add_upload(account, uploaded, *, user=None):
    """En uppladdad fil (Djangos UploadedFile) blir ett MediaAsset. Högst
    UPLOADS_PER_HOUR filer per konto och timme, räknat innan filen avkodas.
    Kastar MediaError med en text som börjar med filens namn."""
    name = os.path.basename(str(getattr(uploaded, "name", "") or "bild"))[:120]
    try:
        if media_count(account) >= MEDIA_MAX_PER_ACCOUNT:
            raise MediaError(ARCHIVE_FULL)
        if not limits.reserve_hourly(account, USAGE_UPLOADS, UPLOADS_PER_HOUR):
            raise MediaError(TOO_MANY_UPLOADS)
        processed = process_image(read_upload(uploaded))
        return store_image(account, processed, alt=alt_from_filename(name), user=user)
    except MediaError as exc:
        raise MediaError(f"{name}: {exc.message}") from None


def asset_json(asset):
    """Bilden för redigerarens bildväljare (flamingo:app_media_json)."""
    file_url = asset.file.url if asset.file else ""
    return {
        "id": asset.pk,
        "thumb": asset.thumb.url if asset.thumb else file_url,
        "url": file_url,
        "width": asset.width,
        "height": asset.height,
        "alt": asset.alt,
        "is_logo": asset.is_logo,
    }


# -- Logotypen och dess färger ------------------------------------------------


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, int(c))):02X}" for c in rgb[:3])


def _saturation(rgb):
    high, low = max(rgb), min(rgb)
    return 0.0 if high == 0 else (high - low) / high


def _distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


def _near_white(rgb):
    """Vitt, och ljusgrått utan färg (bakgrunder, kanter)."""
    return min(rgb) >= 228 or (min(rgb) >= 200 and _saturation(rgb) < 0.08)


def _near_black(rgb):
    """Svart, och mörkgrått utan färg (logotypens text, #202124). En mörk
    färg med mättnad, som marinblått, räknas inte hit."""
    return max(rgb) <= 34 or (max(rgb) <= 72 and _saturation(rgb) < 0.25)


def logo_colors_from(image):
    """3-5 färger ur logotypen (Pillows quantize), nästan vitt, nästan svart
    och genomskinligt borträknat, störst andel först, och huvudfärgen:
    den största färg som är tydligt mättad (annars den största).

    {"primary": "#1B66D2", "colors": ["#1B66D2", "#F2994A", ...]}, eller {}
    när logotypen bara är vit, svart eller genomskinlig. Färgen kan vara för
    ljus för text: renderaren mörkar den tills den klarar WCAG AA
    (pagebuilder/render.py, palette_vars)."""
    sample = image.copy()
    sample.thumbnail((160, 160))
    sample = sample.convert("RGBA")
    pixels = [
        p[:3]
        for p in sample.getdata()
        if p[3] >= 160 and not _near_white(p[:3]) and not _near_black(p[:3])
    ]
    if len(pixels) < 16:
        return {}
    strip = Image.new("RGB", (len(pixels), 1))
    strip.putdata(pixels)
    quantized = strip.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    total = len(pixels)
    found = []
    for count, index in sorted(quantized.getcolors() or [], reverse=True):
        rgb = tuple(palette[index * 3 : index * 3 + 3])
        if len(rgb) != 3 or count / total < 0.03:
            continue
        if any(_distance(rgb, other) < 48 for other, _ in found):
            continue
        found.append((rgb, count / total))
        if len(found) == 5:
            break
    if not found:
        return {}
    vivid = [(rgb, share) for rgb, share in found if _saturation(rgb) >= 0.25]
    primary = max(vivid, key=lambda item: item[1])[0] if vivid else found[0][0]
    colors = [_hex(rgb) for rgb, _ in found]
    accent = next((c for c in colors if c != _hex(primary)), "")
    out = {"primary": _hex(primary), "colors": colors}
    if accent:
        out["accent"] = accent
    return out


def colors_for_asset(asset):
    """Färgerna ur bildens miniatyr (eller bilden), med bildens id ("asset").
    {} om filen inte går att läsa eller saknar tydliga färger."""
    image_file = asset.thumb or asset.file
    if not image_file:
        return {}
    try:
        with image_file.open("rb") as handle:
            data = handle.read(MAX_UPLOAD_BYTES + 1)
        colors = logo_colors_from(open_image(data))
    except Exception:  # noqa: BLE001 - färgerna är ett förslag, aldrig ett fel
        logger.info("Flamingo: färgerna ur bild %s gick inte att läsa", asset.pk)
        return {}
    if colors:
        colors["asset"] = asset.pk
    return colors


def logo_colors_for_account(account_id):
    """Färgerna ur kontots logotyp, för en ny sida: samma som en annan sida
    redan har för samma logotyp, annars räknade ur logotypen. {} utan logotyp."""
    logo = MediaAsset.objects.filter(account_id=account_id, is_logo=True).order_by("-pk").first()
    if logo is None:
        return {}
    known = (
        LandingPage.objects.filter(account_id=account_id, logo_colors__asset=logo.pk)
        .values_list("logo_colors", flat=True)
        .first()
    )
    if isinstance(known, dict) and known.get("primary"):
        return dict(known)
    return colors_for_asset(logo)


def _set_logo_colors(account_id, colors):
    """Logotypens färger på kontots sidor. Utan färger (ingen logotyp, eller
    en utan tydliga färger) töms de, och en sida med paletten "logo" faller
    tillbaka till den blå, så att paletten inte står kvar med en gammal
    logotyps färger. Returnerar sidorna som bytte palett."""
    pages = LandingPage.objects.filter(account_id=account_id)
    fallback = []
    if not colors:
        fallback = list(pages.filter(palette=LandingPage.PALETTE_LOGO).values_list("pk", flat=True))
        LandingPage.objects.filter(pk__in=fallback).update(
            palette=LandingPage.PALETTE_BLUE, updated_at=timezone.now()
        )
    pages.update(logo_colors=colors or {})
    return fallback


def _alert_logo(account_id, user, what):
    """Logotypen står i sidhuvudet på varje sida, och dess färger på sidor
    med paletten "logo": en ändring syns direkt på live-sidorna, så byrån
    larmas (aldrig kunden; inte för ett demokonto)."""
    from . import exports, pagebuilder
    from .models import FlamingoAccount

    account = FlamingoAccount.objects.select_related("customer").get(pk=account_id)
    live = pagebuilder.live_campaigns(LandingPage.objects.filter(account_id=account_id))
    if not live:
        return False
    logo = MediaAsset.objects.filter(account_id=account_id, is_logo=True).order_by("-pk").first()
    who = pagebuilder.pages.who_text(user)
    return pagebuilder.alert_live_change(
        account,
        live,
        f"Flamingo: ny logotyp på live-sidorna ({account.customer.name}, "
        f"{'bild ' + str(logo.pk) if logo else 'ingen logotyp'})",
        [
            f"{what} för {account.customer.name}" + (f" av {who}." if who else "."),
            "Logotypen står i sidhuvudet, och dess färger på sidor med paletten Från "
            "logotypen. Ändringen syns direkt i de här kampanjerna, som är live:",
            *[f"- {c.name}: {exports.landing_page_url(c)}" for c in live],
        ],
    )


def set_logo(asset, user=None):
    """Bilden blir kontots logotyp (den förra slutar vara det), och färgerna
    ur den sparas på alla kontots sidor (LandingPage.logo_colors). Paletten
    ändras inte: det gör kunden med "Använd som palett". Har bilden inga
    tydliga färger töms sidornas färger (en sida med paletten "logo" blir
    blå). Byrån larmas när en kampanj är live. Returnerar färgerna."""
    with transaction.atomic():
        _lock_media(asset.account_id)
        MediaAsset.objects.filter(account_id=asset.account_id, is_logo=True).exclude(
            pk=asset.pk
        ).update(is_logo=False)
        MediaAsset.objects.filter(pk=asset.pk).update(is_logo=True)
    asset.is_logo = True
    colors = colors_for_asset(asset)
    _set_logo_colors(asset.account_id, colors)
    _alert_logo(asset.account_id, user, "Logotypen byttes")
    return colors


def unset_logo(asset, user=None):
    """Bilden är inte längre logotypen. Sidornas färger ur den töms, och en
    sida med paletten "logo" faller tillbaka till den blå (ingen sida står
    kvar med en borttagen logotyps färger). Byrån larmas när en kampanj är
    live."""
    was_logo = MediaAsset.objects.filter(pk=asset.pk, is_logo=True).update(is_logo=False)
    asset.is_logo = False
    if was_logo:
        _logo_removed(asset.account_id, user, "Logotypen togs bort")


def _logo_removed(account_id, user, what):
    if MediaAsset.objects.filter(account_id=account_id, is_logo=True).exists():
        return
    _set_logo_colors(account_id, {})
    _alert_logo(account_id, user, what)


def current_logo_colors(account):
    """Färgerna ur kontots logotyp som de står på sidorna, eller räknade nu."""
    return logo_colors_for_account(account.pk)


def apply_logo_palette(account, page=None, user=None):
    """Paletten "Från logotypen" på kontots sidor (eller bara page), med
    färgerna ur logotypen. Gäller direkt, också på sidor som är live: då
    larmas byrån (aldrig kunden; inte för ett demokonto). Returnerar antalet
    sidor. Kastar MediaError utan logotyp eller utan tydliga färger."""
    logo = MediaAsset.objects.filter(account=account, is_logo=True).order_by("-pk").first()
    if logo is None:
        raise MediaError("Välj en logotyp i arkivet först.")
    colors = colors_for_asset(logo)
    if not colors:
        raise MediaError(
            "Logotypen har inga tydliga färger (bara vitt, svart eller genomskinligt). "
            "Välj en av de färdiga paletterna i stället."
        )
    pages = LandingPage.objects.filter(account=account)
    if page is not None:
        pages = pages.filter(pk=page.pk)
    count = pages.update(
        palette=LandingPage.PALETTE_LOGO, logo_colors=colors, updated_at=timezone.now()
    )
    _alert_live_palette(account, pages, user, colors)
    return count


def _alert_live_palette(account, pages, user, colors):
    from . import exports, pagebuilder

    live = pagebuilder.live_campaigns(pages)
    if not live:
        return
    who = pagebuilder.pages.who_text(user)
    lines = [
        f"Paletten Från logotypen valdes för {account.customer.name}"
        + (f" av {who}." if who else "."),
        "Färgerna syns direkt i de här kampanjerna, som är live:",
        *[f"- {c.name}: {exports.landing_page_url(c)}" for c in live],
        "",
        "Kontrasten prövas mot WCAG AA när sidan ritas.",
    ]
    subject = (
        f"Flamingo: ny palett på en live-sida ({account.customer.name}, "
        f"{colors.get('primary', '')})"
    )
    pagebuilder.alert_live_change(account, live, subject, lines)


def _new_page_logo_colors(sender, instance, raw=False, **kwargs):
    """En ny sida får färgerna ur kontots logotyp (för paletten "logo"),
    hur den än skapas (pagebuilder.pages, demot, admin)."""
    if raw or not instance._state.adding or instance.logo_colors or not instance.account_id:
        return
    try:
        instance.logo_colors = logo_colors_for_account(instance.account_id)
    except Exception:  # noqa: BLE001 - en sida skapas alltid, med eller utan färger
        logger.exception("Flamingo: logotypens färger till en ny sida")


pre_save.connect(
    _new_page_logo_colors, sender=LandingPage, dispatch_uid="flamingo_new_page_logo_colors"
)


# -- Var en bild används ------------------------------------------------------


@dataclass
class Use:
    page: LandingPage
    #: "utkastet", "den publicerade sidan" eller båda.
    where: str


def _is_media_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def media_ids_in(blocks, types=None):
    """Bildernas id i blocken: alla versioner, inte bara den aktiva (en
    version kan väljas igen, och schemat nekar en bild som inte finns).
    types: blocktyperna (standard sidornas TYPES; utskickens Brev skickar
    sina, apps/utskick/email/registry.py)."""
    from .pagebuilder.registry import ITEMS, MEDIA, TYPES

    types = TYPES if types is None else types
    ids = set()
    for block in blocks:
        block_type = types.get(block.get("type")) if isinstance(block, dict) else None
        if block_type is None:
            continue
        for version in block.get("versions") or []:
            fields = version.get("fields") if isinstance(version, dict) else None
            if not isinstance(fields, dict):
                continue
            for spec in block_type.fields:
                value = fields.get(spec.key)
                if spec.kind == MEDIA and _is_media_id(value):
                    ids.add(value)
                elif spec.kind == ITEMS and isinstance(value, list):
                    for item in value:
                        if not isinstance(item, dict):
                            continue
                        for sub in spec.items:
                            if sub.kind == MEDIA and _is_media_id(item.get(sub.key)):
                                ids.add(item[sub.key])
    return ids


def _uses(pages):
    out = {}
    for page in pages:
        draft = media_ids_in(page.draft_blocks)
        published = media_ids_in(page.published_blocks)
        for asset_id in draft | published:
            if asset_id in draft and asset_id in published:
                where = "utkastet och den publicerade sidan"
            elif asset_id in draft:
                where = "utkastet"
            else:
                where = "den publicerade sidan"
            out.setdefault(asset_id, []).append(Use(page=page, where=where))
    return out


def media_usage(account):
    """{bildens id: [Use]} för kontots sidor, utkast och publicerat."""
    return _uses(LandingPage.objects.filter(account=account).order_by("name", "pk"))


def _utskick_uses(asset):
    """Namnen på utskicken som inte är skickade och använder bilden
    (utkast, schemalagda, pågående och pausade; apps/utskick C.2). Raderna
    låses under prövningen, som sidorna. Ett skickat mejl hindrar inte:
    dess bilder ligger kvar som EmailImage (asset blir null)."""
    from apps.utskick.email import images

    return images.used_by(asset.account_id, lock=True).get(asset.pk, [])


def delete_asset(asset, user=None):
    """Ta bort bilden (och filerna, när borttagningen är sparad). Används
    den på en sida, i utkastet eller det publicerade, eller i ett utskick
    som inte är skickat, nekas det med MediaInUse som säger var. Sidorna
    och utskicken låses under prövningen. Var den logotypen gäller samma
    sak som unset_logo (färgerna töms, byrån larmas om en kampanj är live)."""
    was_logo = asset.is_logo
    with transaction.atomic():
        pages = list(
            LandingPage.objects.select_for_update()
            .filter(account_id=asset.account_id)
            .order_by("name", "pk")
        )
        uses = _uses(pages).get(asset.pk, [])
        utskick = _utskick_uses(asset)
        if uses or utskick:
            raise MediaInUse(uses, utskick=utskick)
        asset.delete()
    if was_logo:
        _logo_removed(asset.account_id, user, "Logotypen togs bort ur mediaarkivet")


# ---------------------------------------------------------------------------
# Bilderna från hemsidan
# ---------------------------------------------------------------------------

#: Högst så många bildadresser per läsning.
SITE_CANDIDATES = 24
#: Så många obekräftade miniatyrer sparas per konto; de äldsta tas bort.
SITE_CANDIDATES_KEEP = 48
#: Största bild som hämtas för en miniatyr.
SITE_THUMB_MAX_BYTES = 3 * 1024 * 1024
#: Mindre än så (längsta sidan) är en ikon eller en spårpixel, inte en bild.
MIN_SIDE = 200
#: En trolig logotyp (eller hemsidans ikon) får vara mindre.
MIN_LOGO_SIDE = 96
#: Sekunder för miniatyrerna, räknat från när sidorna är lästa. De hämtas
#: medan AI-förslaget skrivs, och aldrig efter läsningens egen tidsgräns.
SITE_IMAGE_BUDGET = 6.0
#: Trådarna hämtar bara filerna; miniatyrerna avkodas en i taget i finish(),
#: högst SITE_DECODE_BUDGET sekunder efter att hämtningen tog slut.
SITE_WORKERS = 4
SITE_DECODE_BUDGET = 4.0
#: En miniatyr avkodas aldrig ur en större bild än så (en JPEG i den skala
#: den avkodas i). Hemsidornas bilder är sällan större.
CANDIDATE_MAX_PIXELS = 8_000_000
#: Hämtningen till arkivet: så många filer hämtas samtidigt, och de avkodas
#: en i taget när de kommit (högst IMPORT_WORKERS + 1 filer i minnet).
IMPORT_WORKERS = 2
#: Hämtningen till arkivet: bilder per konto och dag, och tid per gång.
SITE_IMPORT_DAILY_MAX = 50
IMPORT_BUDGET = 25.0
USAGE_SITE_IMPORT = "site_import"

#: Lat laddning först: src är då ofta en platshållare (data: eller en pixel).
_SRC_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-orig-src", "src")
_SRCSET_ATTRS = ("srcset", "data-srcset", "data-lazy-srcset")
_OG_IMAGE = ("og:image", "og:image:url", "og:image:secure_url", "twitter:image")
_ICON_RELS = frozenset(("apple-touch-icon", "apple-touch-icon-precomposed"))
_SKIP_HINTS = ("facebook.com/tr", "google-analytics", "doubleclick", "/pixel", "spacer.gif")


def _srcset_largest(value):
    """Den största bilden i ett srcset ("a.jpg 480w, b.jpg 1080w" eller
    "a.jpg 1x, b.jpg 2x"), enligt HTML-standardens sätt att läsa listan."""
    best, best_score = "", -1.0
    pos, n = 0, len(value)
    while pos < n:
        while pos < n and (value[pos].isspace() or value[pos] == ","):
            pos += 1
        start = pos
        while pos < n and not value[pos].isspace():
            pos += 1
        url, descriptor = value[start:pos], ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            start = pos
            while pos < n and value[pos] != ",":
                pos += 1
            descriptor = value[start:pos].strip()
        if not url:
            continue
        score = 1.0
        match = re.match(r"^(\d+(?:\.\d+)?)([wx])$", descriptor.split()[0]) if descriptor else None
        if match:
            number = float(match.group(1))
            score = number if match.group(2) == "w" else number * 1000
        if score > best_score:
            best, best_score = url, score
    return best


def _clean_url(raw, base_url):
    raw = (raw or "").strip()
    if not raw or raw.lower().startswith(("data:", "javascript:", "blob:", "about:")):
        return ""
    url = urljoin(base_url, raw)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.path.lower().endswith((".svg", ".svgz", ".ico")):
        return ""
    url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    if len(url) > 500 or any(hint in url.lower() for hint in _SKIP_HINTS):
        return ""
    return url


def _declared_small(attrs):
    """width och height i taggen säger att bilden är pyttestor (spårpixel)."""
    sizes = []
    for name in ("width", "height"):
        match = re.match(r"^\s*(\d+)", attrs.get(name) or "")
        if match:
            sizes.append(int(match.group(1)))
    return bool(sizes) and max(sizes) < 48


def image_ref(tag, attrs, base_url, in_nav=False):
    """En bildadress ur en tagg, för parsern i scan.py, eller None.

    {"url", "kind": "logo" | "icon" | "og" | "img", "logo": bool, "alt"}.
    img och source: den största i srcset, annars src (och de vanliga
    lazy-varianterna). En trolig logotyp: "logo" i adressen, alt, class
    eller id, eller "brand" i sidhuvudet eller menyn. og:image och
    apple-touch-icon räknas också."""
    if tag == "meta":
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name not in _OG_IMAGE:
            return None
        url = _clean_url(attrs.get("content"), base_url)
        return {"url": url, "kind": "og", "logo": False, "alt": ""} if url else None
    if tag == "link":
        rels = set((attrs.get("rel") or "").lower().split())
        if not rels & _ICON_RELS:
            return None
        url = _clean_url(attrs.get("href"), base_url)
        return {"url": url, "kind": "icon", "logo": True, "alt": ""} if url else None
    if tag not in ("img", "source"):
        return None
    if "svg" in (attrs.get("type") or "").lower() or _declared_small(attrs):
        return None
    url = ""
    for name in _SRCSET_ATTRS:
        if attrs.get(name):
            url = _clean_url(_srcset_largest(attrs[name]), base_url)
            if url:
                break
    if not url and tag == "img":
        for name in _SRC_ATTRS:
            url = _clean_url(attrs.get(name), base_url)
            if url:
                break
    if not url:
        return None
    alt = " ".join((attrs.get("alt") or "").split())[:200]
    hint = " ".join(
        [urlsplit(url).path, alt, attrs.get("class") or "", attrs.get("id") or ""]
    ).lower()
    logo = "logo" in hint or (in_nav and ("brand" in hint or "logga" in hint))
    return {"url": url, "kind": "logo" if logo else "img", "logo": logo, "alt": alt}


_KIND_ORDER = {"logo": 0, "icon": 1, "og": 2, "img": 3}


def site_candidates(pages, limit=SITE_CANDIDATES):
    """Upp till limit bildadresser ur sidorna som redan lästs (inga nya
    hämtningar): troliga logotyper först, sedan hemsidans ikon, og:image
    och bilderna i den ordning de står. Varje adress en gång."""
    refs, seen = [], set()
    for page_index, page in enumerate(pages):
        for position, ref in enumerate(getattr(page, "images", None) or []):
            if not isinstance(ref, dict) or not ref.get("url") or ref["url"] in seen:
                continue
            seen.add(ref["url"])
            refs.append((_KIND_ORDER.get(ref.get("kind"), 3), page_index, position, ref))
    refs.sort(key=lambda item: item[:3])
    return [ref for *_, ref in refs[:limit]]


@dataclass
class Thumb:
    url: str
    width: int
    height: int
    data: bytes
    digest: str


def _thumb_from(data):
    """(bredd, höjd, miniatyren som WebP) för en bild från hemsidan: högst
    CANDIDATE_MAX_PIXELS avkodas, och bilden skalas ner direkt (_decode).
    Bredden och höjden är originalets, vänd efter EXIF."""
    with _DECODE:
        image, (width, height) = _decode(
            data, max_side=MEDIA_THUMB_SIDE * 2, max_pixels=CANDIDATE_MAX_PIXELS
        )
        if (image.width > image.height) != (width > height) and image.width != image.height:
            width, height = height, width  # vänd efter EXIF
        return width, height, _webp(make_thumb(image), THUMB_QUALITY)


def _peek_ok(body):
    """Ser filen ut som en bild som får avkodas för en miniatyr? Bara
    filens huvud läses (inget avkodas), så att en fil som ändå skulle
    nekas inte ligger kvar i minnet."""
    try:
        probe = Image.open(io.BytesIO(body))
        if probe.format not in ACCEPTED_FORMATS:
            return False
        width, height = probe.size
        limit = MAX_PIXELS if probe.format == "JPEG" else _pixel_limit(probe, CANDIDATE_MAX_PIXELS)
        return 0 < width * height <= limit
    except Exception:  # noqa: BLE001 - allt som inte går att läsa hoppas över
        return False


def _fetch_body(ref, deadline, fetcher=None):
    """Tråden: bilden hämtas (SSRF-skyddet i analyzer.fetch, högst
    SITE_THUMB_MAX_BYTES, bara den tid som är kvar). Ingen avkodning här:
    (adress, filen) eller None om det inte gick."""
    remaining = deadline - time.monotonic()
    if remaining < 0.3:
        return None
    try:
        sida = (fetcher or fetch)(ref["url"], max_bytes=SITE_THUMB_MAX_BYTES, time_limit=remaining)
        body = getattr(sida, "body", b"") or b""
        if getattr(sida, "truncated", False) or not body:
            return None
        kind = (sida.headers.get("content-type") or "").lower()
        if "svg" in kind or "html" in kind or "xml" in kind:
            return None
    except AnalysError as exc:
        logger.info("Flamingo: bilden %s hoppades över: %s", ref["url"], exc)
        return None
    except Exception:  # noqa: BLE001 - en bild stoppar aldrig läsningen
        logger.exception("Flamingo: bilden %s gick inte att hämta", ref["url"])
        return None
    if not _peek_ok(body):
        logger.info("Flamingo: bilden %s hoppades över: för stor eller inte en bild", ref["url"])
        return None
    return ref["url"], body


def _thumb_for(url, body):
    """Miniatyren av en hämtad fil (avkodas här, en i taget), eller None."""
    try:
        width, height, data = _thumb_from(body)
    except MediaError as exc:
        logger.info("Flamingo: bilden %s hoppades över: %s", url, exc)
        return None
    except Exception:  # noqa: BLE001 - en bild stoppar aldrig läsningen
        logger.exception("Flamingo: bilden %s gick inte att läsa", url)
        return None
    return Thumb(url, width, height, data, hashlib.sha256(body).hexdigest())


def _fetch_thumb(ref, deadline, fetcher=None):
    """Hämta och gör en miniatyr direkt (_fetch_body och _thumb_for i samma
    tråd). None om det inte gick."""
    fetched = _fetch_body(ref, deadline, fetcher)
    return _thumb_for(*fetched) if fetched is not None else None


@dataclass
class SiteImageFetch:
    """Miniatyrerna från en läsning, hämtade i trådar medan resten av
    läsningen pågår. start() gör inga anrop i den här tråden; finish()
    väntar högst till deadline, avkodar det som hann hämtas en bild i taget
    (högst SITE_DECODE_BUDGET sekunder) och sparar miniatyrerna."""

    refs: list = field(default_factory=list)
    deadline: float = 0.0
    known: set = field(default_factory=set)
    futures: dict = field(default_factory=dict)
    executor: object = None

    @classmethod
    def start(cls, account, pages, *, deadline, fetcher=None):
        """Börja hämta miniatyrerna. fetcher är analyzer.fetch (läsningen
        skickar sin egen, så att samma skydd och samma attrapp i testerna
        gäller sidorna och bilderna). Ett demokonto hämtar ingenting."""
        job = cls(deadline=min(deadline, time.monotonic() + SITE_IMAGE_BUDGET))
        if account.is_demo:
            return job
        try:
            job.refs = site_candidates(pages)
            job.known = set(
                SiteImageCandidate.objects.filter(
                    account=account, source_url__in=[r["url"] for r in job.refs]
                ).values_list("source_url", flat=True)
            )
            todo = [ref for ref in job.refs if ref["url"] not in job.known]
            if todo:
                job.executor = ThreadPoolExecutor(
                    max_workers=SITE_WORKERS, thread_name_prefix="flamingo-bilder"
                )
                for ref in todo:
                    future = job.executor.submit(_fetch_body, ref, job.deadline, fetcher)
                    job.futures[future] = ref
        except Exception:  # noqa: BLE001 - bilderna stoppar aldrig läsningen
            logger.exception("Flamingo: bilderna från hemsidan hämtas inte")
            job.cancel()
            job.refs, job.futures = [], {}
        return job

    def cancel(self):
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None

    def finish(self, account, now=None):
        """Spara miniatyrerna som hann klart. Kastar aldrig: bilderna är en
        bisak till läsningen. Returnerar antalet nya kandidater."""
        try:
            return self._store(account, now or timezone.now())
        except Exception:  # noqa: BLE001
            logger.exception("Flamingo: bilderna från hemsidan sparades inte")
            return 0
        finally:
            self.cancel()

    def _store(self, account, now):
        bodies = {}
        if self.futures:
            done, _ = wait(self.futures, timeout=max(0.0, self.deadline - time.monotonic()))
            for future in done:
                fetched = future.result()
                if fetched is not None:
                    bodies[fetched[0]] = fetched[1]
        self.cancel()
        # Avkodas här, en i taget och i läsningens ordning, inom en egen tid.
        results = {}
        decode_until = time.monotonic() + SITE_DECODE_BUDGET
        for ref in self.refs:
            body = bodies.pop(ref["url"], None)
            if body is None:
                continue
            if time.monotonic() > decode_until:
                break
            thumb = _thumb_for(ref["url"], body)
            if thumb is not None:
                results[thumb.url] = thumb
        if self.known:
            SiteImageCandidate.objects.filter(account=account, source_url__in=self.known).update(
                found_at=now
            )
        created, digests = 0, set()
        for ref in self.refs:
            thumb = results.get(ref["url"])
            if thumb is None or thumb.digest in digests:
                continue
            smallest = MIN_LOGO_SIDE if ref.get("logo") else MIN_SIDE
            if max(thumb.width, thumb.height) < smallest:
                continue
            digests.add(thumb.digest)
            candidate = SiteImageCandidate(
                account=account,
                source_url=thumb.url,
                width=thumb.width,
                height=thumb.height,
                found_at=now,
                likely_logo=bool(ref.get("logo")),
                alt=sanitize_plain_text(ref.get("alt") or "", max_length=200),
            )
            candidate.thumb.save("tumme.webp", ContentFile(thumb.data), save=False)
            try:
                with transaction.atomic():
                    candidate.save()
            except IntegrityError:
                candidate.thumb.storage.delete(candidate.thumb.name)
                continue
            created += 1
        self._prune(account)
        return created

    @staticmethod
    def _prune(account):
        stale = SiteImageCandidate.objects.filter(
            account=account, imported_asset__isnull=True
        ).order_by("-found_at", "-pk")[SITE_CANDIDATES_KEEP:]
        for candidate in list(stale):
            candidate.delete()


@dataclass
class ImportResult:
    imported: list = field(default_factory=list)
    #: [(kandidaten, varför den inte hämtades)]
    failed: list = field(default_factory=list)


def _fetch_original(url, deadline):
    """Tråden: filen hämtas (ingen avkodning här; _import_one gör det)."""
    remaining = deadline - time.monotonic()
    if remaining < 0.5:
        raise MediaError("Hann inte hämtas. Försök igen.")
    try:
        sida = fetch(url, max_bytes=MAX_UPLOAD_BYTES, time_limit=remaining)
    except AnalysError:
        raise MediaError("Gick inte att hämta från hemsidan.") from None
    if getattr(sida, "truncated", False):
        raise MediaError(TOO_BIG)
    return getattr(sida, "body", b"") or b""


def site_alt(candidate):
    """Alternativtexten för en bild från hemsidan: sidans egen alt-text,
    annars ur filens namn."""
    return candidate.alt or alt_from_filename(urlsplit(candidate.source_url).path)


def import_candidates(account, ids, *, user=None, rights_confirmed=False, now=None):
    """Kundens valda bilder från hemsidan hämtas på riktigt (samma
    SSRF-skydd, högst MAX_UPLOAD_BYTES), kodas om som en uppladdning och
    sparas med källan "site", adressen och vem som intygade rätten. Kräver
    intyget (rights_confirmed). Högst SITE_IMPORT_DAILY_MAX bilder per konto
    och dag. Ett demokonto hämtar ingenting. Kastar MediaError när inget
    alls får hämtas; annars ImportResult med det som gick och inte gick."""
    if account.is_demo:
        raise MediaError(DEMO_REFUSED)
    if not rights_confirmed:
        raise MediaError(RIGHTS_REQUIRED)
    wanted = []
    for raw in ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in wanted:
            wanted.append(value)
    candidates = list(
        SiteImageCandidate.objects.filter(
            account=account, pk__in=wanted[:SITE_CANDIDATES], imported_asset__isnull=True
        ).order_by("-found_at", "-pk")
    )
    if not candidates:
        raise MediaError("Välj minst en bild att hämta.")
    room = MEDIA_MAX_PER_ACCOUNT - media_count(account)
    if room <= 0:
        raise MediaError(ARCHIVE_FULL)
    if len(candidates) > room:
        raise MediaError(
            f"Det får plats {room} bilder till i arkivet. Välj färre, eller ta bort bilder "
            "som inte används."
        )
    if not limits.reserve_daily(
        account, USAGE_SITE_IMPORT, SITE_IMPORT_DAILY_MAX, count=len(candidates), now=now
    ):
        left = SITE_IMPORT_DAILY_MAX - limits.daily_used(account, USAGE_SITE_IMPORT, now)
        if left <= 0:
            raise MediaError(
                f"I dag har {SITE_IMPORT_DAILY_MAX} bilder hämtats från hemsidan, och det är "
                "gränsen. Ladda upp bilderna i stället, eller hämta dem i morgon."
            )
        raise MediaError(f"I dag går det att hämta {left} bilder till från hemsidan.")

    result = ImportResult()
    deadline = time.monotonic() + IMPORT_BUDGET
    pool = ThreadPoolExecutor(max_workers=IMPORT_WORKERS, thread_name_prefix="flamingo-hamta")
    queue = list(candidates)
    running = {}
    with pool:
        while queue or running:
            # Högst IMPORT_WORKERS hämtningar åt gången: filerna (upp till
            # 15 MB var) ligger inte i minnet och väntar på avkodningen.
            while queue and len(running) < IMPORT_WORKERS:
                candidate = queue.pop(0)
                running[pool.submit(_fetch_original, candidate.source_url, deadline)] = candidate
            done, _ = wait(
                running,
                timeout=max(0.0, deadline - time.monotonic()) + 1,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                for future, candidate in running.items():
                    future.cancel()
                    result.failed.append((candidate, "Hann inte hämtas. Försök igen."))
                for candidate in queue:
                    result.failed.append((candidate, "Hann inte hämtas. Försök igen."))
                break
            for future in done:
                candidate = running.pop(future)
                _import_one(account, future, candidate, result, user=user, now=now)
    if result.failed:
        limits.release_daily(account, USAGE_SITE_IMPORT, count=len(result.failed), now=now)
    return result


def _import_one(account, future, candidate, result, *, user, now):
    """En hämtad bild avkodas (här, i anroparens tråd, en i taget) och
    sparas i arkivet; det som inte gick hamnar i result.failed."""
    try:
        processed = process_image(future.result())
        asset = store_image(
            account,
            processed,
            alt=site_alt(candidate),
            user=user,
            source=MediaAsset.SOURCE_SITE,
            source_url=candidate.source_url,
            rights_confirmed=True,
            now=now,
        )
    except MediaError as exc:
        result.failed.append((candidate, exc.message))
        return
    except Exception:  # noqa: BLE001 - en bild stoppar inte de andra
        logger.exception("Flamingo: bilden %s hämtades inte", candidate.source_url)
        result.failed.append((candidate, BROKEN))
        return
    candidate.imported_asset = asset
    candidate.save(update_fields=["imported_asset"])
    result.imported.append(asset)
