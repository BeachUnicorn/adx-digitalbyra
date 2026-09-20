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

Kopplingen till ärendesystemet (apps/projects) är frivillig och manuell:
Quote.project pekar på ett projekt, och knappen "Skapa ärenden av raderna"
gör varje rad till ett Issue (QuoteLine.issue minns vilket). Ingenting
skapas automatiskt när kunden accepterar - det är byråns beslut när
offerten blir arbete.

Alla belopp är hela kronor exklusive moms. Momsen är en visningsfråga
(25 procent på allt ADX säljer), inte en datafråga.
"""

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.projects.models import private_storage


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
    # Kopplingen till ärendesystemet. Kunden lagras INTE separat på offerten:
    # har den ett projekt så är kunden projektets (samma regel som Issue),
    # annars finns bara fritexten customer_name. SET_NULL: offerten är en
    # affärshandling och ska överleva att projektet tas bort.
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quotes",
        verbose_name="Projekt",
    )
    intro = models.TextField(
        "Hälsning",
        blank=True,
        help_text="Visas överst på kundens offertsida.",
    )
    status = models.CharField(max_length=10, choices=QuoteStatus.choices, default=QuoteStatus.DRAFT)
    valid_until = models.DateField("Giltig till", null=True, blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    # Vem som tryckte Acceptera, för kvittots skull. Inga personnummer,
    # bara vad webbservern ändå ser.
    accepted_ip = models.GenericIPAddressField(null=True, blank=True)
    accepted_user_agent = models.CharField(max_length=300, blank=True)
    # Beställarens uppgifter, ifyllda på acceptsidan. Skrivs i samma
    # villkorade UPDATE som statusövergången, så de hör ihop med accepten
    # och kan aldrig komma från en förlorad dubbelrequest.
    accept_first_name = models.CharField("Förnamn", max_length=80, blank=True)
    accept_last_name = models.CharField("Efternamn", max_length=80, blank=True)
    accept_email = models.EmailField("E-post", blank=True)
    accept_phone = models.CharField("Telefon", max_length=40, blank=True)
    accept_company = models.CharField("Företag", max_length=200, blank=True)
    accept_org_number = models.CharField("Organisationsnummer", max_length=20, blank=True)
    accept_billing_address = models.TextField("Fakturaadress", blank=True)
    accept_billing_email = models.EmailField("Faktura-e-post", blank=True)
    accept_reference = models.CharField("Er referens", max_length=100, blank=True)
    accept_message = models.TextField("Meddelande", blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    # Spårbarhet när samma offert går till flera företag: varje företag får
    # en egen offert (eget avtal, egen accept), kopian minns originalet.
    copied_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="copies"
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

    @property
    def customer(self):
        """Kunden i ärendesystemet - alltid härledd ur projektet, aldrig lagrad här."""
        return self.project.customer if self.project_id else None

    def totals(self):
        """
        Summor per pristyp, hela kronor exkl. moms.

        Ett tillval räknas bara när det är valt - före accept är det
        förvalet, efter accept är det kundens faktiska val.
        """
        sums = {p.value: 0 for p in PricePeriod}
        for line in self.lines.all():
            if line.is_optional and not line.is_selected:
                continue
            sums[line.period] += line.price
        return sums

    def totals_display(self):
        return {key: format_kr(value) for key, value in self.totals().items()}

    def is_answerable(self):
        """Kan kunden fortfarande agera på offerten?"""
        return self.status in (QuoteStatus.SENT, QuoteStatus.OPENED)

    def duplicate(self, *, customer_name, customer_email="", project_title=None, user=None):
        """
        En exakt kopia som utkast, med ny länk, till ett annat företag.

        Rader (med tillval och förval), hälsning och bilagor följer med.
        Status, accept, beställare, projektkoppling och radernas ärenden
        gör det inte - det hör till originalets affär, inte kopians.
        """
        from django.core.files.base import ContentFile

        copy = Quote.objects.create(
            customer_name=customer_name.strip()[:200],
            customer_email=customer_email.strip()[:254],
            project_title=(self.project_title if project_title is None else project_title)[:200],
            intro=self.intro,
            valid_until=timezone.localdate() + timezone.timedelta(days=30),
            created_by=user,
            copied_from=self,
        )
        for line in self.lines.all():
            QuoteLine.objects.create(
                quote=copy,
                product=line.product,
                label=line.label,
                description=line.description,
                price=line.price,
                period=line.period,
                is_optional=line.is_optional,
                is_selected=line.is_selected,
                order=line.order,
            )
        for attachment in self.attachments.all():
            with attachment.file.open("rb") as source:
                content = ContentFile(source.read(), name=attachment.original_name)
            QuoteAttachment.objects.create(
                quote=copy,
                file=content,
                original_name=attachment.original_name,
                content_type=attachment.content_type,
                size=attachment.size,
            )
        return copy

    @property
    def accepted_by(self):
        """'Nina Nordan, Nordan Bygg AB' - tomt om accepten gjordes manuellt i panelen."""
        name = f"{self.accept_first_name} {self.accept_last_name}".strip()
        if not name:
            return ""
        return f"{name}, {self.accept_company}" if self.accept_company else name

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


def quote_attachment_path(instance, filename):
    # Slumpad katalog per fil, som ärendenas bilagor: ingen kan räkna upp
    # andra offerters filer även med tillgång till lagringen.
    safe = slugify(filename.rsplit(".", 1)[0])[:60] or "bilaga"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"offerter/{secrets.token_urlsafe(12)}/{safe}.{ext}"


class QuoteAttachment(models.Model):
    """
    Bilaga på offerten - typiskt en PDF med mer information. Privat
    lagring (utanför /media/); kundens offerttoken är behörigheten att
    hämta den, precis som för offerten själv.
    """

    quote = models.ForeignKey(Quote, related_name="attachments", on_delete=models.CASCADE)
    file = models.FileField(upload_to=quote_attachment_path, storage=private_storage)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Offertbilaga"
        verbose_name_plural = "Offertbilagor"

    def __str__(self):
        return self.original_name

    @property
    def size_display(self):
        if self.size >= 1024 * 1024:
            return f"{self.size / (1024 * 1024):.1f} MB"
        return f"{max(1, self.size // 1024)} kB"


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
    # Tillval: raden visas som en toggle på kundsidan och kunden väljer
    # själv. is_selected är förvalet innan accept - och KUNDENS val efter.
    is_optional = models.BooleanField("Tillval", default=False)
    is_selected = models.BooleanField("Vald", default=True)
    order = models.PositiveIntegerField(default=0)
    # Ärendet raden blev när offerten omsattes i arbete. Minnet gör att
    # "Skapa ärenden" kan köras om utan dubbletter: rader som redan har ett
    # ärende hoppas över. SET_NULL så att ett raderat ärende kan skapas om.
    issue = models.ForeignKey(
        "projects.Issue",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="quote_lines",
        verbose_name="Ärende",
    )

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Offertrad"
        verbose_name_plural = "Offertrader"

    def __str__(self):
        return self.label

    def price_display(self):
        return format_kr(self.price) + " " + PERIOD_SUFFIX[PricePeriod(self.period)]
