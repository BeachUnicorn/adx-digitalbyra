"""
OAuth 2.1-lagring för MCP-anslutningen.

Claude-appens connector kan inte skicka en egen bearer-header - den kör
OAuth. Klienten registrerar sig själv (dynamisk klientregistrering), kunden
loggar in och godkänner, och först då får klienten en token.

Modellerna här är rena lagringsdetaljer; protokollet sköts av
`oauth_provider.py` och MCP-SDK:ts routes. Hemligheter lagras hashade, precis
som AssistantToken.

Kort livslängd på access-token (1 h) med refresh-token som roterar: en läckt
access-token blir snabbt värdelös, och kunden kan när som helst dra tillbaka
åtkomsten i /manage/.
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

#: Access-token lever kort; klienten förnyar med refresh-token.
ACCESS_TOKEN_TTL = timedelta(hours=1)
#: Refresh-token lever länge - kunden ska inte behöva logga in varje dag.
REFRESH_TOKEN_TTL = timedelta(days=90)
#: Auktoriseringskoden är ett engångskvitto mellan två omdirigeringar.
AUTH_CODE_TTL = timedelta(minutes=5)

#: Ett scope räcker: verktygsytan är redan riskklassad per operation, och
#: godkännandet i /manage/ är den verkliga behörighetsgränsen.
DEFAULT_SCOPE = "redaktor"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _fernet():
    """
    Nyckel härledd ur SECRET_KEY. Roteras SECRET_KEY blir befintliga
    klienthemligheter oläsbara - klienten registrerar då om sig, vilket är
    rätt beteende och samma sak som händer med Djangos sessioner.
    """
    import base64

    from cryptography.fernet import Fernet
    from django.conf import settings

    digest = hashlib.sha256(f"assistant-oauth:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class OAuthClient(models.Model):
    """
    En registrerad OAuth-klient, normalt skapad av Claude själv via
    dynamisk klientregistrering (RFC 7591).
    """

    client_id = models.CharField(max_length=64, unique=True, editable=False)
    # Krypterad, inte hashad: MCP-SDK:t jämför den inkommande hemligheten mot
    # det `get_client()` returnerar, så den måste gå att läsa tillbaka.
    # Kryptering i vila skyddar ändå mot en läckt databasdump. Access- och
    # refresh-tokens - det som faktiskt ger åtkomst - är hashade som vanligt.
    client_secret_encrypted = models.BinaryField(blank=True, null=True, editable=False)
    client_name = models.CharField(max_length=200, blank=True)
    redirect_uris = models.JSONField(default=list)
    grant_types = models.JSONField(default=list)
    response_types = models.JSONField(default=list)
    token_endpoint_auth_method = models.CharField(max_length=40, default="client_secret_post")
    scope = models.CharField(max_length=200, default=DEFAULT_SCOPE)
    client_uri = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "OAuth-klient"
        verbose_name_plural = "OAuth-klienter"
        ordering = ["-created_at"]

    def __str__(self):
        return self.client_name or self.client_id

    def set_secret(self, secret):
        self.client_secret_encrypted = _fernet().encrypt(secret.encode()) if secret else None

    @property
    def client_secret(self):
        """Klartexthemligheten, eller None för en publik klient (bara PKCE)."""
        if not self.client_secret_encrypted:
            return None
        from cryptography.fernet import InvalidToken

        try:
            return _fernet().decrypt(bytes(self.client_secret_encrypted)).decode()
        except InvalidToken:
            # SECRET_KEY har roterats. Klienten får registrera om sig.
            return None


class AuthorizationCode(models.Model):
    """Engångskod mellan samtycket och tokenutbytet. PKCE krävs."""

    code_hash = models.CharField(max_length=64, unique=True, editable=False)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="codes")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_codes"
    )
    redirect_uri = models.CharField(max_length=500)
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    code_challenge = models.CharField(max_length=200)
    scopes = models.JSONField(default=list)
    resource = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Auktoriseringskod"
        verbose_name_plural = "Auktoriseringskoder"

    def __str__(self):
        return f"Kod för {self.user} ({self.client})"

    @property
    def is_usable(self):
        return self.used_at is None and self.expires_at > timezone.now()


class OAuthToken(models.Model):
    """Access- eller refresh-token. Samma tabell, olika livslängd och roll."""

    class Kind(models.TextChoices):
        ACCESS = "access", _("Access-token")
        REFRESH = "refresh", _("Refresh-token")

    kind = models.CharField(max_length=8, choices=Kind.choices)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="tokens")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_tokens"
    )
    scopes = models.JSONField(default=list)
    resource = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "OAuth-token"
        verbose_name_plural = "OAuth-tokens"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["kind", "expires_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} för {self.user}"

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now()

    @classmethod
    def issue(cls, kind, client, user, scopes, resource=""):
        """Skapa en token. Returnerar (instans, klartextvärde)."""
        raw = secrets.token_urlsafe(32)
        ttl = ACCESS_TOKEN_TTL if kind == cls.Kind.ACCESS else REFRESH_TOKEN_TTL
        token = cls.objects.create(
            kind=kind,
            token_hash=_hash(raw),
            client=client,
            user=user,
            scopes=list(scopes or []),
            resource=resource or "",
            expires_at=timezone.now() + ttl,
        )
        return token, raw

    @classmethod
    def lookup(cls, kind, raw):
        """Klartextvärde -> giltig token, annars None."""
        if not raw:
            return None
        token = (
            cls.objects.select_related("client", "user")
            .filter(kind=kind, token_hash=_hash(raw))
            .first()
        )
        if token is None or not token.is_valid or not token.user.is_active:
            return None
        return token

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])
