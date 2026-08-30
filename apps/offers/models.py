"""
Offertsystemet: byggaren i /manage/, kundsidan på en hemlig länk.

Tre modeller:

- Product: återanvändbar katalogpost med RIKTPRIS. När den läggs på en
  offert KOPIERAS namn, beskrivning, pris och pristyp till raden - därefter
  äger raden sina värden. Det är därför en produkt kan prissättas olika i
  olika offerter, och därför en skickad offert aldrig ändras av att någon
  redigerar katalogen i efterhand.
- Quote: offerten. Kunden ser den via en slumpad token-länk (/offert/<token>/),
  aldrig via id - länken ÄR behörigheten, precis som en olistad video.
- QuoteLine: en rad. Pristypen (engång/månad/år) sitter på raden, inte på
  offerten, så samma offert kan blanda leveranspris och löpande avtal.

Alla belopp är hela kronor exklusive moms. Momsen är en visningsfråga
(25 procent på allt ADX säljer), inte en datafråga.
"""

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def _generate_token():
    # 24 byte -> 32 tecken urlsafe. Gissningsrymden är poängen: länken
    # är offertens enda skydd, så den måste vara omöjlig att räkna upp.
    return secrets.token_urlsafe(24)


def format_kr(amount):
    """Hela kronor med svenskt tusentalsmellanrum: 50000 -> '50 000'."""
    return f"{amount:,}".replace(",", " ")


class PricePeriod(models.TextChoices):
    ONE_TIME = "one_time", "Engång"
    MONTHLY = "monthly", "Per månad"
    YEARLY = "yearly", "Per år"


PERIOD_SUFFIX = {
    PricePeriod.ONE_TIME: "kr",
    PricePeriod.MONTHLY: "kr/mån",
    PricePeriod.YEARLY: "kr/år",
}


class Product(models.Model):
    """Katalogpost. Riktpriset är ett förslag - raden ärver och äger det."""

    name = models.CharField("Namn", max_length=200)
    description = models.TextField("Beskrivning", blank=True)
    default_price = models.PositiveIntegerField("Riktpris (kr exkl. moms)", default=0)
    default_period = models.CharField(
        "Pristyp", max_length=10, choices=PricePeriod.choices, default=PricePeriod.ONE_TIME
    )
    is_active = models.BooleanField("Aktiv", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Produkt"
        verbose_name_plural = "Produkter"

    def __str__(self):
        return self.name


class QuoteStatus(models.TextChoices):
    DRAFT = "draft", "Utkast"
    SENT = "sent", "Skickad"
    OPENED = "opened", "Öppnad"
    ACCEPTED = "accepted", "Accepterad"
    DECLINED = "declined", "Förlorad"


class Quote(models.Model):
    token = models.CharField(max_length=48, unique=True, default=_generate_token, editable=False)
    customer_name = models.CharField("Kund", max_length=200)
    customer_email = models.EmailField("Kundens e-post", blank=True)
    project_title = models.CharField("Projekt", max_length=200, blank=True)
    intro = models.TextField(
        "Hälsning",
        blank=True,
        help_text="Visas överst på kundens offertsida.",
    )
    status = models.CharField(
        max_length=10, choices=QuoteStatus.choices, default=QuoteStatus.DRAFT
    )
    valid_until = models.DateField("Giltig till", null=True, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    # Vem som tryckte Acceptera, för kvittots skull. Inga personnummer,
    # bara vad webbservern ändå ser.
    accepted_ip = models.GenericIPAddressField(null=True, blank=True)
    accepted_user_agent = models.CharField(max_length=300, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "Offert"
        verbose_name_plural = "Offerter"

    def __str__(self):
        return f"{self.customer_name} - {self.project_title or 'offert'}"

    def get_public_url(self):
        return f"/offert/{self.token}/"

    def totals(self):
        """Summor per pristyp, hela kronor exkl. moms."""
        sums = {p.value: 0 for p in PricePeriod}
        for line in self.lines.all():
            sums[line.period] += line.price
        return sums

    def totals_display(self):
        return {key: format_kr(value) for key, value in self.totals().items()}

    def is_answerable(self):
        """Kan kunden fortfarande agera på offerten?"""
        return self.status in (QuoteStatus.SENT, QuoteStatus.OPENED)

    def mark_opened(self):
        """
        Första gången kunden öppnar länken.

        Villkorad UPDATE i stället för läs-och-spara: två samtidiga GET
        (eller en GET mitt i ett Acceptera) får aldrig skriva över en
        senare status med en tidigare.
        """
        Quote.objects.filter(pk=self.pk, status=QuoteStatus.SENT).update(
            status=QuoteStatus.OPENED,
            opened_at=timezone.now(),
            updated_at=timezone.now(),
        )


class QuoteLine(models.Model):
    quote = models.ForeignKey(Quote, related_name="lines", on_delete=models.CASCADE)
    # SET_NULL: raden överlever att produkten tas bort ur katalogen -
    # en skickad offert får aldrig tappa rader.
    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="lines"
    )
    label = models.CharField("Rad", max_length=200)
    description = models.TextField("Beskrivning", blank=True)
    price = models.PositiveIntegerField("Pris (kr exkl. moms)", default=0)
    period = models.CharField(
        max_length=10, choices=PricePeriod.choices, default=PricePeriod.ONE_TIME
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Offertrad"
        verbose_name_plural = "Offertrader"

    def __str__(self):
        return self.label

    def price_display(self):
        return format_kr(self.price) + " " + PERIOD_SUFFIX[PricePeriod(self.period)]
