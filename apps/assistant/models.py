"""
AI-redaktörens datamodeller.

Tre saker bor här:

- RevisionMeta: det django-reversion saknar per revision - källa (manuell/AI/
  import), prompten bakom en AI-ändring och batch-kopplingen till ett AIJob.
- AIJob + DraftChange: utkastflödet. AI:n skriver aldrig direkt; varje
  skrivoperation blir en DraftChange som kunden godkänner i /manage/.
- AssistantToken: per-användarnyckel för MCP-anslutningen. Lagras hashad,
  visas en gång.
"""

import hashlib
import secrets

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Risk(models.TextChoices):
    """Riskklass per operation. Styr hur godkännandet ser ut."""

    READ = "read", _("Läsning")
    TEXT = "text", _("Textändring")
    BUSINESS = "business", _("Affärsdata")


class AIJob(models.Model):
    """
    En arbetssession: "Skapa 8 Roslagen-sidor" är ett jobb.

    Via MCP mappas klientens session till ett jobb, så en stor batch kan
    granskas på en sida och ångras i ett klick.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Väntar på granskning")
        APPLIED = "applied", _("Genomfört")
        DISCARDED = "discarded", _("Förkastat")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_jobs"
    )
    session_key = models.CharField(max_length=128, blank=True, db_index=True)
    title = models.CharField(_("Rubrik"), max_length=200, blank=True)
    prompt = models.TextField(_("Instruktion"), blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    tool_log = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI-jobb"
        verbose_name_plural = "AI-jobb"

    def __str__(self):
        return self.title or f"AI-jobb #{self.pk}"

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("manage:assistant_job", kwargs={"pk": self.pk})

    def log_call(self, operation, summary):
        self.tool_log.append(
            {
                "at": timezone.now().isoformat(timespec="seconds"),
                "op": operation,
                "summary": summary,
            }
        )
        self.save(update_fields=["tool_log", "updated_at"])

    @property
    def pending_count(self):
        return self.changes.filter(status=DraftChange.Status.PENDING).count()


class DraftChange(models.Model):
    """
    En föreslagen ändring - den atomära enheten för godkännande.

    payload är redan validerad och sanerad när raden skapas; apply-steget
    kör den genom samma kodväg som manage-formulären. Objekt skapas först
    vid godkännande, aldrig vid förslag.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Väntar")
        REJECTED = "rejected", _("Avslagen")
        APPLIED = "applied", _("Genomförd")
        FAILED = "failed", _("Misslyckades")

    job = models.ForeignKey(AIJob, on_delete=models.CASCADE, related_name="changes")
    operation = models.CharField(max_length=60)
    risk = models.CharField(max_length=10, choices=Risk.choices)
    summary = models.CharField(_("Sammanfattning"), max_length=300, blank=True)
    target_ct = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    target_id = models.PositiveIntegerField(null=True, blank=True)
    payload = models.JSONField()
    before = models.JSONField(null=True, blank=True)
    depends_on = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="dependants"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    error = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Utkast"
        verbose_name_plural = "Utkast"

    def __str__(self):
        return f"{self.operation}: {self.summary or self.pk}"

    @property
    def target(self):
        """
        Objektet ändringen gäller, eller None.

        None även när objektet hunnit raderas mellan förslag och godkännande -
        anroparen skiljer på "skapar nytt" och "borta" via target_ct_id, och
        ett raderat objekt ska ge ett begripligt fel, inte en DoesNotExist.
        """
        if not (self.target_ct_id and self.target_id):
            return None
        model = self.target_ct.model_class()
        if model is None:
            return None
        return model._base_manager.filter(pk=self.target_id).first()


class RevisionMeta(models.Model):
    """Sidometadata per reversion-revision: källa, prompt och batch."""

    class Source(models.TextChoices):
        MANUAL = "manual", _("Manuell ändring")
        AI = "ai", _("AI-förslag")
        IMPORT = "import", _("Import")

    revision = models.OneToOneField(
        "reversion.Revision", on_delete=models.CASCADE, related_name="meta"
    )
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    prompt = models.TextField(blank=True)
    job = models.ForeignKey(
        AIJob, null=True, blank=True, on_delete=models.SET_NULL, related_name="revisions"
    )

    class Meta:
        verbose_name = "Revisionsmetadata"
        verbose_name_plural = "Revisionsmetadata"

    def __str__(self):
        return f"{self.get_source_display()} (revision {self.revision_id})"


class AssistantToken(models.Model):
    """
    Bearer-token för MCP-anslutningen. Nyckeln lagras som SHA-256-hash och
    visas i klartext exakt en gång, vid skapandet.
    """

    PREFIX = "adx_"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_tokens"
    )
    name = models.CharField(_("Namn"), max_length=100, default="AI-koppling")
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    key_hint = models.CharField(max_length=12, editable=False)  # "adx_ab12…"
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI-nyckel"
        verbose_name_plural = "AI-nycklar"

    def __str__(self):
        return f"{self.name} ({self.key_hint}…)"

    @staticmethod
    def _hash(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name="AI-koppling"):
        """Skapa en token. Returnerar (instans, klartextnyckel)."""
        raw = cls.PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(user=user, name=name, key_hash=cls._hash(raw), key_hint=raw[:12])
        return token, raw

    @classmethod
    def authenticate(cls, raw):
        """
        Klartextnyckel -> aktiv token med aktiv användare, annars None.

        Samma behörighetsbar som /manage/ (login_required) - tokenens
        användare är den som "klickar".
        """
        if not raw or not raw.startswith(cls.PREFIX):
            return None
        token = (
            cls.objects.select_related("user")
            .filter(key_hash=cls._hash(raw), is_active=True, user__is_active=True)
            .first()
        )
        if token is None:
            return None
        cls.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        return token


class ChatRole(models.TextChoices):
    USER = "user", _("Kund")
    ASSISTANT = "assistant", _("Assistent")


class ChatMessage(models.Model):
    """
    Ett meddelande i den inbyggda chatten.

    Assistentens svar skapas som PENDING när frågan tas emot och fylls i när
    modellen är klar. Kunden kan stänga fliken mitt i ett långt jobb utan att
    förlora det - raden ligger kvar och fylls i ändå.

    Meddelandena hänger på ett AIJob, så en konversation och dess utkast är
    samma sak: granskningssidan för jobbet visar vad samtalet faktiskt
    föreslog.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Arbetar")
        DONE = "done", _("Klart")
        FAILED = "failed", _("Misslyckades")

    job = models.ForeignKey(AIJob, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=ChatRole.choices)
    content = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DONE)
    error = models.TextField(blank=True)
    #: Modellens tool_use/tool_result-turer, så konversationen kan fortsätta
    #: utan att köra om verktygen.
    transcript = models.JSONField(default=list, blank=True)
    #: Kort rad per verktyg, för "vad gjorde den"-raden i gränssnittet.
    steps = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Chattmeddelande"
        verbose_name_plural = "Chattmeddelanden"

    def __str__(self):
        return f"{self.get_role_display()}: {self.content[:60]}"


class AICall(models.Model):
    """
    En rad per modellanrop: tokens, verklig kostnad, utfall.

    Det här är mätaren. Utan den syns aldrig vad assistenten kostar, och en
    dyr vana upptäcks först på fakturan. Kostnaden i mikrodollar för att
    priserna är satta i USD - växling hör hemma i gränssnittet.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    job = models.ForeignKey(
        AIJob, null=True, blank=True, on_delete=models.SET_NULL, related_name="calls"
    )
    model = models.CharField(max_length=60)
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    tokens_cached = models.PositiveIntegerField(default=0)
    cost_micros = models.PositiveIntegerField(default=0, help_text="Mikrodollar")
    stop_reason = models.CharField(max_length=40, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]
        verbose_name = "AI-anrop"
        verbose_name_plural = "AI-anrop"

    def __str__(self):
        return f"{self.model} {self.tokens_in}+{self.tokens_out} tokens"

    @property
    def cost_usd(self):
        return self.cost_micros / 1_000_000


# OAuth-lagringen bor i en egen modul för läsbarhet, men måste importeras
# här för att Django ska hitta modellerna.
from .oauth_models import (  # noqa: E402,F401
    AuthorizationCode,
    OAuthClient,
    OAuthToken,
)
