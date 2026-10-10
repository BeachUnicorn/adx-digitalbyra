"""
SQS-köerna (README D.7, G.3, J S3 test_s3_queues): kvittot och effekterna i
samma transaktion, en dubblett som inte gör något andra gången, ett fel som
lämnar meddelandet kvar (DLQ efter fem mottagningar), meddelanden som inte
är JSON, SNS-kuvertet, de inkommande mejlen till inbound.email, när köerna
läses (poll_due), DLQ:ernas antal, "Skicka tillbaka" och larmet, ticken och
byråns sida för köerna.

aws.client("sqs") är FakeSqs; inget når nätet.
"""

import json
import time
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .email import transport
from .inbound import events, queues
from .models import Contact, EventReceipt, Recipient, Switchboard
from .sending import tick
from .test_s3_events import bounce, delivery
from .test_s3_transport import EVENTS_URL, INBOUND_URL, EmailFixture

RS = Recipient.Status


class FakeSqs:
    """SQS i testerna: köerna är listor med meddelanden; receive_message
    lämnar ut högst MaxNumberOfMessages, delete_message tar bort efter
    ReceiptHandle. Antalen för get_queue_attributes står i counts."""

    def __init__(self):
        self.queues = {}
        self.deleted = []
        self.received = 0
        self.counts = {}
        self.moves = []

    def put(self, url, body):
        handle = f"h-{len(self.queues.get(url, [])) + len(self.deleted) + 1}-{time.monotonic_ns()}"
        text = body if isinstance(body, str) else json.dumps(body)
        self.queues.setdefault(url, []).append({"Body": text, "ReceiptHandle": handle})
        return handle

    def receive_message(self, **kwargs):
        self.received += 1
        assert kwargs["WaitTimeSeconds"] == 0
        assert kwargs["VisibilityTimeout"] == 60
        pending = [m for m in self.queues.get(kwargs["QueueUrl"], []) if not m.get("taken")]
        batch = pending[: kwargs["MaxNumberOfMessages"]]
        for message in batch:
            message["taken"] = True
        return {
            "Messages": [{"Body": m["Body"], "ReceiptHandle": m["ReceiptHandle"]} for m in batch]
        }

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)
        self.queues[QueueUrl] = [
            m for m in self.queues.get(QueueUrl, []) if m["ReceiptHandle"] != ReceiptHandle
        ]

    def visible_again(self):
        """VisibilityTimeout har gått: det som inte tagits bort syns igen."""
        for messages in self.queues.values():
            for message in messages:
                message.pop("taken", None)

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        if "QueueArn" in AttributeNames:
            return {
                "Attributes": {"QueueArn": "arn:aws:sqs:eu-west-1:1:" + QueueUrl.rsplit("/", 1)[1]}
            }
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(self.counts.get(QueueUrl, 0)),
                "ApproximateNumberOfMessagesNotVisible": "0",
            }
        }

    def start_message_move_task(self, **kwargs):
        self.moves.append(kwargs)
        return {"TaskHandle": "task-1"}


QUEUES = override_settings(
    UTSKICK_SQS_EVENTS_URL=EVENTS_URL,
    UTSKICK_SQS_INBOUND_URL=INBOUND_URL,
    UTSKICK_SES_INBOUND_BUCKET="adx-utskick-inbound-500841883756",
)


class QueueFixture(EmailFixture):
    def setUp(self):
        super().setUp()
        QUEUES.enable()
        self.addCleanup(QUEUES.disable)
        self.sqs = FakeSqs()
        patcher = mock.patch("apps.utskick.aws.client", return_value=self.sqs)
        patcher.start()
        self.addCleanup(patcher.stop)
        pending = mock.patch(
            "apps.utskick.inbound.email.process_pending", return_value={"fetched": 0}
        )
        self.process_pending = pending.start()
        self.addCleanup(pending.stop)

    def poll(self, seconds=30):
        return queues.poll(timezone.now(), time.monotonic() + seconds)

    def sent(self, n=1):
        u = self.sending(n)
        with transport.FakeSes():
            self.run_email()
        return u, list(u.recipients.order_by("pk"))


class PollTests(QueueFixture, TestCase):
    def test_receipt_and_effects_in_one_transaction_then_delete(self):
        _u, (recipient,) = self.sent()
        self.sqs.put(EVENTS_URL, delivery(recipient))
        counts = self.poll()
        self.assertEqual(counts.get("events"), 1)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.DELIVERED)
        self.assertTrue(EventReceipt.objects.filter(key=f"{recipient.ses_message_id}:Delivery"))
        self.assertEqual(self.sqs.queues[EVENTS_URL], [])
        self.assertIsNotNone(Switchboard.get_solo().last_queue_poll_at)

    def test_a_duplicate_does_nothing_the_second_time(self):
        _u, (recipient,) = self.sent()
        soft = bounce(recipient, kind="Transient", subtype="MailboxFull")
        self.sqs.put(EVENTS_URL, soft)
        self.sqs.put(EVENTS_URL, soft)
        counts = self.poll()
        self.assertEqual(counts.get("events"), 1)
        self.assertEqual(counts.get("duplicates"), 1)
        self.assertEqual(Contact.objects.get(pk=recipient.contact_id).email_soft_bounces, 1)
        self.assertEqual(self.sqs.queues[EVENTS_URL], [], "dubbletten tas också bort")

    def test_a_failure_leaves_the_message_and_writes_no_receipt(self):
        _u, (recipient,) = self.sent()
        self.sqs.put(EVENTS_URL, delivery(recipient))
        with mock.patch.object(events, "apply", side_effect=RuntimeError("trasigt")):
            counts = self.poll()
        self.assertEqual(counts.get("failed"), 1)
        self.assertEqual(EventReceipt.objects.count(), 0)
        self.assertEqual(len(self.sqs.queues[EVENTS_URL]), 1)
        # Nästa gång (synligt igen) går det.
        self.sqs.visible_again()
        self.assertEqual(self.poll().get("events"), 1)
        self.assertEqual(self.sqs.queues[EVENTS_URL], [])

    def test_not_json_is_dropped_and_a_notice_without_id_is_ignored(self):
        self.sqs.put(EVENTS_URL, "inte json")
        self.sqs.put(EVENTS_URL, {"notificationType": "AmazonSnsSubscriptionSucceeded"})
        counts = self.poll()
        self.assertEqual(counts.get("bad"), 1)
        self.assertEqual(counts.get("ignored"), 1)
        self.assertEqual(self.sqs.queues[EVENTS_URL], [])

    def test_the_ses_validation_line_is_dropped_without_an_error(self):
        from apps.utskick.inbound import queues as q

        self.sqs.put(EVENTS_URL, q.SES_VALIDATION_TEXT + " Example topic.")
        with self.assertLogs("apps.utskick.inbound.queues", level="INFO") as logs:
            counts = self.poll()
        self.assertEqual(counts.get("bad"), 1)
        self.assertFalse([r for r in logs.records if r.levelname == "ERROR"])
        self.assertEqual(self.sqs.queues[EVENTS_URL], [])

    def test_an_sns_envelope_is_unwrapped(self):
        _u, (recipient,) = self.sent()
        envelope = {"Type": "Notification", "Message": json.dumps(delivery(recipient))}
        self.sqs.put(EVENTS_URL, envelope)
        self.assertEqual(self.poll().get("events"), 1)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.DELIVERED)

    def test_more_than_ten_messages_are_read_in_rounds(self):
        _u, recipients = self.sent(12)
        for recipient in recipients:
            self.sqs.put(EVENTS_URL, delivery(recipient))
        self.assertEqual(self.poll().get("events"), 12)
        self.assertGreaterEqual(self.sqs.received, 2)

    def test_an_inbound_mail_goes_to_inbound_email_in_the_same_transaction(self):
        notice = {
            "notificationType": "Received",
            "mail": {"messageId": "in-123"},
            "receipt": {"recipients": ["s+x@svar.utskick.adx.se"]},
        }
        self.sqs.put(INBOUND_URL, notice)
        with mock.patch("apps.utskick.inbound.email.receive", return_value=object()) as receive:
            counts = self.poll()
        self.assertEqual(counts.get("inbound"), 1)
        receive.assert_called_once()
        self.assertTrue(EventReceipt.objects.filter(key="in:in-123").exists())
        self.assertEqual(self.sqs.queues[INBOUND_URL], [])
        self.process_pending.assert_called_once()
        # Samma notis igen: kvittot finns, receive anropas inte.
        self.sqs.put(INBOUND_URL, notice)
        with mock.patch("apps.utskick.inbound.email.receive") as receive:
            self.assertEqual(self.poll().get("duplicates"), 1)
        receive.assert_not_called()

    def test_the_deadline_stops_the_reading(self):
        _u, (recipient,) = self.sent()
        self.sqs.put(EVENTS_URL, delivery(recipient))
        self.assertEqual(queues.poll(timezone.now(), time.monotonic() - 1), {})
        self.assertEqual(len(self.sqs.queues[EVENTS_URL]), 1)


class PollDueTests(QueueFixture, TestCase):
    def test_off_without_queues_or_ready_marks(self):
        now = timezone.now()
        with override_settings(UTSKICK_SQS_EVENTS_URL="", UTSKICK_SQS_INBOUND_URL=""):
            self.assertFalse(queues.poll_due(now))
        Switchboard.objects.update(email_ready_at=None, doi_ready_at=None)
        self.assertFalse(queues.poll_due(now))

    def test_every_tick_after_a_send_and_every_five_minutes_otherwise(self):
        now = timezone.now()
        Switchboard.objects.update(last_queue_poll_at=now - timedelta(minutes=1))
        with mock.patch("apps.utskick.inbound.email.pending_exists", return_value=False):
            self.assertFalse(queues.poll_due(now))
            Switchboard.objects.update(last_queue_poll_at=now - timedelta(minutes=6))
            self.assertTrue(queues.poll_due(now))
            Switchboard.objects.update(last_queue_poll_at=now)
            self.sent()
            self.assertTrue(queues.poll_due(timezone.now()))
        with mock.patch("apps.utskick.inbound.email.pending_exists", return_value=True):
            Recipient.objects.update(sent_at=now - timedelta(days=4))
            self.assertTrue(queues.poll_due(now))

    def test_the_tick_reads_the_queues_in_phase_two(self):
        Switchboard.objects.update(last_queue_poll_at=None)
        with mock.patch("apps.utskick.inbound.email.pending_exists", return_value=False):
            self.assertTrue(tick.work_exists(timezone.now()))
            with mock.patch.object(queues, "poll", return_value={"events": 2}) as poll:
                summary = tick.run(budget=5)
        poll.assert_called_once()
        self.assertEqual(summary.get("queues"), {"events": 2})
        self.assertIn("queues events=2", tick.summary_line(summary))


class DlqTests(QueueFixture, TestCase):
    def test_counts_redrive_and_the_daily_alert(self):
        self.sqs.counts = {EVENTS_URL: 3, EVENTS_URL + "-dlq": 2, INBOUND_URL + "-dlq": 0}
        counts = queues.dlq_counts()
        self.assertEqual(counts["events"], 3)
        self.assertEqual(counts["events_dlq"], 2)
        self.assertEqual(counts["inbound_dlq"], 0)
        with override_settings(INQUIRY_NOTIFICATION_EMAIL="byran@adx.example"):
            queues.check_dlq()
            queues.check_dlq()
        alerts = [m for m in mail.outbox if m.subject == "Utskick: meddelanden i en DLQ"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("events_dlq: 2 meddelanden.", alerts[0].body)
        text = queues.redrive("events")
        self.assertIn("skickas tillbaka", text)
        self.assertEqual(
            self.sqs.moves,
            [
                {
                    "SourceArn": "arn:aws:sqs:eu-west-1:1:adx-utskick-events-dlq",
                    "DestinationArn": "arn:aws:sqs:eu-west-1:1:adx-utskick-events",
                }
            ],
        )

    def test_nothing_to_count_without_queues(self):
        with override_settings(UTSKICK_SQS_EVENTS_URL="", UTSKICK_SQS_INBOUND_URL=""):
            self.assertEqual(queues.dlq_counts(), {})
            self.assertEqual(queues.redrive("events"), "Kön är inte inkopplad.")

    def test_the_agency_page(self):
        staff = Client()
        staff.force_login(self.staff)
        self.sqs.counts = {EVENTS_URL + "-dlq": 4}
        html = staff.get(reverse("manage:utskick_dlq")).content.decode()
        self.assertIn("<thead>", html)
        self.assertIn("Skicka tillbaka", html)
        response = staff.post(
            reverse("manage:utskick_dlq"), {"action": "redrive", "which": "events"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.sqs.moves), 1)
        customer = self.client_for(self.anna)
        self.assertEqual(customer.get(reverse("manage:utskick_dlq")).status_code, 302)
