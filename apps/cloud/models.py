"""
Kundernas AWS-konton (noll eller flera per kund).

Vi sparar INGA nycklar. Varje konto har en läsroll som litar på byråns
AWS-konto och kräver ett ExternalId; här ligger bara konto-ID, rollens namn
och ExternalId. adx.se-servern antar rollen med sin egen instansroll.

Kunden ser en enda sak i portalen: fakturorna (om rutan är ikryssad).
Kostnad, resurser, domäner och säkerhet är byråns egen bild och visas bara
i /manage/. Ögonblicksbilden sparas som JSON - den byts ut vid varje
hämtning och har inget värde som historik; fakturorna är egna rader.
"""

import secrets

from django.core.validators import RegexValidator
from django.db import models

from apps.projects.models import Customer, private_storage

DEFAULT_ROLE_NAME = "ADXReadOnly"


def new_external_id():
    return "adx-" + secrets.token_hex(16)


def invoice_pdf_path(instance, filename):
    return f"aws-fakturor/{instance.account.account_id}/{instance.invoice_id}.pdf"


class AwsAccount(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="aws_accounts")
    label = models.CharField("Namn", max_length=80, blank=True)
    account_id = models.CharField(
        "Konto-ID",
        max_length=12,
        unique=True,
        validators=[RegexValidator(r"^\d{12}$", "Konto-ID är tolv siffror.")],
    )
    role_name = models.CharField("Roll", max_length=64, default=DEFAULT_ROLE_NAME)
    external_id = models.CharField(max_length=64, default=new_external_id, editable=False)
    show_invoices = models.BooleanField("Kunden ser fakturorna i portalen", default=True)
    is_active = models.BooleanField(default=True)

    snapshot = models.JSONField(default=dict, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["customer__name", "label", "account_id"]
        verbose_name = "AWS-konto"
        verbose_name_plural = "AWS-konton"

    def __str__(self):
        return f"{self.label or self.account_id} ({self.customer.name})"

    @property
    def role_arn(self):
        return f"arn:aws:iam::{self.account_id}:role/{self.role_name}"

    @property
    def display_id(self):
        """1234-5678-9012, som AWS själv skriver det."""
        a = self.account_id
        return f"{a[:4]}-{a[4:8]}-{a[8:]}"

    @property
    def warnings(self):
        return self.snapshot.get("warnings", []) if self.snapshot else []


class AwsInvoice(models.Model):
    account = models.ForeignKey(AwsAccount, on_delete=models.CASCADE, related_name="invoices")
    invoice_id = models.CharField(max_length=64)
    invoice_type = models.CharField(max_length=20, blank=True)  # INVOICE / CREDIT_MEMO
    entity = models.CharField("Utfärdare", max_length=120, blank=True)
    period_year = models.PositiveSmallIntegerField()
    period_month = models.PositiveSmallIntegerField()
    issued_on = models.DateField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    tax = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    pdf = models.FileField(upload_to=invoice_pdf_path, storage=private_storage, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_year", "-period_month", "-issued_on", "invoice_id"]
        constraints = [
            models.UniqueConstraint(fields=["account", "invoice_id"], name="unik_faktura_per_konto")
        ]
        verbose_name = "AWS-faktura"
        verbose_name_plural = "AWS-fakturor"

    def __str__(self):
        return f"{self.invoice_id} ({self.period_year}-{self.period_month:02d})"

    @property
    def is_credit(self):
        return self.invoice_type == "CREDIT_MEMO"

    @property
    def filename(self):
        return f"AWS-{self.period_year}-{self.period_month:02d}-{self.invoice_id}.pdf"
