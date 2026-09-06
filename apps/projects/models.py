"""
Projekt och ärenden: en lätt JIRA för byrån.

Hierarkin:

    Customer (kund)
      └── Project (projekt, har egna kolumner = sitt eget arbetsflöde)
            └── Issue (ärende)
                  ├── TimeEntry (tidsposter - timern ÄR en öppen tidspost)
                  └── Comment

Tre designbeslut som bär allt annat:

1. Ett ärende kan ha kund UTAN projekt (ett supportärende, en snabb
   fråga) och projekt utan kund (internt arbete). Har ärendet ett projekt
   med kund så ÄR kunden projektets - det får aldrig finnas två sanningar.
   Issue.clean() vaktar detta; Issue.effective_customer ger svaret.

2. Kolumner är data, inte en hårdkodad statuslista. Varje projekt äger
   sina kolumner (kopieras från DEFAULT_COLUMNS vid skapande), så ett
   projekt kan ha "Väntar på kund" och ett annat "Granskas" utan att
   någon annans tavla ändras. Kolumnen med is_done=True stänger ärendet
   (closed_at sätts) - det är vad rapporter räknar på.

3. Timern är inte ett fält utan en TimeEntry med ended_at=NULL. Det ger
   historik (vem, när, hur länge, fakturerbart eller inte), summering per
   ärende/projekt/kund, och databasens egen garanti att en användare bara
   kan ha EN timer igång (partiellt unikt index). Att trycka på timern är
   att öppna eller stänga en post - inget mer.

Nycklar: ärenden i ett projekt numreras löpande per projekt (NORD-12)
via en räknare på projektet som låses vid tilldelning. Ärenden utan
projekt visas som #<id>.
"""

import re
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import models, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.text import slugify


def private_storage():
    """
    Lagring för bilagor UTANFÖR MEDIA_ROOT.

    /media/ serveras rakt av nginx till hela internet; en kunds skärmdump
    får aldrig ligga där. Bilagor sparas i PRIVATE_MEDIA_ROOT utan
    webbadress och lämnas ut av en vy som först kontrollerar att den som
    frågar får se ärendet. Anropbar (inte instans) så att sökvägen aldrig
    hamnar i en migration.
    """
    return FileSystemStorage(location=str(settings.PRIVATE_MEDIA_ROOT), base_url=None)


ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024
ATTACHMENT_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "heic",
    "svg",
    "pdf",
    "txt",
    "md",
    "csv",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "odt",
    "ods",
    "zip",
    "mp4",
    "mov",
}


def validate_attachment(uploaded):
    ext = (uploaded.name.rsplit(".", 1)[-1] if "." in uploaded.name else "").lower()
    if ext not in ATTACHMENT_EXTENSIONS:
        raise ValidationError(f"Filtypen .{ext or '?'} tillåts inte.")
    if uploaded.size > ATTACHMENT_MAX_BYTES:
        raise ValidationError("Filen är större än 15 MB.")


def attachment_path(instance, filename):
    # Slumpad katalog per fil: även den som känner till lagringen kan inte
    # räkna upp andra kunders filer.
    safe = slugify(filename.rsplit(".", 1)[0])[:60] or "fil"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"arenden/{secrets.token_urlsafe(12)}/{safe}.{ext}"


DEFAULT_COLUMNS = [
    ("Att göra", False),
    ("Pågår", False),
    ("Klart", True),
]


class Customer(models.Model):
    name = models.CharField("Namn", max_length=200)
    org_number = models.CharField("Organisationsnummer", max_length=20, blank=True)
    email = models.EmailField("E-post", blank=True)
    phone = models.CharField("Telefon", max_length=40, blank=True)
    website = models.URLField("Webbplats", blank=True)
    notes = models.TextField("Anteckningar", blank=True)
    is_active = models.BooleanField("Aktiv", default=True)
    # Kundens inloggningar i portalen. Vanliga användare utan is_staff;
    # PortalGateMiddleware håller dem borta från /manage/.
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="customers", verbose_name="Kontakter"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Kund"
        verbose_name_plural = "Kunder"

    def __str__(self):
        return self.name

    def support_project(self):
        """
        Projektet kundens egna ärenden hamnar i. Skapas vid första behovet.

        Varje ärende ska ha kolumner (annars kan det inte flyttas på en
        tavla), så portalens ärenden får alltid ett projekt - kundens
        "Support"-projekt - i stället för att ligga lösa.
        """
        project = self.projects.filter(name="Support").first()
        if project is None:
            project = Project.objects.create(
                customer=self, name="Support", key=Project.make_key(self.name)
            )
        return project

    def total_seconds(self):
        """All loggad tid hos kunden: projektens ärenden + fristående ärenden."""
        return TimeEntry.objects.filter(
            Q(issue__project__customer=self) | Q(issue__customer=self)
        ).total_seconds()


class ProjectStatus(models.TextChoices):
    ACTIVE = "active", "Aktivt"
    PAUSED = "paused", "Pausat"
    DONE = "done", "Avslutat"
    ARCHIVED = "archived", "Arkiverat"


class Project(models.Model):
    customer = models.ForeignKey(
        Customer,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="projects",
        verbose_name="Kund",
    )
    name = models.CharField("Namn", max_length=200)
    # Kort nyckel för ärendenummer: NORD-12. Versaler, unik.
    key = models.CharField("Nyckel", max_length=10, unique=True)
    description = models.TextField("Beskrivning", blank=True)
    status = models.CharField(
        max_length=10, choices=ProjectStatus.choices, default=ProjectStatus.ACTIVE
    )
    hourly_rate = models.PositiveIntegerField(
        "Timpris (kr)", null=True, blank=True, help_text="Tomt = inte fakturerat per timme."
    )
    budget_hours = models.PositiveIntegerField("Budget (timmar)", null=True, blank=True)
    starts_on = models.DateField("Start", null=True, blank=True)
    due_on = models.DateField("Deadline", null=True, blank=True)
    # Löpande ärendenummer. Läses och räknas upp under lås i Issue.assign_number.
    next_issue_number = models.PositiveIntegerField(default=1, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "name"]
        verbose_name = "Projekt"
        verbose_name_plural = "Projekt"

    def __str__(self):
        return f"{self.key} {self.name}"

    def save(self, *args, **kwargs):
        self.key = self.key.strip().upper()
        creating = self._state.adding
        super().save(*args, **kwargs)
        if creating and not self.columns.exists():
            for position, (title, is_done) in enumerate(DEFAULT_COLUMNS):
                Column.objects.create(project=self, title=title, position=position, is_done=is_done)

    @classmethod
    def make_key(cls, name):
        """Ledig nyckel ur ett namn: 'Nordan Bygg AB' -> NORD, NORD2, NORD3..."""
        letters = re.sub(r"[^A-Z]", "", slugify(name).upper().replace("-", "")) or "PROJ"
        base = letters[:4]
        key, n = base, 1
        while cls.objects.filter(key=key).exists():
            n += 1
            key = f"{base}{n}"
        return key

    def total_seconds(self):
        return TimeEntry.objects.filter(issue__project=self).total_seconds()


class Column(models.Model):
    """En kolumn på projektets tavla. Ordningen är position; is_done stänger."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="columns")
    title = models.CharField("Rubrik", max_length=60)
    position = models.PositiveIntegerField(default=0)
    wip_limit = models.PositiveIntegerField(
        "Max samtidiga", null=True, blank=True, help_text="Tomt = ingen gräns."
    )
    is_done = models.BooleanField(
        "Avslutar ärendet",
        default=False,
        help_text="Ärenden som flyttas hit räknas som klara.",
    )

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Kolumn"
        verbose_name_plural = "Kolumner"
        constraints = [
            models.UniqueConstraint(
                fields=["project", "title"], name="unique_column_title_per_project"
            ),
        ]

    def __str__(self):
        return f"{self.project.key}: {self.title}"


class Label(models.Model):
    name = models.CharField("Namn", max_length=40, unique=True)
    color = models.CharField("Färg", max_length=7, default="#e8930c")

    class Meta:
        ordering = ["name"]
        verbose_name = "Etikett"
        verbose_name_plural = "Etiketter"

    def __str__(self):
        return self.name


class IssuePriority(models.IntegerChoices):
    LOW = 10, "Låg"
    NORMAL = 20, "Normal"
    HIGH = 30, "Hög"
    URGENT = 40, "Akut"


class IssueType(models.TextChoices):
    TASK = "task", "Uppgift"
    BUG = "bug", "Fel"
    FEATURE = "feature", "Ny funktion"
    SUPPORT = "support", "Support"


class IssueQuerySet(models.QuerySet):
    def open(self):
        return self.filter(closed_at__isnull=True)

    def for_customer(self, customer):
        """Ärenden som hör till kunden direkt ELLER via sitt projekt."""
        return self.filter(Q(customer=customer) | Q(project__customer=customer))

    def with_logged_seconds(self):
        """Annoterar summan av AVSLUTADE tidsposter (pågående läggs på i Python)."""
        return self.annotate(logged_seconds=Sum("time_entries__seconds"))


class Issue(models.Model):
    project = models.ForeignKey(
        Project,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="issues",
        verbose_name="Projekt",
    )
    # Bara satt när ärendet INTE har ett projekt med kund - se clean().
    customer = models.ForeignKey(
        Customer,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="issues",
        verbose_name="Kund",
    )
    column = models.ForeignKey(
        Column,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="issues",
        verbose_name="Kolumn",
    )
    number = models.PositiveIntegerField(null=True, blank=True, editable=False)
    title = models.CharField("Rubrik", max_length=200)
    description = models.TextField("Beskrivning", blank=True)
    issue_type = models.CharField(
        "Typ", max_length=10, choices=IssueType.choices, default=IssueType.TASK
    )
    priority = models.PositiveSmallIntegerField(
        "Prioritet", choices=IssuePriority.choices, default=IssuePriority.NORMAL
    )
    labels = models.ManyToManyField(Label, blank=True, related_name="issues")
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_issues",
        verbose_name="Ansvarig",
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reported_issues",
    )
    estimate_minutes = models.PositiveIntegerField("Uppskattning (minuter)", null=True, blank=True)
    due_on = models.DateField("Förfaller", null=True, blank=True)
    is_billable = models.BooleanField("Fakturerbart", default=True)
    # Kunden ser BARA ärenden med den här bocken. Standard av: ett internt
    # ärende ska aldrig läcka till portalen av misstag. Ärenden kunden själv
    # skapar sätts till synliga.
    visible_to_customer = models.BooleanField("Synlig för kund", default=False)
    created_in_portal = models.BooleanField(default=False, editable=False)
    # Ordning inom kolumnen. Skrivs om för hela kolumnen vid omordning -
    # samma enkla, robusta grepp som offertraderna.
    position = models.PositiveIntegerField(default=0)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = IssueQuerySet.as_manager()

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "Ärende"
        verbose_name_plural = "Ärenden"
        indexes = [
            models.Index(fields=["project", "column", "position"]),
            models.Index(fields=["customer", "closed_at"]),
            models.Index(fields=["assignee", "closed_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "number"], name="unique_issue_number_per_project"
            ),
            # Ett ärende utan projekt får inte heller ha ett nummer -
            # numret är projektets.
            models.CheckConstraint(
                condition=Q(project__isnull=False) | Q(number__isnull=True),
                name="issue_number_requires_project",
            ),
        ]

    def __str__(self):
        return f"{self.key} {self.title}"

    def save(self, *args, **kwargs):
        # Normalisera: har projektet en kund är den sanningen - lagra inte
        # samma sak på två ställen.
        if self.project_id and self.project.customer_id:
            self.customer = None
        if self.project_id and self.column_id is None:
            self.column = self.project.columns.order_by("position").first()
        if self.project_id and self.number is None:
            self.number = self._next_number()
        if not self.project_id:
            self.column = None
            self.number = None
        # Kolumnen avgör om ärendet är stängt.
        if self.column_id and self.column.is_done:
            self.closed_at = self.closed_at or timezone.now()
        elif self.column_id:
            self.closed_at = None
        super().save(*args, **kwargs)

    # ---- nycklar och härledda fält -----------------------------------------
    @property
    def key(self):
        if self.project_id and self.number:
            return f"{self.project.key}-{self.number}"
        return f"#{self.pk}" if self.pk else "nytt"

    @property
    def effective_customer(self):
        """Kunden ärendet gäller: projektets om det har en, annars ärendets egen."""
        if self.project_id and self.project.customer_id:
            return self.project.customer
        return self.customer

    @property
    def is_closed(self):
        return self.closed_at is not None

    @property
    def stage(self):
        """
        Grov status oberoende av projektets kolumnnamn: new / active / done.
        Portalen visar kundens ärenden från flera projekt på EN tavla, och
        då är det här den gemensamma nämnaren.
        """
        if self.closed_at:
            return "done"
        if self.column_id and self.column.position > 0:
            return "active"
        return "new"

    # ---- validering ----------------------------------------------------------
    def clean(self):
        if self.project_id and self.column_id and self.column.project_id != self.project_id:
            raise ValidationError({"column": "Kolumnen tillhör ett annat projekt."})
        if not self.project_id and self.column_id:
            raise ValidationError({"column": "Ett ärende utan projekt har ingen kolumn."})
        if (
            self.project_id
            and self.customer_id
            and self.project.customer_id
            and self.project.customer_id != self.customer_id
        ):
            raise ValidationError(
                {"customer": "Projektet tillhör en annan kund. Ta bort kunden på ärendet."}
            )

    def _next_number(self):
        """Nästa löpnummer i projektet, tilldelat under radlås."""
        with transaction.atomic():
            project = Project.objects.select_for_update().get(pk=self.project_id)
            number = project.next_issue_number
            Project.objects.filter(pk=project.pk).update(
                next_issue_number=F("next_issue_number") + 1
            )
        return number

    # ---- tid ----------------------------------------------------------------
    def running_entry(self):
        return self.time_entries.filter(ended_at__isnull=True).first()

    def total_seconds(self):
        return self.time_entries.total_seconds()

    def start_timer(self, user):
        """Öppnar en tidspost. Användarens ev. andra timer stängs först."""
        return TimeEntry.start(issue=self, user=user)

    def stop_timer(self, user):
        entry = self.time_entries.filter(user=user, ended_at__isnull=True).first()
        if entry:
            entry.stop()
        return entry


class TimeEntryQuerySet(models.QuerySet):
    def running(self):
        return self.filter(ended_at__isnull=True)

    def total_seconds(self):
        """Avslutade poster ur databasen + pågående räknade just nu."""
        done = self.filter(ended_at__isnull=False).aggregate(s=Sum("seconds"))["s"] or 0
        now = timezone.now()
        live = sum(int((now - e.started_at).total_seconds()) for e in self.running())
        return done + live


class TimeEntry(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="time_entries")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="time_entries"
    )
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    # Denormaliserat vid stopp så att summor blir en enda SUM() i databasen.
    seconds = models.PositiveIntegerField(default=0)
    note = models.CharField("Anteckning", max_length=300, blank=True)
    is_billable = models.BooleanField("Fakturerbart", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TimeEntryQuerySet.as_manager()

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Tidspost"
        verbose_name_plural = "Tidsposter"
        indexes = [models.Index(fields=["user", "ended_at"])]
        constraints = [
            # Databasens garanti: en användare, en timer igång.
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(ended_at__isnull=True),
                name="one_running_timer_per_user",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=F("started_at")),
                name="time_entry_ends_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.issue.key}: {self.seconds}s"

    @property
    def is_running(self):
        return self.ended_at is None

    def elapsed_seconds(self):
        if self.is_running:
            return int((timezone.now() - self.started_at).total_seconds())
        return self.seconds

    @classmethod
    def start(cls, issue, user):
        """Starta timer på ett ärende. Stänger användarens pågående post först."""
        with transaction.atomic():
            for running in cls.objects.select_for_update().filter(user=user, ended_at__isnull=True):
                running.stop()
            return cls.objects.create(issue=issue, user=user, is_billable=issue.is_billable)

    def stop(self):
        if not self.is_running:
            return
        self.ended_at = timezone.now()
        self.seconds = max(0, int((self.ended_at - self.started_at).total_seconds()))
        self.save(update_fields=["ended_at", "seconds"])


class Comment(models.Model):
    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField("Kommentar")
    # Intern anteckning: syns aldrig i portalen. Kundens egna är alltid externa.
    is_internal = models.BooleanField("Intern anteckning", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Kommentar"
        verbose_name_plural = "Kommentarer"

    def __str__(self):
        return f"{self.issue.key}: {self.body[:40]}"


class Attachment(models.Model):
    """Bilaga på ett ärende: skärmdump, dokument. Privat lagring, gated utlämning."""

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path, storage=private_storage)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Bilaga"
        verbose_name_plural = "Bilagor"

    def __str__(self):
        return self.original_name

    @property
    def is_image(self):
        return self.content_type.startswith("image/")

    @property
    def size_display(self):
        if self.size >= 1024 * 1024:
            return f"{self.size / (1024 * 1024):.1f} MB"
        return f"{max(1, self.size // 1024)} kB"
