"""
Byråns egna texter till kunden nämner inte byråns leverantörer (Giovanni
2026-10-10). I utvecklingsdatabasen fanns redan kundloggens "Åtgärdade ett
fel i Sentry..." och "Flyttade DNS till Route 53.", som syns i portalen, på
statussidan och i loggens månadsmejl.

    AssistantRefusalTests   assistentens verktyg vägrar text som kunden ser
                            (skapa_arende och uppdatera_arende på ett
                            kundsynligt ärende, kommentera_arende utan intern,
                            skriv_kundlogg) och säger vilket namn det gäller
    ManageWarningTests      /manage/ sparar men varnar (kundloggen, svaret i
                            portalen, rubriken på ett synligt ärende,
                            anteckningen på statussidan); mejlet till kunden
                            frågar först och går bara med providers_ok
    DataScanTests           manage.py leverantorsnamn listar raderna och
                            skriver ingenting
"""

import io
import json

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.assistant.operations import OperationError
from apps.common import providers
from apps.monitor.models import settings_for

from .models import Comment, Customer, CustomerLogEntry, Issue, Project

EMAIL = {
    "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
    "EMAIL_HOST_USER": "x",
    "EMAIL_HOST_PASSWORD": "y",
}


class Fixture:
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user(
            "byra", "b@t.local", "x12345678", is_staff=True, first_name="Giovanni"
        )
        cls.customer = Customer.objects.create(name="Nordan Bygg AB", email="info@nordan.se")
        cls.contact = User.objects.create_user("anna@nordan.se", "anna@nordan.se", "x")
        cls.customer.users.add(cls.contact)
        cls.project = Project.objects.create(
            name="Ny hemsida", key="NORD", customer=cls.customer, created_by=cls.staff
        )
        cls.issue = Issue.objects.create(
            project=cls.project, title="Byt logotyp", reporter=cls.staff
        )

    def run_op(self, name, **params):
        from apps.assistant.runtime import run_operation

        def no_job():
            raise AssertionError("Ett direktverktyg ska aldrig behöva ett jobb.")

        return json.loads(run_operation(self.staff, no_job, name, params))


class AssistantRefusalTests(Fixture, TestCase):
    def assert_refused(self, name, field, **params):
        with self.assertRaises(OperationError) as ctx:
            self.run_op(name, **params)
        message = str(ctx.exception)
        self.assertIn(f"{field} nämner", message)
        self.assertIn("kunden ser texten", message)
        self.assertIn("sms-tjänsten", message)
        return message

    def test_the_customer_log_refuses_a_provider_and_takes_plain_text(self):
        message = self.assert_refused(
            "skriv_kundlogg", "text", kund=self.customer.name, text="Flyttade DNS till Route 53."
        )
        self.assertIn("Route 53", message)
        self.assertFalse(CustomerLogEntry.objects.exists())
        result = self.run_op(
            "skriv_kundlogg", kund=self.customer.name, text="Flyttade DNS till vår driftleverantör."
        )
        self.assertEqual(result["status"], "skapat")

    def test_a_visible_issue_refuses_and_an_internal_one_does_not(self):
        self.assert_refused(
            "skapa_arende",
            "beskrivning",
            rubrik="Fel i formuläret",
            projekt="NORD",
            beskrivning="Sentry visar felet.",
            synlig_for_kund=True,
        )
        self.assertEqual(Issue.objects.count(), 1)
        result = self.run_op(
            "skapa_arende", rubrik="Fel i formuläret", projekt="NORD", beskrivning="Sentry visar."
        )
        self.assertEqual(result["status"], "skapat")
        self.assertFalse(Issue.objects.get(pk=result["id"]).visible_to_customer)

    def test_updating_checks_what_the_customer_will_see(self):
        # Internt: allt går.
        self.run_op("uppdatera_arende", nyckel_eller_id="NORD-1", beskrivning="Kolla SES-loggen.")
        # Att göra det synligt prövar texten som redan står där.
        self.assert_refused(
            "uppdatera_arende", "beskrivning", nyckel_eller_id="NORD-1", synlig_for_kund=True
        )
        self.issue.refresh_from_db()
        self.assertFalse(self.issue.visible_to_customer)
        self.run_op(
            "uppdatera_arende",
            nyckel_eller_id="NORD-1",
            beskrivning="Kolla e-posttjänstens logg.",
            synlig_for_kund=True,
        )
        # Synligt: en ny rubrik prövas, prioriteten rör ingen text.
        self.assert_refused("uppdatera_arende", "rubrik", nyckel_eller_id="NORD-1", rubrik="AWS")
        self.run_op("uppdatera_arende", nyckel_eller_id="NORD-1", prioritet="hog")

    def test_only_comments_the_customer_can_see_are_checked(self):
        self.run_op("kommentera_arende", nyckel_eller_id="NORD-1", text="Sentry larmade.")
        self.assert_refused(
            "kommentera_arende",
            "text",
            nyckel_eller_id="NORD-1",
            text="46elks svarar inte.",
            intern=False,
        )
        self.assertEqual(Comment.objects.filter(is_internal=False).count(), 0)


@override_settings(**EMAIL)
class ManageWarningTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.staff)

    def post_json(self, url, payload):
        return self.client.post(url, json.dumps(payload), content_type="application/json")

    def warnings(self, url, data):
        """Beskeden med nivån warning på sidan formuläret leder till."""
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)
        return [str(m) for m in response.context["messages"] if m.level_tag == "warning"]

    def test_the_customer_log_is_saved_with_a_warning(self):
        url = reverse("manage:customer_log_add", args=[self.customer.pk])
        (warning,) = self.warnings(url, {"text": "Åtgärdade ett fel i Sentry."})
        self.assertEqual(CustomerLogEntry.objects.count(), 1)
        self.assertIn("Texten nämner Sentry, och kunden ser den.", warning)
        self.assertEqual(self.warnings(url, {"text": "Åtgärdade ett fel i formuläret."}), [])

    def test_a_portal_reply_warns_and_an_internal_note_does_not(self):
        url = reverse("manage:issue_comment", args=[self.issue.pk])
        reply = self.post_json(url, {"body": "Vi bytte till SES.", "internal": False}).json()
        self.assertTrue(reply["ok"])
        self.assertIn("Texten nämner SES", reply["warning"])
        note = self.post_json(url, {"body": "Vi bytte till SES.", "internal": True}).json()
        self.assertNotIn("warning", note)
        self.assertEqual(Comment.objects.count(), 2)
        # Formuläret med bilagor: samma varning som besked.
        (warning,) = self.warnings(url, {"body": "Loggarna ligger i S3."})
        self.assertIn("Texten nämner S3", warning)

    def test_the_title_of_a_visible_issue_warns(self):
        url = reverse("manage:issue_field", args=[self.issue.pk])
        answer = self.post_json(url, {"field": "title", "value": "Flytta till EC2"}).json()
        self.assertNotIn("warning", answer, "ärendet är internt")
        answer = self.post_json(url, {"field": "visible_to_customer", "value": True}).json()
        self.assertIn("Texten nämner EC2", answer["warning"])
        answer = self.post_json(url, {"field": "title", "value": "Flytta servern"}).json()
        self.assertNotIn("warning", answer)

    def test_the_mail_to_the_customer_asks_first(self):
        self.issue.visible_to_customer = True
        self.issue.save()
        url = reverse("manage:issue_email_customer", args=[self.issue.pk])
        response = self.post_json(url, {"body": "Det var ett fel hos 46elks."})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["providers"])
        self.assertIn("Mejla ändå?", response.json()["error"])
        self.assertEqual(Comment.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)
        response = self.post_json(
            url, {"body": "Det var ett fel hos 46elks.", "providers_ok": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        response = self.post_json(url, {"body": "Det var ett fel hos sms-tjänsten."})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)

    def test_the_status_page_note_warns(self):
        url = reverse("manage:monitor_update", args=[self.customer.pk])
        (warning,) = self.warnings(url, {"note": "Vi flyttade till Ubuntu 24 i helgen."})
        self.assertEqual(settings_for(self.customer).note, "Vi flyttade till Ubuntu 24 i helgen.")
        self.assertIn("Anteckningen: Texten nämner Ubuntu", warning)

    def test_the_warning_text(self):
        self.assertEqual(providers.warning("Inget här", ""), "")
        self.assertIn("AWS och 46elks", providers.warning("AWS", "46elks"))


class DataScanTests(Fixture, TestCase):
    def test_the_scan_lists_customer_visible_rows_and_writes_nothing(self):
        CustomerLogEntry.objects.create(
            customer=self.customer, text="Flyttade DNS till Route 53.", author=self.staff
        )
        CustomerLogEntry.objects.create(
            customer=self.customer, text="Bytte logotyp.", author=self.staff
        )
        Comment.objects.create(issue=self.issue, body="Intern: Sentry.", is_internal=True)
        Comment.objects.create(issue=self.issue, body="Svar: Sentry.", is_internal=False)
        out = io.StringIO()
        call_command("leverantorsnamn", stdout=out)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[-1], "2 träffar.")
        self.assertTrue(any("CustomerLogEntry" in line and "Route 53" in line for line in lines))
        self.assertTrue(any("Comment" in line and "Svar: Sentry." in line for line in lines))
        self.assertFalse(any("Intern: Sentry." in line for line in lines))
        self.assertEqual(CustomerLogEntry.objects.count(), 2)
