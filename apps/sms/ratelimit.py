"""
Gränserna (README: Gränser).

    SMS_RATE_PER_SECOND    alla anrop, per nyckel och sekund (standard 20)
    SMS_RATE_PER_MINUTE    sms per minut, per nyckel och per konto (standard 60)
    SMS_GLOBAL_PER_MINUTE  sms per minut för alla kunder tillsammans (standard 80)
    SMS_DAILY_MAX_PER_KEY  sms per nyckel och svenskt dygn (standard 5000)

Sekundgränsen och minutgränsen per nyckel räknas i Djangos cache (fasta
fönster). Med processlokal cache gäller de per arbetare, så de är ett skydd
mot en loop hos kunden, inte en exakt kvot; minutgränsen per nyckel räknar
också provkörningar.

Minutgränsen per konto och byråns gräns räknas i databasen, över alla
arbetare (check_account_minute): sms-raderna de senaste 60 sekunderna. 46elks
släpper igenom 100 sms i minuten för hela byråns konto, och Flamingos sms går
via samma konto; byråns gräns lämnar 20 i minuten åt dem. Kontrollen görs
under kontots radlås, så kontots gräns är exakt; byråns kan passeras med
högst så många sms som skickas samtidigt från olika konton.

Dygnsgränsen räknas också i databasen (sms-raderna nyckeln skapat sedan
midnatt) och gäller exakt.

Kostnaden skyddas inte här utan av kostnadstaket (service.py).
"""

import math
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import SmsMessage
from .pricing import STOCKHOLM


def limits():
    """Kundens gränser (API:ts usage och dokumentationen)."""
    return {
        "per_second": int(getattr(settings, "SMS_RATE_PER_SECOND", 20)),
        "per_minute": int(getattr(settings, "SMS_RATE_PER_MINUTE", 60)),
        "per_day": int(getattr(settings, "SMS_DAILY_MAX_PER_KEY", 5000)),
    }


def global_per_minute():
    """Alla kunders sms per minut tillsammans, under 46elks gräns för kontot."""
    return int(getattr(settings, "SMS_GLOBAL_PER_MINUTE", 80))


def _hit(name, window, limit, now):
    """Räkna anropet i fönstret. Sekunder kvar av fönstret om gränsen är
    passerad, annars None."""
    if limit <= 0:
        return None
    slot = int(now.timestamp()) // window
    key = f"sms:rl:{name}:{window}:{slot}"
    cache.add(key, 0, timeout=window + 1)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window + 1)
        count = 1
    if count > limit:
        return max(1, window - int(now.timestamp()) % window)
    return None


def check_burst(api_key, now=None):
    now = now or timezone.now()
    return _hit(f"k{api_key.pk}", 1, limits()["per_second"], now)


def check_send(api_key, now=None):
    """Minutgränsen och dygnsgränsen för en sändning eller provkörning."""
    now = now or timezone.now()
    retry = _hit(f"k{api_key.pk}:send", 60, limits()["per_minute"], now)
    if retry:
        return retry
    local = timezone.localtime(now, STOCKHOLM)
    midnight = datetime.combine(local.date(), time(0, 0), tzinfo=STOCKHOLM)
    sent_today = SmsMessage.objects.filter(api_key=api_key, created_at__gte=midnight).count()
    if sent_today >= limits()["per_day"]:
        return int((midnight + timedelta(days=1) - now).total_seconds()) or 1
    return None


def _window_retry(rows, limit, now):
    """Sekunder tills en plats blir ledig, om raderna redan fyller gränsen
    för minuten, annars None."""
    if limit <= 0 or rows.count() < limit:
        return None
    oldest = rows.order_by("-created_at").values_list("created_at", flat=True)[limit - 1]
    return max(1, math.ceil(60 - (now - oldest).total_seconds()))


def check_account_minute(account, now=None):
    """Exakt minutgräns i databasen, för en sändning (inte en provkörning).
    Körs under kontots radlås i service.send.

    - Kontot: dess sms de senaste 60 sekunderna, utom de som stoppades före
      46elks som rejected, mot SMS_RATE_PER_MINUTE. Gäller oavsett hur många
      nycklar och arbetare kunden använder.
    - Byrån: alla kunders sms som gick till 46elks (inte rejected eller
      blocked_cap), mot SMS_GLOBAL_PER_MINUTE.

    Returnerar Retry-After i sekunder, eller None."""
    now = now or timezone.now()
    recent = SmsMessage.objects.filter(created_at__gt=now - timedelta(seconds=60))
    own = recent.filter(account=account).exclude(status=SmsMessage.Status.REJECTED)
    retry = _window_retry(own, limits()["per_minute"], now)
    if retry:
        return retry
    everyone = recent.exclude(status__in=SmsMessage.STOPPED)
    return _window_retry(everyone, global_per_minute(), now)
