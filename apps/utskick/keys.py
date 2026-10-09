"""
Nycklarna (H.7): hashen av adresser och signaturerna på länkar.

    value_hash(channel, value)      HMAC-SHA256(UTSKICK_HASH_KEY, "sms:+4670...")
                                    eller "email:anna@exempelror.example", 64 hex
    link_digest(message)            HMAC-SHA256(UTSKICK_LINK_KEY, message), råa byte
                                    (tokens.py bygger signaturerna på den)
    check_fingerprints()            True när nycklarna stämmer med Switchboard
    require_fingerprints()          samma, men KeyMismatch när de inte gör det

Spärrlistan, samtyckena och samtyckesloggen nycklas på value_hash, aldrig
på själva adressen. Värdet ska vara normaliserat först (normalize.phone ger
E.164, normalize.email gemener med plustaggen kvar); value_hash rensar bara
blanksteg och gör e-post till gemener, så samma adress alltid får samma hash.

Fingeravtrycken: första gången sparas HMAC(nyckel, "utskick-fingerprint")
på Switchboard. Varje process jämför innan den skriver samtycken eller
spärrar eller skickar något (webbarbetarna vid första sådana skrivning,
ticken vid start). Stämmer det inte nekas skrivningen, felet loggas och
byrån larmas. Det fångar arbetare som behållit en gammal systemd-miljö
medan cron-ticken läser den nya .env.
"""

import hashlib
import hmac
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

FINGERPRINT_MESSAGE = b"utskick-fingerprint"
MISMATCH_TEXT = "Nyckeln för spärrlistan skiljer sig mellan processerna."

#: Fingeravtrycken som den här processen redan har prövat mot databasen.
_verified = set()


class KeyMismatch(RuntimeError):
    """Processens nyckel är inte den som skrev spärrarna."""


def _key(name):
    value = getattr(settings, name, "")
    if not value:
        raise KeyMismatch(f"{name} saknas.")
    return value.encode() if isinstance(value, str) else value


def clean_value(channel, value):
    """Adressen som hashas: utan blanksteg runt, e-post i gemener."""
    text = str(value or "").strip()
    return text.lower() if channel == "email" else text


def value_hash(channel, value):
    """HMAC-SHA256 av f"{kanal}:{adress}" med UTSKICK_HASH_KEY, 64 hextecken.
    Tom adress ger en tom sträng (inget att spärra)."""
    text = clean_value(channel, value)
    if not text:
        return ""
    message = f"{channel}:{text}".encode()
    return hmac.new(_key("UTSKICK_HASH_KEY"), message, hashlib.sha256).hexdigest()


def link_digest(message):
    """HMAC-SHA256 med UTSKICK_LINK_KEY. Aldrig SECRET_KEY (E.2)."""
    if isinstance(message, str):
        message = message.encode()
    return hmac.new(_key("UTSKICK_LINK_KEY"), message, hashlib.sha256).digest()


def fingerprints():
    """(hash, link): nycklarnas fingeravtryck i den här processen."""
    return tuple(
        hmac.new(_key(name), FINGERPRINT_MESSAGE, hashlib.sha256).hexdigest()
        for name in ("UTSKICK_HASH_KEY", "UTSKICK_LINK_KEY")
    )


def check_fingerprints(alert=True):
    """True när processens nycklar är de som står på Switchboard (sparas
    första gången). False vid en avvikelse: loggas och, med alert, larmar
    byrån högst en gång i timmen."""
    from django.db import transaction

    from .models import Switchboard

    current = fingerprints()
    if current in _verified:
        return True
    row = Switchboard.get_solo()
    stored = (row.hash_fingerprint, row.link_fingerprint)
    if not all(stored):
        # Första gången: spara under radlås, så att två processer som
        # startar samtidigt inte skriver var sitt avtryck.
        with transaction.atomic():
            row = Switchboard.objects.select_for_update().get(pk=Switchboard.SOLO_PK)
            fields = []
            if not row.hash_fingerprint:
                row.hash_fingerprint = current[0]
                fields.append("hash_fingerprint")
            if not row.link_fingerprint:
                row.link_fingerprint = current[1]
                fields.append("link_fingerprint")
            if fields:
                row.save(update_fields=fields)
            stored = (row.hash_fingerprint, row.link_fingerprint)
    ok = stored == current
    if ok:
        _verified.add(current)
        return True
    which = [
        name
        for name, mine, theirs in zip(
            ("UTSKICK_HASH_KEY", "UTSKICK_LINK_KEY"), current, stored, strict=True
        )
        if theirs and mine != theirs
    ]
    logger.error("Utskick: fel nyckel i processen (%s). Skrivningar och sändningar nekas.", which)
    if alert:
        from . import alerts

        alerts.agency(
            "Utskick: " + MISMATCH_TEXT,
            [
                MISMATCH_TEXT,
                "Gäller: " + ", ".join(which) + ".",
                "Samtycken, spärrar och sändningar nekas tills processerna har samma .env.",
                "Kör systemctl restart adx efter att .env ändrats.",
            ],
            once="key_mismatch",
        )
    return False


def require_fingerprints():
    """Före varje skrivning av samtycken och spärrar: KeyMismatch om
    processens nycklar inte är de som står på Switchboard."""
    if not check_fingerprints():
        raise KeyMismatch(MISMATCH_TEXT)


def forget_verified():
    """Testerna: glöm att den här processen redan prövat sina nycklar."""
    _verified.clear()
