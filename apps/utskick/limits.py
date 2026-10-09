"""
Exakta gränser i databasen (Counter) och utskickens rådgivande lås.

    hit(scope, key, window, limit)  räknar upp och svarar om gränsen är passerad
    count(scope, key, window)       läser utan att räkna upp
    hour_window(now), day_window(now)   fönstrets början (timme, svenskt dygn)
    purge(before)                   tar bort gamla rader (utskick_daily)
    lock_contacts(account)          kontaktgränsens lås, resten av transaktionen

Fasta fönster, inte rullande. LocMem-cachen är per arbetare och duger inte
för gränser som skyddar adresser och kostnader; en rad per (scope, nyckel,
fönster) med INSERT ... ON CONFLICT DO UPDATE ... RETURNING är en enda fråga
och räknar rätt mellan processer.

Scopen som används (Counter.scope, högst 20 tecken): link_miss, signup_ip,
signup_account, optin_addr, optin_addr_all, optin_hour, optin_account_hour,
link_check, export, inbound_mail, test_send, alert. Nyckeln är en ip_hash,
ett konto-id, f"{account}:{value_hash}" eller "" för hela ADX; aldrig en
adress i klartext.

Låsens nycklar (pg_advisory_xact_lock), bredvid flamingo.limits._LOCK_SPACE
(0x464C << 32) och flamingo.media._LOCK_MEDIA (0x464D << 32):

    TICK_LOCK            0x5554 << 32               utskick_tick (pg_try_advisory_lock)
    ADX_MAIL_LOCK        (0x5555 << 32) + konto     ADX-domänens tak per kund (S3)
    CONTACT_LIMIT_LOCK   (0x5556 << 32) + konto     kontaktgränsen per kund
"""

from datetime import timedelta

from django.db import connection
from django.utils import timezone

TICK_LOCK = 0x5554 << 32
ADX_MAIL_LOCK = 0x5555 << 32
CONTACT_LIMIT_LOCK = 0x5556 << 32

#: Räknarna behövs inte längre än så (utskick_daily rensar).
KEEP = timedelta(days=2)


def hour_window(now=None):
    """Början på timmen (UTC-tid i databasen, samma tidpunkt överallt)."""
    now = now or timezone.now()
    return now.replace(minute=0, second=0, microsecond=0)


def day_window(now=None):
    """Midnatt i Stockholm för dagen now faller på, som en medveten tid."""
    from apps.sms.pricing import STOCKHOLM

    local = timezone.localtime(now or timezone.now(), STOCKHOLM)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def hit(scope, key, window, limit):
    """Räkna en träff. True när gränsen är passerad (räknaren > limit),
    alltså att det här försöket ska nekas. Träffen räknas ändå, så att den
    som fortsätter hamra stannar över gränsen hela fönstret."""
    with connection.cursor() as cursor:
        cursor.execute(
            'INSERT INTO utskick_counter ("scope", "key", "window", "count") '
            "VALUES (%s, %s, %s, 1) "
            'ON CONFLICT ("scope", "key", "window") '
            'DO UPDATE SET "count" = utskick_counter."count" + 1 '
            'RETURNING "count"',
            [str(scope)[:20], str(key or "")[:80], window],
        )
        (count,) = cursor.fetchone()
    return count > limit


def count(scope, key, window):
    from .models import Counter

    row = Counter.objects.filter(scope=scope, key=str(key or "")[:80], window=window).first()
    return row.count if row else 0


def purge(before=None):
    """Ta bort räknare vars fönster började före before (standard: två dygn
    sedan). Antal borttagna rader."""
    from .models import Counter

    before = before or timezone.now() - KEEP
    deleted, _ = Counter.objects.filter(window__lt=before).delete()
    return deleted


def lock_contacts(account):
    """Kontaktgränsens lås för kontot, resten av transaktionen. Två importer
    eller en import och en anmälan räknar då inte förbi contact_limit."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [CONTACT_LIMIT_LOCK + int(account.pk)])
