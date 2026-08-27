"""
Analytics beacon endpoint.

Receives the slice of data that only exists client-side:
- screen resolution + viewport size (set once per session)
- tel: / mailto: / outbound link clicks, with where on the page they happened
- engaged time heartbeats (active seconds, tab visible only)
- inquiry form abandonment

Uses the httponly session cookie (sent automatically) to resolve the session
server-side - JS never needs to read it. Always returns 204 quickly.

The endpoint is csrf_exempt by necessity (sendBeacon cannot carry a CSRF
token), so it is rate limited per session and every payload value is validated
against a whitelist before it reaches the database.
"""

import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import EventType, Session
from .tracking import (
    SESSION_COOKIE,
    record_engagement,
    record_event,
)
from .utils import is_bot

logger = logging.getLogger(__name__)

_MAX_BODY = 4096  # generous cap for a tiny JSON payload

# Rate limit: a well-behaved page sends a handful of beacons per minute
# (one screen report, heartbeats every 15s, the odd click). This ceiling leaves
# plenty of headroom while stopping a loop from filling the database.
_RATE_LIMIT_MAX = 40
_RATE_LIMIT_WINDOW_SECONDS = 60


def _rate_limited(session_uuid) -> bool:
    """
    Count beacons per session per minute; True once the ceiling is passed.

    Backed by the default cache. With LocMemCache the counter is per process,
    so several workers each get their own allowance - it bounds abuse rather
    than enforcing an exact global quota. Swap in a shared cache backend if
    that ever matters.
    """
    key = f"analytics:beacon:{session_uuid}"
    try:
        added = cache.add(key, 1, _RATE_LIMIT_WINDOW_SECONDS)
        if added:
            return False
        count = cache.incr(key)
    except ValueError:
        # Key expired between add() and incr().
        cache.set(key, 1, _RATE_LIMIT_WINDOW_SECONDS)
        return False
    except Exception:  # noqa: BLE001 - never fail a request over throttling
        return False
    return count > _RATE_LIMIT_MAX


@csrf_exempt
@require_POST
def beacon(request):
    """Handle a beacon payload. Returns 204 regardless (fire-and-forget)."""
    if not getattr(settings, "ANALYTICS_ENABLED", True):
        return HttpResponse(status=204)

    if is_bot(request.META.get("HTTP_USER_AGENT", "")):
        return HttpResponse(status=204)

    session_uuid = request.COOKIES.get(SESSION_COOKIE)
    if not session_uuid:
        return HttpResponse(status=204)  # no session yet; nothing to attach to

    if _rate_limited(session_uuid):
        return HttpResponse(status=429)

    body = request.body[:_MAX_BODY]
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, TypeError):
        return HttpResponseBadRequest("invalid json")

    if not isinstance(payload, dict):
        return HttpResponseBadRequest("invalid payload")

    session = Session.objects.filter(uuid=session_uuid).first()
    if session is None:
        return HttpResponse(status=204)

    kind = payload.get("type")

    if kind == "screen":
        _record_screen(session, payload)
    elif kind == "event":
        _record_click(session, payload)
    elif kind == "engagement":
        _record_engagement(session, payload)
    elif kind == "form":
        _record_form(session, payload)

    return HttpResponse(status=204)


def _positive_int(value, ceiling):
    """Return value as an int within (0, ceiling], else None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 < value <= ceiling:
        return value
    return None


def _record_screen(session, payload):
    """Store screen resolution and viewport size once per session."""
    fields = []

    if not session.screen_resolution:
        width = _positive_int(payload.get("w"), 100000)
        height = _positive_int(payload.get("h"), 100000)
        if width and height:
            session.screen_resolution = f"{width}x{height}"
            fields.append("screen_resolution")

    if session.viewport_width is None:
        vw = _positive_int(payload.get("vw"), 100000)
        vh = _positive_int(payload.get("vh"), 100000)
        if vw and vh:
            session.viewport_width = vw
            session.viewport_height = vh
            fields.extend(["viewport_width", "viewport_height"])

    if fields:
        session.save(update_fields=fields)


_EVENT_MAP = {
    "tel": EventType.TEL_CLICK,
    "email": EventType.EMAIL_CLICK,
    "outbound": EventType.OUTBOUND,
}


def _record_click(session, payload):
    """Record a tel/email/outbound click, including where on the page."""
    raw = payload.get("event", "")
    event_type = _EVENT_MAP.get(raw, EventType.OTHER)
    record_event(
        session,
        event_type,
        label=str(payload.get("label", ""))[:255],
        path=str(payload.get("path", ""))[:500],
        # Validated against Placement.values inside record_event.
        placement=str(payload.get("placement", ""))[:20],
    )
    Session.objects.filter(pk=session.pk).update(last_activity=timezone.now())


def _record_engagement(session, payload):
    """Add a heartbeat's worth of engaged seconds to the session and page."""
    record_engagement(
        session,
        path=str(payload.get("path", ""))[:500],
        seconds=payload.get("seconds"),
    )


def _record_form(session, payload):
    """Record that a visitor left an inquiry step without submitting it."""
    if payload.get("event") != "abandon":
        return
    record_event(
        session,
        EventType.FORM_ABANDON,
        label=str(payload.get("label", ""))[:255],
        path=str(payload.get("path", ""))[:500],
    )
