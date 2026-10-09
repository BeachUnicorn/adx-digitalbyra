"""
Sms-koderna på k.adx.se (README E.2, B.2 LinkCode): sex tecken ur
[A-Za-z0-9] med secrets.choice. Bara GSM-7:s grundtecken, så en länk tvingar
aldrig sms:et till UCS-2. 62^6 = 56,8 miljarder; koderna är
skiftlägeskänsliga (Postgres standardkollation).

    new_code() -> str                 en kod
    new_codes(n) -> list[str]         n olika koder (frysningen tar dem i omgångar)
    taken(codes) -> set               de av koderna som redan finns
    create_confirm(account, *, value_hash, purpose, contact=None, now=None) -> LinkCode
                                      en bekräftelsekod (/b/, 24 timmar), med nytt
                                      försök vid krock, högst MAX_TRIES gånger
    find(code, kind) -> LinkCode | None

Frysningen (sending/freeze.py, D.3) lägger sina klick- och personkoder med
bulk_create utan ignore_conflicts i en savepoint och drar nya vid krock
(högst MAX_TRIES gånger, sedan misslyckas biten och tas om nästa tick).
Bekräftelsekoderna (START, Mina val, anmälan med sms) går genom
create_confirm. Koden skrivs aldrig i loggar eller i __str__.
"""

import secrets
import string
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import CHANNEL_SMS, LinkCode

ALPHABET = string.ascii_letters + string.digits
LENGTH = 6
MAX_TRIES = 3


class CodeCollision(RuntimeError):
    """Ingen ledig kod efter MAX_TRIES försök (ska i praktiken aldrig hända)."""


def new_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def new_codes(n):
    """n olika koder. Krockar med befintliga rader prövas av den som sparar
    (unik kolumn), inte här: en fråga per omgång vore för dyr."""
    codes = set()
    while len(codes) < n:
        codes.add(new_code())
    return list(codes)


def taken(codes):
    return set(LinkCode.objects.filter(code__in=list(codes)).values_list("code", flat=True))


def create_confirm(account, *, value_hash, purpose, contact=None, now=None):
    """En bekräftelsekod (kind confirm) för /b/<kod>, giltig
    LinkCode.CONFIRM_HOURS timmar. purpose: LinkCode.Purpose (signup, start,
    pref_on)."""
    now = now or timezone.now()
    for _ in range(MAX_TRIES):
        try:
            with transaction.atomic():
                return LinkCode.objects.create(
                    code=new_code(),
                    kind=LinkCode.Kind.CONFIRM,
                    account=account,
                    channel=CHANNEL_SMS,
                    value_hash=value_hash,
                    contact=contact,
                    purpose=purpose,
                    expires_at=now + timedelta(hours=LinkCode.CONFIRM_HOURS),
                    created_at=now,
                )
        except IntegrityError:
            continue
    raise CodeCollision("Ingen ledig sms-kod.")


def find(code, kind):
    """Raden för koden av sorten kind, eller None. Utgång och användning
    prövar vyn (den ska svara likadant för okänd och gammal kod, E.3)."""
    if not code or len(code) != LENGTH:
        return None
    return LinkCode.objects.filter(code=code, kind=kind).first()
