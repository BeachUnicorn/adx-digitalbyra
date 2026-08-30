"""
Analytics middleware.

Records a page view for each successful HTML GET on the public site and
attaches the resolved analytics Session to the request so downstream views
(e.g. the inquiry form) can link a booking to it.

The page title is read out of the rendered response, which keeps reports
readable without every view having to pass one in. A view can override it by
setting request.analytics_title.

Skips: non-GET, /admin/, /manage/, /static/, /media/, the analytics beacon
endpoint, AJAX, non-HTML responses, and anything when ANALYTICS_ENABLED=False.

Tracking failures must never break a page - everything is wrapped so an
analytics error degrades to "no data" rather than a 500.
"""

import html
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.utils.deprecation import MiddlewareMixin

from .tracking import record_pageview, resolve_visitor_and_session

logger = logging.getLogger(__name__)

# Path prefixes that are never tracked.
_SKIP_PREFIXES = (
    "/admin/",
    "/manage/",
    "/static/",
    "/media/",
    "/healthz",
    "/analytics/",
    "/favicon",
    # Offertlänkar: token i adressen ÄR behörigheten (inkl. rätten att
    # acceptera). Den får aldrig skrivas till PageView.path eller
    # sessionens/besökarens landing_page - ett analyslager har en helt
    # annan åtkomst- och backupprofil än en hemlig kapabilitetslänk.
    "/offert/",
)

_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# <title> lives in <head>, so there is no reason to scan a whole page for it.
_TITLE_SCAN_BYTES = 8192

_SITE_NAME_CACHE_KEY = "analytics:site_name"
_SITE_NAME_CACHE_SECONDS = 300


def _site_name():
    """
    Site name, cached, used to strip the " - ADX" title suffix.

    SiteSettings.load() is an uncached get_or_create, so calling it on every
    tracked pageview would add a query per request for a value that changes
    approximately never.
    """
    name = cache.get(_SITE_NAME_CACHE_KEY)
    if name is not None:
        return name
    try:
        from apps.website.models import SiteSettings

        name = SiteSettings.load().name or ""
    except Exception:  # noqa: BLE001 - analytics must never break a page
        name = ""
    cache.set(_SITE_NAME_CACHE_KEY, name, _SITE_NAME_CACHE_SECONDS)
    return name


def _extract_title(response, site_name=""):
    """
    Pull the page title out of a rendered HTML response.

    Reading it here means every page gets a title without touching each view.
    Only the head of the document is scanned, and the trailing
    " - <site name>" suffix is dropped so titles stay readable in reports.
    Returns "" when anything is off - a missing title must never break a page.
    """
    try:
        content = response.content[:_TITLE_SCAN_BYTES]
    except AttributeError:
        # Streaming responses have no .content.
        return ""

    match = _TITLE_RE.search(content)
    if not match:
        return ""

    try:
        title = match.group(1).decode(response.charset or "utf-8", errors="replace")
    except (LookupError, TypeError):
        return ""

    title = html.unescape(title)
    # Collapse the whitespace that template line breaks leave behind.
    title = " ".join(title.split())

    if site_name:
        for sep in (" - ", " \u2013 ", " | "):
            suffix = f"{sep}{site_name}"
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
                break

    return title[:255]


class AnalyticsMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.analytics_session = None

    @staticmethod
    def _page_title(request, response):
        """
        Resolve the title to store for this pageview.

        A view can set request.analytics_title to override; otherwise it is
        read out of the rendered HTML.
        """
        explicit = getattr(request, "analytics_title", "")
        if explicit:
            return str(explicit)[:255]
        return _extract_title(response, site_name=_site_name())

    def process_response(self, request, response):
        try:
            self._maybe_track(request, response)
        except Exception:  # noqa: BLE001 - analytics must never break a page
            logger.exception("Analytics tracking failed")
        return response

    def _maybe_track(self, request, response):
        if not getattr(settings, "ANALYTICS_ENABLED", True):
            return

        if request.method != "GET":
            return

        path = request.path
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return

        # Only track successful HTML page loads.
        if response.status_code != 200:
            return
        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return

        # Skip AJAX/fetch requests.
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return

        result = resolve_visitor_and_session(request)
        if result is None:
            return

        session = result.session
        request.analytics_session = session
        record_pageview(session, path, title=self._page_title(request, response))

        # Apply first-party cookies.
        for name, (value, max_age) in result.cookies_to_set.items():
            response.set_cookie(
                name,
                value,
                max_age=max_age,
                httponly=True,
                samesite="Lax",
                secure=not settings.DEBUG,
            )
