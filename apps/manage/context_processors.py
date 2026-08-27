"""Context processors for the customer control panel (/manage/)."""

from apps.assistant.models import DraftChange
from apps.inquiries.models import Inquiry


def inquiry_badge(request):
    """
    Expose the unread-inquiry count + an admin-dock flag to templates.

    Runs for authenticated staff on any page (the public site shows a floating
    admin dock; /manage/ shows the nav badge). Anonymous visitors get nothing,
    so the public site stays query-free for them.

    The pending-draft count rides along here for the same reason: the AI can
    leave work waiting while the customer is anywhere in /manage/, and a badge
    is what turns "go find it" into "one click".
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
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

    css = Path(settings.BASE_DIR) / "static" / "css" / "manage.css"
    try:
        return str(int(css.stat().st_mtime))
    except OSError:
        return "1"


_CSS_VERSION = _css_mtime()
_SITE_CSS_VERSION = _site_css_mtime()
