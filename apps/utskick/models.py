"""
Kontakter och utskick (README.md, avsnitt B). Alla modeller bor i den här
filen, som i apps/flamingo.

Steg S1:

    Switchboard       byråns nödstopp och klarmarkeringar (en rad, pk=1)
    UtskickSettings   kundens utskick: på/av (byrån), namn, samtyckestexter
    DpaVersion        biträdesavtalets text som den publicerades
    DpaAcceptance     vem som godkände vilken version (bara nya rader)
    Contact           en kontakt i kundens register (i mallar: "kontakt")
    Consent           samtycket just nu per kanal, bundet till adressen
    ConsentLog        varje ändring av samtycket, beviset (bara nya rader)
    Suppression       spärrlistan per kund och kanal, nycklad på adressens hash
    FieldDef          kundens extrafält
    Tag, ContactList, ListMembership   taggar och listor
    ImportJob         en import från fil eller inklistrad text
    SignupForm        den publika anmälningssidan (en per kund)
    Event             händelser som inte har en egen rad någon annanstans
    ExportLog         vem som exporterade vad
    Counter           exakta gränser i databasen (LocMem är per arbetare)

Varje rad hör till ett flamingo.FlamingoAccount, direkt eller via sin
förälder, och varje fråga filtrerar på kontot (H.1). Inga personuppgifter i
__str__ eller i loggar: bara pk (H.3). Telefonnummer och e-post skrivs efter
att kontakten skapats bara av contacts.change_address, och samtycket bara av
consent.set_status.

Regeln för migreringar (B.0): ett nytt fält på en tabell som en tidigare
version skriver till ska vara null=True eller ha db_default.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount
from apps.projects.models import private_storage

CHANNEL_SMS = "sms"
CHANNEL_EMAIL = "email"
CHANNEL_CHOICES = [(CHANNEL_SMS, "Sms"), (CHANNEL_EMAIL, "E-post")]
CHANNELS = (CHANNEL_SMS, CHANNEL_EMAIL)

#: Reserverade adresser för anmälningssidan (/utskick/<public_slug>/) och
#: avsändaren på ADX-domänen (<public_slug>@utskick.adx.se). Utöver dem
#: gäller allt i apps.manage.forms.BlockPageForm.RESERVED_SLUGS.
RESERVED_PUBLIC_SLUGS = frozenset(
    {
        "adx",
        "admin",
        "abuse",
        "postmaster",
        "bounce",
        "bekrafta",
        "noreply",
        "info",
        "support",
        "security",
        "svar",
        "utskick",
        "k",
        "klick",
        "www",
        "mail",
        "integritet",
        "val",
        "tack",
    }
)

#: Tidsfönstret för sms per kund i hela timmar (S2), med gränserna 8 till 21.
SMS_WINDOW_DEFAULT = {"weekday": [9, 20], "weekend": [10, 18]}


def default_sms_window():
    return {key: list(hours) for key, hours in SMS_WINDOW_DEFAULT.items()}


def default_signup_channels():
    # Sms erbjuds på anmälningssidan först när länkvärdarna finns (S2).
    return [CHANNEL_EMAIL]


def default_consent_text(channel, display_name):
    """Samtyckestexten som kryssrutan visar, med företagets namn ifyllt."""
    name = display_name or "företaget"
    via = "sms" if channel == CHANNEL_SMS else "e-post"
    return f"Ja, jag vill få erbjudanden från {name} via {via}."


# ---------------------------------------------------------------------------
# Byråns brytare och kundens inställningar
# ---------------------------------------------------------------------------


class Switchboard(models.Model):
    """Nödstoppet och klarmarkeringarna (en rad, pk=1). Klarmarkeringarna
    sätts bara av byrån på /manage/utskick/nodstopp/, var och en med en
    anteckning. Inget skickas förrän byrån slagit på brytarna (D.8)."""

    SOLO_PK = 1

    sms_enabled = models.BooleanField("Sms-utskick på", default=False)
    email_enabled = models.BooleanField("E-postutskick på", default=False)
    sms_paused_until = models.DateTimeField("Sms pausade till", null=True, blank=True)
    doi_ready_at = models.DateTimeField("Bekräftelsemejl klara", null=True, blank=True)
    links_ready_at = models.DateTimeField("Länkvärdarna klara", null=True, blank=True)
    sms_inbound_ready_at = models.DateTimeField("Inkommande sms klara", null=True, blank=True)
    email_ready_at = models.DateTimeField("E-post klar", null=True, blank=True)
    ses_max_rate = models.PositiveSmallIntegerField("SES högsta takt", default=0)
    ses_daily_quota = models.PositiveIntegerField("SES dygnskvot", default=0)
    #: HMAC(nyckel, "utskick-fingerprint") för UTSKICK_HASH_KEY respektive
    #: UTSKICK_LINK_KEY (keys.py, H.7). Fångar en process med en annan nyckel.
    hash_fingerprint = models.CharField(max_length=64, blank=True)
    link_fingerprint = models.CharField(max_length=64, blank=True)
    last_tick_at = models.DateTimeField(null=True, blank=True)
    last_tick_summary = models.JSONField(default=dict, blank=True)
    last_queue_poll_at = models.DateTimeField(null=True, blank=True)
    last_elks_reconcile_at = models.DateTimeField(null=True, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    changed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField("Anteckning", max_length=200, blank=True)

    class Meta:
        verbose_name = "Nödstopp och klarmarkeringar"
        verbose_name_plural = "Nödstopp och klarmarkeringar"

    def __str__(self):
        return "Utskickens brytare"

    @classmethod
    def get_solo(cls):
        """Den enda raden, skapad vid första behovet."""
        row, _ = cls.objects.get_or_create(pk=cls.SOLO_PK)
        return row


class UtskickSettings(models.Model):
    """Kundens utskick. En egen rad av samma skäl som FlamingoAccount:
    kundkortets formulär sparar kryssrutor det inte ritar som avbockade.
    access.settings_for(account) ger raden eller en osparad standard."""

    REPLY_INBOX = "inbox"
    REPLY_OWN = "own"
    REPLY_CHOICES = [(REPLY_INBOX, "Inkorgen"), (REPLY_OWN, "Egen adress")]

    account = models.OneToOneField(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick", verbose_name="Konto"
    )
    is_enabled = models.BooleanField("Utskick aktiverat", default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    disabled_at = models.DateTimeField(null=True, blank=True)
    public_slug = models.SlugField("Adress för anmälan", max_length=40, unique=True)
    display_name = models.CharField("Företagsnamn i utskick", max_length=80)
    consent_text_sms = models.CharField("Samtyckestext för sms", max_length=200)
    consent_text_email = models.CharField("Samtyckestext för e-post", max_length=200)
    lp_consent = models.BooleanField("Kryssrutor på landningssidor", default=True)
    privacy_url = models.URLField("Integritetspolicy", max_length=500, blank=True)
    pref_email_note = models.CharField(
        "Text under E-post med erbjudanden", max_length=120, blank=True
    )
    sms_window = models.JSONField("Tidsfönster för sms", default=default_sms_window)
    weekly_cap_sms = models.PositiveSmallIntegerField("Sms per kontakt och vecka", default=2)
    weekly_cap_email = models.PositiveSmallIntegerField("Mejl per kontakt och vecka", default=4)
    open_tracking = models.BooleanField("Spåra öppningar", default=False)
    email_reply_mode = models.CharField(
        "Svar på mejl", max_length=8, choices=REPLY_CHOICES, default=REPLY_INBOX
    )
    own_reply_to = models.EmailField("Egen svarsadress", blank=True)
    own_reply_to_confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribe_text = models.CharField(
        "Extra text på avregistreringssidan", max_length=300, blank=True
    )
    contact_limit = models.PositiveIntegerField("Högsta antal kontakter", default=25000)
    notify_on_reply = models.BooleanField("Meddela mig om svar", default=True)
    reply_notice_at = models.DateTimeField(null=True, blank=True)
    sending_blocked = models.BooleanField("All sändning stoppad av ADX", default=False)
    blocked_reason = models.CharField("Varför sändningen är stoppad", max_length=200, blank=True)
    email_daily_cap = models.PositiveIntegerField("Mejl per dag (0 = upptrappning)", default=0)
    email_first_sent_at = models.DateTimeField(null=True, blank=True)
    email_probe_passed_at = models.DateTimeField(null=True, blank=True)
    first_utskick_alerted = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Utskick för kund"
        verbose_name_plural = "Utskick för kunder"

    def __str__(self):
        return f"Utskick för konto {self.account_id}"

    def consent_text(self, channel):
        text = self.consent_text_sms if channel == CHANNEL_SMS else self.consent_text_email
        return text or default_consent_text(channel, self.display_name)


class DpaVersion(models.Model):
    """Biträdesavtalet som det publicerades från sidan /bitradesavtal/.
    Exakt en version är aktuell (partiellt unikt villkor)."""

    version = models.CharField("Version", max_length=20, unique=True)
    text = models.TextField("Text")
    sha256 = models.CharField(max_length=64)
    published_at = models.DateTimeField(default=timezone.now)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    is_current = models.BooleanField("Aktuell", default=False)

    class Meta:
        verbose_name = "Biträdesavtal"
        verbose_name_plural = "Biträdesavtal"
        ordering = ["-published_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_current"], condition=Q(is_current=True), name="utskick_dpa_current"
            ),
        ]

    def __str__(self):
        return f"Biträdesavtal {self.version}"


class DpaAcceptance(models.Model):
    """Ett godkännande av en version. Bara nya rader; byrån i kundvyn måste
    skriva vem hos kunden som godkände och hur (staff_statement)."""

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_dpa"
    )
    version = models.ForeignKey(DpaVersion, on_delete=models.PROTECT, related_name="acceptances")
    accepted_at = models.DateTimeField(default=timezone.now)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    accepted_as_staff = models.BooleanField(default=False)
    staff_statement = models.CharField("Godkänt av, och hur", max_length=300, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = "Godkänt biträdesavtal"
        verbose_name_plural = "Godkända biträdesavtal"
        indexes = [
            models.Index(fields=["account", "-accepted_at"], name="utskick_dpa_account"),
        ]

    def __str__(self):
        return f"Biträdesavtal godkänt för konto {self.account_id}"


# ---------------------------------------------------------------------------
# Registret
# ---------------------------------------------------------------------------


class FieldDef(models.Model):
    """Ett extrafält. Högst 30 per konto (vyn) och högst ett som visas i
    listan under namnet ("Regnr ABC 123")."""

    class Kind(models.TextChoices):
        TEXT = "text", "Text"
        DATE = "date", "Datum"
        NUMBER = "number", "Tal"
        CHOICE = "choice", "Val"

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_fields"
    )
    key = models.SlugField("Nyckel", max_length=40)
    label = models.CharField("Rubrik", max_length=60)
    kind = models.CharField("Typ", max_length=8, choices=Kind.choices, default=Kind.TEXT)
    choices = models.JSONField("Val", default=list, blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    show_in_list = models.BooleanField("Visas i listan", default=False)
    created_at = models.DateTimeField(default=timezone.now)

    MAX_PER_ACCOUNT = 30

    class Meta:
        verbose_name = "Extrafält"
        verbose_name_plural = "Extrafält"
        ordering = ["order", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["account", "key"], name="utskick_field_key"),
            models.UniqueConstraint(
                fields=["account"], condition=Q(show_in_list=True), name="utskick_field_in_list"
            ),
        ]

    def __str__(self):
        return self.label


class Tag(models.Model):
    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_tags"
    )
    name = models.CharField("Namn", max_length=40)

    class Meta:
        verbose_name = "Tagg"
        verbose_name_plural = "Taggar"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["account", "name"], name="utskick_tag_name"),
        ]

    def __str__(self):
        return self.name


class ContactList(models.Model):
    """En lista ("Lista" i gränssnittet) som kunden fyller själv."""

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_lists"
    )
    name = models.CharField("Namn", max_length=80)
    description = models.CharField("Beskrivning", max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "Lista"
        verbose_name_plural = "Listor"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["account", "name"], name="utskick_list_name"),
        ]

    def __str__(self):
        return self.name


class Contact(models.Model):
    """En kontakt i kundens register. I mallar och vykontexter heter den
    alltid "kontakt" (Kontakter betyder inloggningar på andra ställen i
    Flamingo, se README "Naming rule")."""

    class Kind(models.TextChoices):
        PERSON = "person", "Privatperson"
        COMPANY = "company", "Företag"

    class EmailState(models.TextChoices):
        OK = "ok", "Fungerar"
        BOUNCED = "bounced", "Studsad"

    class Source(models.TextChoices):
        IMPORT = "import", "Import"
        FORM = "form", "Formulär"
        SIGNUP = "signup", "Anmälan"
        MANUAL = "manual", "Manuellt"
        API = "api", "API"
        REPLY = "reply", "Svar"
        LEAD = "lead", "Förfrågan"

    #: Fem mjuka studsar i rad gör adressen studsad.
    SOFT_BOUNCE_LIMIT = 5

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_contacts"
    )
    kind = models.CharField("Typ", max_length=10, choices=Kind.choices, default=Kind.PERSON)
    first_name = models.CharField("Förnamn", max_length=60, blank=True)
    last_name = models.CharField("Efternamn", max_length=80, blank=True)
    company_name = models.CharField("Företag", max_length=120, blank=True)
    #: Tio siffror, bara juridiska personer (normalize.org_number). Ett
    #: personnummer (enskild firma) sparas aldrig.
    org_number = models.CharField("Organisationsnummer", max_length=10, blank=True)
    #: E.164 från apps.sms.numbers.parse, tomt om inget eller ogiltigt.
    phone = models.CharField("Mobil", max_length=16, blank=True)
    phone_country = models.CharField(max_length=2, blank=True)
    #: Utan blanksteg, gemener, validerad (normalize.email).
    email = models.CharField("E-post", max_length=254, blank=True)
    email_state = models.CharField(max_length=10, choices=EmailState.choices, default=EmailState.OK)
    email_soft_bounces = models.PositiveSmallIntegerField(default=0)
    email_bounced_at = models.DateTimeField(null=True, blank=True)
    #: FieldDef.key -> text (datum som åååå-mm-dd).
    fields = models.JSONField("Extrafält", default=dict, blank=True)
    tags = models.ManyToManyField(Tag, related_name="contacts", blank=True)
    source = models.CharField("Källa", max_length=10, choices=Source.choices)
    source_detail = models.CharField(max_length=200, blank=True)
    #: Gemener: namn, telefonsiffror, e-post och fältvärden (sökningen).
    search_text = models.TextField(blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    last_activity_kind = models.CharField(max_length=20, blank=True)
    #: utskick_daily: inget samtycke och 24 månader utan aktivitet (E.7).
    inactive_flagged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Kontakt"
        verbose_name_plural = "Kontakter"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "phone"], condition=~Q(phone=""), name="utskick_contact_phone"
            ),
            models.UniqueConstraint(
                fields=["account", "email"], condition=~Q(email=""), name="utskick_contact_email"
            ),
        ]
        indexes = [
            models.Index(fields=["account", "-last_activity_at"], name="utskick_contact_activity"),
            models.Index(fields=["account", "-created_at"], name="utskick_contact_created"),
            models.Index(fields=["account", "kind"], name="utskick_contact_kind"),
        ]

    def __str__(self):
        return f"Kontakt {self.pk}"

    @property
    def full_name(self):
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    @property
    def display_name(self):
        """Namnet i listor: personens namn, annars företaget."""
        if self.kind == self.Kind.COMPANY and self.company_name:
            return self.company_name
        return self.full_name or self.company_name or "Utan namn"

    def address(self, channel):
        return self.phone if channel == CHANNEL_SMS else self.email


class Consent(models.Model):
    """Samtycket just nu för en kanal. Skrivs bara av consent.set_status.
    value_hash är hashen av adressen som samtycket gäller (keys.value_hash);
    raden finns för varje kanal där kontakten har en adress."""

    class Status(models.TextChoices):
        YES = "yes", "Ja"
        EXISTING = "existing", "Befintlig kund"
        COMPANY = "company", "Företag"
        PENDING = "pending", "Väntar på bekräftelse"
        MISSING = "missing", "Inget samtycke"
        DECLINED = "declined", "Vill inte ha erbjudanden"
        UNSUBSCRIBED = "unsubscribed", "Avregistrerad"

    class Basis(models.TextChoices):
        CONSENT = "consent", "Samtycke"
        EXISTING_CUSTOMER = "existing_customer", "Befintlig kund"
        COMPANY = "company", "Företag"
        NONE = "none", "Ingen grund"

    class Source(models.TextChoices):
        IMPORT = "import", "Import"
        LP_FORM = "lp_form", "Formulär på landningssidan"
        SIGNUP = "signup", "Anmälningssidan"
        PREFERENCE = "preference", "Mina utskick"
        DOI = "doi", "Bekräftelse via e-post"
        CONFIRM = "confirm", "Bekräftelse via sms"
        MANUAL = "manual", "Manuellt"
        API = "api", "API"
        STOP = "stop", "STOPP"
        START = "start", "START"
        LINK = "link", "Avregistreringslänk"
        LIST_UNSUB = "list_unsub", "Avregistrering i e-postprogrammet"
        COMPLAINT = "complaint", "Klagomål"
        REPLY = "reply", "Svar"
        ADDRESS = "address", "Ny adress"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="consents")
    channel = models.CharField(max_length=5, choices=CHANNEL_CHOICES)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.MISSING)
    basis = models.CharField(max_length=17, choices=Basis.choices, default=Basis.NONE)
    value_hash = models.CharField(max_length=64, blank=True)
    #: Exakt den text personen såg, med företagets namn ifyllt.
    text_shown = models.TextField(blank=True)
    #: Texten innehöll meningen om att mejlen mäter öppningar (H.5).
    tracking_ok = models.BooleanField(default=False)
    #: Kundens anteckning, till exempel "kassan, från 2024".
    evidence = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices, blank=True)
    source_detail = models.CharField(max_length=200, blank=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirm_sent_at = models.DateTimeField(null=True, blank=True)
    confirm_count = models.PositiveSmallIntegerField(default=0)
    changed_at = models.DateTimeField(default=timezone.now)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    changed_by_label = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name = "Samtycke"
        verbose_name_plural = "Samtycken"
        constraints = [
            models.UniqueConstraint(fields=["contact", "channel"], name="utskick_consent_channel"),
        ]
        indexes = [
            models.Index(fields=["channel", "status"], name="utskick_consent_status"),
            # Tickens kö för bekräftelsemejl (optin.py): väntande, inget skickat.
            models.Index(
                fields=["changed_at"],
                condition=Q(status="pending", confirm_sent_at__isnull=True),
                name="utskick_consent_to_confirm",
            ),
        ]

    def __str__(self):
        return f"Samtycke {self.channel} för kontakt {self.contact_id}"


class ConsentLog(models.Model):
    """Beviset: varje ändring av ett samtycke, med texten som visades och
    vem som gjorde den. Bara nya rader. Raden överlever att kontakten tas
    bort (contact blir null; hashen och texten finns kvar). Enda ändringen
    är GDPR-tömningen av evidence i contacts.delete_contact (en uttrycklig
    QuerySet.update); raderas bara av retentionen (E.7)."""

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="+")
    contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="consent_log"
    )
    channel = models.CharField(max_length=5)
    value_hash = models.CharField(max_length=64)
    old_status = models.CharField(max_length=12, blank=True)
    new_status = models.CharField(max_length=12)
    basis = models.CharField(max_length=17)
    text_shown = models.TextField(blank=True)
    evidence = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=16)
    source_detail = models.CharField(max_length=200, blank=True)
    by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    by_label = models.CharField(max_length=120, blank=True)
    by_staff = models.BooleanField(default=False)
    #: Bara publika formulär (flamingo.limits.ip_hash).
    ip_hash = models.CharField(max_length=64, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Samtyckeslogg"
        verbose_name_plural = "Samtyckesloggen"
        ordering = ["-at", "-pk"]
        indexes = [
            models.Index(fields=["contact", "-at"], name="utskick_clog_contact"),
            models.Index(fields=["account", "value_hash"], name="utskick_clog_hash"),
            models.Index(fields=["value_hash", "-at"], name="utskick_clog_hash_at"),
        ]

    def __str__(self):
        return f"Samtyckeslogg {self.pk}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Samtyckesloggen ändras aldrig: skriv en ny rad.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Samtyckesloggen raderas bara av retentionen (E.7).")


class Suppression(models.Model):
    """Spärrlistan per kund och kanal, nycklad på adressens hash. Överlever
    att kontakten tas bort, och ingen import kan ta bort den: bara personen
    själv med en bekräftelse (H.6)."""

    class Reason(models.TextChoices):
        STOP = "stop", "STOPP"
        LINK = "link", "Avregistreringslänk"
        LIST_UNSUB = "list_unsub", "Avregistrering i e-postprogrammet"
        PREFERENCE = "preference", "Mina utskick"
        BOUNCE = "bounce", "Studsad adress"
        COMPLAINT = "complaint", "Klagomål"
        MANUAL = "manual", "Manuellt"
        IMPORT = "import", "Import"
        ERASURE = "erasure", "Borttagen kontakt"
        REPLY = "reply", "Svar"

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_suppressions"
    )
    channel = models.CharField(max_length=5, choices=CHANNEL_CHOICES)
    value_hash = models.CharField(max_length=64)
    reason = models.CharField(max_length=12, choices=Reason.choices)
    created_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = "Spärr"
        verbose_name_plural = "Spärrlistan"
        constraints = [
            models.UniqueConstraint(
                fields=["account", "channel", "value_hash"], name="utskick_suppression_unique"
            ),
        ]

    def __str__(self):
        return f"Spärr {self.pk} ({self.channel})"


class ListMembership(models.Model):
    class Source(models.TextChoices):
        IMPORT = "import", "Import"
        MANUAL = "manual", "Manuellt"
        SIGNUP = "signup", "Anmälan"
        FLOW = "flow", "Flöde"
        API = "api", "API"
        REPORT = "report", "Rapport"

    list = models.ForeignKey(ContactList, on_delete=models.CASCADE, related_name="memberships")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="memberships")
    added_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)

    class Meta:
        verbose_name = "Plats i lista"
        verbose_name_plural = "Platser i listor"
        constraints = [
            models.UniqueConstraint(fields=["list", "contact"], name="utskick_membership"),
        ]

    def __str__(self):
        return f"Kontakt {self.contact_id} i lista {self.list_id}"


# ---------------------------------------------------------------------------
# Import, anmälan, händelser, exporter och räknare
# ---------------------------------------------------------------------------


class ImportJob(models.Model):
    """En import. Filen ligger i PRIVATE_MEDIA_ROOT/utskick-import/ (aldrig
    MEDIA_ROOT) och tas bort enligt E.7; felrapporten läser om filen i
    stället för att spara värden (errors har rad, kolumn och orsak)."""

    class Kind(models.TextChoices):
        CSV = "csv", "CSV"
        XLSX = "xlsx", "Excel"
        PASTE = "paste", "Inklistrat"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uppladdad"
        CONVERTING = "converting", "Läses in"
        MAPPING = "mapping", "Kolumner"
        CONSENT = "consent", "Samtycke"
        ANALYSING = "analysing", "Granskas"
        REVIEW = "review", "Granska"
        IMPORTING = "importing", "Importeras"
        DONE = "done", "Klar"
        FAILED = "failed", "Misslyckades"
        CANCELLED = "cancelled", "Avbruten"

    account = models.ForeignKey(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_imports"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_as_staff = models.BooleanField(default=False)
    file = models.FileField(storage=private_storage, upload_to="utskick-import/%Y/%m/", blank=True)
    #: Den normaliserade UTF-8-CSV:n som importer.convert skrev.
    csv_path = models.CharField(max_length=300, blank=True)
    original_name = models.CharField(max_length=200)
    kind = models.CharField(max_length=5, choices=Kind.choices)
    size = models.PositiveIntegerField(default=0)
    delimiter = models.CharField(max_length=1, blank=True)
    encoding = models.CharField(max_length=20, blank=True)
    header = models.JSONField(default=list, blank=True)
    #: De fem första raderna. Töms vid klar, avbruten, misslyckad och övergiven.
    sample = models.JSONField(default=list, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    #: {"0": "full_name", "1": "phone", "3": "field:regnummer", "6": "skip"}
    mapping = models.JSONField(default=dict, blank=True)
    #: {"choice": "consent"|"existing"|"unknown", "sms": bool, "email": bool,
    #:  "where": "kassan, 2024"}
    consent = models.JSONField(default=dict, blank=True)
    target_list = models.ForeignKey(
        ContactList, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    target_tag = models.ForeignKey(
        Tag, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UPLOADED)
    #: Små filer (högst 2 000 rader) körs i förfrågan, större av tick.
    in_request = models.BooleanField(default=False)
    byte_offset = models.PositiveBigIntegerField(default=0)
    progress = models.PositiveIntegerField(default=0)
    #: new, updated, suppressed, errors, conflicts
    counts = models.JSONField(default=dict, blank=True)
    #: [{"row": 17, "column": "Mobil", "reason": "Ogiltigt nummer"}], högst 1000, inga värden
    errors = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    file_deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Import"
        verbose_name_plural = "Importer"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="utskick_import_status"),
        ]

    def __str__(self):
        return f"Import {self.pk}"


class SignupForm(models.Model):
    """Den publika anmälningssidan, en per kund. Skapas inaktiv; kunden
    slår på den själv."""

    account = models.OneToOneField(
        FlamingoAccount, on_delete=models.CASCADE, related_name="utskick_signup"
    )
    title = models.CharField("Rubrik", max_length=80)
    intro = models.CharField("Ingress", max_length=400, blank=True)
    channels = models.JSONField("Kanaler", default=default_signup_channels)
    add_to_list = models.ForeignKey(
        ContactList, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    add_tags = models.ManyToManyField(Tag, blank=True, related_name="+")
    is_active = models.BooleanField("Sidan är på", default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Anmälningssida"
        verbose_name_plural = "Anmälningssidor"

    def __str__(self):
        return f"Anmälningssida för konto {self.account_id}"


class Event(models.Model):
    """Händelser som inte har en egen rad någon annanstans. Utskick,
    klick, samtycken och svar läses ur sina egna tabeller av
    timeline.for_contact och kopieras aldrig hit (disken)."""

    IMPORTED = "imported"
    SIGNUP = "signup"
    LEAD = "lead"
    TEST_SEND = "test_send"
    #: Slagen som finns från S1 (fler tillkommer per steg, README B.1).
    S1_KINDS = (IMPORTED, SIGNUP, LEAD, TEST_SEND)

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="+")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=20)
    at = models.DateTimeField(default=timezone.now)
    lead = models.ForeignKey(
        "flamingo.Lead", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: Litet, och ingen fritext från tredje part.
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "Händelse"
        verbose_name_plural = "Händelser"
        ordering = ["-at", "-pk"]
        indexes = [
            models.Index(fields=["contact", "-at"], name="utskick_event_contact"),
            models.Index(fields=["account", "kind", "-at"], name="utskick_event_kind"),
        ]

    def __str__(self):
        return f"Händelse {self.kind} för kontakt {self.contact_id}"


class ExportLog(models.Model):
    class Kind(models.TextChoices):
        CONTACTS = "contacts", "Hela registret"
        CONTACT = "contact", "En person"
        UTSKICK = "utskick", "Mottagare"

    account = models.ForeignKey(FlamingoAccount, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    as_staff = models.BooleanField(default=False)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    rows = models.PositiveIntegerField(default=0)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Export"
        verbose_name_plural = "Exporter"
        ordering = ["-at", "-pk"]
        indexes = [
            models.Index(fields=["account", "-at"], name="utskick_export_account"),
        ]

    def __str__(self):
        return f"Export {self.pk}"


class Counter(models.Model):
    """Exakta gränser (limits.hit): en rad per scope, nyckel och fast fönster
    (timme eller svenskt dygn). utskick_daily tar bort rader äldre än två
    dygn."""

    scope = models.CharField(max_length=20)
    key = models.CharField(max_length=80, blank=True)
    window = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Räknare"
        verbose_name_plural = "Räknare"
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "key", "window"], name="utskick_counter_window"
            ),
        ]

    def __str__(self):
        return f"{self.scope} {self.window:%Y-%m-%d %H:%M}"
