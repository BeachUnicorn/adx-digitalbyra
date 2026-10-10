"""
SQS-köerna i eu-west-1 (README D.7, G.3): SES-händelserna och de
inkommande mejlen, lästa av tickens fas 2. Inget HTTPS-anrop från AWS och
ingen SNS-signaturkod: SQS är IAM-skyddat (H.8), och inget går förlorat
medan adx.se ligger nere eller rullas tillbaka.

    queue_url("events" | "inbound") -> str   ur inställningarna, eller ""
    events_enabled(), inbound_enabled(), enabled()
    dlq_url(url) -> url + "-dlq"
    poll_due(now) -> bool             varje tick när ett mejl (utskick eller
                                      bekräftelse) skickats senaste 72 timmarna
                                      eller ett inkommande mejl väntar, annars var
                                      femte minut (Switchboard.last_queue_poll_at);
                                      bara när köerna är satta och email_ready_at
                                      eller doi_ready_at finns
    poll(now, deadline) -> dict       {"events", "inbound", "duplicates", "ignored",
                                      "failed", "bad", ...}: ReceiveMessage
                                      (MaxNumberOfMessages=10, WaitTimeSeconds=0,
                                      VisibilityTimeout=60) per kö tills deadline; per
                                      meddelande EN transaction.atomic(): EventReceipt(key)
                                      (en dubblett betyder klart), effekterna
                                      (events.apply eller inbound.email.receive),
                                      commit, sedan DeleteMessage. Ett undantag lämnar
                                      meddelandet kvar (efter fem mottagningar DLQ:n).
                                      Sist inbound.email.process_pending(now, deadline)
                                      (hämtningen ur S3 efter commit).
    dlq_counts() -> dict              {"events": n, "events_dlq": n, "inbound": n,
                                      "inbound_dlq": n} (GetQueueAttributes), bara köer
                                      som är satta; None för en kö som inte gick att läsa
    redrive(which) -> str             StartMessageMoveTask från DLQ:n tillbaka till kön
                                      ("events" eller "inbound"); manage:utskick_dlq
    check_dlq(now=None) -> dict       utskick_daily: larmar byrån när en DLQ har något

Klienten är aws.client("sqs") (api-klienten med omförsök). boto3 läses in
först när en kö faktiskt ska läsas; poll_due rör bara databasen.
"""

import json
import logging
import time
from collections import Counter as Tally
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Köernas namn i inställningarna, och deras korta namn i svaren.
QUEUES = (("events", "UTSKICK_SQS_EVENTS_URL"), ("inbound", "UTSKICK_SQS_INBOUND_URL"))
#: Hur ofta köerna läses när inget mejl skickats på 72 timmar (D.7).
POLL_IDLE = timedelta(minutes=5)
#: Så länge efter ett skickat mejl läses köerna varje tick (D.7).
POLL_BUSY_AFTER_SEND = timedelta(hours=72)
#: ReceiveMessage (D.7).
MAX_MESSAGES = 10
VISIBILITY_TIMEOUT = 60
#: Så här mycket tid måste finnas kvar för att hämta fler meddelanden.
SAFETY_SECONDS = 1
#: Prefixet på kvittot för ett inkommande mejl (EventReceipt.key).
INBOUND_PREFIX = "in:"


def queue_url(which):
    """Kön ("events" eller "inbound") ur inställningarna, eller ""."""
    names = dict(QUEUES)
    return (getattr(settings, names[which], "") or "").strip()


def events_enabled():
    """SES-händelserna läses bara när kön är satt (D.7)."""
    return bool(queue_url("events"))


def inbound_enabled():
    """De inkommande mejlen kräver både kön och hinken (G.3)."""
    return bool(queue_url("inbound")) and bool(
        (getattr(settings, "UTSKICK_SES_INBOUND_BUCKET", "") or "").strip()
    )


def enabled():
    """Finns det någon kö att läsa alls?"""
    return events_enabled() or inbound_enabled()


def dlq_url(url):
    """DLQ:n för en kö: samma adress med -dlq sist (server/aws-utskick-s3.sh
    skapar dem så: adx-utskick-events och adx-utskick-events-dlq)."""
    url = str(url or "").rstrip("/")
    return f"{url}-dlq" if url else ""


def _active():
    """Köerna att läsa, i ordning: [(namn, adress)]."""
    found = []
    if events_enabled():
        found.append(("events", queue_url("events")))
    if inbound_enabled():
        found.append(("inbound", queue_url("inbound")))
    return found


# ---------------------------------------------------------------------------
# När köerna läses
# ---------------------------------------------------------------------------


def _busy(now):
    """Ett mejl skickat de senaste 72 timmarna (ett utskick eller ett
    bekräftelsemejl), eller ett inkommande mejl som väntar på att hämtas."""
    from ..models import CHANNEL_EMAIL, Consent, Recipient

    since = now - POLL_BUSY_AFTER_SEND
    if Recipient.objects.filter(
        channel=CHANNEL_EMAIL, sent_at__gte=since, simulated=False
    ).exists():
        return True
    if Consent.objects.filter(channel=CHANNEL_EMAIL, confirm_sent_at__gte=since).exists():
        return True
    if inbound_enabled():
        from . import email as inbound_email

        return inbound_email.pending_exists()
    return False


def poll_due(now):
    """Ska tickens fas 2 läsa köerna nu? (modulens text)"""
    if not enabled():
        return False
    from ..models import Switchboard

    switch = Switchboard.objects.filter(pk=Switchboard.SOLO_PK).first()
    if switch is None or not (switch.email_ready_at or switch.doi_ready_at):
        return False
    if _busy(now):
        return True
    last = switch.last_queue_poll_at
    return last is None or now - last >= POLL_IDLE


# ---------------------------------------------------------------------------
# Läsningen
# ---------------------------------------------------------------------------


def _client():
    from .. import aws

    return aws.client("sqs")


def _receipt(key):
    """Kvittot i den pågående transaktionen. False när det redan fanns."""
    from ..models import EventReceipt

    try:
        with transaction.atomic():
            EventReceipt.objects.create(key=key[:140])
    except IntegrityError:
        return False
    return True


def _event(body, now):
    """En SES-händelse: kvittot och effekterna i en transaktion."""
    from . import events

    key = events.receipt_key(body)
    if not key:
        return "ignored"
    with transaction.atomic():
        if not _receipt(key):
            return "duplicates"
        result = events.apply(body, now)
    return "events" if result == events.APPLIED else result


def _inbound(body, now):
    """En notis om ett inkommande mejl: kvittot och raden i en transaktion."""
    from . import email as inbound_email

    message_id = str(((body.get("mail") or {}).get("messageId")) or "").strip()
    if not message_id:
        return "ignored"
    with transaction.atomic():
        if not _receipt(f"{INBOUND_PREFIX}{message_id}"):
            return "duplicates"
        row = inbound_email.receive(body, now)
    return "inbound" if row is not None else "ignored"


#: Textraden SES skickar till SNS-ämnet när ett händelsemål skapas.
SES_VALIDATION_TEXT = "Successfully validated SNS topic for Amazon SES event publishing"


def _unwrap(body):
    """Kroppen är SES-notisen själv (RawMessageDelivery). Kommer den ändå i
    ett SNS-kuvert packas det upp."""
    if body.get("Type") == "Notification" and isinstance(body.get("Message"), str):
        try:
            inner = json.loads(body["Message"])
        except ValueError:
            return body
        return inner if isinstance(inner, dict) else body
    return body


def handle(which, message, now):
    """Ett meddelande ur kön. Svarar utfallet; "failed" betyder att
    meddelandet ligger kvar (det kommer tillbaka efter VisibilityTimeout)."""
    raw = message.get("Body") or ""
    try:
        body = json.loads(raw)
    except ValueError:
        if raw.strip().startswith(SES_VALIDATION_TEXT):
            # SES lägger en vanlig textrad i ämnet när konfigurationssetets
            # händelsemål skapas eller ändras (aws-utskick-s3.sh). Väntat,
            # inget fel.
            logger.info("Utskick: SES bekräftade händelsemålet för kön %s", which)
            return "bad"
        logger.error("Utskick: ett meddelande i kön %s var inte JSON och släpps", which)
        return "bad"
    if not isinstance(body, dict):
        return "bad"
    body = _unwrap(body)
    from .. import keys

    try:
        if which == "events":
            return _event(body, now)
        return _inbound(body, now)
    except keys.KeyMismatch:
        raise
    except Exception:
        logger.exception("Utskick: ett meddelande i kön %s gick inte (ligger kvar)", which)
        return "failed"


def _delete(client, url, message):
    try:
        client.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
    except Exception as exc:  # noqa: BLE001 - kvittot gör att det inte görs två gånger
        logger.warning("Utskick: meddelandet kunde inte tas bort ur kön (%s)", type(exc).__name__)
        return False
    return True


def poll(now, deadline):
    """Läs köerna tills deadline (time.monotonic()) och hämta sedan de
    inkommande mejlen ur hinken (modulens text). Bara antal i svaret."""
    from botocore.exceptions import BotoCoreError, ClientError

    from .. import aws
    from ..models import Switchboard

    counts = Tally()
    queues = _active()
    if not queues:
        return {}
    try:
        client = _client()
    except (aws.AwsNotConfigured, BotoCoreError, ClientError) as exc:
        logger.error("Utskick: köerna kunde inte läsas (%s)", type(exc).__name__)
        counts["aws"] += 1
        return dict(counts)
    for which, url in queues:
        while time.monotonic() < deadline - SAFETY_SECONDS:
            try:
                answer = client.receive_message(
                    QueueUrl=url,
                    MaxNumberOfMessages=MAX_MESSAGES,
                    WaitTimeSeconds=0,
                    VisibilityTimeout=VISIBILITY_TIMEOUT,
                )
            except (BotoCoreError, ClientError) as exc:
                logger.error("Utskick: kön %s gick inte att läsa (%s)", which, type(exc).__name__)
                counts["aws"] += 1
                break
            messages = answer.get("Messages") or []
            for message in messages:
                if time.monotonic() >= deadline:
                    # Resten blir synliga igen efter VisibilityTimeout.
                    break
                result = handle(which, message, now)
                counts[result] += 1
                if result != "failed":
                    _delete(client, url, message)
            if len(messages) < MAX_MESSAGES:
                break
    Switchboard.get_solo()
    Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(last_queue_poll_at=now)
    if inbound_enabled() and time.monotonic() < deadline:
        from . import email as inbound_email

        fetched = inbound_email.process_pending(now, deadline)
        for key, value in (fetched or {}).items():
            if value:
                counts[f"mail_{key}"] += value
    return {k: v for k, v in counts.items() if v}


# ---------------------------------------------------------------------------
# DLQ:erna
# ---------------------------------------------------------------------------


def _count(client, url):
    answer = client.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )
    attributes = answer.get("Attributes") or {}
    return int(attributes.get("ApproximateNumberOfMessages") or 0) + int(
        attributes.get("ApproximateNumberOfMessagesNotVisible") or 0
    )


def dlq_counts():
    """Antal meddelanden i köerna och DLQ:erna som är satta. Ett värde som
    inte gick att läsa blir None."""
    from botocore.exceptions import BotoCoreError, ClientError

    from .. import aws

    counts = {}
    names = [(which, queue_url(which)) for which, _setting in QUEUES if queue_url(which)]
    if not names:
        return counts
    try:
        client = _client()
    except (aws.AwsNotConfigured, BotoCoreError, ClientError) as exc:
        logger.error("Utskick: köerna kunde inte läsas (%s)", type(exc).__name__)
        return {key: None for which, _url in names for key in (which, f"{which}_dlq")}
    for which, url in names:
        for key, target in ((which, url), (f"{which}_dlq", dlq_url(url))):
            try:
                counts[key] = _count(client, target)
            except (BotoCoreError, ClientError) as exc:
                logger.warning("Utskick: %s gick inte att räkna (%s)", key, type(exc).__name__)
                counts[key] = None
    return counts


def _arn(client, url):
    answer = client.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])
    return str((answer.get("Attributes") or {}).get("QueueArn") or "")


def redrive(which):
    """Flytta DLQ:ns meddelanden tillbaka till kön (StartMessageMoveTask).
    Svarar en svensk text för byråns sida."""
    from botocore.exceptions import BotoCoreError, ClientError

    from .. import aws

    if which not in dict(QUEUES):
        raise ValueError(f"Okänd kö: {which!r}")
    url = queue_url(which)
    if not url:
        return "Kön är inte inkopplad."
    try:
        client = _client()
        source = _arn(client, dlq_url(url))
        target = _arn(client, url)
        client.start_message_move_task(SourceArn=source, DestinationArn=target)
    except (aws.AwsNotConfigured, BotoCoreError, ClientError) as exc:
        logger.error("Utskick: DLQ:n %s gick inte tillbaka (%s)", which, type(exc).__name__)
        return f"Det gick inte att skicka tillbaka ({type(exc).__name__})."
    logger.warning("Utskick: DLQ:n för %s skickas tillbaka till kön", which)
    return "Meddelandena skickas tillbaka till kön. Det kan ta några minuter."


def check_dlq(now=None):
    """utskick_daily (D.7): byrån larmas en gång per dygn när en DLQ har
    meddelanden. Svarar antalen."""
    from .. import alerts

    now = now or timezone.now()
    counts = dlq_counts()
    waiting = {k: v for k, v in counts.items() if k.endswith("_dlq") and v}
    if waiting:
        lines = [f"{name}: {count} meddelanden." for name, count in sorted(waiting.items())]
        alerts.agency(
            "Utskick: meddelanden i en DLQ",
            [
                *lines,
                "Händelser eller svar har misslyckats fem gånger. Se /manage/utskick/koer/ "
                "och loggen; Skicka tillbaka när felet är rättat.",
            ],
            once="dlq",
            window="day",
            now=now,
        )
    return counts
