"""
Customer-facing admin (/manage/).

This is the customer's control panel - distinct from Django admin (/admin/),
which is for the platform superuser only.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.common.security import sanitize_plain_text, validate_image_upload
from apps.inquiries.models import Inquiry
from apps.website.models import Block, BlockPage, MediaFile, Menu, SiteSettings

from .forms import SiteSettingsForm


@login_required
def dashboard(request):
    """Overview page: quick stats + entry points into each area."""
    settings = SiteSettings.load()

    pages = BlockPage.objects.all()
    stats = {
        "pages_total": pages.count(),
        "pages_published": pages.filter(is_published=True).count(),
        "blocks_total": Block.objects.count(),
        "menus_total": Menu.objects.count(),
        "media_total": MediaFile.objects.count(),
        "inquiries_unread": Inquiry.objects.filter(is_read=False).count(),
        "inquiries_total": Inquiry.objects.count(),
    }

    # Länklarmet (länkregeln): besökare ska aldrig hinna hitta en död länk
    # före ägaren. Räknaren driver varningsraden på översikten.
    from apps.website.links import dead_links

    stats["dead_links"] = len(dead_links())

    recent_pages = pages.order_by("-updated_at")[:5]

    context = {
        "site_settings": settings,
        "stats": stats,
        "recent_pages": recent_pages,
        "active": "dashboard",
    }
    return render(request, "manage/dashboard.html", context)


@login_required
def link_report(request):
    """Länkrapporten: varje lagrad länk vars mål inte fungerar, med plats
    och en väg till stället där den lagas. Larmets andra halva - räknaren
    på översikten är den första."""
    from apps.website.links import dead_links

    context = {
        "site_settings": SiteSettings.load(),
        "problems": dead_links(),
        "active": "dashboard",
    }
    return render(request, "manage/links.html", context)


# ---------------------------------------------------------------------------
# Media library
# ---------------------------------------------------------------------------


def _media_usage(media_file):
    """
    Return a list of human-readable places this MediaFile is referenced.

    Blocks store image IDs in JSON; we scan for the id appearing as a hero
    `image_id` or inside a cards list. Kept simple and read-only.
    """
    uses = []
    mid = media_file.id
    for block in Block.objects.select_related("page"):
        data = block.data or {}
        found = False
        if data.get("image_id") == mid:
            found = True
        for card in data.get("cards", []) or []:
            if isinstance(card, dict) and card.get("image_id") == mid:
                found = True
        if found:
            uses.append(f"{block.get_block_type_display()} – {block.page.title}")
    return uses


@login_required
def media_library(request):
    """Grid of all uploaded media with thumbnails + metadata."""
    settings = SiteSettings.load()
    query = request.GET.get("q", "").strip()

    media = MediaFile.objects.all()
    if query:
        media = media.filter(Q(original_filename__icontains=query) | Q(alt_text__icontains=query))

    # All unoptimized images, regardless of the search filter - the
    # "Optimera alla" button should empty the whole library, not the hits.
    optimize_ids = [m.pk for m in MediaFile.objects.all() if m.can_optimize]

    context = {
        "site_settings": settings,
        "media": media,
        "query": query,
        "media_count": MediaFile.objects.count(),
        "optimize_ids": optimize_ids,
        "active": "media",
    }
    return render(request, "manage/media/library.html", context)


@login_required
@require_POST
def media_upload(request):
    """
    Handle an image upload from the media library.

    Validates via the security boundary (server-detected MIME, no SVG,
    size + decompression-bomb caps). Stores server-detected metadata, never
    the browser-supplied Content-Type.
    """
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, _("No file selected."))
        return redirect("manage:media_library")

    try:
        info = validate_image_upload(upload)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("manage:media_library")

    media_file = MediaFile(
        original_filename=upload.name[:255],
        alt_text=sanitize_plain_text(request.POST.get("alt_text", ""), max_length=255),
        mime_type=info.mime_type,
        file_size=upload.size,
        width=info.width,
        height=info.height,
    )
    media_file.file.save(upload.name, upload, save=True)

    # Optimize right away (customer's call 2026-08-26): no button to
    # remember. If it fails the upload still stands - the file is intact
    # and the image stays in the "Optimera alla" pool.
    if media_file.can_optimize:
        from .image_optimizer import optimize_image

        success, opt_msg = optimize_image(media_file)
        if success:
            messages.success(request, f"Bilden laddades upp. {opt_msg}")
        else:
            messages.success(request, _("Image uploaded."))
            messages.info(request, opt_msg)
    else:
        messages.success(request, _("Image uploaded."))
    return redirect("manage:media_library")


def _percent_or(value, fallback):
    """Parse a 0-100 percent from form input; keep the current value on junk."""
    try:
        return min(100, max(0, int(value)))
    except (TypeError, ValueError):
        return fallback


@login_required
@require_POST
def media_update(request, pk):
    """Update editable metadata (alt text, focal point) for a media file.

    Only fields present in the POST are touched: the alt-text form and the
    focal-point picker submit independently, and neither may blank out the
    other's field.
    """
    media_file = get_object_or_404(MediaFile, pk=pk)
    fields = []
    if "alt_text" in request.POST:
        media_file.alt_text = sanitize_plain_text(request.POST.get("alt_text", ""), max_length=255)
        fields.append("alt_text")
    if "focal_x" in request.POST or "focal_y" in request.POST:
        media_file.focal_x = _percent_or(request.POST.get("focal_x"), media_file.focal_x)
        media_file.focal_y = _percent_or(request.POST.get("focal_y"), media_file.focal_y)
        fields += ["focal_x", "focal_y"]
    if fields:
        media_file.save(update_fields=fields)

    # The focal picker saves over fetch and shows its own confirmation;
    # a flash message here would pop up on some later page load instead.
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(
            {"ok": True, "focal_x": media_file.focal_x, "focal_y": media_file.focal_y}
        )
    messages.success(request, _("Saved."))
    return redirect("manage:media_library")


@login_required
@require_POST
def media_delete(request, pk):
    """
    Delete a media file - but refuse if it's still referenced by a block,
    so the public site never ends up with broken image references.
    """
    media_file = get_object_or_404(MediaFile, pk=pk)
    uses = _media_usage(media_file)
    if uses:
        messages.error(
            request,
            _("Can't delete - still used by: %(where)s.") % {"where": ", ".join(uses)},
        )
        return redirect("manage:media_library")

    media_file.file.delete(save=False)
    media_file.delete()
    messages.success(request, _("Image deleted."))
    return redirect("manage:media_library")


@login_required
@require_POST
def media_optimize(request, pk):
    """Optimize an image: resize + convert to WebP, preserving the original."""
    from .image_optimizer import optimize_image

    media_file = get_object_or_404(MediaFile, pk=pk)
    success, msg = optimize_image(media_file)

    # "Optimera alla" runs this endpoint once per image over fetch - one
    # small request per image is what keeps the bulk run timeout-proof.
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"ok": success, "message": msg})
    if success:
        messages.success(request, msg)
    else:
        messages.info(request, msg)
    return redirect("manage:media_library")


@login_required
@require_POST
def media_restore(request, pk):
    """Restore the original (pre-optimization) file."""
    from .image_optimizer import restore_original

    media_file = get_object_or_404(MediaFile, pk=pk)
    success, msg = restore_original(media_file)
    if success:
        messages.success(request, msg)
    else:
        messages.error(request, msg)
    return redirect("manage:media_library")


# ---------------------------------------------------------------------------
# Site settings
# ---------------------------------------------------------------------------


@login_required
def settings_view(request):
    """Edit contact details + footer intro (the variable sources)."""
    settings = SiteSettings.load()
    if request.method == "POST":
        form = SiteSettingsForm(request.POST, instance=settings)
        if form.is_valid():
            form.save()
            messages.success(request, _("Inställningar sparade."))
            return redirect("manage:settings")
        messages.error(request, _("Kontrollera fälten nedan."))
    else:
        form = SiteSettingsForm(instance=settings)

    context = {
        "site_settings": settings,
        "form": form,
        "active": "settings",
        "all_media": MediaFile.objects.all(),
    }
    return render(request, "manage/settings/settings.html", context)
