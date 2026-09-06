"""
Datamodellens regler, låsta som tester.

Det som bär allt: en sanning om kunden (projektets vinner), löpnummer per
projekt under lås, kolumner som data där is_done stänger ärendet, och
timern som en öppen tidspost med databasens garanti om EN igång per
användare.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .models import Column, Customer, Issue, Project, TimeEntry


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
