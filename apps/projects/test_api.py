"""API:t för ADX Fokus (apps/projects/api.py)."""

import json

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase

from apps.assistant.models import AssistantToken

from .models import ChecklistItem, Customer, Issue, IssuePriority, Project, ProjectStatus, TimeEntry

User = get_user_model()


class ApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Acme")
        cls.contact = User.objects.create_user("anna@acme.se", password="x12345678")
        cls.acme.users.add(cls.contact)
        cls.bdg = Project.objects.create(name="BD Group", key="BDG", customer=cls.acme)
        cls.skv = Project.objects.create(name="Skandi VVS", key="SKV")
        cls.old = Project.objects.create(name="Gammalt", key="OLD", status=ProjectStatus.DONE)
        cls.bug = Issue.objects.create(
            project=cls.bdg, title="Frakt", priority=IssuePriority.URGENT
        )
        cls.img = Issue.objects.create(project=cls.bdg, title="Bild")
        cls.form = Issue.objects.create(project=cls.skv, title="Formulär")
        Issue.objects.create(project=cls.old, title="Arkiverat")
        cls.step = ChecklistItem.objects.create(issue=cls.bug, text="Återskapa")
        _, cls.raw = AssistantToken.issue(cls.staff, name="ADX Fokus")

    def call(self, method, path, data=None, raw=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw or self.raw}"}
        if method == "get":
            return self.client.get(f"/api/v1/{path}", data or {}, **headers)
        return self.client.post(
            f"/api/v1/{path}", json.dumps(data or {}), content_type="application/json", **headers
        )

    def test_without_a_valid_key_it_is_401_json(self):
        for raw in ("", "adx_fel"):
            response = self.client.get(
                "/api/v1/state/", HTTP_AUTHORIZATION=f"Bearer {raw}" if raw else ""
            )
            self.assertEqual(response.status_code, 401)
            self.assertFalse(response.json()["ok"])

    def test_a_customer_contact_with_a_key_is_shut_out(self):
        _, raw = AssistantToken.issue(self.contact)
        self.assertEqual(self.call("get", "state/", raw=raw).status_code, 403)

    def test_a_revoked_key_stops_working(self):
        AssistantToken.objects.filter(user=self.staff).update(is_active=False)
        self.assertEqual(self.call("get", "state/").status_code, 401)

    def test_state_lists_active_projects_and_filters_issues(self):
        data = self.call("get", "state/").json()
        self.assertEqual([p["key"] for p in data["projects"]], ["BDG", "SKV"])
        self.assertEqual({p["key"]: p["open"] for p in data["projects"]}, {"BDG": 2, "SKV": 1})
        self.assertEqual(len(data["issues"]), 3, "alla aktiva projekt, inte det avslutade")
        self.assertIsNone(data["running"])

        data = self.call("get", "state/", {"project": "bdg"}).json()
        self.assertEqual(data["project"], "BDG")
        self.assertEqual([i["key"] for i in data["issues"]], ["BDG-1", "BDG-2"], "akut först")
        step = {"id": self.step.pk, "text": "Återskapa", "done": False}
        self.assertEqual(data["issues"][0]["checklist"], [step])

    def test_start_runs_the_timer_and_moves_new_to_active(self):
        data = self.call("post", f"issues/{self.img.pk}/start/", {"project": "BDG"}).json()
        self.assertEqual(data["running"]["key"], "BDG-2")
        self.img.refresh_from_db()
        self.assertEqual(self.img.stage, "active")
        self.assertEqual(data["issues"][0]["key"], "BDG-2", "pågående först")
        # En timer i taget: att starta en annan stoppar den första.
        self.call("post", f"issues/{self.bug.pk}/start/")
        self.assertEqual(TimeEntry.objects.running().get(user=self.staff).issue, self.bug)
        self.assertEqual(TimeEntry.objects.filter(user=self.staff).count(), 2)

    def test_stop_ends_the_timer(self):
        self.call("post", f"issues/{self.bug.pk}/start/")
        data = self.call("post", "timer/stop/").json()
        self.assertIsNone(data["running"])
        self.assertFalse(TimeEntry.objects.running().exists())

    def test_done_closes_the_issue_quietly_and_stops_its_timer(self):
        self.call("post", f"issues/{self.bug.pk}/start/")
        data = self.call("post", f"issues/{self.bug.pk}/done/", {"project": "BDG"}).json()
        self.bug.refresh_from_db()
        self.assertTrue(self.bug.is_closed)
        self.assertIsNone(data["running"])
        self.assertNotIn("BDG-1", [i["key"] for i in data["issues"]])
        self.assertEqual(mail.outbox, [], "kunden mejlas aldrig automatiskt")

    def test_checklist_toggles_and_can_be_set(self):
        self.call("post", f"checklist/{self.step.pk}/")
        self.step.refresh_from_db()
        self.assertTrue(self.step.is_done)
        self.call("post", f"checklist/{self.step.pk}/", {"done": False})
        self.step.refresh_from_db()
        self.assertFalse(self.step.is_done)

    def test_create_needs_a_project_and_a_title(self):
        self.assertEqual(self.call("post", "issues/", {"title": "x"}).status_code, 400)
        blank = self.call("post", "issues/", {"title": " ", "project": "SKV"})
        self.assertEqual(blank.status_code, 400)
        data = self.call("post", "issues/", {"title": "<b>Ny</b> sida", "project": "SKV"}).json()
        issue = Issue.objects.get(project=self.skv, title="Ny sida")
        self.assertIn(issue.key, [i["key"] for i in data["issues"]])
        self.assertFalse(issue.visible_to_customer)

    def test_wrong_method_and_unknown_paths(self):
        self.assertEqual(self.call("get", f"issues/{self.bug.pk}/done/").status_code, 405)
        response = self.call("get", "finns-inte/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
