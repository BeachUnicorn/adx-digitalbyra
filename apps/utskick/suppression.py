"""
Spärrlistan (README B.1 och H.6): per kund och kanal, nycklad på adressens
hash (keys.value_hash). Överlever att kontakten tas bort och vinner alltid:
ingen import, ingen kund och ingen byrå tar bort en spärr. Bara personen
själv, bevisat med en bekräftelse (DOI-klick, bekräftelselänk efter START,
Ångra inom 30 minuter, Mina utskick plus bekräftelse), via
consent.set_status(..., proved=True).

    is_suppressed(account, channel, value=None, value_hash=None)
    suppressed_hashes(account, channel, hashes)     mängden spärrade bland hashes
    add(account, channel, value_hash, reason, note="")  -> (Suppression, created)
    suppress(account, channel, value, reason=..., source=..., actor=...)
                                    spärra en adress och avregistrera kontakten som har den
    lift(account, channel, value_hash)  bara från consent.set_status med proved=True

En spärr stoppar varje utskick och flödesmeddelande på kanalen från kunden,
reklam och information. "Vill inte ha erbjudanden" (declined) är ingen spärr.
"""

from django.db import IntegrityError, transaction

from . import keys
from .models import Suppression


def is_suppressed(account, channel, value=None, value_hash=None):
    """Finns adressen (eller hashen) på kontots spärrlista för kanalen?
    apps/sms använder den här för GET /api/sms/v1/suppressions/ (S2)."""
    if value_hash is None:
        value_hash = keys.value_hash(channel, value)
    if not value_hash:
        return False
    return Suppression.objects.filter(
        account=account, channel=channel, value_hash=value_hash
    ).exists()


def suppressed_hashes(account, channel, hashes):
    """De hashar i hashes som är spärrade, med en fråga (frysningen, importen)."""
    wanted = {h for h in hashes if h}
    if not wanted:
        return set()
    return set(
        Suppression.objects.filter(
            account=account, channel=channel, value_hash__in=wanted
        ).values_list("value_hash", flat=True)
    )


def add(account, channel, value_hash, reason, note="", now=None):
    """Lägg till en spärr (eller låt den som finns stå). Returnerar
    (Suppression, created). Kräver att processens nycklar stämmer."""
    if not value_hash:
        raise ValueError("En spärr behöver en adress.")
    keys.require_fingerprints()
    defaults = {"reason": reason, "note": (note or "")[:200]}
    if now is not None:
        defaults["created_at"] = now
    try:
        with transaction.atomic():
            return Suppression.objects.get_or_create(
                account=account, channel=channel, value_hash=value_hash, defaults=defaults
            )
    except IntegrityError:
        # Två samtidiga spärrar av samma adress: den andra hittar den första.
        row = Suppression.objects.get(account=account, channel=channel, value_hash=value_hash)
        return row, False


def lift(account, channel, value_hash):
    """Ta bort en spärr. Anropas bara av consent.set_status när personen
    själv bevisat att den vill ha utskick igen. True om en spärr fanns."""
    keys.require_fingerprints()
    deleted, _ = Suppression.objects.filter(
        account=account, channel=channel, value_hash=value_hash
    ).delete()
    return bool(deleted)


def suppress(
    account,
    channel,
    value,
    reason,
    source,
    source_detail="",
    actor=None,
    note="",
    ip_hash="",
    now=None,
):
    """Spärra en adress och sätt samtycket till unsubscribed på kontakten
    som har den (om någon). value är den normaliserade adressen (E.164 eller
    e-post). Returnerar (Suppression, contact eller None).

    Fungerar också när utskick är avstängt för kontot: en avregistrering
    ska alltid gå igenom (D.8)."""
    from . import consent
    from .models import Contact

    value_hash = keys.value_hash(channel, value)
    with transaction.atomic():
        row, _ = add(account, channel, value_hash, reason, note=note, now=now)
        field = "phone" if channel == "sms" else "email"
        clean = keys.clean_value(channel, value)
        contact = Contact.objects.filter(account=account, **{field: clean}).first()
        if contact is not None:
            consent.set_status(
                contact,
                channel,
                consent.UNSUBSCRIBED,
                source=source,
                source_detail=source_detail,
                actor=actor,
                suppression_reason=reason,
                ip_hash=ip_hash,
                now=now,
            )
    return row, contact
