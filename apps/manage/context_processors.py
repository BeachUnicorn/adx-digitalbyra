"""Context processors for the customer control panel (/manage/)."""

from apps.assistant.models import DraftChange
from apps.inquiries.models import Inquiry


def inquiry_badge(request):
    """
    Expose the unread-inquiry count + an admin-dock flag to templates.

    Runs for agency users on any page (the public site shows a floating
    admin dock; /manage/ shows the nav badge). Anonymous visitors and
    customer contacts get nothing, so the public site stays query-free for them.

    The pending-draft count rides along here for the same reason: the AI can
    leave work waiting while the customer is anywhere in /manage/, and a badge
    is what turns "go find it" into "one click".
    """
    from apps.projects.access import is_agency_user

    user = getattr(request, "user", None)
    # Bara byrån: en inloggad kund på sajten ska varken se dockan eller
    # hur många förfrågningar byrån har oläst.
    if not is_agency_user(user):
        return {}
    return {
        "unread_inquiries": Inquiry.objects.filter(is_read=False).count(),
        "pending_drafts": DraftChange.objects.filter(
            job__user=user, status=DraftChange.Status.PENDING
        ).count(),
        "show_admin_dock": True,
    }


def _site_css_mtime():
    from pathlib import Path

    from django.conf import settings

    css = Path(settings.BASE_DIR) / "static" / "css" / "site.css"
    try:
        return str(int(css.stat().st_mtime))
    except OSError:
        return "1"


def static_version(request):
    """
    Cache-busting för /manage/-stylesheeten.

    Webbläsare cachar statiska filer hårt, och utan versionsstämpel ser
    kunden gammal (eller i värsta fall trasig) styling tills de råkar
    hårduppdatera - det hände 2026-08-21 när en korrupt manage.css hann
    cachas. Stämpeln är filens mtime, läst vid processtart: ny fil på
    disk => ny URL => ny nedladdning. Ingen mtime-läsning per request.
    """
    from django.conf import settings

    # I utveckling läses stämpeln per request: uvicorns --reload startar bara
    # om processen vid .py-ändringar, så en processtart-stämpel blir stående
    # gammal när CSS:en ändras - lokala sidan ser då oförändrad ut medan
    # produktion (omstartad vid deploy) visar det nya. Det kostade en
    # förvirrad felsökning 2026-08-22. I produktion räcker processtart.
    if settings.DEBUG:
        return {"static_version": _css_mtime(), "site_css_version": _site_css_mtime()}
    return {"static_version": _CSS_VERSION, "site_css_version": _SITE_CSS_VERSION}


def _css_mtime():
    from pathlib import Path

    from django.conf import settings

    # Stämpeln täcker alla panelens filer: manage.css (struktur),
    # manage-skin.css (utseende) och tavla.css (tavlan), plus verktygslagret
    # (site-tools.css, laddas av både sajten och Flamingo) och ADX Flamingos
    # stilmallar och skript (sidorna, verktyget, landningssidorna och byråns
    # sida; alla länkas med ?v={{ static_version }}). Senaste ändringen av
    # någon av dem ger ny URL.
    static = Path(settings.BASE_DIR) / "static"
    stamps = []
    for name in (
        "css/manage.css",
        "css/manage-skin.css",
        "css/tavla.css",
        "css/offert.css",
        "css/tiptap.css",
        "css/site-tools.css",
        "css/flamingo.css",
        "css/flamingo-blocks.css",
        "css/flamingo-app.css",
        "css/flamingo-app-onboarding.css",
        "css/flamingo-app-campaigns.css",
        "css/flamingo-app-inbox.css",
        "css/flamingo-lp-ren.css",
        "css/flamingo-pb.css",
        "css/flamingo-pb-ai.css",
        "css/manage-flamingo.css",
        "js/dist/tiptap-editor.js",
        "js/manage-tables.js",
        "js/menu.js",
        "js/portal.js",
        "js/manage.js",
        "js/flamingo-app-onboarding.js",
        "js/flamingo-lp.js",
        "js/flamingo-pb.js",
        "js/flamingo-pb-ai.js",
        "js/manage-flamingo.js",
    ):
        try:
            stamps.append(int((static / name).stat().st_mtime))
        except OSError:
            continue
    return str(max(stamps)) if stamps else "1"


_CSS_VERSION = _css_mtime()
_SITE_CSS_VERSION = _site_css_mtime()
