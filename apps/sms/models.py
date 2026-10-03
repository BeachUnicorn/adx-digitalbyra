"""
SMS-API:t för ADX kunder (apps/sms/README.md): kundens konto, nycklarna,
varje sms och månadsunderlagen.

Belopp lagras som heltal i 46elks egen enhet, tiotusendels krona: 10 000 =
1 kr, 100 = 1 öre. 46elks anger cost och estimated_cost i den enheten (5200
= 52 öre), så ingen omräkning sker på vägen och inga avrundningsfel samlas
på hög. Kontots inställningar skrivs i hela öre och kronor, som byrån och
kunden tänker; omräkningen sker i pricing.py. Alla belopp är utan moms.

Allt sparas för alltid, texter och nummer inräknade (beslut 2026-10-03):
kunden ser sina skickade sms i portalen. Raderna skyddas mot kaskadradering
(PROTECT), så att ett borttaget konto inte tar historiken med sig.
"""

import hashlib
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.projects.models import Customer

#: 46elks enhet: tiotusendels krona.
UNITS_PER_KR = 10_000
UNITS_PER_ORE = 100

DEFAULT_MARKUP_ORE = 5
DEFAULT_YEARLY_FEE_KR = 999
#: Kundens tak per månad tills kunden själv ändrar det. 500 kr räcker till
#: knappt 900 svenska sms-delar med standardpåslaget.
DEFAULT_MONTHLY_CAP_KR = 500
MAX_MONTHLY_CAP_KR = 1_000_000

#: 46elks: en textavsändare är 3-11 tecken a-z, A-Z och 0-9 och börjar med en
#: bokstav ("Alphanumeric numbers may not start with a digit"). Prövas med
#: fullmatch: "$" i match släpper annars igenom ett radbrytningstecken sist.
SENDER_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,10}")


def validate_sender(value):
    if not SENDER_RE.fullmatch(value or ""):
        raise ValidationError(
            "Avsändaren ska vara 3-11 tecken, bara a-z, A-Z och 0-9, och börja med en bokstav."
        )


def default_countries():
    return ["SE"]


class SmsAccount(models.Model):
    """Kundens SMS-tjänst. API:t och nycklarna fungerar bara när is_enabled
    är på, och det slår bara byrån på (kundkortet)."""

    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name="sms_account")
    is_enabled = models.BooleanField("Aktiverat", default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    #: Senaste avstängningen. Årsavgiften för en månad beror på om SMS var
    #: aktiverat under månaden, inte på läget när månaden stängs
    #: (pricing.fee_due).
    disabled_at = models.DateTimeField(null=True, blank=True)
    sender_name = models.CharField(
        "Avsändare", max_length=11, blank=True, validators=[validate_sender]
    )
    markup_ore_per_part = models.PositiveIntegerField(
        "Påslag per sms-del (öre)", default=DEFAULT_MARKUP_ORE
    )
    yearly_fee_kr = models.PositiveIntegerField("Årsavgift (kr)", default=DEFAULT_YEARLY_FEE_KR)
    monthly_cap_kr = models.PositiveIntegerField(
        "Kostnadstak per månad (kr)", default=DEFAULT_MONTHLY_CAP_KR
    )
    #: Vem som senast ändrade taket och när: kunden i portalen, byrån i
    #: kundvyn eller på kundkortet.
    monthly_cap_changed_at = models.DateTimeField(null=True, blank=True)
    monthly_cap_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    allowed_countries = models.JSONField("Tillåtna länder", default=default_countries)
    #: Tjänsteårets första dag. Årsavgiften hamnar på underlaget för den
    #: månaden och för samma månad varje år därefter (pricing.fee_due).
    service_year_start = models.DateField("Tjänsteåret börjar", null=True, blank=True)
    #: Larm till byrån: månaden taket senast larmades för, och senaste
    #: larmet om fel hos 46elks (alerts.py). Villkorliga UPDATE:s, så att
    #: flera arbetare samtidigt inte fyller byråns inkorg.
    cap_alerted_period = models.DateField(null=True, blank=True, editable=False)
    provider_alerted_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "SMS-konto"
        verbose_name_plural = "SMS-konton"
        ordering = ["customer__name"]

    def __str__(self):
        return f"SMS för {self.customer.name}"

    def record_cap_change(self, user, now=None):
        """Taket ändrades: när och av vem (sparas av den som anropar)."""
        self.monthly_cap_changed_at = now or timezone.now()
        self.monthly_cap_changed_by = user if user and user.is_authenticated else None

    @property
    def countries(self):
        return [str(c).upper() for c in (self.allowed_countries or []) if c]

    @property
    def cap_units(self):
        return int(self.monthly_cap_kr) * UNITS_PER_KR

    @property
    def markup_units_per_part(self):
        return int(self.markup_ore_per_part) * UNITS_PER_ORE

    @property
    def yearly_fee_units(self):
        return int(self.yearly_fee_kr) * UNITS_PER_KR


def _hash_secret(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


class SmsApiKey(models.Model):
    """API-nyckel. Bara SHA-256-hashen sparas; klartexten visas en gång, när
    nyckeln skapas (samma mönster som apps/assistant)."""

    PREFIX = "adxsms_"
    #: Så mycket av nyckeln som sparas och visas, för att känna igen den.
    SHOWN = 12

    account = models.ForeignKey(SmsAccount, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField("Namn", max_length=80)
    prefix = models.CharField(max_length=16, editable=False)
    secret_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "SMS-nyckel"
        verbose_name_plural = "SMS-nycklar"
        ordering = ["revoked_at", "-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}...)"

    @property
    def is_active(self):
        return self.revoked_at is None

    @classmethod
    def issue(cls, account, name, user=None):
        """Skapa en nyckel. Returnerar (nyckel, klartext)."""
        raw = cls.PREFIX + secrets.token_urlsafe(32)
        key = cls.objects.create(
            account=account,
            name=(name or "API-nyckel").strip()[:80] or "API-nyckel",
            prefix=raw[: cls.SHOWN],
            secret_hash=_hash_secret(raw),
            created_by=user if user and user.is_authenticated else None,
        )
        return key, raw

    @classmethod
    def lookup(cls, raw):
        """Klartext -> nyckel som inte är återkallad, annars None. Om kontot
        är aktiverat prövas av den som anropar (felet ska gå att skilja)."""
        if not raw or not raw.startswith(cls.PREFIX) or len(raw) > 200:
            return None
        return (
            cls.objects.select_related("account", "account__customer")
            .filter(secret_hash=_hash_secret(raw), revoked_at__isnull=True)
            .first()
        )

    def touch(self, now=None):
        """last_used_at, högst en skrivning i minuten per nyckel."""
        now = now or timezone.now()
        stale = now - timedelta(minutes=1)
        SmsApiKey.objects.filter(pk=self.pk).filter(
            Q(last_used_at__isnull=True) | Q(last_used_at__lt=stale)
        ).update(last_used_at=now)

    def revoke(self, user=None):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.revoked_by = user if user and user.is_authenticated else None
            self.save(update_fields=["revoked_at", "revoked_by"])


class SmsMessage(models.Model):
    """Ett sms, eller ett försök. Varje anrop som klarat nyckeln och
    aktiveringen blir en rad, också de som stoppades (rejected,
    blocked_cap), så att kunden ser vad som hände."""

    class Status(models.TextChoices):
        RESERVED = "reserved", "Skickas"
        SENT = "sent", "Skickat"
        DELIVERED = "delivered", "Levererat"
        FAILED = "failed", "Misslyckades"
        REJECTED = "rejected", "Stoppat"
        BLOCKED_CAP = "blocked_cap", "Taket nått"

    class Encoding(models.TextChoices):
        GSM7 = "gsm7", "GSM-7"
        UCS2 = "ucs2", "UCS-2"

    account = models.ForeignKey(SmsAccount, on_delete=models.PROTECT, related_name="messages")
    api_key = models.ForeignKey(
        SmsApiKey, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages"
    )
    #: Kundens egen nyckel för idempotens: samma reference ger samma sms.
    reference = models.CharField(max_length=64, blank=True)
    to = models.CharField("Till", max_length=32)
    country = models.CharField("Land", max_length=2, blank=True)
    sender = models.CharField("Från", max_length=11, blank=True)
    body = models.TextField("Text")
    parts = models.PositiveSmallIntegerField("Delar", default=0)
    encoding = models.CharField(max_length=4, choices=Encoding.choices, default=Encoding.GSM7)
    status = models.CharField(max_length=12, choices=Status.choices, db_index=True)
    #: Stabil felkod (samma som API:t svarar med) och en förklaring.
    error_code = models.CharField(max_length=32, blank=True)
    error = models.CharField(max_length=300, blank=True)
    provider_id = models.CharField(max_length=64, blank=True, db_index=True)
    #: Belopp i tiotusendels krona (UNITS_PER_KR).
    estimated_cost = models.IntegerField(default=0)
    provider_cost = models.IntegerField(default=0)
    markup = models.IntegerField(default=0)
    customer_price = models.IntegerField(default=0)
    #: 46elks anropades med dryrun (SMS_SEND_LIVE av): inget skickades.
    test_mode = models.BooleanField(default=False)
    #: Byrån ska stämma av sms:et mot 46elks: svaret på sändningen var oklart
    #: (felkod provider_unknown), eller 46elks id kom först med en
    #: leveransrapport och priset är därför uppskattat (service.py).
    needs_check = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    #: Statusar som inte längre ändras av en leveransrapport.
    FINAL = (Status.DELIVERED, Status.FAILED, Status.REJECTED, Status.BLOCKED_CAP)
    #: Stoppade före 46elks: kostar inget och håller ingen reference.
    STOPPED = (Status.REJECTED, Status.BLOCKED_CAP)
    #: Tog 46elks emot sms:et (och debiterade det)?
    ACCEPTED = (Status.SENT, Status.DELIVERED, Status.FAILED)

    class Meta:
        verbose_name = "Sms"
        verbose_name_plural = "Sms"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["account", "created_at"], name="sms_msg_account_created"),
            models.Index(fields=["api_key", "created_at"], name="sms_msg_key_created"),
            models.Index(fields=["country", "sent_at"], name="sms_msg_country_sent"),
        ]
        constraints = [
            # En reference hör till ett sms per konto - men bara när sms:et
            # gick vidare. Ett stoppat försök eller ett som 46elks inte tog
            # emot släpper den, så att kunden kan försöka igen med samma.
            models.UniqueConstraint(
                fields=["account", "reference"],
                condition=~Q(reference="")
                & ~Q(status__in=["rejected", "blocked_cap"])
                & ~Q(error_code="provider_error"),
                name="sms_msg_unique_reference",
            ),
        ]

    def __str__(self):
        # Utan numret: strängen hamnar i loggar och felrapporter.
        return f"Sms {self.pk} ({self.status})"

    @property
    def was_accepted(self):
        """46elks tog emot sms:et: det har ett id och ett pris."""
        return bool(self.provider_id) or (
            self.test_mode and self.status in (self.Status.SENT, self.Status.DELIVERED)
        )

    @property
    def is_estimate(self):
        return self.status == self.Status.RESERVED

    @property
    def is_unknown(self):
        """46elks svar på sändningen var oklart: sms:et kan ha skickats. Står
        kvar som reserverat tills en leveransrapport eller byrån avgör det."""
        return self.status == self.Status.RESERVED and self.error_code == "provider_unknown"


class MonthlyStatement(models.Model):
    """Månadsunderlaget för fakturering, i efterhand. Fryst när månaden
    stängs (closed_at): siffrorna räknas aldrig om efter det. Den pågående
    månaden visas som ett osparat underlag (pricing.build_statement)."""

    account = models.ForeignKey(SmsAccount, on_delete=models.PROTECT, related_name="statements")
    #: Månadens första dag.
    period = models.DateField()
    sms_count = models.PositiveIntegerField(default=0)
    parts = models.PositiveIntegerField(default=0)
    #: Belopp i tiotusendels krona, utan moms.
    provider_cost = models.BigIntegerField(default=0)
    markup = models.BigIntegerField(default=0)
    fee = models.BigIntegerField(default=0)
    total = models.BigIntegerField(default=0)
    #: Rader per land: [{"country", "sms", "parts", "provider_cost", "markup", "total"}].
    lines = models.JSONField(default=list)
    markup_ore_per_part = models.PositiveIntegerField(default=0)
    yearly_fee_kr = models.PositiveIntegerField(default=0)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Månadsunderlag"
        verbose_name_plural = "Månadsunderlag"
        ordering = ["-period", "account__customer__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "period"], name="sms_statement_unique_period"
            )
        ]

    def __str__(self):
        return f"{self.account.customer.name} {self.period:%Y-%m}"

    @property
    def is_closed(self):
        return self.closed_at is not None
