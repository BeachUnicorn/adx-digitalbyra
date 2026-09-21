"""
AI-guider: integrationsdokumentation som andra AI-assistenter hämtar.

Flödet: en AI i ett kundprojekt öppnar https://adx.se/aiz/ (öppen, inga
hemligheter), ber Giovanni om en åtkomstkod, och hämtar sedan guiderna
med koden. Guiderna innehåller allt som behövs för att implementera t.ex.
övervakningens statusendpoint - inklusive den delade nyckeln - så koden
är det enda skyddet:

- kortlivad (en eller tjugofyra timmar), återkallbar, skapad i /manage/
- lagras bara som hash (saltad med SECRET_KEY); klartexten visas EN gång
- varje användning loggas: när, varifrån, vilken klient, vilken guide

AI-verktyg hämtar nästan alltid med GET, så koden får ligga i adressen
(?kod=). Därför hålls /aiz/ utanför robots och besöksstatistiken, och
därför är koderna kortlivade.
"""

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

#: Utan 0/O, 1/I/L - koden ska gå att läsa upp och skriva av.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def hash_code(code):
    normalized = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return hashlib.sha256(f"{settings.SECRET_KEY}:aidocs:{normalized}".encode()).hexdigest()


def generate_code():
    body = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"ADX-{body[:4]}-{body[4:]}"


class AccessCode(models.Model):
    code_hash = models.CharField(max_length=64, unique=True)
    # Sista fyra tecknen, för att känna igen koden i listan.
    hint = models.CharField(max_length=8)
    note = models.CharField("Till vad", max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    uses = models.PositiveIntegerField(default=0)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "AI-kod"
        verbose_name_plural = "AI-koder"

    def __str__(self):
        return f"...{self.hint}"

    @property
    def is_valid(self):
        return self.revoked_at is None and self.expires_at > timezone.now()

    @classmethod
    def issue(cls, user, hours=1, note=""):
        """Skapar en kod och returnerar (objekt, klartext). Klartexten sparas aldrig."""
        code = generate_code()
        obj = cls.objects.create(
            code_hash=hash_code(code),
            hint=code[-4:],
            note=note[:120],
            created_by=user,
            expires_at=timezone.now() + timezone.timedelta(hours=hours),
        )
        return obj, code

    @classmethod
    def verify(cls, raw):
        """Giltig kod eller None. Jämförelsen sker på hashen, aldrig på klartext."""
        if not raw:
            return None
        obj = cls.objects.filter(code_hash=hash_code(raw)).first()
        return obj if obj is not None and obj.is_valid else None


class AccessLog(models.Model):
    code = models.ForeignKey(AccessCode, on_delete=models.CASCADE, related_name="log")
    guide = models.CharField(max_length=80)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.guide} {self.at:%Y-%m-%d %H:%M}"
