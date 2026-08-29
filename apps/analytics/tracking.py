"""
Core tracking service.

Resolves the Visitor + Session for a request and records page views / events.
Cookie management is handled here; the middleware just calls into it and
applies the returned cookies to the response.

Cookies (first-party, no third parties):
    av_id  - Visitor UUID, 2-year lifetime
    as_id  - Session UUID, refreshed on activity (session window cookie)
"""

import re
import uuid

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import (
    Event,
    EventType,
    PageView,
    Placement,
    Session,
    TrafficSource,
    Visitor,
)
from .utils import (
    anonymize_ip,
    classify_source,
    get_client_ip,
    is_bot,
    normalize_path,
    parse_user_agent,
)

VISITOR_COOKIE = "av_id"
SESSION_COOKIE = "as_id"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 years
SESSION_INACTIVITY_SECONDS = 30 * 60  # 30 min window
SESSION_COOKIE_MAX_AGE = SESSION_INACTIVITY_SECONDS

# Largest engaged-time delta accepted from a single beacon call. Heartbeats send
# far smaller increments; this just bounds the damage from a forged payload.
MAX_ENGAGEMENT_DELTA_SECONDS = 300


class TrackingResult:
    """Carries the resolved session + which cookies the response must set."""

    def __init__(self, session):
        self.session = session
        self.cookies_to_set = {}  # name -> (value, max_age)

    def set_cookie(self, name, value, max_age):
        self.cookies_to_set[name] = (value, max_age)


UTM_ALLOWED = re.compile(r"^[\w åäöÅÄÖéÉ.,+%|-]{1,100}$")


def _clean_utm(value):
    """
    Ett UTM-värde, eller tom sträng om det inte ser ut som ett.

    Riktiga kampanjnamn är slug-artade ("sommar-2026", "google / cpc").
    Sårbarhetsskannrar skickar HTML-payloads i utm-parametrarna, och även
    om mallarna escapar (ingen XSS körs) blev varje payload en egen
    "kampanj" i statistiken - omöjlig att radera eftersom den bara är
    värden på sessionsrader. Skräp ska inte in alls (2026-08-22).
    """
    value = (value or "").strip()[:100]
    if not value or not UTM_ALLOWED.match(value):
        return ""
    return value


def _is_truthy_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def resolve_visitor_and_session(request):
    """
    Resolve (or create) the Visitor and Session for this request.

    Returns a TrackingResult, or None if tracking should be skipped (bot,
    disabled, etc.). Does NOT record a pageview - call record_pageview after.
    """
    if not getattr(settings, "ANALYTICS_ENABLED", True):
        return None

    user_agent = request.META.get("HTTP_USER_AGENT", "")
    if is_bot(user_agent):
        return None

    now = timezone.now()
    current_host = request.get_host().split(":")[0]

    # --- Visitor -----------------------------------------------------------
    visitor = None
    visitor_cookie = request.COOKIES.get(VISITOR_COOKIE)
    new_visitor = False
    if _is_truthy_uuid(visitor_cookie):
        visitor = Visitor.objects.filter(uuid=visitor_cookie).first()
    if visitor is None:
        new_visitor = True
        visitor = Visitor(uuid=uuid.uuid4(), first_seen=now, last_seen=now)

    # --- Per-request attribution inputs ------------------------------------
    referrer = request.META.get("HTTP_REFERER", "")[:1000]
    utm_source = _clean_utm(request.GET.get("utm_source", ""))
    utm_medium = _clean_utm(request.GET.get("utm_medium", ""))
    utm_campaign = _clean_utm(request.GET.get("utm_campaign", ""))
    utm_term = _clean_utm(request.GET.get("utm_term", ""))
    utm_content = _clean_utm(request.GET.get("utm_content", ""))
    source, source_detail = classify_source(referrer, utm_source, utm_medium, current_host)

    # A visit that resumes after the 30-minute window looks "internal" because
    # the referrer is our own domain. That would overwrite the real acquisition
    # channel with noise, so carry the previous visit's source across the
    # artificial session boundary.
    if source == TrafficSource.INTERNAL and not new_visitor and visitor.pk:
        carried = (
            Session.objects.filter(visitor=visitor)
            .exclude(source=TrafficSource.INTERNAL)
            .order_by("-started_at")
            .values("source", "source_detail")
            .first()
        )
        if carried:
            source = carried["source"]
            source_detail = carried["source_detail"]

    # First-touch attribution - write once on a brand-new visitor.
    if new_visitor:
        visitor.first_referrer = referrer
        visitor.first_source = source
        visitor.first_source_detail = source_detail
        visitor.first_landing_page = request.path[:500]
    visitor.last_seen = now

    # --- Session -----------------------------------------------------------
    session = None
    session_cookie = request.COOKIES.get(SESSION_COOKIE)
    if _is_truthy_uuid(session_cookie):
        session = (
            Session.objects.filter(uuid=session_cookie, visitor__uuid=visitor.uuid)
            .select_related("visitor")
            .first()
        )
        # Expire the session after the inactivity window.
        if session:
            idle = (now - session.last_activity).total_seconds()
            if idle > SESSION_INACTIVITY_SECONDS:
                session = None

    if session is None:
        # New session - persist visitor first (counts the visit).
        if new_visitor:
            visitor.session_count = 1
            visitor.save()
        else:
            visitor.save(update_fields=["last_seen"])
            # Atomic increment. A read-modify-write drops visits whenever two
            # requests for the same visitor land concurrently. The in-memory
            # instance keeps its stale count - nothing downstream reads it.
            Visitor.objects.filter(pk=visitor.pk).update(session_count=F("session_count") + 1)

        ua = parse_user_agent(user_agent)
        ip = anonymize_ip(get_client_ip(request))
        session = Session.objects.create(
            uuid=uuid.uuid4(),
            visitor=visitor,
            started_at=now,
            last_activity=now,
            referrer=referrer,
            source=source,
            source_detail=source_detail,
            landing_page=request.path[:500],
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            utm_term=utm_term,
            utm_content=utm_content,
            device_type=ua["device_type"],
            os=ua["os"],
            browser=ua["browser"],
            user_agent=user_agent[:2000],
            ip_address=ip,
        )
    else:
        # Existing session - just bump activity + visitor.
        visitor.save(update_fields=["last_seen"])
        session.last_activity = now
        session.save(update_fields=["last_activity"])

    result = TrackingResult(session)
    result.set_cookie(VISITOR_COOKIE, str(visitor.uuid), VISITOR_COOKIE_MAX_AGE)
    result.set_cookie(SESSION_COOKIE, str(session.uuid), SESSION_COOKIE_MAX_AGE)
    return result


def record_pageview(session, path, title=""):
    """Record a page view and increment the session counter."""
    if session is None:
        return None
    pv = PageView.objects.create(
        session=session,
        path=normalize_path(path)[:500],
        title=title[:255],
    )
    Session.objects.filter(pk=session.pk).update(
        pageview_count=F("pageview_count") + 1,
        last_activity=timezone.now(),
    )
    return pv


def record_event(session, event_type, label="", path="", placement=""):
    """
    Record an interaction event (tel click, booking, form error, ...).

    `event_type` and `placement` are validated against their choice lists -
    both can originate from the beacon payload, which is untrusted input.
    """
    if session is None:
        return None
    if event_type not in EventType.values:
        event_type = EventType.OTHER
    if placement not in Placement.values:
        placement = ""
    return Event.objects.create(
        session=session,
        event_type=event_type,
        label=label[:255],
        path=normalize_path(path)[:500],
        placement=placement,
    )


def record_engagement(session, path, seconds):
    """
    Add engaged seconds to the session and to its latest view of `path`.

    Called from the beacon on a heartbeat interval, so deltas are small. The
    per-call cap keeps a forged payload from inflating the numbers, and F()
    expressions keep concurrent heartbeats from clobbering each other.
    """
    if session is None:
        return 0
    try:
        delta = int(seconds)
    except (TypeError, ValueError):
        return 0
    if delta <= 0:
        return 0
    delta = min(delta, MAX_ENGAGEMENT_DELTA_SECONDS)

    Session.objects.filter(pk=session.pk).update(
        engaged_seconds=F("engaged_seconds") + delta,
        last_activity=timezone.now(),
    )

    latest_pk = (
        # Pageviews are stored normalised, so the lookup key must be too.
        PageView.objects.filter(session=session, path=normalize_path(path)[:500])
        .order_by("-viewed_at")
        .values_list("pk", flat=True)
        .first()
    )
    if latest_pk is not None:
        PageView.objects.filter(pk=latest_pk).update(engaged_seconds=F("engaged_seconds") + delta)
    return delta


def source_snapshot(session) -> dict:
    """
    Build a durable snapshot of a session's traffic source.

    Stored on the Inquiry so attribution survives even if analytics rows are
    later purged. Returns plain strings safe to show the customer.
    """
    if session is None:
        return {
            "source": TrafficSource.DIRECT,
            "source_detail": "",
            "referrer": "",
        }
    return {
        "source": session.source,
        "source_detail": session.source_detail,
        "referrer": session.referrer,
    }
