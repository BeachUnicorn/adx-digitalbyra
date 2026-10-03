"""
Mediaarkivet i verktyget (/flamingo/app/media/): kundens logotyp och
bilder, bilderna från hemsidan och färgerna ur logotypen (media.py).

    media_archive   sidan, flikarna Alla / Logotyp / Uppladdat / Från
                    hemsidan (?visa=), och formulären (POST, action=):
                    upload (utan skript), alt, logo, unlogo, delete,
                    import (bilderna från hemsidan) och palette
    media_json      GET, redigerarens bildväljare:
                    {"assets": [{"id", "thumb", "url", "width", "height",
                    "alt", "is_logo"}], "can_upload", "limit", "count"}
    media_upload    POST (multipart, fältet "file", flera filer), CSRF med
                    X-CSRFToken: {"assets": [...]} (och "errors": [...] om
                    någon fil nekades), eller {"error": "..."} med 400 när
                    ingen fil togs emot. Högst media.UPLOADS_PER_HOUR filer
                    per konto och timme

Logotypen syns direkt på live-sidorna (sidhuvudet och paletten Från
logotypen): ett byte, en borttagen logotyp eller en ny palett larmar byrån
(media.set_logo, unset_logo, apply_logo_palette).

Allt går via kundens konto (app_view): ett id ur ett formulär hämtas alltid
med account=account, så ett annat kontos id ger 404. Byrån i kundvyn gör
samma sak som kunden.
"""

import re
from urllib.parse import unquote, urlsplit

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.common.security import sanitize_plain_text

from .. import media
from ..models import MEDIA_MAX_PER_ACCOUNT, LandingPage, MediaAsset
from . import app_view, render_app

TABS = (
    ("alla", "Alla"),
    ("logotyp", "Logotyp"),
    ("uppladdat", "Uppladdat"),
    ("hemsidan", "Från hemsidan"),
)
ACCEPT = "image/jpeg,image/png,image/webp,image/gif"


def _tab(value):
    return value if value in dict(TABS) else "alla"


def _back(tab):
    url = reverse("flamingo:app_media")
    return redirect(url if tab == "alla" else f"{url}?visa={tab}")


def _can_upload(request, account):
    return not request.flamingo.read_only and media.media_count(account) < MEDIA_MAX_PER_ACCOUNT


def _assets_for(account, tab):
    assets = MediaAsset.objects.filter(account=account)
    if tab == "logotyp":
        assets = assets.filter(is_logo=True)
    elif tab == "uppladdat":
        assets = assets.filter(source=MediaAsset.SOURCE_UPLOAD)
    elif tab == "hemsidan":
        assets = assets.filter(source=MediaAsset.SOURCE_SITE)
    return assets.order_by("-is_logo", "-created_at", "-pk")


def _swatches(colors):
    """[(hex, är huvudfärgen)] för färgerna ur logotypen."""
    primary = colors.get("primary") if isinstance(colors, dict) else ""
    out = []
    for color in (colors or {}).get("colors") or ([primary] if primary else []):
        if isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            out.append((color.upper(), color.upper() == str(primary).upper()))
    return out


def _host(url):
    host = (urlsplit(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _display_path(url, site_host):
    """Var bilden låg: sökvägen ("/wp-content/uploads/bad.jpg"), med
    värden först när den är en annan än hemsidans (en bildtjänst)."""
    parts = urlsplit(url)
    path = unquote(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    host = _host(url)
    text = path if host == site_host else f"{host}{path}"
    return text if len(text) <= 160 else text[:157] + "..."


@app_view
def media_archive(request, account):
    if request.method == "POST":
        return _post(request, account)
    tab = _tab(request.GET.get("visa"))
    usage = media.media_usage(account)
    all_assets = MediaAsset.objects.filter(account=account)
    rows = [
        {"asset": asset, "uses": usage.get(asset.pk, [])} for asset in _assets_for(account, tab)
    ]
    site_host = _host(account.website_url)
    candidates = []
    if tab == "hemsidan":
        candidates = [
            {"c": c, "path": _display_path(c.source_url, site_host)}
            for c in account.site_images.filter(imported_asset__isnull=True).order_by(
                "-likely_logo", "-found_at", "-pk"
            )
        ]
    logo = all_assets.filter(is_logo=True).order_by("-pk").first()
    colors = media.current_logo_colors(account) if logo is not None else {}
    count = all_assets.count()
    counts = {
        "alla": count,
        "logotyp": 1 if logo is not None else 0,
        "uppladdat": all_assets.filter(source=MediaAsset.SOURCE_UPLOAD).count(),
        "hemsidan": all_assets.filter(source=MediaAsset.SOURCE_SITE).count()
        + account.site_images.filter(imported_asset__isnull=True).count(),
    }
    pages = list(LandingPage.objects.filter(account=account).order_by("name", "pk"))
    return render_app(
        request,
        "flamingo/app/media/archive.html",
        "media",
        {
            "tab": tab,
            "tabs": [(key, label, counts[key]) for key, label in TABS],
            "rows": rows,
            "count": count,
            "limit": MEDIA_MAX_PER_ACCOUNT,
            "full": count >= MEDIA_MAX_PER_ACCOUNT,
            "can_upload": _can_upload(request, account),
            "accept": ACCEPT,
            "max_files": media.MAX_FILES_PER_UPLOAD,
            "logo": logo,
            "swatches": _swatches(colors),
            "pages": pages,
            "logo_pages": sum(1 for p in pages if p.palette == LandingPage.PALETTE_LOGO),
            "candidates": candidates,
            "site_host": site_host,
            "import_max": media.SITE_CANDIDATES,
            "is_demo": account.is_demo,
        },
    )


def _post(request, account):
    action = request.POST.get("action", "")
    tab = _tab(request.POST.get("visa"))
    if action == "upload":
        _upload_messages(request, account)
        return _back(tab)
    if action == "import":
        return _import(request, account)
    if action == "palette":
        return _palette(request, account, tab)
    asset = get_object_or_404(MediaAsset, pk=_int(request.POST.get("asset")), account=account)
    if action == "alt":
        asset.alt = sanitize_plain_text(request.POST.get("alt", ""), max_length=200)
        asset.save(update_fields=["alt"])
        messages.success(request, "Alternativtexten är sparad.")
    elif action == "logo":
        colors = media.set_logo(asset, user=request.user)
        if colors:
            messages.success(
                request,
                "Bilden är logotypen nu. Färgerna ur den finns under Färger från logotypen.",
            )
        else:
            messages.success(
                request,
                "Bilden är logotypen nu. Den har inga tydliga färger, så sidor med paletten "
                "Från logotypen visas i blått.",
            )
    elif action == "unlogo":
        media.unset_logo(asset, user=request.user)
        messages.success(request, "Bilden är inte logotypen längre.")
    elif action == "delete":
        try:
            media.delete_asset(asset, user=request.user)
        except media.MediaInUse as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "Bilden är borttagen.")
    else:
        messages.error(request, "Okänd åtgärd.")
    return _back(tab)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _upload_files(request, account):
    """(sparade, fel) för filerna i fältet "file"."""
    files = request.FILES.getlist("file")
    if not files:
        return [], ["Välj en eller flera bilder."]
    if len(files) > media.MAX_FILES_PER_UPLOAD:
        return [], [f"Högst {media.MAX_FILES_PER_UPLOAD} bilder åt gången."]
    saved, errors = [], []
    for uploaded in files:
        try:
            saved.append(media.add_upload(account, uploaded, user=request.user))
        except media.MediaError as exc:
            errors.append(exc.message)
    return saved, errors


def _upload_messages(request, account):
    saved, errors = _upload_files(request, account)
    if saved:
        word = "bild" if len(saved) == 1 else "bilder"
        messages.success(request, f"{len(saved)} {word} uppladdade.")
    for error in errors[:5]:
        messages.error(request, error)


def _import(request, account):
    try:
        result = media.import_candidates(
            account,
            request.POST.getlist("candidate"),
            user=request.user,
            rights_confirmed=request.POST.get("rights") == "1",
        )
    except media.MediaError as exc:
        messages.error(request, exc.message)
        return _back("hemsidan")
    if result.imported:
        word = "bild" if len(result.imported) == 1 else "bilder"
        messages.success(request, f"{len(result.imported)} {word} hämtade till arkivet.")
    for candidate, reason in result.failed[:5]:
        messages.error(request, f"{candidate.source_url}: {reason}")
    return _back("hemsidan")


def _palette(request, account, tab):
    target = request.POST.get("page", "alla")
    page = None
    if target != "alla":
        page = get_object_or_404(LandingPage, pk=_int(target), account=account)
    try:
        count = media.apply_logo_palette(account, page=page, user=request.user)
    except media.MediaError as exc:
        messages.error(request, exc.message)
        return _back(tab)
    if page is not None:
        messages.success(request, f"Sidan {page.name} har färgerna från logotypen nu.")
    elif count:
        word = "sidan" if count == 1 else f"alla {count} sidor"
        messages.success(request, f"Färgerna från logotypen gäller för {word} nu.")
    else:
        messages.info(
            request, "Det finns inga sidor än. Nya sidor får färgerna från logotypen att välja."
        )
    return _back(tab)


@app_view
@require_GET
def media_json(request, account):
    assets = MediaAsset.objects.filter(account=account).order_by("-is_logo", "-created_at", "-pk")
    return JsonResponse(
        {
            "assets": [media.asset_json(asset) for asset in assets],
            "can_upload": _can_upload(request, account),
            "limit": MEDIA_MAX_PER_ACCOUNT,
            "count": len(assets),
        }
    )


@app_view
@require_POST
def media_upload(request, account):
    saved, errors = _upload_files(request, account)
    if not saved:
        return JsonResponse({"error": " ".join(errors[:3]) or "Ingen bild togs emot."}, status=400)
    payload = {"assets": [media.asset_json(asset) for asset in saved]}
    if errors:
        payload["errors"] = errors
    return JsonResponse(payload)
