"""
Image optimization: resize + convert to WebP with transparency support.

Called from the media_optimize view. Preserves the original file so it can
be restored. Produces a WebP file at max 1920px wide, quality 82.
"""

import io
import os

from django.core.files.base import ContentFile
from PIL import Image as PILImage

MAX_WIDTH = 1920
WEBP_QUALITY = 82


def optimize_image(media_file):
    """
    Optimize a MediaFile in-place. Returns (success: bool, message: str).

    Steps:
    1. Open the current file with Pillow.
    2. Resize if wider than MAX_WIDTH (proportional).
    3. Convert to WebP (preserving alpha/transparency).
    4. Save the original file path to media_file.original_file.
    5. Replace media_file.file with the optimized WebP.
    6. Update metadata (size, dimensions, mime_type, is_optimized).
    """
    if not media_file.file:
        return False, "Ingen fil att optimera."

    try:
        media_file.file.seek(0)
        img = PILImage.open(media_file.file)
    except Exception as e:
        return False, f"Kunde inte öppna bilden: {e}"

    original_size = media_file.file_size or media_file.file.size

    # Detect transparency (alpha channel)
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)

    # Convert palette images with transparency to RGBA
    if img.mode == "P" and has_alpha:
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if has_alpha else "RGB")

    # Resize if too wide
    w, h = img.size
    resized = False
    if w > MAX_WIDTH:
        ratio = MAX_WIDTH / w
        new_h = int(h * ratio)
        img = img.resize((MAX_WIDTH, new_h), PILImage.LANCZOS)
        w, h = MAX_WIDTH, new_h
        resized = True

    # Encode as WebP
    buffer = io.BytesIO()
    save_kwargs = {"format": "WEBP", "quality": WEBP_QUALITY, "method": 4}
    if has_alpha:
        # WebP supports lossless alpha; lossy+alpha is fine too
        save_kwargs["lossless"] = False
    img.save(buffer, **save_kwargs)
    new_size = len(buffer.getvalue())

    # Only proceed if we actually made it smaller (or converted format).
    # Count it as done: otherwise the image sits in the to-optimize pool
    # forever and every "Optimera alla" run redoes the same work. Nothing
    # was changed, so there is no original backup - the restore button
    # only shows when original_file exists.
    if new_size >= original_size and not resized and media_file.mime_type == "image/webp":
        media_file.is_optimized = True
        media_file.save(update_fields=["is_optimized"])
        return True, "Bilden var redan optimal."

    # Preserve original: copy current file to original_file field
    if not media_file.original_file:
        # Save the current file as the original backup
        media_file.file.seek(0)
        original_content = media_file.file.read()
        original_name = os.path.basename(media_file.file.name)
        media_file.original_file.save(original_name, ContentFile(original_content), save=False)

    # Replace the active file with the optimized WebP
    base_name = os.path.splitext(os.path.basename(media_file.file.name))[0]
    new_filename = f"{base_name}.webp"
    buffer.seek(0)

    # Delete old file from storage before saving new one
    old_file_name = media_file.file.name
    media_file.file.save(new_filename, ContentFile(buffer.read()), save=False)

    # Try to clean up old file (non-critical)
    try:
        from django.core.files.storage import default_storage

        if old_file_name and default_storage.exists(old_file_name):
            default_storage.delete(old_file_name)
    except Exception:
        pass

    # Update metadata
    media_file.file_size = new_size
    media_file.width = w
    media_file.height = h
    media_file.mime_type = "image/webp"
    media_file.is_optimized = True
    media_file.save()

    saving = original_size - new_size
    saving_pct = int((saving / original_size) * 100) if original_size > 0 else 0
    parts = []
    parts.append(f"{original_size // 1024} KB → {new_size // 1024} KB (-{saving_pct}%)")
    if resized:
        parts.append(f"skalad till {w}×{h}")
    return True, f"Optimerad: {', '.join(parts)}"


def restore_original(media_file):
    """
    Restore the original file. Returns (success: bool, message: str).
    """
    if not media_file.original_file:
        return False, "Inget original att återställa."

    try:
        media_file.original_file.seek(0)
        original_content = media_file.original_file.read()
    except Exception as e:
        return False, f"Kunde inte läsa originalet: {e}"

    # Re-read original dimensions and info
    try:
        media_file.original_file.seek(0)
        img = PILImage.open(media_file.original_file)
        orig_w, orig_h = img.size
        orig_format = (img.format or "").upper()
        mime_map = {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "WEBP": "image/webp",
            "GIF": "image/gif",
        }
        orig_mime = mime_map.get(orig_format, "image/png")
    except Exception:
        orig_w, orig_h, orig_mime = None, None, ""

    # Replace optimized file with original
    original_name = os.path.basename(media_file.original_file.name)
    media_file.file.save(original_name, ContentFile(original_content), save=False)

    # Clear the original backup
    old_original_name = media_file.original_file.name
    media_file.original_file = ""
    media_file.is_optimized = False
    media_file.file_size = len(original_content)
    media_file.width = orig_w
    media_file.height = orig_h
    media_file.mime_type = orig_mime
    media_file.save()

    # Clean up the backup file
    try:
        from django.core.files.storage import default_storage

        if old_original_name and default_storage.exists(old_original_name):
            default_storage.delete(old_original_name)
    except Exception:
        pass

    return True, "Original återställt."
