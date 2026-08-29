"""
Central security helpers for ADX.

Goal: every untrusted, outward-facing value (block content, menu labels,
headings, footer text) must pass through a sanitizer declared here before it
reaches a template. There is deliberately no per-view freelancing.

Ported from the ADX `apps/common/security.py`. For now this covers the pieces
the public site needs:

* HTML sanitization (rich-text fields)            - `sanitize_rich_html`
* Plain-text normalization (headlines, labels, …) - `sanitize_plain_text`
* URL scheme allowlist (CTAs, nav links)          - `validate_url`

The block-data field schema, colour/OKLCH validators and image-upload checks
from ADX are intentionally NOT ported yet - they belong with the /manage/
editing endpoints and will be added when those land.

Anything that produces user-facing HTML must also escape variable
substitutions - see `apps.website.templatetags.render_context`.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import nh3
from django.core.exceptions import ValidationError
from django.utils.html import strip_tags as _strip_tags
from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_RICH_HTML_LENGTH = 50_000
MAX_PLAIN_TEXT_LENGTH = 1_000  # individual fields override this
MAX_URL_LENGTH = 500
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_IMAGE_PIXELS = 60_000_000  # 60 MP - guards against decompression bombs


# ---------------------------------------------------------------------------
# Typographic normalization (AI-generated text cleanup)
# ---------------------------------------------------------------------------

# Characters commonly inserted by AI (ChatGPT, Claude, etc.) that look
# "off" in casual Swedish copy and reveal machine origin. We normalise them
# to their plain-keyboard equivalents at save time so stored content never
# contains them regardless of source.
_NORMALIZE_MAP = str.maketrans(
    {
        "\u2014": "-",  # em dash → hyphen
        "\u2013": "-",  # en dash → hyphen
        "\u201c": '"',  # left double curly quote
        "\u201d": '"',  # right double curly quote
        "\u2018": "'",  # left single curly quote
        "\u2019": "'",  # right single curly quote
        "\u2026": "...",  # ellipsis → three dots
    }
)


def _normalize_typography(text: str) -> str:
    """Replace fancy/AI-inserted typographic characters with plain equivalents."""
    return text.translate(_NORMALIZE_MAP) if text else text


# Publik yta för normaliseringen. Sanerarna nedan kör den alltid vid spara,
# men innehåll som INTE går genom redigeringsvägarna (seed_site,
# import_site_data, direkta ORM-skrivningar) behöver kunna anropa samma
# regeluppsättning - och vakttesterna behöver teckenmängden. En källa.
AI_TYPOGRAPHY_CHARS = frozenset(chr(cp) for cp in _NORMALIZE_MAP)


def normalize_typography(text: str | None) -> str | None:
    """Publik variant av typografinormaliseringen (samma karta som sanerarna)."""
    return _normalize_typography(text) if text else text


def normalize_json(value):
    """Normalisera alla strängar rekursivt i en JSON-struktur (dict/list/str).

    Nycklar lämnas orörda - de är schemafältnamn, inte innehåll.
    """
    if isinstance(value, str):
        return _normalize_typography(value)
    if isinstance(value, list):
        return [normalize_json(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize_json(v) for k, v in value.items()}
    return value


# Tags allowed in rich-text fields (article body / FAQ answers).
# Deliberately conservative: no <img>, <iframe>, <video>, <style>, <script>,
# no class/id/style attributes, no event handlers.
_RICH_HTML_TAGS = {
    "p",
    "br",
    "hr",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "code",
    "sub",
    "sup",
    "h2",
    "h3",
    "h4",
    "ul",
    "ol",
    "li",
    "blockquote",
    "a",
}

_RICH_HTML_ATTRIBUTES = {
    "a": {"href", "title", "target"},
}

_RICH_HTML_URL_SCHEMES = {"http", "https", "mailto", "tel"}


# Basic rich text: bold, italic, links only. Everything else (headings,
# lists, quotes, code) is stripped. Used for customer-facing fields that
# were historically abused with heavy markup.
_BASIC_HTML_TAGS = {"p", "br", "strong", "b", "em", "i", "a"}
_BASIC_HTML_ATTRIBUTES = {"a": {"href", "title", "target"}}


def sanitize_rich_html(html: str | None, *, max_length: int = MAX_RICH_HTML_LENGTH) -> str:
    """
    Sanitize rich HTML using a strict allowlist.

    * Strips <script>, <style>, <iframe>, on* handlers, javascript: URIs.
    * Forces target=_blank links to carry rel="noopener noreferrer".
    * Normalises AI-inserted typographic characters.
    * Truncates to `max_length` characters of *output* HTML.
    """
    if not html:
        return ""

    cleaned = nh3.clean(
        str(html),
        tags=_RICH_HTML_TAGS,
        attributes=_RICH_HTML_ATTRIBUTES,
        url_schemes=_RICH_HTML_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )

    cleaned = _normalize_typography(cleaned)

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]

    return cleaned


def sanitize_rich_html_basic(html: str | None, *, max_length: int = MAX_RICH_HTML_LENGTH) -> str:
    """
    Sanitize rich HTML down to a minimal allowlist: bold, italic, links.

    Headings, lists, quotes, code, etc. are stripped to plain paragraphs.
    Normalises AI-inserted typographic characters.
    """
    if not html:
        return ""

    cleaned = nh3.clean(
        str(html),
        tags=_BASIC_HTML_TAGS,
        attributes=_BASIC_HTML_ATTRIBUTES,
        url_schemes=_RICH_HTML_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )

    cleaned = _normalize_typography(cleaned)

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length]

    return cleaned


def sanitize_plain_text(value: str | None, *, max_length: int = MAX_PLAIN_TEXT_LENGTH) -> str:
    """
    Strip ALL HTML tags from a value and collapse whitespace.

    Used for fields like headlines / labels that must never contain markup.
    Returns a regular `str` (not SafeString) - Django auto-escape handles
    output encoding.
    """
    if not value:
        return ""

    # Two-pass: nh3 with empty allowlist + Django strip_tags. nh3 alone
    # decodes entities like &lt; back to "<"; a final strip_tags ensures
    # nothing slips through.
    text = nh3.clean(str(value), tags=set(), attributes={}, strip_comments=True)
    text = _strip_tags(text)

    # nh3 escapes special chars (& -> &amp;). Since this returns PLAIN text and
    # output is always re-encoded by Django's autoescape, decode entities back
    # to literal characters here to avoid double-escaping (&amp;amp;). Safe:
    # all tags are already stripped, so no markup can be reintroduced.
    import html as _html

    text = _html.unescape(text)

    # Normalise AI-typical typographic characters to plain equivalents.
    text = _normalize_typography(text)

    # Collapse newlines and tabs to a single space, but preserve consecutive
    # spaces — they may be intentional (e.g. "Värme  &  kyla").
    text = re.sub(r"[\t\n\r\f\v]+", " ", text).strip()

    if len(text) > max_length:
        text = text[:max_length].rstrip()

    return text


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

_ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}
_DANGEROUS_URL_PREFIXES = ("javascript:", "data:", "vbscript:", "file:")


def validate_url(value: str | None, *, max_length: int = MAX_URL_LENGTH) -> str:
    """
    Validate a user-supplied URL.

    Returns the cleaned URL or empty string for empty input.
    Accepts:
      * Absolute http/https URLs
      * mailto: / tel:
      * Site-relative paths (start with `/` and not `//host`)
      * Fragment links (start with `#`)

    Rejects everything else with `ValidationError`.
    """
    if not value:
        return ""

    url = str(value).strip()

    if len(url) > max_length:
        raise ValidationError(_("URL is too long."))

    # Defense in depth - explicit deny list before parsing.
    lowered = url.lower()
    for bad in _DANGEROUS_URL_PREFIXES:
        if lowered.startswith(bad):
            raise ValidationError(_("URL scheme is not allowed."))

    # Fragment-only links are fine
    if url.startswith("#"):
        return url

    # Site-relative paths: must start with single `/`, not `//` (protocol-relative).
    if url.startswith("/"):
        if url.startswith("//"):
            raise ValidationError(_("Protocol-relative URLs are not allowed."))
        return url

    # Otherwise it must be a full URL with an allowed scheme
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
        raise ValidationError(_("URL scheme is not allowed."))

    if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
        raise ValidationError(_("URL is missing a host."))

    return url


# ---------------------------------------------------------------------------
# Media reference validation
# ---------------------------------------------------------------------------


def validate_media_id(value) -> int | str:
    """
    Validate a media reference stored on a block (`image_id`).

    Blocks reference images by `MediaFile` ID - never by raw URL. Returns the
    integer ID when it points at an existing MediaFile, or "" to clear it.
    Raises ValidationError for malformed or non-existent references.

    (Single-site: no tenant scoping. When this becomes multi-tenant, add a
    tenant filter here - see ADX `validate_block_field` media_id handling.)
    """
    if value in (None, ""):
        return ""
    try:
        mid = int(value)
    except (TypeError, ValueError):
        raise ValidationError(_("Invalid media reference."))
    if mid <= 0:
        raise ValidationError(_("Invalid media reference."))

    # Import here to avoid a model import at module load time.
    from apps.website.models import MediaFile

    if not MediaFile.objects.filter(id=mid).exists():
        raise ValidationError(_("Media file not found."))
    return mid


# ---------------------------------------------------------------------------
# Image upload validation
# ---------------------------------------------------------------------------

# Real, decode-verified MIME types. SVG is intentionally NOT here - it can
# carry script content. Add it back only with a dedicated SVG sanitizer.
_ALLOWED_IMAGE_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

# Favicon-only extra formats.
_ALLOWED_FAVICON_FORMATS = dict(_ALLOWED_IMAGE_FORMATS)
_ALLOWED_FAVICON_FORMATS["ICO"] = "image/x-icon"


@dataclass
class ImageUploadInfo:
    mime_type: str
    width: int
    height: int


def validate_image_upload(
    uploaded_file,
    *,
    max_size: int = MAX_UPLOAD_SIZE,
    allow_favicon: bool = False,
) -> ImageUploadInfo:
    """
    Verify that an uploaded file is actually a safe image.

    Rejects:
      * Files larger than `max_size`
      * Files whose decoded format isn't in the allowlist (no SVG, no HTML)
      * Decompression bombs (PIL.Image.MAX_IMAGE_PIXELS guard)
      * Anything PIL refuses to decode

    Returns `ImageUploadInfo` with the *server-detected* mime type and
    dimensions. The caller MUST use these - never trust the browser's
    `Content-Type` header or the original filename.

    Resets the file pointer to 0 before returning.
    """
    from PIL import Image as PILImage
    from PIL import ImageFile, UnidentifiedImageError

    # Pillow's decompression-bomb guard warns by default; force it to raise.
    PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False

    # 1. Size
    size = getattr(uploaded_file, "size", None)
    if size is None:
        uploaded_file.seek(0, io.SEEK_END)
        size = uploaded_file.tell()
        uploaded_file.seek(0)
    if size <= 0:
        raise ValidationError(_("Uploaded file is empty."))
    if size > max_size:
        raise ValidationError(
            _("File is too large. Max %(max)s MB.") % {"max": max_size // (1024 * 1024)}
        )

    # 2. Decode + verify header
    uploaded_file.seek(0)
    try:
        with PILImage.open(uploaded_file) as probe:
            probe.verify()
    except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning):
        raise ValidationError(_("Image is too large to process."))
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ValidationError(_("File is not a valid image."))

    # verify() leaves the file unusable; reopen for size + format
    uploaded_file.seek(0)
    try:
        with PILImage.open(uploaded_file) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
    except Exception:
        raise ValidationError(_("File is not a valid image."))
    finally:
        uploaded_file.seek(0)

    allowed = _ALLOWED_FAVICON_FORMATS if allow_favicon else _ALLOWED_IMAGE_FORMATS
    if fmt not in allowed:
        raise ValidationError(_("Image format not allowed. Use PNG, JPEG, WEBP, or GIF."))

    return ImageUploadInfo(mime_type=allowed[fmt], width=width, height=height)
