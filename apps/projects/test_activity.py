"""
Kontakternas senaste aktivitet (apps/projects/activity.py) och kundkortets
rad om varje kontakt: inloggad, aktiv senast, aldrig ett lösenord.
"""

import io
import re
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, JsonResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount

from . import activity
from .models import ActivityArea, ContactActivity, Customer

User = get_user_model()

#: Det en webbläsare skickar när någon öppnar en sida.
NAVIGATE = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Accept": "text/html"}
#: Det fetch() skickar från en sida som uppdaterar sig själv.
FETCH = {"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Accept": "text/html"}

#: Mitt på dagen i svensk tid: "i dag" och "i går" kan inte slå om mitt i
#: ett test, som de kunde strax efter midnatt med den riktiga klockan.
NOON = datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("Europe/Stockholm"))


class Clock:
    """timezone.now() som testet flyttar fram själv."""

    def __init__(self, start=NOON):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def clock(start=NOON):
    return mock.patch("django.utils.timezone.now", new=Clock(start))


def _activity_queries(queries):
    return [q["sql"] for q in queries if "projects_contactactivity" in q["sql"]]


def _text(html):
    """Det man ser, utan taggar och med ett mellanslag mellan orden. Punkten
    mellan delarna (&middot;) blir |."""
    return " ".join(re.sub(r"<[^>]+>", " ", html).replace("&middot;", "|").split())


class ActivityFixture:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Acme AB")
        cls.other = Customer.objects.create(name="Annan AB")
        cls.anna = cls.contact("anna@acme.se", cls.acme)
        cls.bo = cls.contact("bo@acme.se", cls.acme, first_name="Bo")
        FlamingoAccount.objects.create(customer=cls.acme, is_enabled=True)

    @staticmethod
    def contact(email, *customers, **fields):
        user = User.objects.create_user(email, email=email, **fields)
        user.set_unusable_password()
        user.save()
        for customer in customers:
            customer.users.add(user)
        return user

    def setUp(self):
        # Spärren mot täta skrivningar ligger i processens cache.
        cache.clear()

    def login(self, user):
        client = Client()
        client.force_login(user)
        return client

    def as_anna(self):
        return self.login(self.anna)

    def as_staff(self):
        return self.login(self.staff)

    def seen(self, user=None):
        return ContactActivity.objects.filter(user=user or self.anna).first()

    def active(self, user, at, area=ActivityArea.PORTAL, customer=None):
        return ContactActivity.objects.create(
            user=user, last_seen_at=at, last_area=area, last_customer=customer or self.acme
        )


class RecordingTests(ActivityFixture, TestCase):
    """Vad som räknas som en sidvisning, och vems."""

    def test_a_page_in_the_portal_is_recorded_as_the_portal(self):
        before = timezone.now()
        response = self.as_anna().get("/kund/tavla/", headers=NAVIGATE)
        self.assertEqual(response.status_code, 200)
        row = self.seen()
        self.assertEqual(row.last_area, ActivityArea.PORTAL)
        self.assertEqual(row.last_customer, self.acme)
        self.assertGreaterEqual(row.last_seen_at, before)

    def test_a_page_in_flamingo_is_recorded_as_flamingo(self):
        response = self.as_anna().get("/flamingo/app/inkorg/", headers=NAVIGATE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen().last_area, ActivityArea.FLAMINGO)
        self.assertEqual(self.seen().last_customer, self.acme)

    def test_the_page_is_credited_to_the_customer_it_showed(self):
        """Portalen visar kontaktens första aktiva kund, Flamingo den valda
        Flamingo-kunden. Raden får den kund sidan faktiskt gällde."""
        bolaget = Customer.objects.create(name="Bolaget AB")
        FlamingoAccount.objects.create(customer=bolaget, is_enabled=True)
        dan = self.contact("dan@x.se", self.other, bolaget)
        client = self.login(dan)
        client.get("/kund/tavla/", headers=NAVIGATE)
        self.assertEqual(self.seen(dan).last_customer, self.other)
        ContactActivity.objects.all().delete()
        cache.clear()
        client.get("/flamingo/app/inkorg/", headers=NAVIGATE)
        self.assertEqual(self.seen(dan).last_customer, bolaget)

    def test_older_browsers_without_sec_fetch_count_by_accept(self):
        client = self.as_anna()
        client.get("/kund/tavla/", headers={"Accept": "text/html,application/xhtml+xml"})
        self.assertIsNotNone(self.seen())

    def test_staff_is_never_recorded(self):
        client = self.as_staff()
        self.assertEqual(client.get("/manage/kunder/", headers=NAVIGATE).status_code, 200)
        self.assertEqual(client.get("/flamingo/app/", headers=NAVIGATE).status_code, 200)
        self.assertFalse(ContactActivity.objects.exists())

    def test_staff_viewing_the_portal_as_the_customer_is_not_the_customer(self):
        client = self.as_staff()
        client.post(f"/manage/kunder/{self.acme.pk}/visa-som/")
        response = client.get("/kund/tavla/", headers=NAVIGATE)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Du ser portalen som")
        self.assertFalse(ContactActivity.objects.exists())

    def test_staff_viewing_flamingo_as_the_customer_is_not_the_customer(self):
        client = self.as_staff()
        client.post(f"/manage/kunder/{self.acme.pk}/flamingo/visa/")
        self.assertEqual(client.get("/flamingo/app/inkorg/", headers=NAVIGATE).status_code, 200)
        self.assertFalse(ContactActivity.objects.exists())

    def test_background_requests_json_and_head_do_not_count(self):
        """Svaren är riktiga (200 för det mesta), bara inte sidor kontakten öppnade."""
        xhr = {"Accept": "text/html", "X-Requested-With": "XMLHttpRequest"}
        cases = (
            ("fetch", "get", "/flamingo/app/inkorg/", FETCH, 200),
            ("XHR utan Sec-Fetch", "get", "/kund/tavla/", xhr, 200),
            ("JSON", "get", "/flamingo/app/media/lista/", NAVIGATE, 200),
            ("HEAD", "head", "/kund/tavla/", NAVIGATE, 200),
            ("förhämtning", "get", "/kund/tavla/", {**NAVIGATE, "Sec-Purpose": "prefetch"}, 200),
            ("iframe", "get", "/kund/tavla/", {**NAVIGATE, "Sec-Fetch-Dest": "iframe"}, 200),
            ("utan huvuden", "get", "/kund/tavla/", {}, 200),
            ("404", "get", "/kund/arenden/999999/", NAVIGATE, 404),
            ("3xx", "get", "/kund/", NAVIGATE, 302),
        )
        client = self.as_anna()
        for name, method, url, headers, status in cases:
            with self.subTest(name):
                # Varje fall för sig: ett fel här ska inte fälla nästa.
                ContactActivity.objects.all().delete()
                cache.clear()
                response = getattr(client, method)(url, headers=headers)
                self.assertEqual(response.status_code, status)
                self.assertFalse(ContactActivity.objects.exists())

    def test_the_login_pages_count_for_nobody(self):
        """En inloggad kontakt vars enda kund är inaktiv får 403 i portalen
        men 200 på inloggningen och kodsidan. Ingen av dem visade en kund."""
        dead = Customer.objects.create(name="Död AB", is_active=False)
        cia = self.contact("cia@dod.se", dead)
        client = self.login(cia)
        self.assertEqual(client.get("/kund/tavla/", headers=NAVIGATE).status_code, 403)
        self.assertEqual(client.get("/kund/logga-in/", headers=NAVIGATE).status_code, 200)
        session = client.session
        session["portal_login_email"] = "cia@dod.se"
        session.save()
        self.assertEqual(client.get("/kund/kod/", headers=NAVIGATE).status_code, 200)
        self.assertFalse(ContactActivity.objects.exists())

    def test_only_the_portal_and_the_flamingo_tool_are_areas(self):
        self.assertEqual(activity.area_for("/kund/tavla/"), ActivityArea.PORTAL)
        self.assertEqual(activity.area_for("/kund/sms/"), ActivityArea.PORTAL)
        self.assertEqual(activity.area_for("/flamingo/app/"), ActivityArea.FLAMINGO)
        self.assertEqual(activity.area_for("/flamingo/app/inkorg/"), ActivityArea.FLAMINGO)
        for path in ("/", "/flamingo/", "/flamingo/rorjour/", "/lp/x/", "/manage/", "/kundcase/"):
            self.assertIsNone(activity.area_for(path), path)

    def test_a_contact_without_flamingo_is_not_recorded_by_the_404(self):
        loner = self.contact("cia@annan.se", self.other)
        client = self.login(loner)
        self.assertEqual(client.get("/flamingo/app/", headers=NAVIGATE).status_code, 404)
        self.assertFalse(ContactActivity.objects.exists())

    def test_anonymous_visitors_are_not_recorded(self):
        Client().get("/kund/logga-in/", headers=NAVIGATE)
        self.assertFalse(ContactActivity.objects.exists())

    def test_a_failure_never_breaks_the_page(self):
        with (
            mock.patch.object(activity, "record", side_effect=RuntimeError("db borta")),
            self.assertLogs("apps.projects.activity", "ERROR"),
        ):
            response = self.as_anna().get("/kund/tavla/", headers=NAVIGATE)
        self.assertEqual(response.status_code, 200)


class PageViewRuleTests(TestCase):
    """is_page_view för svar som är svåra att få fram genom en riktig vy."""

    def check(self, response, method="get", headers=NAVIGATE):
        request = getattr(RequestFactory(), method)("/kund/x/", headers=headers)
        return activity.is_page_view(request, response)

    def test_a_plain_html_page_counts(self):
        self.assertTrue(self.check(HttpResponse("<p>hej</p>")))

    def test_files_and_downloads_do_not_count(self):
        pdf = FileResponse(io.BytesIO(b"%PDF"), as_attachment=True, filename="faktura.pdf")
        self.assertFalse(self.check(pdf))
        html_file = FileResponse(io.BytesIO(b"<p>"), content_type="text/html")
        self.assertFalse(self.check(html_file))
        download = HttpResponse("<p>", content_type="text/html")
        download["Content-Disposition"] = 'attachment; filename="sida.html"'
        self.assertFalse(self.check(download))

    def test_only_2xx_counts(self):
        self.assertFalse(self.check(HttpResponseRedirect("/kund/tavla/")))
        self.assertFalse(self.check(HttpResponse("<p>", status=403)))
        self.assertFalse(self.check(HttpResponse("<p>", status=500)))

    def test_json_and_other_methods_do_not_count(self):
        self.assertFalse(self.check(JsonResponse({"ok": True})))
        self.assertFalse(self.check(HttpResponse("<p>"), method="head"))
        self.assertFalse(self.check(HttpResponse("<p>"), method="post"))


class ThrottleTests(ActivityFixture, TestCase):
    """Högst en skrivning per kontakt och fem minuter."""

    def test_a_second_page_within_five_minutes_does_no_database_work(self):
        client = self.as_anna()
        client.get("/kund/tavla/", headers=NAVIGATE)
        first = self.seen().last_seen_at
        with CaptureQueriesContext(connection) as queries:
            client.get("/flamingo/app/inkorg/", headers=NAVIGATE)
        self.assertEqual(_activity_queries(queries.captured_queries), [])
        row = self.seen()
        self.assertEqual((row.last_seen_at, row.last_area), (first, ActivityArea.PORTAL))

    def test_another_worker_within_five_minutes_writes_nothing(self):
        """Utan cachen (en annan process) stoppar den villkorade UPDATE:n."""
        client = self.as_anna()
        recent = timezone.now() - timedelta(minutes=2)
        # Inloggad före sidan worker A sparade, annars sparas nästa sida ändå.
        User.objects.filter(pk=self.anna.pk).update(last_login=recent - timedelta(hours=1))
        self.active(self.anna, recent)
        client.get("/flamingo/app/inkorg/", headers=NAVIGATE)
        row = self.seen()
        self.assertEqual((row.last_seen_at, row.last_area), (recent, ActivityArea.PORTAL))
        self.assertEqual(activity.record(self.anna, ActivityArea.FLAMINGO, self.acme), recent)

    def test_after_five_minutes_the_next_page_is_written(self):
        old = timezone.now() - timedelta(minutes=6)
        self.active(self.anna, old)
        self.as_anna().get("/flamingo/app/inkorg/", headers=NAVIGATE)
        row = self.seen()
        self.assertGreater(row.last_seen_at, old)
        self.assertEqual(row.last_area, ActivityArea.FLAMINGO)

    def test_the_same_process_writes_again_after_five_minutes(self):
        with clock() as t:
            client = self.as_anna()
            client.get("/kund/tavla/", headers=NAVIGATE)
            t.advance(minutes=4, seconds=59)
            client.get("/kund/tavla/", headers=NAVIGATE)
            self.assertEqual(self.seen().last_seen_at, NOON)
            t.advance(seconds=2)
            client.get("/flamingo/app/inkorg/", headers=NAVIGATE)
        row = self.seen()
        self.assertEqual(row.last_seen_at, NOON + timedelta(minutes=5, seconds=1))
        self.assertEqual(row.last_area, ActivityArea.FLAMINGO)

    def test_the_window_counts_from_the_stored_time_not_from_the_cache(self):
        """Worker A skriver 12:00. Worker B ser raden 12:04:50 och får inte
        spärra till 12:09:50: sidan 12:05:10 ska sparas, var den än landar."""
        with clock() as t:
            client = self.as_anna()
            client.get("/kund/tavla/", headers=NAVIGATE)
            cache.clear()  # nästa sida landar hos worker B
            t.advance(minutes=4, seconds=50)
            client.get("/kund/tavla/", headers=NAVIGATE)
            self.assertEqual(self.seen().last_seen_at, NOON)
            t.advance(seconds=20)
            client.get("/flamingo/app/inkorg/", headers=NAVIGATE)
        row = self.seen()
        self.assertEqual(row.last_seen_at, NOON + timedelta(minutes=5, seconds=10))
        self.assertEqual(row.last_area, ActivityArea.FLAMINGO)

    def test_the_first_page_after_a_login_is_always_written(self):
        """Annars kunde kortet visa "aktiv senast 12:00, inloggad 12:02"."""
        with clock() as t:
            client = self.as_anna()
            client.get("/kund/tavla/", headers=NAVIGATE)
            t.advance(minutes=2)
            User.objects.filter(pk=self.anna.pk).update(last_login=t.now)
            t.advance(seconds=5)
            client.get("/flamingo/app/inkorg/", headers=NAVIGATE)
            row = self.seen()
            self.assertEqual(row.last_seen_at, NOON + timedelta(minutes=2, seconds=5))
            self.assertEqual(row.last_area, ActivityArea.FLAMINGO)
            t.advance(seconds=5)
            with CaptureQueriesContext(connection) as queries:
                client.get("/kund/tavla/", headers=NAVIGATE)
        self.assertEqual(_activity_queries(queries.captured_queries), [])


class CustomerCardTests(ActivityFixture, TestCase):
    """Kundkortets rad om varje kontakt, klockan stilla mitt på dagen."""

    def setUp(self):
        super().setUp()
        self.enterContext(clock())

    def card(self, customer=None):
        customer = customer or self.acme
        return self.as_staff().get(f"/manage/kunder/{customer.pk}/").content.decode()

    def contact_line(self, html, email):
        match = re.search(rf"<small class=\"tv-muted\">{re.escape(email)}(.*?)</small>", html)
        self.assertIsNotNone(match, email)
        return _text(match.group(1))

    def test_a_contact_who_never_logged_in_says_so(self):
        line = self.contact_line(self.card(), "bo@acme.se")
        self.assertEqual(line, "| har inte loggat in än")

    def test_login_and_latest_activity_are_shown(self):
        User.objects.filter(pk=self.anna.pk).update(last_login=NOON - timedelta(days=7))
        self.active(self.anna, NOON - timedelta(minutes=55), ActivityArea.FLAMINGO)
        line = self.contact_line(self.card(), "anna@acme.se")
        self.assertEqual(line, "| aktiv senast i dag 11:05 i Flamingo | inloggad 30 sep")

    def test_a_login_without_any_page_since_shows_only_the_login(self):
        User.objects.filter(pk=self.anna.pk).update(last_login=NOON - timedelta(days=1))
        line = self.contact_line(self.card(), "anna@acme.se")
        self.assertEqual(line, "| inloggad i går 12:00")

    def test_the_times_never_break_in_the_middle(self):
        User.objects.filter(pk=self.anna.pk).update(last_login=NOON)
        html = self.card()
        self.assertIn('inloggad <span class="tv-nowrap">i dag 12:00</span>', html)

    def test_activity_for_another_customer_names_that_customer(self):
        """Aktiviteten sparas per kontakt. Gällde sidan en annan kund syns
        det, så kortet inte påstår att kontakten var aktiv för den här."""
        self.other.users.add(self.anna)
        self.active(self.anna, NOON - timedelta(hours=1), customer=self.other)
        here = self.contact_line(self.card(), "anna@acme.se")
        self.assertEqual(here, "| aktiv senast i dag 11:00 i Kundportalen för Annan AB")
        there = self.contact_line(self.card(self.other), "anna@acme.se")
        self.assertEqual(there, "| aktiv senast i dag 11:00 i Kundportalen")

    def test_a_staff_user_linked_as_a_contact_is_marked_not_counted(self):
        User.objects.filter(pk=self.staff.pk).update(email="byra@adx.se", last_login=NOON)
        self.acme.users.add(self.staff)
        line = self.contact_line(self.card(), "byra@adx.se")
        self.assertEqual(line, "| byråkonto")

    def test_the_card_never_talks_about_passwords_for_contacts(self):
        html = self.card()
        self.assertNotIn("lösenord", html.lower())
        self.assertIn("välkomstmejl", html)
        self.assertIn("engångskod", html)

    def test_the_contacts_cost_the_same_queries_however_many(self):
        client = self.as_staff()
        url = f"/manage/kunder/{self.acme.pk}/"
        client.get(url)
        with CaptureQueriesContext(connection) as few:
            client.get(url)
        for n in range(4):
            user = self.contact(f"k{n}@acme.se", self.acme)
            self.active(user, NOON, customer=self.other if n % 2 else self.acme)
        with CaptureQueriesContext(connection) as many:
            html = client.get(url).content.decode()
        self.assertIn("k3@acme.se", html)
        self.assertIn("för Annan AB", html)
        self.assertEqual(len(many.captured_queries), len(few.captured_queries))


class InviteCopyTests(ActivityFixture, TestCase):
    @override_settings(SITE_BASE_URL="https://adx.se")
    def test_a_failed_invite_tells_staff_how_the_contact_logs_in(self):
        with mock.patch("apps.projects.manage_views.send_invite", return_value=False):
            response = self.as_staff().post(
                f"/manage/kunder/{self.acme.pk}/bjud-in/", {"email": "ny@acme.se"}, follow=True
            )
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Be kontakten logga in på https://adx.se/kund/logga-in/", text)
        self.assertIn("med sin e-postadress", text)
        self.assertNotIn("lösenord", text.lower())


class CustomerListTests(ActivityFixture, TestCase):
    """Kundlistans kolumn Kontakt aktiv: den senaste bland kundens kontakter."""

    def setUp(self):
        super().setUp()
        self.enterContext(clock())

    def rows(self, **params):
        html = self.as_staff().get("/manage/kunder/", params).content.decode()
        found = re.findall(r"<tr>\s*<td>\s*<a [^>]*><b>([^<]+)</b>(.*?)</tr>", html, re.S)
        return html, {name: row for name, row in found}

    def test_the_column_shows_the_latest_activity_across_contacts(self):
        self.active(self.anna, NOON - timedelta(days=1), ActivityArea.PORTAL)
        self.active(self.bo, NOON - timedelta(minutes=30), ActivityArea.FLAMINGO)
        html, rows = self.rows()
        self.assertIn(">Kontakt aktiv</th>", html)
        self.assertIn(">Ärende ändrat</th>", html)
        self.assertNotIn("Kunden aktiv", html)
        self.assertIn("i dag 11:30 Flamingo", _text(rows["Acme AB"]))
        self.assertNotIn("i går", rows["Acme AB"])
        self.assertNotIn("i dag", rows["Annan AB"])

    def test_the_cell_is_one_value_so_the_phone_card_keeps_it_together(self):
        """Under 760 px är cellen etikett + värde i ett rutnät: tid och var
        måste ligga i ett och samma element, annars hamnar "var" under
        etiketten."""
        self.active(self.bo, NOON, ActivityArea.FLAMINGO)
        _, rows = self.rows()
        self.assertRegex(
            rows["Acme AB"],
            r'<td><div><span class="tv-nowrap">i dag 12:00</span>'
            r'<div class="tv-muted tv-cell-sub">Flamingo</div></div></td>',
        )

    def test_a_contact_in_two_customers_counts_only_where_the_page_was(self):
        self.other.users.add(self.anna)
        self.active(self.anna, NOON - timedelta(hours=1), customer=self.other)
        _, rows = self.rows()
        self.assertIn("i dag 11:00 Kundportalen", _text(rows["Annan AB"]))
        self.assertNotIn("i dag", rows["Acme AB"])

    def test_staff_and_former_contacts_do_not_count(self):
        self.acme.users.add(self.staff)
        self.active(self.staff, NOON)
        gone = self.contact("gone@acme.se")
        self.active(gone, NOON)
        _, rows = self.rows()
        self.assertNotIn("i dag", rows["Acme AB"])

    def test_the_empty_row_spans_every_column(self):
        html, _ = self.rows(q="finns-inte")
        head = re.search(r"<thead>.*?</thead>", html, re.S).group(0)
        columns = len(re.findall(r"<th[ >]", head))
        self.assertEqual(columns, 8)
        self.assertIn(f'colspan="{columns}"', html)

    def test_the_list_query_count_does_not_grow_with_customers_or_contacts(self):
        client = self.as_staff()
        client.get("/manage/kunder/")
        with CaptureQueriesContext(connection) as few:
            client.get("/manage/kunder/")
        for n in range(5):
            customer = Customer.objects.create(name=f"Kund {n}")
            for m in range(2):
                user = self.contact(f"k{n}-{m}@x.se", customer)
                self.active(user, NOON, customer=customer)
        with CaptureQueriesContext(connection) as many:
            html = client.get("/manage/kunder/").content.decode()
        self.assertIn("Kund 4", html)
        self.assertEqual(len(many.captured_queries), len(few.captured_queries))
