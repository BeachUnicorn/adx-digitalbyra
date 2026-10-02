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

    def test_state_is_customer_first_then_project(self):
        data = self.call("get", "state/").json()
        self.assertIsNone(data["customer"])
        self.assertEqual(data["scope_name"], "Alla kunder")
        self.assertIsNone(data["add_target"], "Alla är överblick, ingen plats att lägga till")
        self.assertEqual(data["projects"], [], "projekten visas först när en kund är vald")
        keys = {c["name"]: c["open"] for c in data["customers"]}
        self.assertEqual(keys["Acme"], 2)
        self.assertEqual(keys["Utan kund"], 2, "SKV saknar kund, OLD räknas som utan kund också")
        self.assertEqual(len(data["issues"]), 4)
        # Alla: äldst skapat överst.
        self.assertEqual([i["key"] for i in data["issues"]][:2], ["BDG-1", "BDG-2"])

        data = self.call("get", "state/", {"customer": str(self.acme.pk)}).json()
        self.assertEqual([p["key"] for p in data["projects"]], ["BDG"])
        self.assertEqual({i["key"] for i in data["issues"]}, {"BDG-1", "BDG-2"})
        self.assertEqual(data["add_target"], "Acme, BD Group", "kundens enda aktiva projekt")
        step = {"id": self.step.pk, "text": "Återskapa", "done": False}
        bug = next(i for i in data["issues"] if i["key"] == "BDG-1")
        self.assertEqual(bug["checklist"], [step])
        self.assertEqual(bug["customer"], "Acme")

        data = self.call("get", "state/", {"customer": "utan", "project": "skv"}).json()
        self.assertEqual(data["project"], "SKV")
        self.assertEqual([i["key"] for i in data["issues"]], ["SKV-1"])

    def test_a_loose_issue_shows_under_utan_kund_and_alla(self):
        loose = Issue.objects.create(title="Idé utan hem")
        for params in ({}, {"customer": "utan"}):
            keys = [i["key"] for i in self.call("get", "state/", params).json()["issues"]]
            self.assertIn(loose.key, keys)
        acme = self.call("get", "state/", {"customer": str(self.acme.pk)}).json()
        self.assertNotIn(loose.key, [i["key"] for i in acme["issues"]])

    def test_start_runs_the_timer_and_moves_new_to_active(self):
        data = self.call(
            "post", f"issues/{self.img.pk}/start/", {"customer": str(self.acme.pk)}
        ).json()
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
        data = self.call(
            "post", f"issues/{self.bug.pk}/done/", {"customer": str(self.acme.pk)}
        ).json()
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

    def test_create_follows_the_chosen_place(self):
        self.assertEqual(self.call("post", "issues/", {"title": "x"}).status_code, 400, "Alla")
        blank = self.call("post", "issues/", {"title": " ", "customer": "utan"})
        self.assertEqual(blank.status_code, 400)

        self.call(
            "post", "issues/", {"title": "<b>Ny</b> sida", "customer": "utan", "project": "SKV"}
        )
        issue = Issue.objects.get(title="Ny sida")
        self.assertEqual((issue.project, issue.customer), (self.skv, None))
        self.assertFalse(issue.visible_to_customer)

        # Kund med ett aktivt projekt: i projektet.
        self.call("post", "issues/", {"title": "I projektet", "customer": str(self.acme.pk)})
        self.assertEqual(Issue.objects.get(title="I projektet").project, self.bdg)

        # Kund med flera aktiva projekt: direkt på kunden.
        Project.objects.create(name="Support", key="BDGS", customer=self.acme)
        self.call("post", "issues/", {"title": "På kunden", "customer": str(self.acme.pk)})
        on_customer = Issue.objects.get(title="På kunden")
        self.assertEqual((on_customer.project, on_customer.customer), (None, self.acme))

        # Utan kund och utan projekt: helt löst.
        self.call("post", "issues/", {"title": "Löst", "customer": "utan"})
        loose = Issue.objects.get(title="Löst")
        self.assertEqual((loose.project, loose.customer), (None, None))

    def test_wrong_method_and_unknown_paths(self):
        self.assertEqual(self.call("get", f"issues/{self.bug.pk}/done/").status_code, 405)
        response = self.call("get", "finns-inte/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
