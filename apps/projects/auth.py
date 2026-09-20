"""
Lösenordsfri inloggning i kundportalen: e-post -> engångskod på mejl -> in.

Kunderna har inga lösenord alls (kontona skapas med set_unusable_password).
Regler:
- Bara portalkontakter får koder: aktiva användare utan staff-flagga som är
  kopplade till en AKTIV kund. Byrån loggar in i /manage/ som vanligt.
- Samma svar oavsett om adressen finns eller inte (ingen uppräkning).
- Koden hashas med SECRET_KEY som salt, gäller tio minuter, fem försök,
  och högst fem koder per adress och timme.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import LoginCode

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
MAX_CODES_PER_HOUR = 5


def contact_for_email(email):
    """Portalkontakten med adressen, eller None. Aldrig byråns konton."""
    email = (email or "").strip().lower()
    if not email:
        return None
    user = (
        get_user_model()
        .objects.filter(email__iexact=email, is_active=True, is_staff=False)
        .order_by("pk")
        .first()
    )
    if user is None or not user.customers.filter(is_active=True).exists():
        return None
    return user


def _hash(user, code):
    return hashlib.sha256(f"{settings.SECRET_KEY}:{user.pk}:{code}".encode()).hexdigest()


def issue_code(user):
    """
    Skapa och returnera en ny kod (klartext, för mejlet). None om adressen
    slagit i taket för timmen - då skickas inget, och svaret ser likadant ut.
    """
    now = timezone.now()
    recent = LoginCode.objects.filter(user=user, created_at__gte=now - timedelta(hours=1)).count()
    if recent >= MAX_CODES_PER_HOUR:
        return None
    # En kod i taget: den gamla dör när en ny begärs.
    LoginCode.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
    code = f"{secrets.randbelow(10**6):06d}"
    LoginCode.objects.create(user=user, code_hash=_hash(user, code), expires_at=now + CODE_TTL)
    return code


def verify_code(user, code):
    """Sant om koden är användarens senaste, giltiga, oanvända kod. Förbrukar den."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    entry = (
        LoginCode.objects.filter(user=user, used_at__isnull=True, expires_at__gt=timezone.now())
        .order_by("-created_at")
        .first()
    )
    if entry is None or len(code) != 6:
        return False
    entry.attempts += 1
    if entry.attempts > MAX_ATTEMPTS:
        entry.used_at = timezone.now()
        entry.save(update_fields=["attempts", "used_at"])
        return False
    if not hmac.compare_digest(entry.code_hash, _hash(user, code)):
        entry.save(update_fields=["attempts"])
        return False
    entry.used_at = timezone.now()
    entry.save(update_fields=["attempts", "used_at"])
    return True
