"""
Datamodellens regler, låsta som tester.

Det som bär allt: en sanning om kunden (projektets vinner), löpnummer per
projekt under lås, kolumner som data där is_done stänger ärendet, och
timern som en öppen tidspost med databasens garanti om EN igång per
användare.
"""

import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import Attachment, Column, Comment, Customer, Issue, Project, TimeEntry


class ProjectTests(TestCase):
    def test_a_new_project_gets_default_columns_and_uppercase_key(self):
        project = Project.objects.create(name="Ny hemsida", key="nord")
        self.assertEqual(project.key, "NORD")
        self.assertEqual(
            list(project.columns.values_list("title", "is_done")),
            [("Att göra", False), ("Pågår", False), ("Klart", True)],
        )

    def test_issue_numbers_run_per_project(self):
        a = Project.objects.create(name="A", key="A")
        b = Project.objects.create(name="B", key="B")
        one = Issue.objects.create(project=a, title="x")
        two = Issue.objects.create(project=a, title="y")
        other = Issue.objects.create(project=b, title="z")
        self.assertEqual((one.key, two.key, other.key), ("A-1", "A-2", "B-1"))
        a.refresh_from_db()
        self.assertEqual(a.next_issue_number, 3)

    def test_an_issue_without_project_uses_its_id(self):
        issue = Issue.objects.create(title="Lös fråga")
        self.assertEqual(issue.key, f"#{issue.pk}")
        self.assertIsNone(issue.number)
        self.assertIsNone(issue.column)


class CustomerTruthTests(TestCase):
    def setUp(self):
        self.acme = Customer.objects.create(name="Acme")
        self.other = Customer.objects.create(name="Annan")
        self.project = Project.objects.create(name="Bygge", key="ACME", customer=self.acme)

    def test_project_customer_is_the_truth(self):
        issue = Issue.objects.create(project=self.project, customer=self.acme, title="x")
        self.assertIsNone(issue.customer, "kunden lagras inte dubbelt")
        self.assertEqual(issue.effective_customer, self.acme)

    def test_conflicting_customer_is_refused_by_validation(self):
        issue = Issue(project=self.project, customer=self.other, title="x")
        with self.assertRaises(ValidationError):
            issue.full_clean()

    def test_a_standalone_issue_keeps_its_own_customer(self):
        issue = Issue.objects.create(customer=self.other, title="Supportfråga")
        self.assertEqual(issue.effective_customer, self.other)
        self.assertIn(issue, Issue.objects.for_customer(self.other))

    def test_for_customer_finds_both_direct_and_project_issues(self):
        via_project = Issue.objects.create(project=self.project, title="a")
        direct = Issue.objects.create(customer=self.acme, title="b")
        Issue.objects.create(customer=self.other, title="c")
        self.assertEqual(set(Issue.objects.for_customer(self.acme)), {via_project, direct})

    def test_column_from_another_project_is_refused(self):
        foreign = Project.objects.create(name="Annat", key="ANN")
        issue = Issue(project=self.project, column=foreign.columns.first(), title="x")
        with self.assertRaises(ValidationError):
            issue.full_clean()


class ColumnFlowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="P", key="P")
        self.done = self.project.columns.get(is_done=True)
        self.todo = self.project.columns.get(title="Att göra")

    def test_moving_to_a_done_column_closes_and_back_reopens(self):
        issue = Issue.objects.create(project=self.project, title="x")
        self.assertIsNone(issue.closed_at)
        issue.column = self.done
        issue.save()
        self.assertIsNotNone(issue.closed_at)
        self.assertNotIn(issue, Issue.objects.open())
        issue.column = self.todo
        issue.save()
        self.assertIsNone(issue.closed_at)

    def test_column_titles_are_unique_per_project(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Column.objects.create(project=self.project, title="Klart", position=9)


class TimerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("g", password="x")
        self.project = Project.objects.create(name="P", key="P")
        self.a = Issue.objects.create(project=self.project, title="a")
        self.b = Issue.objects.create(project=self.project, title="b")

    def test_starting_a_timer_stops_the_users_other_timer(self):
        first = self.a.start_timer(self.user)
        second = self.b.start_timer(self.user)
        first.refresh_from_db()
        self.assertFalse(first.is_running)
        self.assertTrue(second.is_running)
        self.assertEqual(TimeEntry.objects.running().count(), 1)

    def test_the_database_refuses_two_running_timers_for_one_user(self):
        TimeEntry.objects.create(issue=self.a, user=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TimeEntry.objects.create(issue=self.b, user=self.user)

    def test_stop_denormalises_seconds_and_totals_add_up(self):
        entry = TimeEntry.objects.create(
            issue=self.a, user=self.user, started_at=timezone.now() - timezone.timedelta(minutes=30)
        )
        self.assertGreaterEqual(self.a.total_seconds(), 1799, "pågående räknas live")
        entry.stop()
        self.assertGreaterEqual(entry.seconds, 1799)
        self.assertEqual(self.a.total_seconds(), entry.seconds)
        self.assertEqual(self.project.total_seconds(), entry.seconds)

    def test_customer_totals_include_project_and_direct_issues(self):
        customer = Customer.objects.create(name="Kund")
        self.project.customer = customer
        self.project.save()
        direct = Issue.objects.create(customer=customer, title="direkt")
        for issue in (self.a, direct):
            TimeEntry.objects.create(
                issue=issue,
                user=self.user,
                started_at=timezone.now() - timezone.timedelta(minutes=10),
                ended_at=timezone.now(),
                seconds=600,
            )
        self.assertEqual(customer.total_seconds(), 1200)

    def test_stop_timer_returns_none_when_nothing_runs(self):
        self.assertIsNone(self.a.stop_timer(self.user))


# ======================================================================
# Vyer: byråns tavla och kundportalen
# ======================================================================


EMAIL = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST_USER": "x",
    "EMAIL_HOST_PASSWORD": "y",
    "INQUIRY_NOTIFICATION_EMAIL": "staff@example.com",
}


class PortalFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Acme AB")
        cls.other = Customer.objects.create(name="Annan AB")
        cls.contact = User.objects.create_user(
            "anna@acme.se", email="anna@acme.se", password="x12345678"
        )
        cls.acme.users.add(cls.contact)
        cls.stranger = User.objects.create_user(
            "bo@annan.se", email="bo@annan.se", password="x12345678"
        )
        cls.other.users.add(cls.stranger)
        cls.project = Project.objects.create(name="Ny hemsida", key="ACME", customer=cls.acme)
        cls.visible = Issue.objects.create(
            project=cls.project, title="Synligt", visible_to_customer=True
        )
        cls.hidden = Issue.objects.create(project=cls.project, title="Internt jobb")
        cls.foreign = Issue.objects.create(
            customer=cls.other, title="Annans ärende", visible_to_customer=True
        )

    def as_staff(self):
        c = Client()
        c.force_login(self.staff)
        return c

    def as_contact(self):
        c = Client()
        c.force_login(self.contact)
        return c


class PortalGateTests(PortalFixtureMixin, TestCase):
    def test_a_customer_contact_never_reaches_manage(self):
        response = self.as_contact().get("/manage/tavla/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/kund/", response["Location"])

    def test_staff_is_sent_from_portal_to_the_board(self):
        response = self.as_staff().get("/kund/tavla/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/manage/tavla/", response["Location"])

    def test_anonymous_portal_visit_goes_to_login(self):
        response = Client().get("/kund/tavla/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/kund/logga-in/", response["Location"])

    def test_robots_blocks_the_portal(self):
        self.assertIn("Disallow: /kund/", Client().get("/robots.txt").content.decode())


class PortalVisibilityTests(PortalFixtureMixin, TestCase):
    def test_the_customer_sees_only_visible_own_issues(self):
        html = self.as_contact().get("/kund/tavla/").content.decode()
        self.assertIn("Synligt", html)
        self.assertNotIn("Internt jobb", html)
        self.assertNotIn("Annans ärende", html)

    def test_hidden_and_foreign_issues_are_404_by_id(self):
        client = self.as_contact()
        self.assertEqual(client.get(f"/kund/arenden/{self.hidden.pk}/").status_code, 404)
        self.assertEqual(client.get(f"/kund/arenden/{self.foreign.pk}/").status_code, 404)
        self.assertEqual(client.get(f"/kund/arenden/{self.visible.pk}/").status_code, 200)

    def test_no_time_or_timer_leaks_into_the_portal(self):
        TimeEntry.objects.create(
            issue=self.visible,
            user=self.staff,
            started_at=timezone.now() - timezone.timedelta(hours=2),
            ended_at=timezone.now(),
            seconds=7200,
        )
        html = self.as_contact().get(f"/kund/arenden/{self.visible.pk}/").content.decode()
        for forbidden in ("timer", "2:00:00", "Tid:", "data-timer"):
            self.assertNotIn(forbidden, html)

    def test_internal_comments_stay_internal(self):
        Comment.objects.create(
            issue=self.visible, author=self.staff, body="Kunden är jobbig", is_internal=True
        )
        Comment.objects.create(
            issue=self.visible, author=self.staff, body="Vi tittar på det i morgon"
        )
        html = self.as_contact().get(f"/kund/arenden/{self.visible.pk}/").content.decode()
        self.assertNotIn("Kunden är jobbig", html)
        self.assertIn("Vi tittar på det i morgon", html)

    def test_visible_flag_defaults_to_false_for_staff_created_issues(self):
        self.assertFalse(Issue.objects.create(project=self.project, title="x").visible_to_customer)


@override_settings(**EMAIL)
class PortalCreateTests(PortalFixtureMixin, TestCase):
    def tearDown(self):
        for att in Attachment.objects.all():
            att.file.delete(save=False)

    def test_customer_creates_an_issue_with_attachments_in_a_support_project(self):
        client = self.as_contact()
        png = SimpleUploadedFile("skarm.png", b"\x89PNG\r\n\x1a\nfake", content_type="image/png")
        pdf = SimpleUploadedFile("brief.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        response = client.post(
            "/kund/arenden/nytt/",
            {
                "title": "Byt telefonnummer i sidfoten",
                "description": "Nytt nummer: se bilaga",
                "files": [png, pdf],
            },
        )
        self.assertEqual(response.status_code, 302)
        issue = Issue.objects.get(title="Byt telefonnummer i sidfoten")
        self.assertTrue(issue.visible_to_customer)
        self.assertTrue(issue.created_in_portal)
        self.assertEqual(issue.project.name, "Support")
        self.assertEqual(issue.project.customer, self.acme)
        self.assertEqual(issue.attachments.count(), 2)
        self.assertEqual(issue.key, f"{issue.project.key}-1")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Acme AB", mail.outbox[0].subject)
        # Byrån ser den direkt på tavlan.
        self.assertContains(self.as_staff().get("/manage/tavla/"), "Byt telefonnummer")

    def test_disallowed_file_types_are_refused(self):
        exe = SimpleUploadedFile("virus.exe", b"MZ", content_type="application/octet-stream")
        response = self.as_contact().post("/kund/arenden/nytt/", {"title": "x", "files": [exe]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Issue.objects.filter(title="x").exists())

    def test_attachment_download_is_gated_per_customer(self):
        png = SimpleUploadedFile("s.png", b"\x89PNGdata", content_type="image/png")
        self.as_contact().post("/kund/arenden/nytt/", {"title": "Med bilaga", "files": [png]})
        att = Attachment.objects.get(original_name="s.png")
        mine = self.as_contact().get(f"/kund/bilagor/{att.pk}/")
        self.assertEqual(mine.status_code, 200)
        theirs = Client()
        theirs.force_login(self.stranger)
        self.assertEqual(theirs.get(f"/kund/bilagor/{att.pk}/").status_code, 404)
        self.assertEqual(Client().get(f"/kund/bilagor/{att.pk}/").status_code, 302)
        self.assertEqual(self.as_staff().get(f"/manage/bilagor/{att.pk}/").status_code, 200)

    def test_attachments_live_outside_media_root(self):
        png = SimpleUploadedFile("s.png", b"\x89PNGdata", content_type="image/png")
        self.as_contact().post("/kund/arenden/nytt/", {"title": "Med bilaga", "files": [png]})
        att = Attachment.objects.get(original_name="s.png")
        self.assertTrue(att.file.path.startswith(str(settings.PRIVATE_MEDIA_ROOT)))
        self.assertFalse(att.file.path.startswith(str(settings.MEDIA_ROOT)))
        self.assertIn("/arenden/", att.file.path)

    def test_customer_comment_notifies_staff_and_is_external(self):
        self.as_contact().post(f"/kund/arenden/{self.visible.pk}/", {"description": "Hur går det?"})
        comment = self.visible.comments.get()
        self.assertFalse(comment.is_internal)
        self.assertEqual(len(mail.outbox), 1)


@override_settings(**EMAIL)
class InviteTests(PortalFixtureMixin, TestCase):
    def test_invite_creates_a_portal_user_and_mails_a_set_password_link(self):
        self.as_staff().post(
            f"/manage/kunder/{self.acme.pk}/bjud-in/", {"email": "Ny@Acme.se", "first_name": "Ny"}
        )
        user = get_user_model().objects.get(username="ny@acme.se")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertIn(user, self.acme.users.all())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/kund/aterstall/", mail.outbox[0].body)

    def test_a_staff_address_cannot_be_invited_as_contact(self):
        self.as_staff().post(
            f"/manage/kunder/{self.acme.pk}/bjud-in/", {"email": self.staff.email or "byra@adx.se"}
        )
        self.assertNotIn(self.staff, self.acme.users.all())


class BoardTests(PortalFixtureMixin, TestCase):
    def test_board_renders_all_and_per_project(self):
        client = self.as_staff()
        self.assertContains(client.get("/manage/tavla/"), "Internt jobb")
        self.assertContains(client.get("/manage/tavla/?projekt=acme"), "Ny hemsida")

    def test_move_between_columns_closes_on_done(self):
        client = self.as_staff()
        done = self.project.columns.get(is_done=True)
        response = client.post(
            f"/manage/arenden/{self.visible.pk}/flytta/",
            json.dumps({"target": f"c{done.pk}", "order": [self.visible.pk]}),
            content_type="application/json",
        )
        self.assertEqual(response.json()["stage"], "done")
        self.visible.refresh_from_db()
        self.assertIsNotNone(self.visible.closed_at)

    def test_move_by_stage_on_the_all_board_picks_the_projects_column(self):
        self.as_staff().post(
            f"/manage/arenden/{self.visible.pk}/flytta/",
            json.dumps({"target": "active"}),
            content_type="application/json",
        )
        self.visible.refresh_from_db()
        self.assertEqual(self.visible.column.title, "Pågår")

    def test_timer_toggle_via_fetch(self):
        client = self.as_staff()
        r = client.post(
            f"/manage/arenden/{self.visible.pk}/timer/",
            json.dumps({}),
            content_type="application/json",
        )
        self.assertTrue(r.json()["running"])
        r2 = client.post(
            f"/manage/arenden/{self.hidden.pk}/timer/",
            json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(r2.json()["stopped"], [self.visible.pk])
        self.assertEqual(TimeEntry.objects.running().count(), 1)

    def test_quick_add_returns_a_card_with_the_projects_key(self):
        r = self.as_staff().post(
            "/manage/arenden/snabb/",
            json.dumps({"title": "Snabbt", "target": "new", "project": "ACME"}),
            content_type="application/json",
        )
        self.assertIn("ACME-", r.json()["html"])

    def test_issue_detail_saves_visibility(self):
        client = self.as_staff()
        client.post(
            f"/manage/arenden/{self.hidden.pk}/",
            {
                "title": "Internt jobb",
                "description": "",
                "project": self.project.pk,
                "column": self.hidden.column_id,
                "issue_type": "task",
                "priority": 20,
                "is_billable": "on",
                "visible_to_customer": "on",
            },
        )
        self.hidden.refresh_from_db()
        self.assertTrue(self.hidden.visible_to_customer)

    def test_time_report_csv(self):
        TimeEntry.objects.create(
            issue=self.visible,
            user=self.staff,
            started_at=timezone.now() - timezone.timedelta(minutes=30),
            ended_at=timezone.now(),
            seconds=1800,
        )
        r = self.as_staff().get("/manage/tid/?format=csv")
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("ACME-1", r.content.decode())
        html = self.as_staff().get("/manage/tid/").content.decode()
        self.assertIn("Acme AB", html)
