"""
ADX Flamingo: annonser, sidor och förfrågningar för kunder, mätt till affär.

Tjänsten är stängd: bara kunder som byrån aktiverat ser den (och byrån).
Aktiveringen är en egen rad per kund, inte ett fält på Customer: kundkortets
formulär sparar varje kryssruta det inte ritar som avbockad, och det har
redan stängt av saker en gång (apps/projects/forms.py, 2026-09-20).

Datamodellen följer flödet i README.md (kundresan, steg 2-12):

    FlamingoAccount   kundens Flamingo: hemsida, Google-kopplingen, sms-val
    Fact              det vi får säga om företaget, med källa och bekräftelse
    Service           tjänsterna och hur de säljs (ringer / offert / boka tid)
    Campaign          en kampanj per tjänst: annonser, sökord, landningssidan
    Review            en granskningsrunda per inskick, med byråns ändringar
    Lead              en förfrågan från landningssidan, ett samtal eller manuellt
    ConversionUpload  en vunnen affär med belopp, på väg tillbaka till Google
    SmsLog            varje sms som skickades, eller varför det inte skickades

Pengar är alltid hela kronor (int). Tider sparas i UTC och visas i
Europe/Stockholm (Django gör det med USE_TZ och TIME_ZONE).
"""

import re

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.projects.models import Customer

#: Betyg och omdömen används bara när de kommer från Google (places.py) eller
#: ADX. En hemsida, eller text någon lagt in på den, eller kunden själv ska
#: inte kunna ge företaget ett betyg i annonserna eller på sidan, oavsett vad
#: uppgiften kallas ("Trustpilot: 4,9").
RATING_SOURCES = ("google", "adx")
#: Ord i nyckeln eller etiketten (slug utan å, ä, ö, eller etiketten), eller i
#: värdet ihop med en siffra.
RATING_WORDS = (
    "betyg",
    "omdöme",
    "omdome",
    "rating",
    "stjärn",
    "stjarn",
    "review",
    "recension",
    "score",
    "stars",
)
#: Betygssajter: namnet ihop med en siffra är ett betyg.
RATING_SITES = frozenset(
    (
        "trustpilot",
        "yelp",
        "tripadvisor",
        "bokadirekt",
        "google",
        "facebook",
        "servicefinder",
        "offerta",
        "mittanbud",
    )
)
#: "4,9 av 5", "4.8/5", "9 av 10", "5 stjärnor", eller bara "4,9".
RATING_VALUE = re.compile(
    r"\b\d(?:[.,]\d)?\s*(?:av|/|of)\s*(?:5|10)\b"
    r"|\b\d(?:[.,]\d)?\s*stj[äa]rn"
    r"|^\s*\d[.,]\d\s*(?:\(\s*\d+\s*\))?\s*$",
    re.I,
)


def is_rating_like(key, label, value=""):
    """Ser uppgiften ut som ett betyg eller omdöme (se RATING_WORDS)?"""
    name = f"{key} {label}".casefold()
    value = str(value or "").casefold()
    if any(word in name for word in RATING_WORDS) or RATING_VALUE.search(value):
        return True
    if not re.search(r"\d", value):
        return False
    if any(word in value for word in RATING_WORDS):
        return True
    return bool(RATING_SITES & set(re.findall(r"[^\W\d_]+", f"{name} {value}")))


# ---------------------------------------------------------------------------
# Gränser som generatorn, formulären och granskningen delar (Googles regler
# för responsiva sökannonser).
# ---------------------------------------------------------------------------

HEADLINE_MAX = 30
DESCRIPTION_MAX = 90
HEADLINE_COUNT = 15
DESCRIPTION_COUNT = 4

#: Budget per dag i hela kronor (checks.validate, formulären, admin).
BUDGET_MIN = 50
BUDGET_MAX = 5000

#: Matchningstyper för sökord: värdet sparas i Campaign.keywords[i]["match"].
MATCH_EXACT = "exact"
MATCH_PHRASE = "phrase"
MATCH_BROAD = "broad"
MATCH_CHOICES = [
    (MATCH_PHRASE, "Fras"),
    (MATCH_EXACT, "Exakt"),
    (MATCH_BROAD, "Bred"),
]

#: Frågetyper på landningssidans formulär (Campaign.page["questions"]).
PAGE_QUESTION_KINDS = ("text", "textarea", "date")

#: Standardtext för autosvaret. Inga tider och inga löften: kunden skriver
#: om den i sina inställningar om hen vill säga mer.
AUTOREPLY_DEFAULT = (
    "Hej! Tack för din förfrågan. Vi har tagit emot den och hör av oss. "
    "Brådskar det går det bra att ringa oss."
)

google_ads_id_validator = RegexValidator(
    r"^\d{3}-\d{3}-\d{4}$",
    "Skriv kontots id som tio siffror, till exempel 123-456-7890.",
)


def format_google_ads_id(value):
    """'1234567890', '123 456 7890' eller '123-456-7890' blir '123-456-7890'.

    Returnerar "" för ett tomt värde och None om det inte är tio siffror.
    """
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if len(digits) != 10:
        return None
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


# ---------------------------------------------------------------------------
# Kontot
# ---------------------------------------------------------------------------


class FlamingoAccount(models.Model):
    GOOGLE_NOT_STARTED = "not_started"
    GOOGLE_REQUESTED_NEW = "requested_new"
    GOOGLE_ID_GIVEN = "id_given"
    GOOGLE_LINKED = "linked"
    GOOGLE_BILLING_OK = "billing_ok"
    GOOGLE_CHOICES = [
        (GOOGLE_NOT_STARTED, "Inte påbörjat"),
        (GOOGLE_REQUESTED_NEW, "Vill ha ett nytt konto"),
        (GOOGLE_ID_GIVEN, "Konto-id angivet"),
        (GOOGLE_LINKED, "Kopplat under ADX"),
        (GOOGLE_BILLING_OK, "Kopplat och betalning klar"),
    ]

    SCAN_NONE = "none"
    SCAN_RUNNING = "running"
    SCAN_DONE = "done"
    SCAN_FAILED = "failed"
    SCAN_CHOICES = [
        (SCAN_NONE, "Inte hämtad"),
        (SCAN_RUNNING, "Hämtas"),
        (SCAN_DONE, "Hämtad"),
        (SCAN_FAILED, "Misslyckades"),
    ]

    customer = models.OneToOneField(
        Customer, on_delete=models.CASCADE, related_name="flamingo", verbose_name="Kund"
    )
    is_enabled = models.BooleanField("ADX Flamingo aktiverat", default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # Steg 1, förslaget: hemsidan som läses av.
    website_url = models.URLField("Hemsida", blank=True)
    scan_status = models.CharField(
        "Hemsidan", max_length=10, choices=SCAN_CHOICES, default=SCAN_NONE
    )
    scanned_at = models.DateTimeField("Hämtad", null=True, blank=True)
    scan_error = models.CharField("Fel vid hämtningen", max_length=300, blank=True)
    # Spärrarna (limits.py): läsningar och AI-förslag per svenskt dygn.
    scan_started_at = models.DateTimeField("Senaste läsningen startade", null=True, blank=True)
    scan_day = models.DateField("Dag för läsningarna", null=True, blank=True)
    scan_count = models.PositiveSmallIntegerField("Läsningar den dagen", default=0)
    ai_day = models.DateField("Dag för AI-förslagen", null=True, blank=True)
    ai_count = models.PositiveSmallIntegerField("AI-förslag den dagen", default=0)

    # Steg 3, Google: kunden äger kontot, ADX förvaltar det under sitt MCC.
    google_ads_customer_id = models.CharField(
        "Google Ads-kontots id",
        max_length=20,
        blank=True,
        validators=[google_ads_id_validator],
        help_text="Tio siffror, till exempel 123-456-7890.",
    )
    google_status = models.CharField(
        "Google Ads", max_length=20, choices=GOOGLE_CHOICES, default=GOOGLE_NOT_STARTED
    )
    google_note = models.CharField("Notering om Google", max_length=300, blank=True)

    # Inställningar kunden själv slår på (av från början). Sms skickas bara
    # när 46elks är konfigurerat; annars loggas det i SmsLog.
    notify_phone = models.CharField("Mobil för sms om nya förfrågningar", max_length=20, blank=True)
    notify_sms = models.BooleanField("Sms till mig om nya förfrågningar", default=False)
    autoreply_enabled = models.BooleanField("Autosvar till den som frågar", default=False)
    autoreply_text = models.TextField("Autosvarets text", default=AUTOREPLY_DEFAULT)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Flamingo-konto"
        verbose_name_plural = "Flamingo-konton"

    def __str__(self):
        state = "på" if self.is_enabled else "av"
        return f"{self.customer.name}: Flamingo {state}"

    @property
    def google_linked(self):
        """Kontot ligger under ADX:s förvaltarkonto (betalningen kan saknas)."""
        return self.google_status in (self.GOOGLE_LINKED, self.GOOGLE_BILLING_OK)

    @property
    def google_ready(self):
        """Kopplat och betalningen klar: en kampanj kan gå live."""
        return self.google_status == self.GOOGLE_BILLING_OK

    @property
    def google_waiting_on_adx(self):
        """Kunden har gjort sin del; byrån kopplar."""
        return self.google_status in (self.GOOGLE_REQUESTED_NEW, self.GOOGLE_ID_GIVEN)

    def usable_fact_rows(self):
        """Bekräftade uppgifter med ett värde, som Fact-rader, utan betyg som
        inte kommer från Google eller ADX (is_rating_like)."""
        rows = self.facts.filter(confirmed=True).exclude(value="").order_by("order", "id")
        return [f for f in rows if f.is_usable]

    def confirmed_facts(self):
        """Bekräftade uppgifter med ett värde, som {key: value}. Det enda
        AI och mallarna får använda (README: AI får bara använda bekräftade
        fakta). Ett betyg från hemsidan eller kunden är aldrig med."""
        return {f.key: f.value for f in self.usable_fact_rows()}


def account_for(customer):
    """Kundens Flamingo-rad, skapad vid första behovet."""
    account, _ = FlamingoAccount.objects.get_or_create(customer=customer)
    return account


def has_flamingo(customer):
    """Har kunden ADX Flamingo aktiverat? En aktiv kund krävs också."""
    if customer is None or not customer.is_active:
        return False
    return FlamingoAccount.objects.filter(customer=customer, is_enabled=True).exists()


# ---------------------------------------------------------------------------
# Företaget och tjänsterna
# ---------------------------------------------------------------------------


class Fact(models.Model):
    """En uppgift om företaget med sin källa. Tomt värde betyder "vet inte"
    och skrivs aldrig som en gissning."""

    SOURCE_SITE = "site"
    SOURCE_GOOGLE = "google"
    SOURCE_CUSTOMER = "customer"
    SOURCE_ADX = "adx"
    SOURCE_CHOICES = [
        (SOURCE_SITE, "Hemsidan"),
        (SOURCE_GOOGLE, "Google"),
        (SOURCE_CUSTOMER, "Du"),
        (SOURCE_ADX, "ADX"),
    ]

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="facts")
    key = models.SlugField("Nyckel", max_length=64)
    label = models.CharField("Uppgift", max_length=120)
    value = models.TextField("Värde", blank=True)
    source = models.CharField(
        "Källa", max_length=10, choices=SOURCE_CHOICES, default=SOURCE_CUSTOMER
    )
    confirmed = models.BooleanField("Bekräftad", default=False)
    order = models.PositiveIntegerField("Ordning", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Uppgift"
        verbose_name_plural = "Uppgifter"
        constraints = [
            models.UniqueConstraint(fields=["account", "key"], name="flamingo_fact_unique_key")
        ]

    def __str__(self):
        return f"{self.label}: {self.value or '(tomt)'}"

    @property
    def is_rating(self):
        return is_rating_like(self.key, self.label, self.value)

    @property
    def is_usable(self):
        """Får annonserna, sidan och kontrollerna använda uppgiften? Betyg
        bara från Google eller ADX."""
        return self.source in RATING_SOURCES or not self.is_rating


class Service(models.Model):
    SALES_CALL = "call"
    SALES_QUOTE = "quote"
    SALES_BOOK = "book"
    SALES_CHOICES = [
        (SALES_CALL, "Ringer direkt"),
        (SALES_QUOTE, "Vill ha offert"),
        (SALES_BOOK, "Vill boka tid"),
    ]

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="services")
    name = models.CharField("Tjänst", max_length=120)
    sales_mode = models.CharField(
        "Hur köper kunderna den?", max_length=10, choices=SALES_CHOICES, default=SALES_QUOTE
    )
    is_active = models.BooleanField("Aktiv", default=True)
    order = models.PositiveIntegerField("Ordning", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Tjänst"
        verbose_name_plural = "Tjänster"

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Kampanjen och granskningen
# ---------------------------------------------------------------------------

#: Bolagsformer som inte hör hemma i en adress ("Lindqvist Rör AB" blir
#: "lindqvist-ror").
_LEGAL_FORMS = {"ab", "hb", "kb", "aktiebolag", "handelsbolag", "kommanditbolag"}


def company_slug(name):
    parts = [p for p in slugify(name or "").split("-") if p and p not in _LEGAL_FORMS]
    return "-".join(parts)


def make_page_slug(customer, service_name, exclude_pk=None):
    """Landningssidans adress: "<kund>-<tjänst>", unik bland alla kampanjer.

    Krockar får -2, -3 ... Längden hålls inom fältets 80 tecken, även med
    suffixet."""
    base = slugify(f"{company_slug(customer.name)} {service_name}")[:80].strip("-") or "sida"
    slug, n = base, 2
    taken = Campaign.objects.exclude(pk=exclude_pk) if exclude_pk else Campaign.objects.all()
    while taken.filter(page_slug=slug).exists():
        suffix = f"-{n}"
        slug = base[: 80 - len(suffix)].rstrip("-") + suffix
        n += 1
    return slug


class Campaign(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_IN_REVIEW = "in_review"
    STATUS_NEEDS_CUSTOMER = "needs_customer"
    STATUS_LIVE = "live"
    STATUS_PAUSED = "paused"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Utkast"),
        (STATUS_IN_REVIEW, "Hos ADX"),
        (STATUS_NEEDS_CUSTOMER, "Väntar på dig"),
        (STATUS_LIVE, "Live"),
        (STATUS_PAUSED, "Pausad"),
    ]
    #: Fälten som utgör kampanjens innehåll: det kunden skickar in, det
    #: byrån granskar och det som jämförs mellan rundorna.
    CONTENT_FIELDS = (
        "name",
        "area",
        "radius_km",
        "daily_budget_kr",
        "headlines",
        "descriptions",
        "keywords",
        "negatives",
        "page",
    )

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="campaigns")
    service = models.ForeignKey(
        Service, on_delete=models.PROTECT, related_name="campaigns", verbose_name="Tjänst"
    )
    name = models.CharField("Namn", max_length=120)
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    area = models.CharField(
        "Område", max_length=200, blank=True, help_text="Till exempel Nacka + 15 km."
    )
    radius_km = models.PositiveSmallIntegerField("Radie (km)", default=15)
    daily_budget_kr = models.PositiveIntegerField(
        "Budget per dag (kr)",
        default=150,
        validators=[MinValueValidator(BUDGET_MIN), MaxValueValidator(BUDGET_MAX)],
    )
    #: Rubriker (högst HEADLINE_COUNT, var och en högst HEADLINE_MAX tecken).
    headlines = models.JSONField("Rubriker", default=list, blank=True)
    #: Beskrivningar (högst DESCRIPTION_COUNT, högst DESCRIPTION_MAX tecken).
    descriptions = models.JSONField("Beskrivningar", default=list, blank=True)
    #: [{"text": "badrumsrenovering nacka", "match": "phrase"}, ...]
    keywords = models.JSONField("Sökord", default=list, blank=True)
    negatives = models.JSONField("Negativa sökord", default=list, blank=True)
    #: Landningssidans innehåll. Generatorn skriver det, byrån granskar det
    #: och /lp/<page_slug>/ ritar det. Bara bekräftade fakta får stå här.
    #:   {"title": "Rörjour i Nacka",                 rubriken
    #:    "lead": "Vattenläcka eller stopp? ...",      ingressen
    #:    "points": ["Säker Vatten-auktoriserade"],    punkter under ingressen
    #:    "phone": "08-000 00 00",                     ringknappen (tom = ingen)
    #:    "form_title": "Berätta om jobbet",           formulärets rubrik
    #:    "questions": [{"key": "storlek",             extra frågor i formuläret;
    #:                   "label": "Ungefär hur stort?", svaren hamnar i
    #:                   "kind": "text"}],              Lead.answers[label]
    #:    "note": "..."}                               text under formuläret
    #: kind är "text", "textarea" eller "date" (boka tid).
    page = models.JSONField("Landningssidan", default=dict, blank=True)
    page_slug = models.SlugField("Sidans adress", max_length=80, unique=True)
    google_campaign_id = models.CharField("Kampanjens id hos Google", max_length=40, blank=True)
    approved_at = models.DateTimeField("Godkänd av kunden", null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    published_at = models.DateTimeField("Publicerad", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Kampanj"
        verbose_name_plural = "Kampanjer"
        indexes = [models.Index(fields=["account", "status"], name="flamingo_campaign_status")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.page_slug:
            self.page_slug = make_page_slug(
                self.account.customer, self.service.name if self.service_id else self.name
            )
        super().save(*args, **kwargs)

    @property
    def sales_mode(self):
        return self.service.sales_mode

    def get_sales_mode_display(self):
        return self.service.get_sales_mode_display()

    @property
    def is_public(self):
        """Landningssidan syns för alla bara när kampanjen är live."""
        return self.status == self.STATUS_LIVE

    @property
    def landing_url(self):
        return reverse("flamingo_public:landing", args=[self.page_slug])

    @property
    def customer_can_edit(self):
        """Kunden ändrar i utkastet och efter granskningen, aldrig medan ADX
        granskar eller i en publicerad kampanj (ändringar går samma väg som
        förslaget: ny runda)."""
        return self.status in (self.STATUS_DRAFT, self.STATUS_NEEDS_CUSTOMER)

    @property
    def monthly_budget_kr(self):
        """Ungefär en månad: 30,4 dagar, avrundat till hela kronor."""
        return round(self.daily_budget_kr * 30.4)

    def content_snapshot(self):
        """Kampanjens innehåll som ett JSON-bart dict (Review.snapshot)."""
        return {field: getattr(self, field) for field in self.CONTENT_FIELDS}

    def latest_review(self):
        return self.reviews.order_by("-round").first()

    def pending_review(self):
        return self.reviews.filter(state=Review.STATE_PENDING).order_by("-round").first()

    def next_round(self):
        last = self.reviews.aggregate(m=models.Max("round"))["m"]
        return (last or 0) + 1


class Review(models.Model):
    """En granskningsrunda: kunden skickar in, byrån rättar och skriver varför.

    changes är listan kunden ser före godkännandet:
        [{"field": "headlines", "label": "Rubrik", "before": "...",
          "after": "...", "reason": "..."}]
    """

    STATE_PENDING = "pending"
    STATE_DONE = "done"
    STATE_CHOICES = [
        (STATE_PENDING, "Väntar på ADX"),
        (STATE_DONE, "Granskad"),
    ]

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="reviews")
    round = models.PositiveSmallIntegerField("Runda")
    submitted_at = models.DateTimeField("Inskickad", default=timezone.now)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    state = models.CharField("Läge", max_length=10, choices=STATE_CHOICES, default=STATE_PENDING)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="Granskare",
    )
    reviewed_at = models.DateTimeField("Granskad", null=True, blank=True)
    #: Kampanjens innehåll som det såg ut när kunden skickade in
    #: (Campaign.content_snapshot()). Grunden för diffen mot granskningen.
    snapshot = models.JSONField("Inskickat innehåll", default=dict, blank=True)
    changes = models.JSONField("Ändringar", default=list, blank=True)
    note = models.TextField("Meddelande till kunden", blank=True)

    class Meta:
        ordering = ["-round"]
        verbose_name = "Granskning"
        verbose_name_plural = "Granskningar"
        constraints = [
            models.UniqueConstraint(fields=["campaign", "round"], name="flamingo_review_round")
        ]

    def __str__(self):
        return f"{self.campaign}: runda {self.round}"


# ---------------------------------------------------------------------------
# Förfrågningar, affärer och sms
# ---------------------------------------------------------------------------


class Lead(models.Model):
    SOURCE_FORM = "form"
    SOURCE_CALL = "call"
    SOURCE_MANUAL = "manual"
    SOURCE_CHOICES = [
        (SOURCE_FORM, "Formulär"),
        (SOURCE_CALL, "Samtal"),
        (SOURCE_MANUAL, "Manuell"),
    ]

    STATUS_NEW = "new"
    STATUS_CONTACTED = "contacted"
    STATUS_QUOTE = "quote"
    STATUS_WON = "won"
    STATUS_LOST = "lost"
    STATUS_JUNK = "junk"
    STATUS_CHOICES = [
        (STATUS_NEW, "Ny"),
        (STATUS_CONTACTED, "Kontaktad"),
        (STATUS_QUOTE, "Offert skickad"),
        (STATUS_WON, "Vunnen"),
        (STATUS_LOST, "Förlorad"),
        (STATUS_JUNK, "Skräp"),
    ]
    #: Förfrågningar som räknas i översikten (skräp räknas inte).
    COUNTED_STATUSES = (
        STATUS_NEW,
        STATUS_CONTACTED,
        STATUS_QUOTE,
        STATUS_WON,
        STATUS_LOST,
    )

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="leads")
    campaign = models.ForeignKey(
        Campaign, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    service = models.ForeignKey(
        Service, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads"
    )
    source = models.CharField("Källa", max_length=10, choices=SOURCE_CHOICES, default=SOURCE_FORM)
    name = models.CharField("Namn", max_length=120, blank=True)
    phone = models.CharField("Telefon", max_length=40, blank=True)
    email = models.EmailField("E-post", blank=True)
    message = models.TextField("Meddelande", blank=True)
    #: Svaren på formulärets frågor: {"Ungefär hur stort?": "6 m2", ...}.
    answers = models.JSONField("Svar", default=dict, blank=True)
    gclid = models.CharField("Googles klick-id", max_length=200, blank=True)
    #: {"utm_source": "google", "utm_campaign": "...", ...}
    utm = models.JSONField("UTM", default=dict, blank=True)
    keyword = models.CharField("Sökord", max_length=200, blank=True)
    #: HMAC av besökarens IP (limits.ip_hash), för spärren på /lp/. Aldrig
    #: själva adressen.
    ip_hash = models.CharField("IP (hash)", max_length=64, blank=True, db_index=True)
    status = models.CharField("Status", max_length=12, choices=STATUS_CHOICES, default=STATUS_NEW)
    value_kr = models.PositiveIntegerField("Affärens värde (kr)", null=True, blank=True)
    won_at = models.DateTimeField("Vunnen", null=True, blank=True)
    created_at = models.DateTimeField("Kom in", default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Förfrågan"
        verbose_name_plural = "Förfrågningar"
        indexes = [
            models.Index(fields=["account", "status", "-created_at"], name="flamingo_lead_status"),
        ]

    def __str__(self):
        return f"{self.display_name} ({self.get_status_display()})"

    @property
    def display_name(self):
        return self.name or self.phone or self.email or "Okänd"

    @property
    def service_name(self):
        if self.service_id:
            return self.service.name
        if self.campaign_id:
            return self.campaign.service.name
        return ""

    @transaction.atomic
    def set_status(self, status, value_kr=None, now=None):
        """Byt status (inkorgen, och senare sms-svaret "VANN 186000").

        Vunnen med ett belopp köar en ConversionUpload när förfrågan har ett
        klick-id från Google; utan klick-id kan Google inte koppla affären
        till annonsen. Ändras beloppet medan uppladdningen står i kö följer
        den med, och lämnar förfrågan "vunnen" tas en uppladdning som ännu
        inte gått iväg bort. Det som redan skickats eller exporterats rörs
        inte."""
        if status not in dict(self.STATUS_CHOICES):
            raise ValueError(f"Okänd status: {status}")
        now = now or timezone.now()
        self.status = status
        if status == self.STATUS_WON:
            if value_kr is not None:
                self.value_kr = int(value_kr)
            if self.won_at is None:
                self.won_at = now
        else:
            self.won_at = None
        self.save()

        upload = ConversionUpload.objects.filter(lead=self).first()
        if status == self.STATUS_WON and self.value_kr is not None and self.gclid:
            if upload is None:
                ConversionUpload.objects.create(lead=self, value_kr=self.value_kr)
            elif (
                upload.status == ConversionUpload.STATUS_QUEUED and upload.value_kr != self.value_kr
            ):
                upload.value_kr = self.value_kr
                upload.save(update_fields=["value_kr"])
        elif upload is not None and upload.status == ConversionUpload.STATUS_QUEUED:
            upload.delete()
        return self


class ConversionUpload(models.Model):
    """En vunnen affär på väg till Google som offline-konvertering. Med API
    laddas den upp; utan exporterar byrån en CSV (/manage/flamingo/)."""

    STATUS_QUEUED = "queued"
    STATUS_EXPORTED = "exported"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "I kö"),
        (STATUS_EXPORTED, "Exporterad"),
        (STATUS_SENT, "Skickad"),
        (STATUS_FAILED, "Misslyckades"),
    ]

    lead = models.OneToOneField(Lead, on_delete=models.CASCADE, related_name="conversion")
    value_kr = models.PositiveIntegerField("Värde (kr)")
    status = models.CharField(
        "Status", max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED
    )
    exported_at = models.DateTimeField("Exporterad", null=True, blank=True)
    #: Svaret från Google (eller exportens filnamn) för felsökning.
    response = models.JSONField("Svar", default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Konvertering till Google"
        verbose_name_plural = "Konverteringar till Google"

    def __str__(self):
        return f"{self.lead.display_name}: {self.value_kr} kr ({self.get_status_display()})"


class SmsLog(models.Model):
    """Varje sms Flamingo skickade eller lät bli att skicka, och varför."""

    KIND_OWNER = "owner_notice"
    KIND_AUTOREPLY = "autoreply"
    KIND_CHOICES = [
        (KIND_OWNER, "Till dig om en ny förfrågan"),
        (KIND_AUTOREPLY, "Autosvar"),
    ]

    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_NOT_CONFIGURED = "not_configured"
    STATUS_DISABLED = "disabled"
    STATUS_CHOICES = [
        (STATUS_SENDING, "Skickas"),
        (STATUS_SENT, "Skickat"),
        (STATUS_FAILED, "Misslyckades"),
        (STATUS_NOT_CONFIGURED, "Inte skickat: sms är inte inkopplat"),
        (STATUS_DISABLED, "Inte skickat: avstängt i inställningarna"),
    ]

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="sms_log")
    lead = models.ForeignKey(
        Lead, null=True, blank=True, on_delete=models.SET_NULL, related_name="sms_log"
    )
    kind = models.CharField("Sort", max_length=20, choices=KIND_CHOICES)
    to = models.CharField("Till", max_length=20, blank=True)
    body = models.TextField("Text")
    status = models.CharField("Status", max_length=20, choices=STATUS_CHOICES)
    provider_id = models.CharField("Id hos 46elks", max_length=64, blank=True)
    error = models.CharField("Fel", max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Sms"
        verbose_name_plural = "Sms"

    def __str__(self):
        return f"{self.get_kind_display()} till {self.to or '-'}: {self.get_status_display()}"
