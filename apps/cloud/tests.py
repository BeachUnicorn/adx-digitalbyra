"""
AWS-kontona (apps/cloud). Inget test pratar med AWS: aws-modulens
funktioner byts ut, och det som testas är vårt - vem som får se vad, att
en synk varken dubblerar eller blandar ihop konton, och att läsrollen
verkligen bara läser.
"""

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from apps.projects.models import Customer

from . import aws, role_template
from .models import AwsAccount, AwsInvoice
from .sync import sync_account

ACME_ID, OTHER_ID = "999999999901", "999999999902"
PDF = b"%PDF-1.4 test"


def invoice_row(invoice_id="EUINSE26-1", month=8, total="125.00"):
    return {
        "invoice_id": invoice_id,
        "invoice_type": "INVOICE",
        "entity": "Amazon Web Services EMEA SARL",
        "period_year": 2026,
        "period_month": month,
        "issued_on": date(2026, month + 1, 2),
        "due_on": date(2026, month + 1, 2),
        "currency": "USD",
        "total": Decimal(total),
        "tax": Decimal("25.00"),
    }


class Fixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.staff = User.objects.create_user("byra", password="x12345678", is_staff=True)
        cls.acme = Customer.objects.create(name="Acme AB")
        cls.other = Customer.objects.create(name="Annan AB")
        cls.contact = User.objects.create_user("nina", email="nina@acme.se")
        cls.acme.users.add(cls.contact)
        cls.stranger = User.objects.create_user("olle", email="olle@annan.se")
        cls.other.users.add(cls.stranger)
        cls.account = AwsAccount.objects.create(customer=cls.acme, account_id=ACME_ID, label="Prod")
        cls.other_account = AwsAccount.objects.create(customer=cls.other, account_id=OTHER_ID)

    def tearDown(self):
        for account_id in (ACME_ID, OTHER_ID):
            folder = Path(settings.PRIVATE_MEDIA_ROOT) / "aws-fakturor" / account_id
            shutil.rmtree(folder, ignore_errors=True)

    def make_invoice(self, account=None, invoice_id="EUINSE26-1", with_pdf=True):
        row = invoice_row(invoice_id)
        row.pop("invoice_id")
        invoice = AwsInvoice.objects.create(
            account=account or self.account, invoice_id=invoice_id, **row
        )
        if with_pdf:
            invoice.pdf.save("x.pdf", ContentFile(PDF), save=True)
        return invoice


class PortalInvoiceTests(Fixture):
    def test_contact_sees_own_invoices_and_downloads_the_pdf(self):
        invoice = self.make_invoice()
        self.make_invoice(self.other_account, "ANNAN-1")
        self.client.force_login(self.contact)
        page = self.client.get("/kund/fakturor/")
        self.assertContains(page, "EUINSE26-1")
        self.assertNotContains(page, "ANNAN-1")
        pdf = self.client.get(f"/kund/fakturor/{invoice.pk}/pdf/")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(b"".join(pdf.streaming_content), PDF)

    def test_another_customers_invoice_is_a_404(self):
        theirs = self.make_invoice(self.other_account, "ANNAN-1")
        self.client.force_login(self.contact)
        self.assertEqual(self.client.get(f"/kund/fakturor/{theirs.pk}/pdf/").status_code, 404)

    def test_hidden_account_shows_nothing(self):
        invoice = self.make_invoice()
        AwsAccount.objects.filter(pk=self.account.pk).update(show_invoices=False)
        self.client.force_login(self.contact)
        self.assertNotContains(self.client.get("/kund/fakturor/"), "EUINSE26-1")
        self.assertEqual(self.client.get(f"/kund/fakturor/{invoice.pk}/pdf/").status_code, 404)
        self.assertNotContains(self.client.get("/kund/tavla/"), "/kund/fakturor/")

    def test_menu_entry_appears_only_when_there_is_something_to_fetch(self):
        self.client.force_login(self.contact)
        self.assertNotContains(self.client.get("/kund/tavla/"), "/kund/fakturor/")
        self.make_invoice()
        self.assertContains(self.client.get("/kund/tavla/"), "/kund/fakturor/")

    def test_the_customer_never_sees_cost_or_warnings(self):
        self.make_invoice()
        AwsAccount.objects.filter(pk=self.account.pk).update(
            snapshot={"warnings": ["Root-kontot saknar MFA."], "cost": {"forecast": 4711}}
        )
        self.client.force_login(self.contact)
        page = self.client.get("/kund/fakturor/").content.decode()
        self.assertNotIn("MFA", page)
        self.assertNotIn("4711", page)

    def test_login_required(self):
        invoice = self.make_invoice()
        self.assertEqual(self.client.get("/kund/fakturor/").status_code, 302)
        self.assertEqual(self.client.get(f"/kund/fakturor/{invoice.pk}/pdf/").status_code, 302)


class ManageTests(Fixture):
    def test_add_account_validates_and_refuses_duplicates(self):
        self.client.force_login(self.staff)
        url = reverse("manage:aws_account_add", args=[self.acme.pk])
        self.client.post(url, {"account_id": "123"})
        self.client.post(url, {"account_id": OTHER_ID})  # finns på en annan kund
        self.assertEqual(AwsAccount.objects.count(), 2)
        self.client.post(url, {"account_id": "1234-5678-9012", "label": "Test"})
        added = AwsAccount.objects.get(account_id="123456789012")
        self.assertEqual(added.customer, self.acme)
        self.assertTrue(added.external_id.startswith("adx-"))
        self.assertNotEqual(added.external_id, self.account.external_id)

    def test_contacts_cannot_reach_the_agency_views(self):
        invoice = self.make_invoice()
        self.client.force_login(self.contact)
        for url in (
            reverse("manage:aws_role_file", args=[self.account.pk]),
            reverse("manage:aws_invoice_pdf", args=[invoice.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.client.post(
            reverse("manage:aws_account_update", args=[self.account.pk]), {"action": "delete"}
        )
        self.assertTrue(AwsAccount.objects.filter(pk=self.account.pk).exists())

    def test_customer_card_and_drift_show_the_account(self):
        AwsAccount.objects.filter(pk=self.account.pk).update(
            snapshot={"warnings": ["Root-kontot saknar MFA."]}
        )
        self.client.force_login(self.staff)
        card = self.client.get(reverse("manage:customer_detail", args=[self.acme.pk]))
        self.assertContains(card, "9999-9999-9901")
        self.assertContains(card, "Root-kontot saknar MFA.")
        self.assertContains(card, self.account.external_id)  # i CLI-kommandot
        self.assertContains(self.client.get(reverse("manage:drift")), "Root-kontot saknar MFA.")

    def test_delete_removes_rows_and_files(self):
        invoice = self.make_invoice()
        path = Path(invoice.pdf.path)
        self.client.force_login(self.staff)
        self.client.post(
            reverse("manage:aws_account_update", args=[self.account.pk]), {"action": "delete"}
        )
        self.assertFalse(AwsAccount.objects.filter(pk=self.account.pk).exists())
        self.assertFalse(path.exists())


class RoleTemplateTests(Fixture):
    def test_role_trusts_only_our_account_with_this_accounts_external_id(self):
        trust = role_template.trust_policy(self.account)["Statement"][0]
        self.assertEqual(
            trust["Principal"]["AWS"], f"arn:aws:iam::{settings.ADX_AWS_ACCOUNT_ID}:root"
        )
        self.assertEqual(
            trust["Condition"]["StringEquals"]["sts:ExternalId"], self.account.external_id
        )

    def test_role_can_only_read(self):
        for action in role_template.READ_ACTIONS:
            verb = action.split(":")[1]
            self.assertRegex(verb, r"^(List|Get|Describe)", action)
            self.assertNotIn("*", action)
        # Aldrig innehåll: inga S3-objekt, inga hemligheter, inga snapshots av data.
        joined = " ".join(role_template.READ_ACTIONS)
        for forbidden in ("s3:GetObject", "secretsmanager", "ssm:", "kms:"):
            self.assertNotIn(forbidden, joined)

    def test_every_call_we_make_is_allowed_by_the_role(self):
        """Lägger någon till ett anrop i aws.py utan att ge rollen rätten, faller det här."""
        source = (Path(__file__).parent / "aws.py").read_text()
        needed = {
            "list_invoice_summaries": "invoicing:ListInvoiceSummaries",
            "get_invoice_pdf": "invoicing:GetInvoicePDF",
            "get_cost_and_usage": "ce:GetCostAndUsage",
            "get_cost_forecast": "ce:GetCostForecast",
            "describe_instances": "ec2:DescribeInstances",
            "describe_snapshots": "ec2:DescribeSnapshots",
            "describe_security_groups": "ec2:DescribeSecurityGroups",
            "describe_db_instances": "rds:DescribeDBInstances",
            "list_buckets": "s3:ListAllMyBuckets",
            "list_domains": "route53domains:ListDomains",
            "get_account_summary": "iam:GetAccountSummary",
            "list_users": "iam:ListUsers",
            "list_access_keys": "iam:ListAccessKeys",
        }
        for call, action in needed.items():
            self.assertIn(call, source, call)
            self.assertIn(action, role_template.READ_ACTIONS)
        self.assertEqual(len(needed), len(role_template.READ_ACTIONS))


class SyncTests(Fixture):
    def patches(self, rows):
        return (
            mock.patch.object(aws, "session_for", return_value=object()),
            mock.patch.object(aws, "fetch_invoices", return_value=rows),
            mock.patch.object(aws, "download_pdf", return_value=PDF),
            mock.patch.object(aws, "fetch_snapshot", return_value={"warnings": []}),
        )

    def run_sync(self, rows):
        s, f, d, n = self.patches(rows)
        with s, f as fetch, d as download, n:
            result = sync_account(self.account)
        return result, fetch, download

    def test_sync_stores_invoices_and_pdfs_privately(self):
        (ok, text), fetch, _ = self.run_sync([invoice_row("A-1"), invoice_row("A-2", month=7)])
        self.assertTrue(ok, text)
        self.assertEqual(len(fetch.call_args.args[2]), 13)  # första gången: tretton månader
        invoice = self.account.invoices.get(invoice_id="A-1")
        self.assertEqual(invoice.total, Decimal("125.00"))
        self.assertTrue(str(invoice.pdf.path).startswith(str(settings.PRIVATE_MEDIA_ROOT)))
        self.account.refresh_from_db()
        self.assertEqual(self.account.last_error, "")
        self.assertIsNotNone(self.account.last_ok_at)

    def test_second_sync_neither_duplicates_nor_downloads_again(self):
        self.run_sync([invoice_row("A-1")])
        (ok, _), fetch, download = self.run_sync([invoice_row("A-1", total="130.00")])
        self.assertTrue(ok)
        self.assertEqual(len(fetch.call_args.args[2]), 3)  # sedan: tre månader
        download.assert_not_called()
        self.assertEqual(self.account.invoices.count(), 1)
        self.assertEqual(self.account.invoices.get().total, Decimal("130.00"))

    def test_a_failing_pdf_is_retried_next_time(self):
        s, f, _, n = self.patches([invoice_row("A-1")])
        with s, f, n, mock.patch.object(aws, "download_pdf", side_effect=aws.AwsError("nej")):
            ok, _ = sync_account(self.account)
        self.assertTrue(ok)
        self.assertFalse(self.account.invoices.get().pdf)
        _, _, download = self.run_sync([invoice_row("A-1")])
        download.assert_called_once()
        self.assertTrue(self.account.invoices.get().pdf)

    def test_an_aws_error_is_recorded_not_raised(self):
        with mock.patch.object(aws, "session_for", side_effect=aws.AwsError("Åtkomst nekad.")):
            ok, text = sync_account(self.account)
        self.assertFalse(ok)
        self.account.refresh_from_db()
        self.assertEqual(self.account.last_error, "Åtkomst nekad.")
        self.assertIsNone(self.account.last_ok_at)

    def test_command_reports_failures_and_needs_a_known_account(self):
        with self.assertRaises(CommandError):
            call_command("aws_sync", "--account", "000000000000")
        with mock.patch.object(aws, "session_for", side_effect=aws.AwsError("nej")):
            with self.assertRaises(CommandError):
                call_command("aws_sync", stdout=mock.Mock())


class AwsModuleTests(TestCase):
    def test_wrong_account_is_refused_before_anything_is_fetched(self):
        account = mock.Mock(account_id=ACME_ID, role_arn="arn", external_id="x")
        sts = mock.Mock()
        sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}
        }
        sts.get_caller_identity.return_value = {"Account": OTHER_ID}
        with (
            mock.patch.object(aws, "_client", return_value=sts),
            mock.patch.object(aws, "base_session"),
        ):
            with self.assertRaisesMessage(aws.AwsError, "väntade"):
                aws.session_for(account)
        self.assertEqual(sts.assume_role.call_args.kwargs["ExternalId"], "x")

    def test_pdf_is_only_fetched_from_amazon_over_https(self):
        client = mock.Mock()
        for url in (
            "http://x.s3.amazonaws.com/f.pdf",
            "https://evil.example/f.pdf",
            "file:///etc/passwd",
        ):
            client.get_invoice_pdf.return_value = {"InvoicePDF": {"DocumentUrl": url}}
            with (
                mock.patch.object(aws, "_client", return_value=client),
                mock.patch.object(aws, "urlopen") as opened,
            ):
                with self.assertRaises(aws.AwsError):
                    aws.download_pdf(object(), "A-1")
                opened.assert_not_called()

    def test_months_back_crosses_the_year(self):
        self.assertEqual(aws.months_back(3, date(2026, 2, 10)), [(2026, 2), (2026, 1), (2025, 12)])

    def test_warnings_in_plain_words(self):
        snapshot = {
            "security": {
                "root_mfa": False,
                "root_keys": True,
                "old_keys": [{"user": "deploy", "created": "2024-01-01"}],
                "open_ports": [
                    {"group": "web", "port": 22, "label": "SSH", "region": "eu-north-1"}
                ],
            },
            "domains": [
                {"name": "acme.se", "expires": "2026-10-01", "auto_renew": False},
                {"name": "trygg.se", "expires": "2026-10-01", "auto_renew": True},
            ],
            "resources": {"rds": [{"id": "db1", "backup_days": 0, "public": True}]},
            "cost": {
                "forecast": 400,
                "months": [{"amount": 100, "currency": "USD"}, {"amount": 5, "partial": True}],
            },
        }
        text = " ".join(aws.build_warnings(snapshot, today=date(2026, 9, 21)))
        for needle in (
            "saknar MFA",
            "har åtkomstnycklar",
            "deploy",
            "port 22",
            "acme.se",
            "db1",
            "prognos",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("trygg.se", text)
        self.assertEqual(
            aws.build_warnings({"security": {"error": "nekad"}, "cost": {"error": "x"}}), []
        )
