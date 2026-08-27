"""
Tests for /manage/statistik/.

Beyond checking the page renders, these pin down two things that are easy to
break silently: the aggregation queries must run on both database backends
(SQLite in development, PostgreSQL in production), and the panels that depend on
newer fields must actually surface values rather than quietly showing zero.
"""

import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.analytics.models import (
    Event,
    EventType,
    PageView,
    Placement,
    Session,
    TrafficSource,
    Visitor,
)
from apps.analytics.tracking import record_event, record_pageview
from apps.inquiries.models import Inquiry


def make_session(**kwargs):
    visitor = kwargs.pop("visitor", None) or Visitor.objects.create(uuid=uuid.uuid4())
    defaults = {
        "uuid": uuid.uuid4(),
        "visitor": visitor,
        "source": TrafficSource.ORGANIC,
        "source_detail": "google",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


class StatsAccessTests(TestCase):
    """The report is locked to superusers while it is being finalised."""

    def setUp(self):
        self.url = reverse("manage:stats")

    def test_anonymous_is_redirected_to_login(self):
        res = Client().get(self.url)
        self.assertEqual(res.status_code, 302)

    def test_staff_without_superuser_gets_404(self):
        """
        404, not a redirect: LOGIN_URL is /manage/login/, so redirecting an
        already logged-in customer would loop them through the login page.
        """
        user = get_user_model().objects.create_user(
            username="kund", password="pw12345678", is_staff=True
        )
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(self.url).status_code, 404)

    def test_superuser_gets_in(self):
        user = get_user_model().objects.create_user(
            username="dev", password="pw12345678", is_staff=True, is_superuser=True
        )
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(self.url).status_code, 200)

    def test_nav_link_only_renders_for_superusers(self):
        User = get_user_model()
        cases = [
            (User.objects.create_user("k2", password="pw12345678", is_staff=True), False),
            (
                User.objects.create_user(
                    "d2", password="pw12345678", is_staff=True, is_superuser=True
                ),
                True,
            ),
        ]
        for user, should_see in cases:
            client = Client()
            client.force_login(user)
            body = client.get(reverse("manage:dashboard")).content.decode()
            self.assertEqual(">Statistik<" in body, should_see, user.get_username())


class StatsPageTests(TestCase):
    def setUp(self):
        # The report is superuser-only, so the fixture user needs that flag.
        user = get_user_model().objects.create_user(
            username="staff", password="pw12345678", is_staff=True, is_superuser=True
        )
        self.client = Client()
        self.client.force_login(user)
        self.url = reverse("manage:stats")

    def test_empty_state(self):
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Ingen trafik registrerad")

    def test_renders_values_from_the_newer_fields(self):
        now = timezone.now()
        visitor = Visitor.objects.create(
            uuid=uuid.uuid4(),
            session_count=2,
            first_source=TrafficSource.SOCIAL,
            first_source_detail="facebook",
        )
        session = make_session(
            visitor=visitor,
            source=TrafficSource.PAID,
            source_detail="google",
            utm_campaign="varmepump-host",
            utm_source="google",
            utm_medium="cpc",
            utm_term="varmepump",
            utm_content="annons-a",
            device_type="mobile",
            browser="Safari",
            viewport_width=390,
            viewport_height=844,
            engaged_seconds=95,
            pageview_count=3,
            started_at=now,
            last_activity=now,
        )
        for path, title in (
            ("/tjanster/varmepump/", "Värmepump"),
            ("/forfragan/", "Skicka en förfrågan"),
            ("/forfragan/kontaktuppgifter/", "Kontaktuppgifter"),
        ):
            record_pageview(session, path, title=title)

        record_event(
            session,
            EventType.TEL_CLICK,
            label="08-123",
            path="/tjanster/varmepump/",
            placement=Placement.STICKY,
        )
        record_event(
            session,
            EventType.FORM_ERROR,
            label="Steg 2 - Kontaktuppgifter: phone",
            path="/forfragan/kontaktuppgifter/",
        )

        body = self.client.get(self.url).content.decode()

        self.assertIn("Värmepump", body)  # page title (fix 7)
        self.assertIn("varmepump-host", body)  # campaign
        self.assertIn("annons-a", body)  # utm_content (fix 17)
        self.assertIn("Fast knapp", body)  # placement label (fix 15)
        self.assertIn("1m 35s", body)  # engaged time 95s (fix 13)
        self.assertIn("360\u2013430 px", body)  # viewport bucket (fix 19)

    def test_style_attributes_use_dot_decimals(self):
        """
        Bar widths must not be localised.

        LANGUAGE_CODE is "sv", so a float in a template renders as "47,6".
        Inside style="width:47,6%" that is invalid CSS and the bar silently
        collapses to its min-height - the chart looks empty even with data.
        """
        import re

        now = timezone.now()
        for index in range(3):
            session = make_session(started_at=now, last_activity=now)
            for _ in range(index + 1):
                record_pageview(session, "/", title="Start")
            record_event(session, EventType.TEL_CLICK, placement=Placement.HEADER)

        body = self.client.get(self.url).content.decode()
        styles = re.findall(r'style="[^"]*"', body)

        self.assertTrue(styles, "no inline styles rendered at all")
        for style in styles:
            self.assertNotIn(",", style, f"localised number in {style}")

    def test_daily_bars_are_not_all_collapsed(self):
        """A day at the peak must render a full-height bar, not a stub."""
        from apps.manage.stats_views import _daily_series

        now = timezone.now()
        for _ in range(5):
            make_session(started_at=now, last_activity=now)

        series = _daily_series(Session.objects.all(), Inquiry.objects.none(), None)
        heights = [day["height"] for day in series["days"]]
        self.assertIn("100.0", heights)

    def test_single_daily_inquiry_does_not_dominate_the_column(self):
        """
        With a daily max of one inquiry, the green segment must stay a small
        share of the visits scale rather than becoming a full-height bar.
        """
        from apps.manage.stats_views import _daily_series

        now = timezone.now()
        for _ in range(20):
            make_session(started_at=now, last_activity=now)
        Inquiry.objects.create(
            customer_type="private",
            name="Test Testsson",
            email="test@example.com",
            phone="070-0000000",
            street_address="Exempelgatan 1",
            postal_code="11122",
            city="Stockholm",
            description="Läckande kran",
        )

        series = _daily_series(Session.objects.all(), Inquiry.objects.all(), None)
        today = series["days"][-1]
        self.assertEqual(today["inquiry_height"], "5.0")
        self.assertEqual(today["height"], "95.0")

    def test_attribution_switch_changes_the_grouping(self):
        visitor = Visitor.objects.create(
            uuid=uuid.uuid4(),
            session_count=1,
            first_source=TrafficSource.SOCIAL,
            first_source_detail="facebook",
        )
        make_session(visitor=visitor, source=TrafficSource.ORGANIC)

        last = self.client.get(self.url, {"attribution": "last"}).content.decode()
        first = self.client.get(self.url, {"attribution": "first"}).content.decode()

        self.assertIn("Sökmotor", last)
        self.assertIn("Sociala medier", first)

    def test_all_periods_render(self):
        make_session()
        for period in ("7", "30", "90", "365", "all"):
            res = self.client.get(self.url, {"period": period})
            self.assertEqual(res.status_code, 200, period)

    def test_unknown_period_falls_back_instead_of_erroring(self):
        make_session()
        res = self.client.get(self.url, {"period": "; drop table"})
        self.assertEqual(res.status_code, 200)


class StatsQueryTests(TestCase):
    """
    The aggregation helpers, exercised directly.

    _exit_pages uses a correlated subquery specifically so it runs on SQLite as
    well as PostgreSQL; an earlier DISTINCT ON version worked only on Postgres.
    """

    def test_exit_pages_picks_the_last_view_per_session(self):
        from apps.manage.stats_views import _exit_pages

        session = make_session()
        record_pageview(session, "/", title="Start")
        record_pageview(session, "/tjanster/", title="Tjänster")
        record_pageview(session, "/kontakt/", title="Kontakt")

        other = make_session()
        record_pageview(other, "/", title="Start")

        rows = {row["path"]: row["exits"] for row in _exit_pages(PageView.objects.all())}
        self.assertEqual(rows.get("/kontakt/"), 1)
        self.assertEqual(rows.get("/"), 1)
        self.assertNotIn("/tjanster/", rows)

    def test_funnel_counts_sessions_not_pageviews(self):
        from apps.manage.stats_views import _funnel

        session = make_session()
        # A reload of step 1 must not look like two people.
        record_pageview(session, "/forfragan/")
        record_pageview(session, "/forfragan/")
        record_pageview(session, "/forfragan/kontaktuppgifter/")

        funnel = _funnel(PageView.objects.all(), Event.objects.all())
        self.assertEqual(len(funnel), 4)
        self.assertEqual(funnel[0]["sessions"], 1)
        self.assertEqual(funnel[1]["sessions"], 1)
        self.assertEqual(funnel[2]["sessions"], 0)

    def test_funnel_counts_normalized_thank_you_page(self):
        from apps.manage.stats_views import _funnel

        session = make_session()
        record_pageview(session, "/forfragan/")
        record_pageview(session, "/forfragan/tack/SKV-2026-0412/")

        funnel = _funnel(PageView.objects.all(), Event.objects.all())
        self.assertEqual(funnel[3]["sessions"], 1)

    def test_loyalty_buckets_sum_to_the_visitor_count(self):
        from apps.manage.stats_views import _loyalty

        for count in (1, 1, 2, 3, 7, 12):
            Visitor.objects.create(uuid=uuid.uuid4(), session_count=count)

        rows = _loyalty(Visitor.objects.all())
        self.assertEqual(sum(row["visitors"] for row in rows), 6)

    def test_placement_report_ignores_events_without_placement(self):
        from apps.manage.stats_views import _placements

        session = make_session()
        record_event(session, EventType.TEL_CLICK, placement=Placement.FOOTER)
        record_event(session, EventType.TEL_CLICK)  # unknown placement

        report = _placements(Event.objects.all())
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["rows"][0]["share"], 100.0)


class FocalPointTests(TestCase):
    """The focal point lives on the MediaFile: picked once in the library,
    consumed everywhere the image is cover-cropped (hero, cards).

    media_update serves two independent forms on the same endpoint - the
    alt-text form and the focal picker - and neither may blank the other's
    field just because it wasn't part of the POST.
    """

    def setUp(self):
        from apps.website.models import MediaFile

        self.user = get_user_model().objects.create_user(username="redaktor", password="testpass")
        self.client = Client()
        self.client.force_login(self.user)
        self.media = MediaFile.objects.create(
            file="media/hero.webp",
            original_filename="hero.webp",
            alt_text="Rörmokare i arbete",
            mime_type="image/webp",
        )

    def _update_url(self):
        return reverse("manage:media_update", args=[self.media.pk])

    def test_focal_css_defaults_to_center(self):
        self.assertEqual(self.media.focal_css, "50% 50%")

    def test_focal_update_keeps_alt_text(self):
        response = self.client.post(
            self._update_url(),
            {"focal_x": 20, "focal_y": 80},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.media.refresh_from_db()
        self.assertEqual((self.media.focal_x, self.media.focal_y), (20, 80))
        self.assertEqual(self.media.alt_text, "Rörmokare i arbete")
        self.assertEqual(self.media.focal_css, "20% 80%")

    def test_alt_update_keeps_focal(self):
        self.media.focal_x, self.media.focal_y = 10, 90
        self.media.save(update_fields=["focal_x", "focal_y"])
        self.client.post(self._update_url(), {"alt_text": "Ny text"})
        self.media.refresh_from_db()
        self.assertEqual(self.media.alt_text, "Ny text")
        self.assertEqual((self.media.focal_x, self.media.focal_y), (10, 90))

    def test_out_of_range_focal_is_clamped(self):
        self.client.post(
            self._update_url(),
            {"focal_x": 150, "focal_y": -3},
            HTTP_ACCEPT="application/json",
        )
        self.media.refresh_from_db()
        self.assertEqual((self.media.focal_x, self.media.focal_y), (100, 0))

    def test_junk_focal_keeps_stored_value(self):
        self.media.focal_x, self.media.focal_y = 30, 40
        self.media.save(update_fields=["focal_x", "focal_y"])
        self.client.post(
            self._update_url(),
            {"focal_x": "abc", "focal_y": ""},
            HTTP_ACCEPT="application/json",
        )
        self.media.refresh_from_db()
        self.assertEqual((self.media.focal_x, self.media.focal_y), (30, 40))

    def test_library_page_offers_the_picker(self):
        response = self.client.get(reverse("manage:media_library"))
        self.assertContains(response, "data-focal-picker")
        self.assertContains(response, 'data-x="50"')


class AutoOptimizeTests(TestCase):
    """Uploads optimize themselves; "Optimera alla" sweeps up the rest.

    The bulk run is one request per image (driven from manage.js), so no
    single request can grow with library size and hit a proxy timeout.
    """

    def setUp(self):
        import tempfile

        from django.test import override_settings

        self._media_override = override_settings(MEDIA_ROOT=tempfile.mkdtemp())
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

        self.user = get_user_model().objects.create_user(username="optimerare", password="testpass")
        self.client = Client()
        self.client.force_login(self.user)

    def _png(self, width=2400, height=1200, name="stor.png"):
        from io import BytesIO

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (width, height), (120, 80, 40)).save(buf, "PNG")
        return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")

    def test_upload_optimizes_automatically(self):
        from apps.website.models import MediaFile

        response = self.client.post(reverse("manage:media_upload"), {"file": self._png()})
        self.assertEqual(response.status_code, 302)
        media = MediaFile.objects.latest("created_at")
        self.assertTrue(media.is_optimized)
        self.assertEqual(media.mime_type, "image/webp")
        self.assertEqual(media.width, 1920)
        self.assertTrue(media.original_file, "originalet ska bevaras för Återställ")

    def test_small_webp_upload_is_left_alone(self):
        from io import BytesIO

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        from apps.website.models import MediaFile

        buf = BytesIO()
        Image.new("RGB", (400, 300), (10, 20, 30)).save(buf, "WEBP")
        upload = SimpleUploadedFile("liten.webp", buf.getvalue(), content_type="image/webp")
        self.client.post(reverse("manage:media_upload"), {"file": upload})
        media = MediaFile.objects.latest("created_at")
        self.assertFalse(media.is_optimized)
        self.assertEqual(media.mime_type, "image/webp")

    def test_optimize_endpoint_answers_json(self):
        from django.core.files.base import ContentFile

        from apps.website.models import MediaFile

        png = self._png(width=2200, height=1100)
        media = MediaFile(
            original_filename="rakning.png",
            mime_type="image/png",
            file_size=png.size,
            width=2200,
            height=1100,
        )
        media.file.save("rakning.png", ContentFile(png.read()), save=True)

        response = self.client.post(
            reverse("manage:media_optimize", args=[media.pk]),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        media.refresh_from_db()
        self.assertTrue(media.is_optimized)

    def test_optimize_endpoint_reports_failure_as_json(self):
        from apps.website.models import MediaFile

        media = MediaFile.objects.create(
            original_filename="borta.png",
            file="media/finns-inte.png",
            mime_type="image/png",
            file_size=500_000,
            width=2500,
            height=1400,
        )
        response = self.client.post(
            reverse("manage:media_optimize", args=[media.pk]),
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])

    def test_bulk_button_only_shows_when_something_needs_it(self):
        from apps.website.models import MediaFile

        response = self.client.get(reverse("manage:media_library"))
        self.assertNotContains(response, "data-optimize-all")

        MediaFile.objects.create(
            original_filename="opt.png",
            file="media/opt.png",
            mime_type="image/png",
            file_size=500_000,
            width=2500,
            height=1400,
        )
        response = self.client.get(reverse("manage:media_library"))
        self.assertContains(response, "data-optimize-all")
        self.assertContains(response, "Optimera alla bilder (1)")
