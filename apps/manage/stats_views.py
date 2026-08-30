"""
/manage/statistik/ - visitor and traffic reporting.

Everything is aggregated straight from the raw analytics tables. There is no
pre-computed rollup, so the queries are written to stay index-friendly: date
ranges hit the indexes added in analytics migration 0002, and each panel is a
single grouped query rather than a loop.

Attribution model is switchable:
- "last"  (default) uses Session.source, i.e. the visit that converted
- "first" uses Visitor.first_source, i.e. how they originally found us
"""

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.db.models.functions import TruncDate
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from apps.analytics.models import (
    DeviceType,
    Event,
    EventType,
    PageView,
    Placement,
    Session,
    TrafficSource,
    Visitor,
    format_seconds,
)
from apps.inquiries.models import Inquiry
from apps.website.models import SiteSettings

# Selectable report windows. None = all time.
PERIODS = [
    ("7", "7 dagar", 7),
    ("30", "30 dagar", 30),
    ("90", "90 dagar", 90),
    ("365", "I år", 365),
    ("all", "Allt", None),
]
DEFAULT_PERIOD = "30"

# ADX har ETT formulär (kontaktsidan plus block på sökordssidorna), inte
# VVS-arvets trestegs-wizard - de gamla stegen /forfragan/* existerar inte
# som sidor här, så varje rad utom tack-raden stod alltid på noll.
FUNNEL_STEPS = [
    ("/kontakt/", "Kontaktsidan"),
]
FUNNEL_DONE_PREFIX = "/forfragan/tack/"

CONTACT_EVENTS = [
    EventType.TEL_CLICK,
    EventType.EMAIL_CLICK,
    EventType.BOOKING,
]

WEEKDAY_NAMES = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]
HEATMAP_HOURS = list(range(6, 22))


def _ctx(**extra):
    ctx = {"site_settings": SiteSettings.load(), "active": "stats"}
    ctx.update(extra)
    return ctx


def _resolve_period(request):
    """Return (key, label, start_datetime_or_None) for the requested window."""
    key = request.GET.get("period", DEFAULT_PERIOD)
    match = next((p for p in PERIODS if p[0] == key), None)
    if match is None:
        match = next(p for p in PERIODS if p[0] == DEFAULT_PERIOD)
        key = match[0]

    _, label, days = match
    start = None
    if days is not None:
        start = timezone.now() - timedelta(days=days)
    return key, label, start


def _pct(part, whole, digits=1):
    """Percentage of part in whole, 0 when whole is empty. For display."""
    if not whole:
        return 0.0
    return round(part * 100.0 / whole, digits)


def _css_pct(part, whole):
    """
    Percentage formatted for a CSS style attribute.

    Returned as a pre-formatted string on purpose. Django localises floats in
    templates, and LANGUAGE_CODE is "sv", so a float renders as "47,6" - which
    is invalid inside style="width:47,6%" and makes the bar silently collapse to
    its min-height. Display values keep going through _pct so they stay
    localised the way Swedish readers expect.
    """
    if not whole:
        return "0"
    return f"{part * 100.0 / whole:.1f}"


@login_required
def stats(request):
    # Superusers only while the report is being finalised. The nav link is
    # hidden in manage/base.html, but the URL has to be closed too.
    #
    # 404 rather than user_passes_test (the pattern in import_views): LOGIN_URL
    # points at /manage/login/, so redirecting an already logged-in customer
    # would bounce them to a login page and straight back again. A missing page
    # is also less of an invitation than a permission error.
    if not request.user.is_superuser:
        raise Http404

    period_key, period_label, start = _resolve_period(request)
    attribution = "first" if request.GET.get("attribution") == "first" else "last"

    sessions = Session.objects.all()
    pageviews = PageView.objects.all()
    events = Event.objects.all()
    visitors = Visitor.objects.all()
    inquiries = Inquiry.objects.all()

    if start is not None:
        sessions = sessions.filter(started_at__gte=start)
        pageviews = pageviews.filter(viewed_at__gte=start)
        events = events.filter(created_at__gte=start)
        visitors = visitors.filter(last_seen__gte=start)
        inquiries = inquiries.filter(created_at__gte=start)

    session_total = sessions.count()

    context = _ctx(
        periods=PERIODS,
        period_key=period_key,
        period_label=period_label,
        attribution=attribution,
        has_data=session_total > 0,
        summary=_summary(sessions, pageviews, events, visitors, inquiries, session_total),
        daily=_daily_series(sessions, inquiries, start),
        sources=_sources(sessions, attribution, session_total),
        campaigns=_campaigns(sessions),
        pages=_pages(pageviews, events),
        exit_pages=_exit_pages(pageviews),
        funnel=_funnel(pageviews, events),
        placements=_placements(events),
        heatmap=_heatmap(events),
        devices=_devices(sessions, session_total),
        viewports=_viewports(sessions),
        browsers=_browsers(sessions),
        loyalty=_loyalty(visitors),
        recent_inquiries=_recent_inquiries(inquiries),
    )
    return render(request, "manage/stats.html", context)


# ---------------------------------------------------------------------------
# Summary cards
# ---------------------------------------------------------------------------


def _summary(sessions, pageviews, events, visitors, inquiries, session_total):
    visitor_total = visitors.count()
    pageview_total = pageviews.count()
    inquiry_total = inquiries.count()

    event_counts = dict(
        events.values_list("event_type").annotate(n=Count("id")).values_list("event_type", "n")
    )
    tel = event_counts.get(EventType.TEL_CLICK, 0)
    email = event_counts.get(EventType.EMAIL_CLICK, 0)
    booking = event_counts.get(EventType.BOOKING, 0)

    engaged_total = sessions.aggregate(n=Sum("engaged_seconds"))["n"] or 0
    engaged_avg = int(engaged_total / session_total) if session_total else 0

    # An engaged visit: more than 15s of active time, or more than one page, or
    # any contact event. Mirrors how the number is described in the UI.
    engaged_sessions = (
        sessions.filter(
            Q(engaged_seconds__gt=15)
            | Q(pageview_count__gt=1)
            | Q(events__event_type__in=CONTACT_EVENTS)
        )
        .distinct()
        .count()
    )

    single_page = sessions.filter(pageview_count__lte=1).count()
    returning = visitors.filter(session_count__gt=1).count()

    return {
        "visitors": visitor_total,
        "visitors_new": visitor_total - returning,
        "visitors_returning": returning,
        "sessions": session_total,
        "sessions_per_visitor": round(session_total / visitor_total, 2) if visitor_total else 0,
        "pageviews": pageview_total,
        "pages_per_session": round(pageview_total / session_total, 2) if session_total else 0,
        "inquiries": inquiry_total,
        "inquiry_rate": _pct(inquiry_total, session_total),
        "tel_clicks": tel,
        "tel_rate": _pct(tel, session_total),
        "email_clicks": email,
        "email_rate": _pct(email, session_total),
        "bookings": booking,
        "contact_rate": _pct(tel + email + booking, session_total),
        "engaged_avg": engaged_avg,
        "engaged_avg_display": format_seconds(engaged_avg),
        "engaged_sessions": engaged_sessions,
        "engaged_share": _pct(engaged_sessions, session_total),
        "single_page": single_page,
        "single_page_share": _pct(single_page, session_total),
    }


# ---------------------------------------------------------------------------
# Sessions + inquiries per day
# ---------------------------------------------------------------------------


def _daily_series(sessions, inquiries, start):
    """Per-day visit and inquiry counts, zero-filled across the window."""
    session_rows = {
        row["day"]: row["n"]
        for row in sessions.annotate(day=TruncDate("started_at"))
        .values("day")
        .annotate(n=Count("id"))
    }
    inquiry_rows = {
        row["day"]: row["n"]
        for row in inquiries.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(n=Count("id"))
    }

    all_days = sorted(set(session_rows) | set(inquiry_rows))
    if not all_days:
        return {"days": [], "max": 0, "max_inquiries": 0}

    first_day = start.date() if start is not None else all_days[0]
    last_day = timezone.localdate()
    if first_day > last_day:
        first_day = last_day

    days = []
    cursor = first_day
    while cursor <= last_day:
        days.append(
            {
                "date": cursor,
                "sessions": session_rows.get(cursor, 0),
                "inquiries": inquiry_rows.get(cursor, 0),
            }
        )
        cursor += timedelta(days=1)

    peak = max(days, key=lambda d: d["sessions"])
    max_sessions = peak["sessions"] or 1

    for day in days:
        # Both segments share the visits scale, so the column total stays "this
        # day's visits" and the green tip is the converting share of them.
        # Scaling inquiries on their own axis would make a single inquiry a
        # full-height bar whenever the daily max is 1.
        converted = min(day["inquiries"], day["sessions"])
        day["height"] = _css_pct(day["sessions"] - converted, max_sessions)
        day["inquiry_height"] = _css_pct(converted, max_sessions)

    return {
        "days": days,
        "first": first_day,
        "last": last_day,
        "middle": days[len(days) // 2]["date"] if days else None,
        "peak": peak,
        "max": max_sessions,
    }


# ---------------------------------------------------------------------------
# Traffic sources
# ---------------------------------------------------------------------------


def _sources(sessions, attribution, session_total):
    """
    Traffic sources ranked by contact rate rather than raw volume.

    With attribution="first" the grouping switches to the visitor's original
    source, which credits the channel that found the customer rather than the
    one they happened to return through.
    """
    field = "visitor__first_source" if attribution == "first" else "source"
    detail_field = "visitor__first_source_detail" if attribution == "first" else "source_detail"

    rows = (
        sessions.values(field)
        .annotate(
            n=Count("id"),
            engaged=Sum("engaged_seconds"),
            tel=Count("events", filter=Q(events__event_type=EventType.TEL_CLICK), distinct=True),
            email=Count(
                "events",
                filter=Q(events__event_type=EventType.EMAIL_CLICK),
                distinct=True,
            ),
            bookings=Count("inquiries", distinct=True),
        )
        .order_by("-n")
    )

    labels = dict(TrafficSource.choices)

    # Detail strings ("google", "facebook") for the top sources, so each row can
    # show what it actually consists of.
    details = {}
    for row in (
        sessions.exclude(**{f"{detail_field}": ""})
        .values(field, detail_field)
        .annotate(n=Count("id"))
        .order_by("-n")
    ):
        details.setdefault(row[field], [])
        if len(details[row[field]]) < 3:
            details[row[field]].append(f"{row[detail_field]} {row['n']}")

    out = []
    for row in rows:
        key = row[field]
        contacts = row["tel"] + row["email"] + row["bookings"]
        engaged_avg = int((row["engaged"] or 0) / row["n"]) if row["n"] else 0
        out.append(
            {
                "key": key,
                "label": labels.get(key, key or "Okänd"),
                "detail": " \u00b7 ".join(details.get(key, [])),
                "sessions": row["n"],
                "share": _pct(row["n"], session_total),
                "engaged_display": format_seconds(engaged_avg),
                "tel": row["tel"],
                "bookings": row["bookings"],
                "contact_rate": _pct(contacts, row["n"]),
            }
        )

    out.sort(key=lambda r: (-r["contact_rate"], -r["sessions"]))
    if out:
        top = max(r["contact_rate"] for r in out) or 1
        for row in out:
            row["bar"] = _css_pct(row["contact_rate"], top)
    return out


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def _campaigns(sessions):
    """UTM-tagged visits, grouped down to term/content so ads can be compared."""
    rows = (
        sessions.exclude(utm_campaign="")
        .values("utm_campaign", "utm_source", "utm_medium", "utm_term", "utm_content")
        .annotate(
            n=Count("id"),
            engaged=Sum("engaged_seconds"),
            tel=Count("events", filter=Q(events__event_type=EventType.TEL_CLICK), distinct=True),
            bookings=Count("inquiries", distinct=True),
        )
        .order_by("-n")[:20]
    )

    out = []
    for row in rows:
        variant = " / ".join(part for part in (row["utm_term"], row["utm_content"]) if part)
        engaged_avg = int((row["engaged"] or 0) / row["n"]) if row["n"] else 0
        out.append(
            {
                "campaign": row["utm_campaign"],
                "source": row["utm_source"] or "\u2014",
                "medium": row["utm_medium"] or "\u2014",
                "variant": variant,
                "sessions": row["n"],
                "engaged_display": format_seconds(engaged_avg),
                "tel": row["tel"],
                "bookings": row["bookings"],
                "contact_rate": _pct(row["tel"] + row["bookings"], row["n"]),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _pages(pageviews, events):
    """Most viewed pages with title, engaged time and phone clicks."""
    rows = (
        pageviews.values("path")
        .annotate(
            views=Count("id"),
            engaged=Sum("engaged_seconds"),
            unique=Count("session", distinct=True),
        )
        .order_by("-views")[:15]
    )

    paths = [row["path"] for row in rows]

    # Latest non-empty title per path, so reports read in plain language.
    titles = {}
    for pv in (
        pageviews.filter(path__in=paths)
        .exclude(title="")
        .order_by("path", "-viewed_at")
        .values("path", "title")
    ):
        titles.setdefault(pv["path"], pv["title"])

    tel_by_path = dict(
        events.filter(event_type=EventType.TEL_CLICK, path__in=paths)
        .values_list("path")
        .annotate(n=Count("id"))
        .values_list("path", "n")
    )

    out = []
    for row in rows:
        engaged_avg = int((row["engaged"] or 0) / row["views"]) if row["views"] else 0
        out.append(
            {
                "path": row["path"],
                "title": titles.get(row["path"], ""),
                "views": row["views"],
                "unique": row["unique"],
                "engaged_display": format_seconds(engaged_avg),
                "tel": tel_by_path.get(row["path"], 0),
            }
        )
    return out


def _exit_pages(pageviews):
    """
    Last page of each visit.

    There is no exit event, but the ordering already tells us where people
    stopped: keep the pageview whose id matches the newest one in its session.
    A correlated subquery rather than DISTINCT ON, so this works on SQLite
    (development) as well as PostgreSQL (production).
    """
    newest_in_session = (
        PageView.objects.filter(session=OuterRef("session")).order_by("-viewed_at").values("pk")[:1]
    )

    rows = (
        pageviews.filter(pk=Subquery(newest_in_session))
        .values("path")
        .annotate(n=Count("id"))
        .order_by("-n")[:10]
    )

    totals = dict(pageviews.values_list("path").annotate(n=Count("id")).values_list("path", "n"))

    titles = {}
    for pv in pageviews.exclude(title="").order_by("path", "-viewed_at").values("path", "title"):
        titles.setdefault(pv["path"], pv["title"])

    return [
        {
            "path": row["path"],
            "title": titles.get(row["path"], ""),
            "exits": row["n"],
            "share": _pct(row["n"], totals.get(row["path"], 0)),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Inquiry funnel
# ---------------------------------------------------------------------------


def _funnel(pageviews, events):
    """
    Step-by-step drop-off through the inquiry wizard.

    Counted in sessions, not pageviews, so a reload doesn't look like progress.
    Abandonment reasons come from the form_error / form_abandon events.
    """
    counts = []
    for path, label in FUNNEL_STEPS:
        counts.append(
            {
                "path": path,
                "label": label,
                "sessions": pageviews.filter(path=path).values("session").distinct().count(),
            }
        )
    counts.append(
        {
            "path": FUNNEL_DONE_PREFIX,
            "label": "Klar - Tack-sidan",
            "sessions": pageviews.filter(path__startswith=FUNNEL_DONE_PREFIX)
            .values("session")
            .distinct()
            .count(),
        }
    )

    errors = _event_labels(events, EventType.FORM_ERROR)
    abandons = _event_labels(events, EventType.FORM_ABANDON)

    top = counts[0]["sessions"] or 1
    for index, step in enumerate(counts):
        step["width"] = _css_pct(step["sessions"], top)
        if index == 0:
            step["dropped"] = 0
            step["drop_share"] = 0
        else:
            previous = counts[index - 1]["sessions"]
            step["dropped"] = max(previous - step["sessions"], 0)
            step["drop_share"] = _pct(step["dropped"], previous)

        # Formuläret finns på många sidor och eventen etiketteras med sin
        # sida - i funneln summeras de, per-sida-nedbrytningen finns i
        # eventadminen. (Per-steg-mappningen var wizard-arv.)
        if index == len(counts) - 1:
            step["errors"] = sum(errors.values())
            step["abandons"] = sum(abandons.values())
        else:
            step["errors"] = 0
            step["abandons"] = 0

    return counts


def _event_labels(events, event_type):
    """
    Count events of a type per step label.

    form_error labels look like "Steg 2 - Kontaktuppgifter: phone", so the step
    is the part before the colon.
    """
    out = {}
    for row in (
        events.filter(event_type=event_type)
        .values_list("label")
        .annotate(n=Count("id"))
        .values_list("label", "n")
    ):
        label, count = row
        step = label.split(":")[0].strip()
        out[step] = out.get(step, 0) + count
    return out


# ---------------------------------------------------------------------------
# Placement + heatmap
# ---------------------------------------------------------------------------


def _placements(events):
    """Which button on the page actually produces phone calls."""
    rows = (
        events.filter(event_type=EventType.TEL_CLICK)
        .exclude(placement="")
        .values("placement")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    total = sum(row["n"] for row in rows)
    labels = dict(Placement.choices)
    return {
        "rows": [
            {
                "label": labels.get(row["placement"], row["placement"]),
                "clicks": row["n"],
                "share": _pct(row["n"], total),
            }
            for row in rows
        ],
        "total": total,
    }


def _heatmap(events):
    """
    Phone clicks by weekday and hour - staffing input for the phone.

    Bucketed into 6 intensity levels so the template can colour cells without
    doing arithmetic.
    """
    grid = {(d, h): 0 for d in range(7) for h in HEATMAP_HOURS}

    for event in events.filter(event_type=EventType.TEL_CLICK).only("created_at"):
        local = timezone.localtime(event.created_at)
        key = (local.weekday(), local.hour)
        if key in grid:
            grid[key] += 1

    peak = max(grid.values(), default=0)
    rows = []
    for day in range(7):
        cells = []
        for hour in HEATMAP_HOURS:
            count = grid[(day, hour)]
            level = 0 if not count or not peak else max(1, round(count * 5 / peak))
            cells.append({"hour": hour, "count": count, "level": level})
        rows.append({"day": WEEKDAY_NAMES[day], "cells": cells})

    return {"rows": rows, "hours": HEATMAP_HOURS, "peak": peak}


# ---------------------------------------------------------------------------
# Technology
# ---------------------------------------------------------------------------


def _devices(sessions, session_total):
    rows = (
        sessions.values("device_type")
        .annotate(
            n=Count("id"),
            tel=Count("events", filter=Q(events__event_type=EventType.TEL_CLICK), distinct=True),
            bookings=Count("inquiries", distinct=True),
        )
        .order_by("-n")
    )
    labels = dict(DeviceType.choices)
    return [
        {
            "label": labels.get(row["device_type"], row["device_type"]),
            "sessions": row["n"],
            "share": _pct(row["n"], session_total),
            "contact_rate": _pct(row["tel"] + row["bookings"], row["n"]),
        }
        for row in rows
    ]


# Buckets chosen to line up with common CSS breakpoints.
VIEWPORT_BUCKETS = [
    (0, 430, "360\u2013430 px"),
    (431, 767, "431\u2013767 px"),
    (768, 1023, "768\u20131023 px"),
    (1024, 1439, "1024\u20131439 px"),
    (1440, 99999, "1440 px och uppåt"),
]


def _viewports(sessions):
    out = []
    for low, high, label in VIEWPORT_BUCKETS:
        count = sessions.filter(viewport_width__gte=low, viewport_width__lte=high).count()
        out.append({"label": label, "sessions": count})
    missing = sessions.filter(viewport_width__isnull=True).count()
    return {"rows": out, "missing": missing}


def _browsers(sessions):
    return [
        {"label": row["browser"] or "Okänd", "sessions": row["n"]}
        for row in sessions.values("browser").annotate(n=Count("id")).order_by("-n")[:8]
    ]


def _loyalty(visitors):
    """How many visits each visitor has made."""
    buckets = [
        ("1 besök", Q(session_count__lte=1)),
        ("2 besök", Q(session_count=2)),
        ("3\u20134 besök", Q(session_count__gte=3, session_count__lte=4)),
        ("5\u20139 besök", Q(session_count__gte=5, session_count__lte=9)),
        ("10+ besök", Q(session_count__gte=10)),
    ]
    total = visitors.count()
    out = []
    for label, condition in buckets:
        # Count once per bucket, not twice.
        count = visitors.filter(condition).count()
        out.append({"label": label, "visitors": count, "share": _pct(count, total)})
    return out


# ---------------------------------------------------------------------------
# Recent inquiries with their journey
# ---------------------------------------------------------------------------


def _recent_inquiries(inquiries, limit=8):
    """
    Latest inquiries with the path the visitor took to get there.

    first vs last source is shown side by side because they often differ - the
    channel that found the customer isn't always the one they converted on.
    """
    rows = inquiries.select_related("analytics_session", "analytics_session__visitor").order_by(
        "-created_at"
    )[:limit]

    labels = dict(TrafficSource.choices)
    out = []
    for inquiry in rows:
        session = inquiry.analytics_session
        journey = []
        first_label = ""
        first_detail = ""
        visits_before = None

        if session is not None:
            journey = [
                {"title": pv.title, "path": pv.path}
                for pv in session.pageviews.order_by("viewed_at")[:8]
            ]
            visitor = session.visitor
            if visitor is not None:
                first_label = labels.get(visitor.first_source, visitor.first_source)
                first_detail = visitor.first_source_detail
                visits_before = visitor.session_count

        out.append(
            {
                "reference": inquiry.reference,
                "created_at": inquiry.created_at,
                "first_label": first_label,
                "first_detail": first_detail,
                "last_label": labels.get(inquiry.traffic_source, inquiry.traffic_source)
                or "\u2014",
                "last_detail": inquiry.traffic_source_detail,
                "journey": journey,
                "visits_before": visits_before,
                "engaged_display": session.engaged_display if session else "",
            }
        )
    return out
