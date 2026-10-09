"""
Återhämtningen efter en krasch (README D.5), tickens fas 1.

    recover_stale(now=None) -> dict      mottagare som stått i sending i mer än
                                         fem minuter: adopterade eller tillbaka i kön
    stale_exists(now=None) -> bool       work_exists

En sms-mottagare i sending har tagits av slingan men processen dog innan
utfallet skrevs. Sms:et kan ha skapats i apps/sms: dess reference är
"~u<utskick>:<mottagare>" (sms_wrapper.internal_reference, som API:t aldrig
kan använda) och hålls per konto så länge sms:et inte är stoppat. Bara
raden som uppfyller villkoret i sms_msg_unique_reference (status inte
rejected eller blocked_cap, felkod inte provider_error), har källan
utskick eller flöde och går till mottagarens nummer adopteras:

    sent, delivered, failed (46elks tog emot)   mottagaren följer sms:et (sent och framåt)
    reserved (svaret kom aldrig)                unknown; byråns avstämning (needs_check)
                                                avgör, och sms:et skickas aldrig igen
    ingen sådan rad                             tillbaka i kön (försöket står kvar;
                                                efter MAX_ATTEMPTS blir den failed)

E-post (S3): en mottagare i sending blir unknown och skickas aldrig igen.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.sms.models import SmsMessage

from ..models import CHANNEL_SMS, Recipient, Utskick
from . import sms as sms_loop
from . import sms_wrapper

logger = logging.getLogger(__name__)

RS = Recipient.Status
#: En mottagare i sending längre än så här har övergetts av en död process.
STALE_AFTER = timedelta(minutes=5)
BATCH = 500
GAVE_UP_TEXT = "Sms:et kunde inte skickas."


def _stale(now):
    return Recipient.objects.filter(status=RS.SENDING, claimed_at__lt=now - STALE_AFTER)


def stale_exists(now=None):
    return _stale(now or timezone.now()).exists()


def held_message(recipient):
    """Sms:et som håller mottagarens reference (villkoret i
    sms_msg_unique_reference), med källan utskick eller flöde och till
    mottagarens nummer, eller None."""
    account = recipient.utskick.account
    return (
        SmsMessage.objects.filter(
            account__customer_id=account.customer_id,
            reference=sms_wrapper.internal_reference(f"u{recipient.utskick_id}:{recipient.pk}"),
            source__in=(SmsMessage.Source.UTSKICK, SmsMessage.Source.FLOW),
            to=recipient.address,
        )
        .exclude(status__in=SmsMessage.STOPPED)
        .exclude(error_code="provider_error")
        .order_by("-pk")
        .first()
    )


def recover_stale(now=None):
    """Fas 1. Klockans tid jämförs med claimed_at (verklig tid).
    {"adopted", "unknown", "requeued", "failed"}."""
    real_now = timezone.now()
    counts = {"adopted": 0, "unknown": 0, "requeued": 0, "failed": 0}
    while True:
        requeued = []
        with transaction.atomic():
            rows = list(
                _stale(real_now)
                .select_for_update(skip_locked=True, of=("self",))
                .select_related("utskick", "utskick__account")
                .order_by("pk")[:BATCH]
            )
            if not rows:
                break
            for recipient in rows:
                if recipient.channel != CHANNEL_SMS:
                    Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
                        status=RS.UNKNOWN
                    )
                    counts["unknown"] += 1
                    continue
                message = held_message(recipient)
                if message is not None:
                    result = sms_loop.adopt(recipient, message, message.sender, real_now)
                    counts["unknown" if result == RS.UNKNOWN else "adopted"] += 1
                    continue
                if recipient.attempts >= sms_loop.MAX_ATTEMPTS:
                    Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
                        status=RS.FAILED, error=GAVE_UP_TEXT, claimed_at=None
                    )
                    counts["failed"] += 1
                    continue
                Recipient.objects.filter(pk=recipient.pk, status=RS.SENDING).update(
                    status=RS.QUEUED, claimed_at=None
                )
                requeued.append(recipient.pk)
                counts["requeued"] += 1
            # Som sms.requeue: i ett utskick som hunnit avbrytas blir de
            # cancelled, annars stod de i kön för alltid (cancel tog bara de
            # som redan var köade).
            Recipient.objects.filter(
                pk__in=requeued, status=RS.QUEUED, utskick__status=Utskick.Status.CANCELLED
            ).update(status=RS.CANCELLED, not_before=None)
        if len(rows) < BATCH:
            break
    total = sum(counts.values())
    if total:
        logger.warning("Utskick: %s mottagare återhämtade efter avbruten sändning", total)
    return {k: v for k, v in counts.items() if v}


def month_end_check(now=None):
    """utskick_daily den 1:a (02.45, före sms_close_month 03.10, D.5):
    sms från utskicken (källa annat än api) som står reserverade eller
    väntar på avstämning i förra månaden stoppar månadens underlag för
    kontot. Byrån larmas en gång om dagen med antalet; avstämningen görs på
    /manage/sms/#kontrollera. Antalet sms (0 andra dagar)."""
    from django.db.models import Q

    from apps.sms import pricing

    from .. import alerts

    now = now or timezone.now()
    if pricing.local_today(now).day != 1:
        return 0
    start, end = pricing.month_bounds(pricing.previous_month(pricing.current_period(now)))
    rows = (
        SmsMessage.objects.filter(created_at__gte=start, created_at__lt=end)
        .exclude(source=SmsMessage.Source.API)
        .filter(Q(status=SmsMessage.Status.RESERVED) | Q(needs_check=True))
    )
    count = rows.count()
    if count:
        alerts.agency(
            "Utskick: sms att stämma av före månadens underlag",
            [
                f"{count} sms från utskicken står reserverade eller väntar på avstämning.",
                "Underlaget för kontot stängs inte förrän de är avstämda: "
                "/manage/sms/#kontrollera.",
            ],
            once="month_end_reserved",
            window="day",
            now=now,
        )
    return count
