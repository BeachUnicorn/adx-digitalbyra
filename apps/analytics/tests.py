"""
Tests for the analytics app.

Two halves: the dependency-free parsing/classification helpers, then the
tracking pipeline that writes to the database (session resolution, middleware,
beacon). The pipeline half matters because the middleware swallows every
exception by design so analytics can never break a page - a regression there
surfaces as quietly wrong numbers months later rather than as an error.
"""

import uuid

from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from .middleware import _extract_title
from .models import (
    DeviceType,
    Event,
    EventType,
    PageView,
    Placement,
    Session,
    TrafficSource,
    Visitor,
)
from .tracking import (
    SESSION_COOKIE,
    VISITOR_COOKIE,
    record_engagement,
    record_event,
    record_pageview,
    resolve_visitor_and_session,
)
from .utils import (
    anonymize_ip,
    classify_referrer,
    classify_source,
    is_bot,
    normalize_path,
    parse_user_agent,
)

CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


class BotDetectionTests(TestCase):
    def test_known_bots(self):
        self.assertTrue(is_bot("Googlebot/2.1 (+http://www.google.com/bot.html)"))
        self.assertTrue(is_bot("python-requests/2.31.0"))
        self.assertTrue(is_bot(""))  # empty UA treated as bot

    def test_real_browser_not_bot(self):
        self.assertFalse(is_bot(CHROME_MAC))


class UserAgentParsingTests(TestCase):
    def test_chrome_mac_desktop(self):
        info = parse_user_agent(CHROME_MAC)
        self.assertEqual(info["os"], "macOS")
        self.assertEqual(info["browser"], "Chrome")
        self.assertEqual(info["device_type"], DeviceType.DESKTOP)

    def test_iphone_mobile(self):
        info = parse_user_agent(SAFARI_IPHONE)
        self.assertEqual(info["os"], "iOS")
        self.assertEqual(info["browser"], "Safari")
        self.assertEqual(info["device_type"], DeviceType.MOBILE)

    def test_bot_device_type(self):
        info = parse_user_agent("Googlebot/2.1")
        self.assertEqual(info["device_type"], DeviceType.BOT)


class ReferrerClassificationTests(TestCase):
    def test_direct(self):
        self.assertEqual(classify_referrer(""), (TrafficSource.DIRECT, ""))

    def test_google_organic(self):
        src, detail = classify_referrer("https://www.google.com/search?q=vvs")
        self.assertEqual(src, TrafficSource.ORGANIC)
        self.assertEqual(detail, "google")

    def test_facebook_social(self):
        src, detail = classify_referrer("https://www.facebook.com/")
        self.assertEqual(src, TrafficSource.SOCIAL)
        self.assertEqual(detail, "facebook")

    def test_internal(self):
        src, _ = classify_referrer("https://adx.se/tjanster/", "adx.se")
        self.assertEqual(src, TrafficSource.INTERNAL)

    def test_other_referral(self):
        src, detail = classify_referrer("https://someblog.example/post")
        self.assertEqual(src, TrafficSource.REFERRAL)
        self.assertEqual(detail, "someblog.example")


class SourceClassificationTests(TestCase):
    def test_utm_paid_wins_over_referrer(self):
        src, _ = classify_source("https://www.google.com/", "google", "cpc", "adx.se")
        self.assertEqual(src, TrafficSource.PAID)

    def test_utm_email(self):
        src, detail = classify_source("", "newsletter", "email")
        self.assertEqual(src, TrafficSource.EMAIL)
        self.assertEqual(detail, "newsletter")

    def test_falls_back_to_referrer(self):
        src, detail = classify_source("https://www.bing.com/search", "", "")
        self.assertEqual(src, TrafficSource.ORGANIC)
        self.assertEqual(detail, "bing")


class IPAnonymizationTests(TestCase):
    def test_ipv4_last_octet_masked(self):
        self.assertEqual(anonymize_ip("192.168.1.55"), "192.168.1.0")

    def test_ipv6_masked(self):
        result = anonymize_ip("2001:db8:abcd:1234:5678:9abc:def0:1234")
        self.assertTrue(result.startswith("2001:db8:abcd"))

    def test_invalid_ip_returns_none(self):
        self.assertIsNone(anonymize_ip("not-an-ip"))
        self.assertIsNone(anonymize_ip(""))


# ===========================================================================
# Tracking pipeline: session resolution, middleware, beacon.
# ===========================================================================


def make_session(**kwargs):
    """A persisted Session with a Visitor, for tests that need one to exist."""
    visitor = kwargs.pop("visitor", None) or Visitor.objects.create(uuid=uuid.uuid4())
    defaults = {
        "uuid": uuid.uuid4(),
        "visitor": visitor,
        "source": TrafficSource.ORGANIC,
        "source_detail": "google",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def browser_request(path="/", visitor=None, **extra):
    request = RequestFactory().get(path, HTTP_USER_AGENT=CHROME_MAC, **extra)
    if visitor is not None:
        request.COOKIES[VISITOR_COOKIE] = str(visitor.uuid)
    return request


class PathNormalizationTests(TestCase):
    def test_thank_you_reference_is_collapsed(self):
        self.assertEqual(
            normalize_path("/forfragan/tack/SKV-2026-0412/"),
            "/forfragan/tack/:referens/",
        )

    def test_works_without_trailing_slash(self):
        self.assertEqual(
            normalize_path("/forfragan/tack/SKV-2026-0412"),
            "/forfragan/tack/:referens/",
        )

    def test_content_slugs_are_left_alone(self):
        """Service slugs are a bounded set and must stay readable."""
        for path in ("/tjanster/stambyte/", "/faq/", "/", "/om-oss/"):
            self.assertEqual(normalize_path(path), path)

    def test_wizard_steps_are_left_alone(self):
        self.assertEqual(
            normalize_path("/forfragan/kontaktuppgifter/"),
            "/forfragan/kontaktuppgifter/",
        )

    def test_empty_path(self):
        self.assertEqual(normalize_path(""), "")

    def test_applied_when_recording(self):
        session = make_session()
        record_pageview(session, "/forfragan/tack/SKV-2026-0001/", title="Tack")
        record_event(session, EventType.TEL_CLICK, path="/forfragan/tack/SKV-2026-0001/")
        self.assertEqual(PageView.objects.get().path, "/forfragan/tack/:referens/")
        self.assertEqual(Event.objects.get().path, "/forfragan/tack/:referens/")

    def test_still_matches_the_funnel_prefix(self):
        """The funnel report matches on /forfragan/tack/ - keep that working."""
        self.assertTrue(normalize_path("/forfragan/tack/SKV-1/").startswith("/forfragan/tack/"))


class TitleExtractionTests(TestCase):
    def test_strips_site_suffix_and_unescapes_entities(self):
        response = HttpResponse("<html><head><title>Stambyte &amp; relining - ADX</title></head>")
        self.assertEqual(_extract_title(response, site_name="ADX"), "Stambyte & relining")

    def test_collapses_template_whitespace(self):
        response = HttpResponse("<html><head><title>\n  Om\n   oss\n</title></head>")
        self.assertEqual(_extract_title(response), "Om oss")

    def test_missing_title_returns_empty(self):
        self.assertEqual(_extract_title(HttpResponse("<html></html>")), "")

    def test_non_html_body_does_not_raise(self):
        self.assertEqual(_extract_title(HttpResponse("not html at all")), "")


class SessionCountTests(TestCase):
    def test_returning_visitor_increments(self):
        """Must go 1 -> 2. A read-modify-write here loses concurrent visits."""
        visitor = Visitor.objects.create(uuid=uuid.uuid4(), session_count=1)
        make_session(visitor=visitor)

        resolve_visitor_and_session(browser_request(visitor=visitor))

        visitor.refresh_from_db()
        self.assertEqual(visitor.session_count, 2)

    def test_new_visitor_starts_at_one(self):
        result = resolve_visitor_and_session(browser_request())
        self.assertIsNotNone(result)
        self.assertEqual(result.session.visitor.session_count, 1)

    def test_bots_are_not_tracked(self):
        request = RequestFactory().get("/", HTTP_USER_AGENT="Googlebot/2.1")
        self.assertIsNone(resolve_visitor_and_session(request))
        self.assertEqual(Session.objects.count(), 0)


class InternalSourceCarryOverTests(TestCase):
    def test_resumed_visit_keeps_the_original_source(self):
        """
        A visit resuming after the 30-minute window has our own domain as the
        referrer. Recording that as "internal" would overwrite the real channel.
        """
        visitor = Visitor.objects.create(uuid=uuid.uuid4(), session_count=1)
        make_session(visitor=visitor, source=TrafficSource.PAID, source_detail="google")

        result = resolve_visitor_and_session(
            browser_request("/tjanster/", visitor=visitor, HTTP_REFERER="http://testserver/")
        )

        self.assertEqual(result.session.source, TrafficSource.PAID)
        self.assertEqual(result.session.source_detail, "google")

    def test_external_referrer_is_not_overridden(self):
        visitor = Visitor.objects.create(uuid=uuid.uuid4(), session_count=1)
        make_session(visitor=visitor, source=TrafficSource.PAID)

        result = resolve_visitor_and_session(
            browser_request("/", visitor=visitor, HTTP_REFERER="https://www.google.com/search")
        )
        self.assertEqual(result.session.source, TrafficSource.ORGANIC)


class UtmCaptureTests(TestCase):
    def test_term_and_content_are_stored(self):
        request = RequestFactory().get(
            "/",
            {
                "utm_source": "google",
                "utm_medium": "cpc",
                "utm_campaign": "varmepump-host",
                "utm_term": "varmepump pris",
                "utm_content": "annons-a",
            },
            HTTP_USER_AGENT=CHROME_MAC,
        )
        session = resolve_visitor_and_session(request).session
        self.assertEqual(session.utm_term, "varmepump pris")
        self.assertEqual(session.utm_content, "annons-a")
        self.assertEqual(session.source, TrafficSource.PAID)


class EngagementTests(TestCase):
    def test_accumulates_on_both_session_and_pageview(self):
        session = make_session()
        record_pageview(session, "/tjanster/", title="Tjänster")

        record_engagement(session, "/tjanster/", 12)
        record_engagement(session, "/tjanster/", 8)

        session.refresh_from_db()
        self.assertEqual(session.engaged_seconds, 20)
        self.assertEqual(PageView.objects.get().engaged_seconds, 20)

    def test_single_call_is_capped(self):
        session = make_session()
        record_pageview(session, "/", title="Start")
        record_engagement(session, "/", 999999)
        session.refresh_from_db()
        self.assertEqual(session.engaged_seconds, 300)

    def test_junk_values_are_ignored(self):
        session = make_session()
        record_pageview(session, "/", title="Start")
        for junk in ("abc", None, -5, 0, [1]):
            record_engagement(session, "/", junk)
        session.refresh_from_db()
        self.assertEqual(session.engaged_seconds, 0)


class PlacementTests(TestCase):
    def test_known_placement_is_stored(self):
        session = make_session()
        record_event(session, EventType.TEL_CLICK, placement=Placement.FOOTER)
        self.assertEqual(Event.objects.get().placement, Placement.FOOTER)

    def test_forged_placement_is_discarded(self):
        session = make_session()
        record_event(session, EventType.TEL_CLICK, placement="<script>evil")
        self.assertEqual(Event.objects.get().placement, "")

    def test_unknown_event_type_falls_back_to_other(self):
        session = make_session()
        record_event(session, "not_a_real_type")
        self.assertEqual(Event.objects.get().event_type, EventType.OTHER)


class BeaconTests(TestCase):
    def setUp(self):
        cache.clear()
        self.session = make_session()
        self.client = Client(HTTP_USER_AGENT=CHROME_MAC)
        self.client.cookies[SESSION_COOKIE] = str(self.session.uuid)
        self.url = reverse("analytics:beacon")

    def post(self, payload):
        return self.client.post(self.url, data=payload, content_type="application/json")

    def test_screen_stores_resolution_and_viewport(self):
        res = self.post({"type": "screen", "w": 1920, "h": 1080, "vw": 1440, "vh": 900})
        self.assertEqual(res.status_code, 204)
        self.session.refresh_from_db()
        self.assertEqual(self.session.screen_resolution, "1920x1080")
        self.assertEqual(self.session.viewport_width, 1440)
        self.assertEqual(self.session.viewport_height, 900)

    def test_screen_rejects_nonsense_dimensions(self):
        self.post({"type": "screen", "w": -1, "h": 0, "vw": 10**9, "vh": True})
        self.session.refresh_from_db()
        self.assertEqual(self.session.screen_resolution, "")
        self.assertIsNone(self.session.viewport_width)

    def test_click_records_type_and_placement(self):
        self.post(
            {
                "type": "event",
                "event": "tel",
                "label": "08-123",
                "path": "/kontakt/",
                "placement": "header",
            }
        )
        event = Event.objects.get()
        self.assertEqual(event.event_type, EventType.TEL_CLICK)
        self.assertEqual(event.placement, Placement.HEADER)

    def test_engagement_heartbeat(self):
        record_pageview(self.session, "/", title="Start")
        self.assertEqual(
            self.post({"type": "engagement", "seconds": 15, "path": "/"}).status_code,
            204,
        )
        self.session.refresh_from_db()
        self.assertEqual(self.session.engaged_seconds, 15)

    def test_form_abandonment(self):
        self.post(
            {
                "type": "form",
                "event": "abandon",
                "label": "Steg 2 - Kontaktuppgifter",
                "path": "/forfragan/kontaktuppgifter/",
            }
        )
        self.assertEqual(Event.objects.get().event_type, EventType.FORM_ABANDON)

    def test_no_session_cookie_is_a_noop(self):
        client = Client(HTTP_USER_AGENT=CHROME_MAC)
        self.assertEqual(
            client.post(
                self.url,
                data={"type": "engagement", "seconds": 5, "path": "/"},
                content_type="application/json",
            ).status_code,
            204,
        )
        self.assertEqual(Event.objects.count(), 0)

    def test_malformed_payloads_are_rejected(self):
        for body in ("[]", "{oops", '"a string"'):
            res = self.client.post(self.url, data=body, content_type="application/json")
            self.assertEqual(res.status_code, 400, body)

    def test_rate_limited_past_the_ceiling(self):
        codes = {
            self.post({"type": "engagement", "seconds": 1, "path": "/"}).status_code
            for _ in range(60)
        }
        self.assertIn(429, codes)

    def test_bots_get_no_further_than_204(self):
        client = Client(HTTP_USER_AGENT="python-requests/2.31")
        client.cookies[SESSION_COOKIE] = str(self.session.uuid)
        res = client.post(
            self.url,
            data={"type": "event", "event": "tel"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 204)
        self.assertEqual(Event.objects.count(), 0)


class UtmSanitationTests(TestCase):
    """
    Sårbarhetsskannrar skickar HTML-payloads i utm-parametrarna. Mallarna
    escapar (ingen XSS körs), men varje payload blev en egen "kampanj" i
    statistiken - omöjlig att radera eftersom den bara är värden på
    sessionsrader. Skräp ska inte lagras alls.
    """

    def test_payloads_are_rejected(self):
        from apps.analytics.tracking import _clean_utm

        for bad in (
            "'>\"></script><svg/onload=confirm('xss')>",
            "<img src=x>",
            "a=b&c=d",
            "javascript:alert(1)",
        ):
            with self.subTest(value=bad):
                self.assertEqual(_clean_utm(bad), "")

    def test_overlong_but_legit_value_is_truncated_not_dropped(self):
        from apps.analytics.tracking import _clean_utm

        self.assertEqual(_clean_utm("x" * 150), "x" * 100)

    def test_real_campaign_names_survive(self):
        from apps.analytics.tracking import _clean_utm

        for good in ("sommar-2026", "google_cpc", "VVS Bromma", "höst.kampanj", "50% rabatt"):
            with self.subTest(value=good):
                self.assertEqual(_clean_utm(good), good)

    def test_session_stores_no_garbage(self):
        from apps.analytics.models import Session

        self.client.get("/", {"utm_campaign": "<svg/onload=x>", "utm_source": "kampanj-ok"})
        row = Session.objects.order_by("-started_at").first()
        if row is not None:
            self.assertEqual(row.utm_campaign, "")
            self.assertEqual(row.utm_source, "kampanj-ok")
