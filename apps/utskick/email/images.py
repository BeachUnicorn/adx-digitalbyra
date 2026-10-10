"""
Mejlens bilder (README F.4, C.2, E.7).

Mediaarkivet sparar WebP (apps/flamingo/media.py), som klassiska Outlook
inte visar. En EmailImage är en bild i mejlens format: JPEG med kvalitet
82, högst MAX_WIDTH bred (560 visas, dubbelt för skarpa skärmar), PNG när
källan har alfa (loggor). En genomskinlig bild läggs alltid på vitt, så att
en mörk logga inte försvinner när ett e-postprogram i mörkt läge byter
bakgrund (F.4). Videoblockets bild får en ritad spelknapp (Pillow), och
porträtten (kontaktperson, underskrift) beskärs kvadratiska. Avkodningen går
genom mediaarkivets semafor (media._DECODE): en bild åt gången i processen.

Raden återanvänds för (asset, purpose, width): bredden räknas ur bildens
mått innan något avkodas, så samma val ger samma rad. En rad som inte finns
görs av en tråd i taget per (asset, purpose, width) i processen (_making), och
raden prövas igen under det låset: flera samtidiga besökare på en sida med
loggan (branding.py) läser och avkodar filen en gång, inte en gång var. Låset
hålls inte av en tråd som väntar på semaforen för en annan bild, så två trådar
kan inte låsa varandra. Filen ligger under
MEDIA_ROOT/utskick-img/<slump>/ (publik, absolut adress i mejlet) och går
med raden (post_delete i models.py). När MediaAsset tas bort blir asset
null och filen ligger kvar, så att skickade mejl behåller sina bilder tills
retentionen tar raden (E.7, purge_unused).

    MAX_WIDTH = 1120
    JPEG_QUALITY = 82
    rendition(asset, purpose, *, width=MAX_WIDTH) -> EmailImage
                                     kastar media.MediaError när bilden inte går att läsa
    logo_asset(account) -> MediaAsset | None
                                     kontots logga i mediaarkivet (is_logo)
    logo_for(account) -> EmailImage | None
                                     PNG av MediaAsset(is_logo=True), 80 px hög fil
                                     (40 px visas), högst 440 bred fil (220 visas)
    display_size(width, height) -> (bredd, höjd)
                                     loggans visade mått för en fil i width x height:
                                     högst 40 hög och 220 bred, proportionerna kvar,
                                     aldrig uppskalad (mejlets sidhuvud, branding.py)
    absolute_url(image) -> str       exports.landing_base_url() + file.url
    used_by(account_id, *, lock=False) -> {asset_id: [utskickens namn]}
                                     bilderna i utkast, schemalagda, pågående och
                                     pausade utskick (alla versioner); media.delete_asset
                                     frågar och kastar MediaInUse (C.2)
    uses(account_id) -> set[int]     samma id:n
    purge_unused(now=None) -> dict   retentionen (E.7): rader som inget öppet utskick
                                     och ingen kvarvarande mottagare behöver
"""

import io
import logging
import threading
from contextlib import contextmanager
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from PIL import Image, ImageDraw, ImageOps

MAX_WIDTH = 1120
JPEG_QUALITY = 82
#: Loggans fil: 80 px hög (40 visas), högst 440 bred (220 visas).
LOGO_HEIGHT = 80
LOGO_MAX_WIDTH = 440
#: Loggan visas högst så här hög och bred (mejlets sidhuvud och mottagarens sidor).
LOGO_SHOWN_HEIGHT = 40
LOGO_SHOWN_MAX_WIDTH = 220
#: Porträttets fil: 112 px i kvadrat (56 visas).
AVATAR_SIDE = 112
#: En rendition som skapats men ännu inte sparats i ett utkast får ligga så
#: här länge innan retentionen tar den.
GRACE = timedelta(days=7)
PURGE_BATCH = 500

CONTENT = "content"
LOGO = "logo"
VIDEO = "video"
AVATAR = "avatar"
PURPOSES = (CONTENT, LOGO, VIDEO, AVATAR)

logger = logging.getLogger(__name__)


def _open_statuses():
    from ..models import Utskick

    S = Utskick.Status
    return (
        S.DRAFT,
        S.SCHEDULED,
        S.FREEZING,
        S.SENDING,
        S.PAUSED_CAP,
        S.PAUSED_HEALTH,
        S.PAUSED,
    )


def target_size(width, height, purpose, max_width=MAX_WIDTH):
    """Filens mått för en bild i width x height (aldrig uppskalad)."""
    width, height = max(1, int(width or 1)), max(1, int(height or 1))
    if purpose == AVATAR:
        side = min(AVATAR_SIDE, width, height)
        return side, side
    if purpose == LOGO:
        scale = min(1.0, LOGO_HEIGHT / height, LOGO_MAX_WIDTH / width)
    else:
        scale = min(1.0, max(1, int(max_width or MAX_WIDTH)) / width)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _flatten(image):
    """RGB på vitt (alfa bort); True när bilden hade alfa."""
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        white = Image.new("RGB", rgba.size, (255, 255, 255))
        white.paste(rgba, mask=rgba.getchannel("A"))
        return white, True
    return image.convert("RGB"), False


def _play_button(image):
    """En vit spelknapp med en mörk triangel mitt i bilden, med en mjuk
    ring så att den syns på ljusa bilder."""
    width, height = image.size
    radius = max(18, round(min(width, height) * 0.11))
    cx, cy = width / 2, height / 2
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    ring = radius + max(2, round(radius * 0.12))
    draw.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), fill=(0, 0, 0, 46))
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(255, 255, 255, 255))
    side = radius * 0.78
    draw.polygon(
        [
            (cx - side * 0.38, cy - side * 0.55),
            (cx - side * 0.38, cy + side * 0.55),
            (cx + side * 0.58, cy),
        ],
        fill=(17, 17, 17, 255),
    )
    base = image.convert("RGBA")
    base.alpha_composite(overlay)
    return base.convert("RGB")


def _encode(image, fmt):
    buffer = io.BytesIO()
    if fmt == "png":
        image.save(buffer, "PNG", optimize=True)
    else:
        image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return buffer.getvalue()


def _render(asset, purpose, size):
    """(bytes, format, bredd, höjd) för bilden i mejlens format."""
    from apps.flamingo import media

    image_file = asset.file
    with image_file.open("rb") as handle:
        data = handle.read(media.MAX_UPLOAD_BYTES + 1)
    with media._DECODE:
        image = media.open_image(data, draft_side=max(size))
        image, had_alpha = _flatten(image)
        if purpose == AVATAR:
            image = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
        elif image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        if purpose == VIDEO:
            image = _play_button(image)
        fmt = "png" if purpose == LOGO or (had_alpha and purpose == CONTENT) else "jpeg"
        return _encode(image, fmt), fmt, image.width, image.height


#: _making: ett lås per rendition som görs just nu i processen, med antalet
#: trådar som håller eller väntar på det (låset tas bort när ingen gör det).
_MAKING = {}
_MAKING_GUARD = threading.Lock()


@contextmanager
def _making(key):
    """En tråd i taget gör renditionen key = (asset, purpose, width) i
    processen; de andra väntar och hittar sedan raden (rendition)."""
    with _MAKING_GUARD:
        entry = _MAKING.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _MAKING_GUARD:
            entry[1] -= 1
            if entry[1] <= 0:
                _MAKING.pop(key, None)


def _existing(asset, purpose, width):
    from ..models import EmailImage

    row = (
        EmailImage.objects.filter(asset=asset, purpose=purpose, width=width).order_by("pk").first()
    )
    return row if row is not None and row.file else None


def rendition(asset, purpose, *, width=MAX_WIDTH):
    """EmailImage för bilden i mejlens format, skapad en gång per (asset,
    purpose, bredd). Kastar media.MediaError när filen inte går att läsa."""
    from apps.flamingo import media

    from ..models import EmailImage

    if purpose not in PURPOSES:
        raise ValueError(f"Okänt syfte för en bild i mejlet: {purpose}")
    if not asset.file:
        raise media.MediaError(media.BROKEN)
    size = target_size(asset.width, asset.height, purpose, width)
    existing = _existing(asset, purpose, size[0])
    if existing is not None:
        return existing
    with _making((asset.pk, purpose, size[0])):
        # En annan tråd i processen kan ha gjort samma rendition medan den här
        # väntade (flera besökare samtidigt): den gäller, och filen läses inte
        # en gång till.
        existing = _existing(asset, purpose, size[0])
        if existing is not None:
            return existing
        try:
            data, fmt, _file_width, file_height = _render(asset, purpose, size)
        except media.MediaError:
            raise
        except Exception:  # noqa: BLE001 - en fil som inte går att läsa är en trasig bild
            logger.warning("Utskick: bilden %s gick inte att göra om för mejl", asset.pk)
            raise media.MediaError(media.BROKEN) from None
        row = EmailImage(
            account_id=asset.account_id,
            asset=asset,
            purpose=purpose,
            format=EmailImage.Format.PNG if fmt == "png" else EmailImage.Format.JPEG,
            width=size[0],
            height=file_height,
            bytes=len(data),
        )
        row.file.save("bild.png" if fmt == "png" else "bild.jpg", ContentFile(data), save=False)
        try:
            with transaction.atomic():
                row.save()
        except IntegrityError:
            # En annan process (eller en transaktion som inte var klar när raden
            # prövades) skapade samma rendition samtidigt: deras rad gäller.
            row.file.storage.delete(row.file.name)
            return EmailImage.objects.get(asset=asset, purpose=purpose, width=size[0])
        return row


def logo_asset(account):
    """Kontots logga i mediaarkivet (den senaste med is_logo), eller None."""
    from apps.flamingo.models import MediaAsset

    return MediaAsset.objects.filter(account=account, is_logo=True).order_by("-pk").first()


def logo_for(account):
    """Loggans rendition (PNG på vitt), eller None utan logga eller när
    filen inte går att läsa."""
    from apps.flamingo import media

    logo = logo_asset(account)
    if logo is None:
        return None
    try:
        return rendition(logo, LOGO)
    except media.MediaError:
        return None


def display_size(width, height):
    """(bredd, höjd) som loggan visas i, för en fil i width x height: högst
    LOGO_SHOWN_HEIGHT hög och LOGO_SHOWN_MAX_WIDTH bred med proportionerna
    kvar, och aldrig större än filen (en liten logga blir annars suddig).
    width- och height-attributen ska stämma med det som visas: Outlook läser
    bara dem."""
    width, height = max(1, int(width or 1)), max(1, int(height or 1))
    shown_height = min(LOGO_SHOWN_HEIGHT, height)
    shown_width = round(width * shown_height / height)
    if shown_width > LOGO_SHOWN_MAX_WIDTH:
        shown_width = LOGO_SHOWN_MAX_WIDTH
        shown_height = round(height * LOGO_SHOWN_MAX_WIDTH / width)
    return max(1, shown_width), max(1, shown_height)


def absolute_url(image):
    """Bildens adress med schema och värd (mejl har ingen bas)."""
    from apps.flamingo.exports import landing_base_url

    url = image.file.url
    if url.startswith(("https://", "http://")):
        return url
    return landing_base_url().rstrip("/") + url


def used_by(account_id, *, lock=False):
    """{asset_id: [utskickens namn]} för kontots utskick som inte är
    skickade (utkast, schemalagda, pågående och pausade), alla versioner.
    lock: raderna låses (inom en transaktion, media.delete_asset)."""
    from ..models import Utskick
    from .blocks import doc_blocks, media_ids

    rows = Utskick.objects.filter(account_id=account_id, status__in=_open_statuses())
    if lock:
        rows = rows.select_for_update()
    out = {}
    for utskick in rows.only("pk", "name", "email_doc", "status").order_by("name", "pk"):
        for asset_id in sorted(media_ids(doc_blocks(utskick))):
            names = out.setdefault(asset_id, [])
            if utskick.name not in names:
                names.append(utskick.name)
    return out


def uses(account_id):
    """MediaAsset-id som kontots utskick som inte är skickade använder."""
    return set(used_by(account_id))


def snapshot_image_ids(snapshot):
    """EmailImage-id:n som ett fryst mejl pekar på (render.snapshot)."""
    if not isinstance(snapshot, dict):
        return set()
    return {i for i in snapshot.get("image_ids") or [] if isinstance(i, int)}


def purge_unused(now=None):
    """Retentionen (E.7): en EmailImage tas bort när inget öppet utskick
    använder dess bild och inget skickat mejl med mottagare kvar pekar på
    den. Loggan som gäller nu och rader yngre än GRACE står kvar. Filen går
    med raden. Returnerar {"deleted": n}."""
    from ..models import EmailImage, Recipient, Utskick
    from .blocks import doc_blocks, media_ids

    now = now or timezone.now()
    keep_assets, keep_images = set(), set()
    for utskick in Utskick.objects.filter(status__in=_open_statuses()).only(
        "pk", "email_doc", "email_snapshot"
    ):
        keep_assets |= media_ids(doc_blocks(utskick))
        keep_images |= snapshot_image_ids(utskick.email_snapshot)
    with_recipients = Exists(Recipient.objects.filter(utskick_id=OuterRef("pk")))
    for snapshot in (
        Utskick.objects.filter(status__in=Utskick.FINISHED)
        .filter(with_recipients)
        .values_list("email_snapshot", flat=True)
    ):
        keep_images |= snapshot_image_ids(snapshot)
    candidates = (
        EmailImage.objects.filter(created_at__lt=now - GRACE)
        .exclude(pk__in=keep_images)
        .exclude(asset_id__in=keep_assets)
        .exclude(purpose=LOGO, asset__is_logo=True)
        .order_by("pk")
    )
    deleted = 0
    while True:
        batch = list(candidates[:PURGE_BATCH])
        if not batch:
            break
        for row in batch:
            row.delete()
            deleted += 1
        if len(batch) < PURGE_BATCH:
            break
    return {"deleted": deleted}
