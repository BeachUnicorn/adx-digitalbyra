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


@override_settings(**EMAIL)
class PasswordlessLoginTests(PortalFixtureMixin, TestCase):
    """E-post -> engångskod på mejl -> inloggad. Aldrig ett lösenord."""

    def _code_from_mail(self):
        import re

        return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)

    def test_contact_logs_in_with_a_mailed_code(self):
        client = Client()
        r = client.post("/kund/logga-in/", {"email": "Anna@Acme.se"})
        self.assertEqual(r["Location"], "/kund/kod/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["anna@acme.se"])
        code = self._code_from_mail()
        self.assertIn(code, mail.outbox[0].subject)
        r = client.post("/kund/kod/", {"code": code})
        self.assertEqual(r["Location"], "/kund/tavla/")
        self.assertContains(client.get("/kund/tavla/"), "Synligt")
        # Koden är förbrukad.
        client2 = Client()
        client2.post("/kund/logga-in/", {"email": "anna@acme.se"})
        self.assertEqual(client2.post("/kund/kod/", {"code": code}).status_code, 200)

    def test_unknown_staff_and_inactive_customer_addresses_get_the_same_answer_and_no_mail(self):
        self.staff.email = "byra@adx.se"
        self.staff.save()
        self.acme.is_active = False
        self.acme.save()
        for email in ("finns@inte.se", "byra@adx.se", "anna@acme.se"):
            r = Client().post("/kund/logga-in/", {"email": email})
            self.assertEqual(r["Location"], "/kund/kod/")
        self.assertEqual(len(mail.outbox), 0)

    def test_wrong_expired_and_overused_codes_fail(self):
        from .auth import MAX_ATTEMPTS, issue_code, verify_code
        from .models import LoginCode

        client = Client()
        client.post("/kund/logga-in/", {"email": "anna@acme.se"})
        r = client.post("/kund/kod/", {"code": "000000"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Fel eller utgången kod")
        self.assertFalse(r.wsgi_request.user.is_authenticated)
        # Utgången.
        code = self._code_from_mail()
        LoginCode.objects.update(expires_at=timezone.now() - timezone.timedelta(minutes=1))
        self.assertFalse(verify_code(self.contact, code))
        # För många försök på en färsk kod.
        code = issue_code(self.contact)
        for _ in range(MAX_ATTEMPTS):
            self.assertFalse(verify_code(self.contact, "111111"))
        self.assertFalse(verify_code(self.contact, code), "spärrad efter för många försök")

    def test_a_new_code_replaces_the_old_and_requests_are_rate_limited(self):
        from .auth import MAX_CODES_PER_HOUR, issue_code, verify_code

        first = issue_code(self.contact)
        second = issue_code(self.contact)
        self.assertFalse(verify_code(self.contact, first))
        self.assertTrue(verify_code(self.contact, second))
        for _ in range(MAX_CODES_PER_HOUR):
            issue_code(self.contact)
        self.assertIsNone(issue_code(self.contact))

    def test_codes_are_stored_hashed(self):
        from .auth import issue_code
        from .models import LoginCode

        code = issue_code(self.contact)
        self.assertNotIn(code, LoginCode.objects.get(user=self.contact).code_hash)

    def test_next_is_kept_through_the_code_step(self):
        client = Client()
        client.post("/kund/logga-in/", {"email": "anna@acme.se", "next": "/kund/arenden/nytt/"})
        r = client.post("/kund/kod/", {"code": self._code_from_mail()})
        self.assertEqual(r["Location"], "/kund/arenden/nytt/")
        client.post("/kund/logga-in/", {"email": "anna@acme.se", "next": "https://ond.se/"})

    def test_code_page_without_pending_email_goes_back_to_login(self):
        self.assertEqual(Client().get("/kund/kod/")["Location"], "/kund/logga-in/")

    def test_the_portal_wears_the_panel_skin(self):
        html = Client().get("/kund/logga-in/").content.decode()
        self.assertIn("manage-skin.css", html)
        self.assertNotIn("site.css", html)
        self.assertNotIn("lösenord", html.lower().replace("inget lösenord", ""))


class CustomerCreateTests(PortalFixtureMixin, TestCase):
    def test_a_new_customer_is_active(self):
        self.as_staff().post("/manage/kunder/", {"name": "Nya AB"})
        self.assertTrue(Customer.objects.get(name="Nya AB").is_active)

    def test_an_inactive_customer_is_flagged_on_its_page(self):
        self.acme.is_active = False
        self.acme.save()
        html = self.as_staff().get(f"/manage/kunder/{self.acme.pk}/").content.decode()
        self.assertIn("inaktiv", html)


@override_settings(**EMAIL)
class PortalRequestTests(PortalFixtureMixin, TestCase):
    """Kundens frågor i formuläret följer med ärendet in i panelen."""

    def test_the_customers_answers_travel_with_the_issue(self):
        client = self.as_contact()
        r = client.post(
            "/kund/arenden/nytt/",
            {
                "title": "Formuläret är trasigt",
                "description": "Felmeddelande vid Skicka.",
                "page_url": "acme.se/kontakt/",
                "urgency": "critical",
                "due_on": "2030-01-02",
            },
        )
        issue = Issue.objects.get(title="Formuläret är trasigt")
        self.assertEqual(r["Location"], f"/kund/arenden/{issue.pk}/")
        self.assertEqual(issue.request_kind, "", "frågan ställs inte längre")
        self.assertEqual(issue.urgency, "critical")
        self.assertEqual(issue.priority, 40, "akut hos kunden = akut på tavlan")
        self.assertEqual(issue.page_url, "https://acme.se/kontakt/")
        self.assertEqual(str(issue.due_on), "2030-01-02")
        html = client.get(f"/kund/arenden/{issue.pk}/").content.decode()
        for text in ("Brådska", "Akut", "acme.se/kontakt/", "Skickat"):
            self.assertIn(text, html)
        self.assertNotIn("Ni bad om", html)
        self.assertNotIn("[ ", html)
        panel = self.as_staff().get(f"/manage/arenden/{issue.pk}/panel/").json()["panel"]
        self.assertIn("Kundens uppgifter", panel)
        self.assertIn("Akut", panel)

    def test_the_form_offers_examples_and_the_logo(self):
        html = self.as_contact().get("/kund/arenden/nytt/").content.decode()
        self.assertIn("Visa exempel", html)
        self.assertIn("Använd som mall", html)
        self.assertIn("adx-logo.png", html)
        self.assertNotIn('name="request_kind"', html)
        self.assertIn('name="urgency"', html)

    def test_no_brackets_anywhere(self):
        client = self.as_contact()
        for url in ("/kund/tavla/", f"/kund/arenden/{self.visible.pk}/", "/kund/arenden/nytt/"):
            self.assertNotIn("[ ", client.get(url).content.decode())
        self.assertNotIn("[ ", self.as_staff().get("/manage/tavla/").content.decode())


class ViewAsCustomerTests(PortalFixtureMixin, TestCase):
    """Byrån tittar på portalen som en kund: ser exakt kundens vy, kan inte skriva."""

    def test_staff_can_view_the_portal_as_a_customer_and_leave(self):
        client = self.as_staff()
        r = client.post(f"/manage/kunder/{self.acme.pk}/visa-som/")
        self.assertEqual(r["Location"], "/kund/tavla/")
        html = client.get("/kund/tavla/").content.decode()
        self.assertIn("Du ser portalen som <b>Acme AB</b>", html)
        self.assertIn("Synligt", html)
        self.assertNotIn("Internt jobb", html)
        self.assertNotIn("Annans ärende", html)
        self.assertNotIn("Logga ut", html)
        self.assertEqual(client.get(f"/kund/arenden/{self.visible.pk}/").status_code, 200)
        self.assertEqual(client.get(f"/kund/arenden/{self.hidden.pk}/").status_code, 404)
        self.assertEqual(client.get("/kund/")["Location"], "/kund/tavla/")
        r = client.post("/kund/lamna-kundvyn/")
        self.assertEqual(r["Location"], f"/manage/kunder/{self.acme.pk}/")
        self.assertEqual(client.get("/kund/tavla/")["Location"], "/manage/tavla/")

    def test_the_customer_view_is_read_only(self):
        client = self.as_staff()
        client.post(f"/manage/kunder/{self.acme.pk}/visa-som/")
        r = client.post("/kund/arenden/nytt/", {"title": "Som kunden"})
        self.assertEqual(r["Location"], "/kund/tavla/")
        self.assertFalse(Issue.objects.filter(title="Som kunden").exists())
        r = client.post(f"/kund/arenden/{self.visible.pk}/", {"description": "Hej"})
        self.assertEqual(self.visible.comments.count(), 0)
        html = client.get(f"/kund/arenden/{self.visible.pk}/").content.decode()
        self.assertNotIn('name="description"', html)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_contact_cannot_switch_customer(self):
        client = self.as_contact()
        self.assertEqual(client.post(f"/manage/kunder/{self.other.pk}/visa-som/").status_code, 302)
        html = client.get("/kund/tavla/").content.decode()
        self.assertNotIn("Annans ärende", html)
        self.assertNotIn("Du ser portalen som", html)

    def test_the_customer_page_offers_the_button(self):
        html = self.as_staff().get(f"/manage/kunder/{self.acme.pk}/").content.decode()
        self.assertIn("Visa portalen som kunden", html)


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
    def test_invite_creates_a_portal_user_and_mails_how_to_log_in(self):
        self.as_staff().post(
            f"/manage/kunder/{self.acme.pk}/bjud-in/", {"email": "Ny@Acme.se", "first_name": "Ny"}
        )
        user = get_user_model().objects.get(username="ny@acme.se")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertIn(user, self.acme.users.all())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/kund/", mail.outbox[0].body)
        self.assertIn("engångskod", mail.outbox[0].body)
        self.assertNotIn("lösenord här", mail.outbox[0].body)

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
        self.assertIn("ACME-", r.json()["card"])

    def test_the_issue_deep_link_opens_the_drawer_on_the_board(self):
        r = self.as_staff().get(f"/manage/arenden/{self.hidden.pk}/")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"arende={self.hidden.pk}", r["Location"])
        self.assertIn("projekt=ACME", r["Location"])
        html = self.as_staff().get(r["Location"]).content.decode()
        self.assertIn(f'data-open="{self.hidden.pk}"', html)

    def test_the_board_wears_the_panel_skin(self):
        html = self.as_staff().get("/manage/tavla/").content.decode()
        self.assertIn("manage-skin.css", html)
        self.assertIn("tavla.css", html)
        self.assertNotIn("site.css", html)
        for page in ("/manage/projekt/", "/manage/kunder/", "/manage/tid/"):
            self.assertIn("manage-skin.css", self.as_staff().get(page).content.decode())

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

    def test_time_report_presets(self):
        TimeEntry.log(self.visible, self.staff, 30, on_date=timezone.localdate())
        for preset in ("vecka", "forra-veckan", "forra-manaden"):
            self.assertEqual(self.as_staff().get(f"/manage/tid/?period={preset}").status_code, 200)
        html = self.as_staff().get("/manage/tid/?period=vecka").content.decode()
        self.assertIn("ACME-1", html)


class FilterTests(PortalFixtureMixin, TestCase):
    """Filtren lever i adressen och servern tillämpar dem."""

    def test_project_filter_and_pills(self):
        from .board import BoardFilter

        flt = BoardFilter({"projekt": "acme", "mina": "1", "q": "x"})
        self.assertEqual(flt.project_key, "ACME")
        self.assertTrue(flt.is_active)
        self.assertIn("mina=1", flt.url())
        self.assertNotIn("mina", flt.url(mina=""))
        self.assertEqual(flt.clear_url, "?projekt=ACME")

    def test_mine_due_prio_and_label_filters(self):
        from .models import Label

        webb, _ = Label.objects.get_or_create(name="webb")
        mine = Issue.objects.create(project=self.project, title="Mitt", assignee=self.staff)
        late = Issue.objects.create(
            project=self.project,
            title="Sent",
            due_on=timezone.localdate() - timezone.timedelta(days=1),
        )
        hot = Issue.objects.create(project=self.project, title="Akut", priority=40)
        hot.labels.add(webb)
        client = self.as_staff()
        self.assertContains(client.get("/manage/tavla/?mina=1"), "Mitt")
        self.assertNotContains(client.get("/manage/tavla/?mina=1"), "Sent")
        self.assertContains(client.get("/manage/tavla/?forfaller=1"), "Sent")
        self.assertNotContains(client.get("/manage/tavla/?forfaller=1"), "Mitt")
        self.assertContains(client.get("/manage/tavla/?prio=1"), "Akut")
        self.assertContains(client.get("/manage/tavla/?etikett=webb"), "Akut")
        self.assertNotContains(client.get("/manage/tavla/?etikett=webb"), "Mitt")
        self.assertContains(client.get("/manage/tavla/?q=sent"), "Sent")
        self.assertNotContains(client.get("/manage/tavla/?q=sent"), "Akut")
        html = client.get("/manage/tavla/").content.decode()
        self.assertIn("försenade", html)
        self.assertIn(f"{late.pk}", html)
        self.assertIn("Mitt", html)
        self.assertEqual(mine.assignee, self.staff)

    def test_unknown_project_key_falls_back_to_all(self):
        html = self.as_staff().get("/manage/tavla/?projekt=NOPE").content.decode()
        self.assertIn("Internt jobb", html)


class DrawerTests(PortalFixtureMixin, TestCase):
    """Glidpanelen: panelen, autospar per fält, checklista, tid, konversation."""

    def _post(self, url, payload):
        return self.as_staff().post(url, json.dumps(payload), content_type="application/json")

    def test_panel_renders_the_issue(self):
        r = self.as_staff().get(f"/manage/arenden/{self.visible.pk}/panel/")
        html = r.json()["panel"]
        self.assertIn("Synligt", html)
        self.assertIn("Svar + mejl till kunden", html)
        self.assertIn('data-field="column"', html)

    def test_field_autosave_and_activity(self):
        url = f"/manage/arenden/{self.hidden.pk}/falt/"
        self.assertTrue(self._post(url, {"field": "title", "value": "Ny rubrik"}).json()["ok"])
        self.assertEqual(self._post(url, {"field": "title", "value": "  "}).status_code, 400)
        self._post(url, {"field": "due_on", "value": "2030-01-01"})
        self._post(url, {"field": "visible_to_customer", "value": True})
        self._post(url, {"field": "priority", "value": 30})
        self._post(url, {"field": "assignee", "value": self.staff.pk})
        self.hidden.refresh_from_db()
        self.assertEqual(self.hidden.title, "Ny rubrik")
        self.assertEqual(str(self.hidden.due_on), "2030-01-01")
        self.assertTrue(self.hidden.visible_to_customer)
        self.assertEqual(self.hidden.priority, 30)
        self.assertEqual(self.hidden.assignee, self.staff)
        texts = list(self.hidden.activity.values_list("text", flat=True))
        self.assertIn("synlig för kund: på", texts)
        self.assertIn("prioritet: Hög", texts)
        self.assertEqual(self._post(url, {"field": "hemligt", "value": 1}).status_code, 400)

    def test_label_toggle_and_column_move_via_the_panel(self):
        from .models import Label

        webb, _ = Label.objects.get_or_create(name="webb")
        url = f"/manage/arenden/{self.hidden.pk}/falt/"
        r = self._post(url, {"field": "label", "value": webb.pk, "panel": True}).json()
        self.assertIn("webb", r["card"])
        self.assertIn("panel", r)
        self._post(url, {"field": "label", "value": webb.pk})
        self.assertEqual(self.hidden.labels.count(), 0)
        done = self.project.columns.get(is_done=True)
        r = self._post(url, {"field": "column", "value": f"c{done.pk}"}).json()
        self.assertEqual(r["stage"], "done")
        self.hidden.refresh_from_db()
        self.assertIsNotNone(self.hidden.closed_at)
        texts = list(self.hidden.activity.values_list("text", flat=True))
        self.assertIn("flyttade till Klart", texts)
        self.assertEqual(len(mail.outbox), 0)

    def test_checklist(self):
        r = self._post(f"/manage/arenden/{self.hidden.pk}/checklista/", {"text": "Första"}).json()
        self.assertIn("Första", r["panel"])
        item = self.hidden.checklist.get()
        self._post(f"/manage/checklista/{item.pk}/", {"done": True})
        item.refresh_from_db()
        self.assertTrue(item.is_done)
        url = f"/manage/arenden/{self.hidden.pk}/falt/"
        r = self._post(url, {"field": "priority", "value": 20})
        self.assertIn("1/1", r.json()["card"])
        self._post(f"/manage/checklista/{item.pk}/", {"delete": True})
        self.assertEqual(self.hidden.checklist.count(), 0)

    def test_time_in_retrospect(self):
        yesterday = (timezone.localdate() - timezone.timedelta(days=1)).isoformat()
        r = self._post(
            f"/manage/arenden/{self.hidden.pk}/tid/",
            {"minutes": "45", "date": yesterday, "note": "Utkast"},
        )
        self.assertTrue(r.json()["ok"])
        entry = self.hidden.time_entries.get()
        self.assertEqual(entry.seconds, 2700)
        self.assertEqual(timezone.localtime(entry.ended_at).date().isoformat(), yesterday)
        self.assertFalse(entry.is_running)
        self.assertIn("loggade 45 min", list(self.hidden.activity.values_list("text", flat=True)))
        # Framtid och noll nekas.
        tomorrow = (timezone.localdate() + timezone.timedelta(days=1)).isoformat()
        url = f"/manage/arenden/{self.hidden.pk}/tid/"
        self.assertEqual(self._post(url, {"minutes": 5, "date": tomorrow}).status_code, 400)
        self.assertEqual(self._post(url, {"minutes": 0}).status_code, 400)
        # Ändra och ta bort.
        self._post(f"/manage/tid/{entry.pk}/", {"minutes": 60, "note": "Mer"})
        entry.refresh_from_db()
        self.assertEqual(entry.seconds, 3600)
        self.assertEqual(entry.note, "Mer")
        self._post(f"/manage/tid/{entry.pk}/", {"delete": True})
        self.assertEqual(self.hidden.time_entries.count(), 0)

    def test_today_summary_counts_finished_and_running_time(self):
        from .board import today_seconds

        TimeEntry.log(self.visible, self.staff, 30)
        self.assertEqual(today_seconds(self.staff), 1800)
        self.visible.start_timer(self.staff)
        self.assertGreaterEqual(today_seconds(self.staff), 1800)
        stats = self._post(f"/manage/arenden/{self.visible.pk}/timer/", {}).json()["stats"]
        self.assertIsNone(stats["timer"])
        self.assertIn("today", stats)

    def test_comments_never_mail_the_customer(self):
        url = f"/manage/arenden/{self.visible.pk}/kommentar/"
        self._post(url, {"body": "Internt", "internal": True})
        self._post(url, {"body": "Till portalen", "internal": False})
        self.assertEqual(self.visible.comments.filter(is_internal=True).count(), 1)
        self.assertEqual(self.visible.comments.filter(is_internal=False).count(), 1)
        self.assertEqual(len(mail.outbox), 0)
        # Flytt till Klart mejlar inte heller.
        done = self.project.columns.get(is_done=True)
        self._post(f"/manage/arenden/{self.visible.pk}/flytta/", {"target": f"c{done.pk}"})
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(**EMAIL)
    def test_the_manual_button_is_the_only_way_to_mail_the_customer(self):
        url = f"/manage/arenden/{self.visible.pk}/mejla-kunden/"
        r = self._post(url, {"body": "Nu är det klart."})
        self.assertTrue(r.json()["mailed"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["anna@acme.se"])
        self.assertIn("Nu är det klart.", mail.outbox[0].body)
        self.assertIn("/kund/arenden/", mail.outbox[0].body)
        comment = self.visible.comments.get()
        self.assertFalse(comment.is_internal)
        texts = list(self.visible.activity.values_list("text", flat=True))
        self.assertIn("mejlade kunden (Acme AB)", texts)
        # Utan kund: nekas.
        internal = Issue.objects.create(title="Internt utan kund")
        r = self._post(f"/manage/arenden/{internal.pk}/mejla-kunden/", {"body": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(mail.outbox), 1)

    def test_customer_recipients_are_deduplicated(self):
        from .emails import customer_recipients

        self.acme.email = "Anna@acme.se"
        self.acme.save()
        self.assertEqual(customer_recipients(self.acme), ["anna@acme.se"])

    def test_attachment_upload_from_the_panel(self):
        r = self.as_staff().post(
            f"/manage/arenden/{self.hidden.pk}/bilaga/",
            {
                "files": SimpleUploadedFile("bild.png", b"x" * 10, content_type="image/png"),
                "panel": "1",
            },
        )
        self.assertIn("bild.png", r.json()["panel"])
        self.assertEqual(self.hidden.attachments.count(), 1)

    def test_customer_contact_cannot_reach_the_panel(self):
        r = self.as_contact().get(f"/manage/arenden/{self.visible.pk}/panel/")
        self.assertEqual(r.status_code, 302)
