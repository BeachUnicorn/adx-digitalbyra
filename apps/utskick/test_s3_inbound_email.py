"""
Svar på mejl och svaren från Inkorgen (README G.2, G.3, H.6; J S3
test_s3_inbound_email).

    ReceiveTests       hinken är fast, token ur receipt.recipients först, okänd token
                       hämtas aldrig, spam och virus, timgränserna, dubbletter
    ProcessTests       hämtningen ur S3, tråden och förfrågan, mottagarens replied_at,
                       From mot mottagarens adress, raden borta, autosvar, HTML,
                       bilagor, för stora mejl, fel och gränsen per tick
    QuoteTests         citerad historik bort (Gmail, Outlook, Apple, svar längst ned)
    MailtoTests        avregistrering med mejl, med och utan mottagarraden
    SweepTests         utskick_daily: objekt utan notis, klara objekt, nya objekt
    EmailReplyTests    svar med mejl från Inkorgen: avsändaren, Reply-To med trådens
                       token, In-Reply-To, citatet, byråns kryssruta, demot, spärren,
                       e-posten av, "Avregistrera från e-post"

Inget når nätet: S3 är en attrapp (FakeS3 genom aws.client), och
sending.email.deliver och from_for (sändnings-byggarens) ersätts med en
attrapp som sparar mejlet.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from unittest import mock

from django.core import mail as django_mail
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount, Lead

from . import contacts, keys, threads, tokens
from .email import transport
from .inbound import email as inbound_email
from .models import (
    CHANNEL_EMAIL,
    Consent,
    ConsentLog,
    EventReceipt,
    InboundMessage,
    Recipient,
    Suppression,
    Switchboard,
    Thread,
    ThreadMessage,
    Utskick,
)
from .testing import UtskickFixture, make_contact

BUCKET = "adx-utskick-inbound-test"
INBOUND = {
    "UTSKICK_SES_INBOUND_BUCKET": BUCKET,
    "UTSKICK_SQS_INBOUND_URL": "https://sqs.eu-west-1.amazonaws.com/1/adx-utskick-inbound",
    "UTSKICK_REPLY_DOMAIN": "svar.utskick.adx.se",
    "INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example",
}
ANNA_EMAIL = "anna@kund.example"


class FakeBody:
    def __init__(self, data):
        self.data = data
        self.reads = 0
        self.closed = False

    def read(self, limit=-1):
        self.reads += 1
        return self.data if limit is None or limit < 0 else self.data[:limit]

    def close(self):
        self.closed = True


class FakeS3:
    """S3 i testerna: objekten i en dict, varje anrop sparat."""

    def __init__(self):
        self.objects = {}
        self.modified = {}
        self.gets = []
        self.deleted = []
        self.bodies = []
        self.fail = None
        self.sizes = {}

    def put(self, key, raw, modified=None):
        self.objects[key] = raw
        self.modified[key] = modified or datetime(2026, 10, 1, tzinfo=UTC)

    def get_object(self, Bucket, Key, Range=None):  # noqa: N803 - boto3:s namn
        from botocore.exceptions import ClientError

        self.gets.append((Bucket, Key, Range))
        if self.fail:
            raise ClientError({"Error": {"Code": self.fail, "Message": "fake"}}, "GetObject")
        if Bucket != BUCKET or Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )
        data = self.objects[Key]
        if Range:
            end = int(Range.split("-")[1])
            data = data[: end + 1]
        body = FakeBody(data)
        self.bodies.append(body)
        return {"ContentLength": self.sizes.get(Key, len(data)), "Body": body}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append((Bucket, Key))
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):  # noqa: N803
        contents = [
            {"Key": key, "LastModified": self.modified[key]}
            for key in sorted(self.objects)
            if key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}


def raw_mail(
    body="Har ni tid tisdag förmiddag?",
    *,
    frm=f"Anna Lind <{ANNA_EMAIL}>",
    to="",
    subject="Sv: Höstservice värmepump",
    message_id="<CAabc123@mail.kund.example>",
    headers=None,
    html=None,
    attachments=(),
):
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to or "Exempelrör <s+r1.1x0000000000@svar.utskick.adx.se>"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    for name, value in (headers or {}).items():
        msg[name] = value
    if body is not None:
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


class InboundFixture(UtskickFixture):
    def setUp(self):
        super().setUp()
        self.s3 = FakeS3()
        patcher = mock.patch("apps.utskick.aws.client", return_value=self.s3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.override = override_settings(**INBOUND)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.contact = make_contact(
            self.account, first_name="Anna", last_name="Lind", email=ANNA_EMAIL
        )
        self.utskick = Utskick.objects.create(
            account=self.account,
            name="Höstservice värmepump",
            channel_mode="email_only",
            status="sent",
            subject="Höstservice värmepump",
        )
        self.recipient = Recipient.objects.create(
            utskick=self.utskick,
            contact=self.contact,
            channel=CHANNEL_EMAIL,
            address=ANNA_EMAIL,
            status=Recipient.Status.DELIVERED,
            sent_at=timezone.now() - timedelta(hours=2),
        )
        self.serial = 0

    def reply_to(self, kind=tokens.REPLY, account=None, object_id=None):
        account = account or self.account
        object_id = object_id if object_id is not None else self.recipient.pk
        return tokens.reply_address(kind, account.pk, object_id)

    def notification(
        self,
        *,
        recipients=None,
        key=None,
        bucket=BUCKET,
        spam="PASS",
        virus="PASS",
        spf="PASS",
        dkim="PASS",
        dmarc="PASS",
        frm=f"Anna Lind <{ANNA_EMAIL}>",
        subject="Sv: Höstservice värmepump",
        headers=(),
        message_id=None,
        action="S3",
    ):
        self.serial += 1
        message_id = message_id or f"ses{self.serial:04d}abc"
        return {
            "notificationType": "Received",
            "mail": {
                "timestamp": "2026-10-10T07:00:00.000Z",
                "source": ANNA_EMAIL,
                "messageId": message_id,
                "headers": [{"name": n, "value": v} for n, v in headers],
                "commonHeaders": {
                    "from": [frm],
                    "subject": subject,
                    "messageId": "<CAabc123@mail.kund.example>",
                },
            },
            "receipt": {
                "recipients": recipients if recipients is not None else [self.reply_to()],
                "spamVerdict": {"status": spam},
                "virusVerdict": {"status": virus},
                "spfVerdict": {"status": spf},
                "dkimVerdict": {"status": dkim},
                "dmarcVerdict": {"status": dmarc},
                "action": {
                    "type": action,
                    "bucketName": bucket,
                    "objectKey": key or f"in/{message_id}",
                },
            },
        }

    def receive(self, notification, raw=None):
        """Som queues.poll: kvittot och receive i en transaktion, sedan commit."""
        key = notification["receipt"]["action"]["objectKey"]
        if raw is not None:
            self.s3.put(key, raw)
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                EventReceipt.objects.create(key=f"in:{notification['mail']['messageId']}")
                return inbound_email.receive(json.dumps(notification))

    def process(self):
        return inbound_email.process_pending(timezone.now(), time.monotonic() + 30)

    def deliver(self, raw=None, **kwargs):
        """Ett svar som kommer in och routas hela vägen."""
        note = self.notification(**kwargs)
        row = self.receive(note, raw=raw if raw is not None else raw_mail())
        self.process()
        row.refresh_from_db()
        return row


# ---------------------------------------------------------------------------
# In i kön
# ---------------------------------------------------------------------------


class ReceiveTests(InboundFixture, TestCase):
    def test_the_bucket_is_pinned(self):
        for note in (
            self.notification(bucket="nagon-annans-hink"),
            self.notification(key="ut/ses9999"),
            self.notification(key="in/../hemligt"),
            self.notification(action="SNS"),
        ):
            with self.subTest(action=note["receipt"]["action"]):
                self.assertIsNone(self.receive(note))
        self.assertFalse(InboundMessage.objects.exists())
        self.assertEqual(self.s3.gets, [])
        self.assertEqual(self.s3.deleted, [])

    @override_settings(UTSKICK_SES_INBOUND_BUCKET="")
    def test_without_a_bucket_nothing_is_taken(self):
        self.assertIsNone(self.receive(self.notification()))
        self.assertFalse(InboundMessage.objects.exists())

    def test_a_setup_notification_is_not_a_mail(self):
        self.assertIsNone(inbound_email.receive({"notificationType": "AMAZON_SES_SETUP"}))
        self.assertIsNone(inbound_email.receive("inte json"))

    def test_the_token_comes_from_the_receipt_recipients_not_the_to_header(self):
        foreign = tokens.reply_address(tokens.REPLY, self.other_account.pk, 999)
        raw = raw_mail(to=f"Annanfirma <{foreign}>")
        note = self.notification(recipients=["nagon@svar.utskick.adx.se", self.reply_to()])
        row = self.receive(note, raw=raw)
        self.assertEqual(row.account_id, self.account.pk)
        self.assertEqual(row.to_address, self.reply_to())
        self.assertEqual(row.routed_via, f"token:r:{self.recipient.pk}")
        self.process()
        row.refresh_from_db()
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        self.assertEqual(Thread.objects.get().account_id, self.account.pk)

    def test_the_token_is_read_case_insensitively(self):
        row = self.receive(self.notification(recipients=[self.reply_to().upper()]), raw_mail())
        self.assertEqual(row.status, InboundMessage.Status.PENDING)

    def test_an_unknown_token_is_never_fetched(self):
        good = self.reply_to()
        local = good.split("@")[0]
        tampered = local[:-1] + ("a" if local[-1] != "a" else "b") + "@svar.utskick.adx.se"
        for recipients in (
            [tampered],
            ["kalle@svar.utskick.adx.se"],
            [good.replace("svar.utskick.adx.se", "svar.annan.example")],
            [],
        ):
            with self.subTest(recipients=recipients):
                note = self.notification(recipients=recipients)
                row = self.receive(note, raw=raw_mail())
                self.assertEqual(row.status, InboundMessage.Status.IGNORED)
                self.assertEqual((row.meta or {}).get("reason"), "token")
                self.assertEqual(row.from_address, "")
                self.assertEqual(row.subject, "")
                key = note["receipt"]["action"]["objectKey"]
                self.assertIn((BUCKET, key), self.s3.deleted)
        self.process()
        self.assertEqual(self.s3.gets, [], "ingen okänd token läses")
        self.assertFalse(Thread.objects.exists())

    def test_a_token_for_an_account_that_is_gone(self):
        ghost = tokens.reply_address(tokens.REPLY, 987654, 1)
        row = self.receive(self.notification(recipients=[ghost]), raw=raw_mail())
        self.assertEqual((row.status, row.meta["reason"]), ("ignored", "account"))
        self.assertEqual(self.s3.gets, [])

    def test_spam_and_virus_are_counted_not_read(self):
        for verdict in ({"spam": "FAIL"}, {"virus": "FAIL"}):
            with self.subTest(verdict=verdict):
                note = self.notification(**verdict)
                row = self.receive(note, raw=raw_mail())
                self.assertEqual(row.status, InboundMessage.Status.SPAM)
                self.assertEqual(row.account_id, self.account.pk)
                self.assertIn((BUCKET, note["receipt"]["action"]["objectKey"]), self.s3.deleted)
        self.process()
        self.assertEqual(self.s3.gets, [])
        self.assertFalse(Thread.objects.exists())

    def test_a_duplicate_notification_changes_nothing(self):
        note = self.notification()
        first = self.receive(note, raw=raw_mail())
        with transaction.atomic():
            again = inbound_email.receive(note)
        self.assertEqual(first.pk, again.pk)
        self.process()
        self.process()
        self.assertEqual(InboundMessage.objects.count(), 1)
        self.assertEqual(ThreadMessage.objects.count(), 1)

    def test_the_hourly_cap_for_all_of_adx(self):
        with mock.patch.object(inbound_email, "PER_HOUR", 2):
            rows = []
            for i in range(3):
                other = make_contact(self.account, email=f"kund{i}@kund.example")
                recipient = Recipient.objects.create(
                    utskick=self.utskick,
                    contact=other,
                    channel=CHANNEL_EMAIL,
                    address=other.email,
                    status=Recipient.Status.DELIVERED,
                )
                note = self.notification(recipients=[self.reply_to(object_id=recipient.pk)])
                rows.append(self.receive(note, raw=raw_mail()))
        self.assertEqual([r.status for r in rows], ["pending", "pending", "counted"])
        self.assertEqual(rows[2].meta["reason"], "hour")
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertIn("Fler än 2 mejl", django_mail.outbox[0].body)
        self.assertNotIn("kund.example", django_mail.outbox[0].body)
        self.process()
        self.assertEqual(len(self.s3.gets), 2, "det räknade mejlet hämtas aldrig")

    def test_one_sender_cannot_take_the_whole_hour(self):
        with mock.patch.object(inbound_email, "PER_REF_HOUR", 2):
            rows = [self.receive(self.notification(), raw=raw_mail()) for _ in range(3)]
        self.assertEqual([r.status for r in rows], ["pending", "pending", "counted"])
        self.assertEqual(rows[2].meta["reason"], "ref_hour")
        self.assertEqual(len(django_mail.outbox), 1)

    def test_pending_exists(self):
        self.assertFalse(inbound_email.pending_exists())
        self.receive(self.notification(), raw=raw_mail())
        self.assertTrue(inbound_email.pending_exists())
        self.process()
        self.assertFalse(inbound_email.pending_exists())


# ---------------------------------------------------------------------------
# Hämtningen och tråden
# ---------------------------------------------------------------------------


class ProcessTests(InboundFixture, TestCase):
    def test_a_reply_lands_in_the_inbox(self):
        row = self.deliver()
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        self.assertEqual((row.body, row.subject), ("", ""), "texten bor i tråden")
        self.assertEqual(row.contact_id, self.contact.pk)
        self.assertEqual(row.meta["message_id"], "<CAabc123@mail.kund.example>")
        thread = Thread.objects.get()
        self.assertEqual(
            (thread.channel, thread.address, thread.contact_id, thread.utskick_id),
            (CHANNEL_EMAIL, ANNA_EMAIL, self.contact.pk, self.utskick.pk),
        )
        self.assertEqual(row.meta["thread"], thread.pk)
        message = thread.messages.get()
        self.assertEqual(message.body, "Har ni tid tisdag förmiddag?")
        self.assertEqual(message.subject, "Sv: Höstservice värmepump")
        self.assertEqual(message.inbound_id, row.pk)
        lead = thread.lead
        self.assertEqual((lead.source, lead.status), (Lead.SOURCE_REPLY, Lead.STATUS_NEW))
        self.assertEqual(lead.email, ANNA_EMAIL)
        self.assertEqual(lead.message, "Har ni tid tisdag förmiddag?")
        self.recipient.refresh_from_db()
        self.assertIsNotNone(self.recipient.replied_at)
        self.assertEqual(threads.channel_label(thread), "E-postsvar")
        self.assertIn((BUCKET, row.meta["s3_key"]), self.s3.deleted)
        self.assertTrue(all(body.closed for body in self.s3.bodies))

    def test_the_next_reply_goes_to_the_same_thread_and_raises_it_again(self):
        self.deliver()
        thread = Thread.objects.get()
        Lead.objects.filter(pk=thread.lead_id).update(status=Lead.STATUS_CONTACTED)
        self.deliver(raw_mail("En sak till."))
        self.assertEqual(Thread.objects.count(), 1)
        self.assertEqual(thread.messages.count(), 2)
        thread.lead.refresh_from_db()
        self.assertEqual(thread.lead.status, Lead.STATUS_NEW)

    def test_a_thread_token_goes_to_that_thread(self):
        self.deliver()
        thread = Thread.objects.get()
        address = tokens.reply_address(tokens.THREAD, self.account.pk, thread.pk)
        row = self.deliver(raw_mail("Tack för svaret."), recipients=[address])
        self.assertEqual(row.routed_via, f"token:t:{thread.pk}")
        self.assertEqual(thread.messages.count(), 2)

    def test_a_reply_from_another_address_is_routed_and_marked(self):
        row = self.deliver(raw_mail(frm="Kalle <kalle@kund.example>"))
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        self.assertTrue(row.meta["other_address"])
        self.assertEqual(row.from_address, "kalle@kund.example")
        thread = Thread.objects.get()
        self.assertEqual(thread.address, ANNA_EMAIL, "tråden gäller mottagaren")
        rows = threads.email_rows(thread)
        self.assertEqual(rows[0].note, "Från en annan adress: kalle@kund.example")

    def test_without_the_recipient_row_the_account_and_from_decide(self):
        recipient_pk = self.recipient.pk
        Recipient.objects.filter(pk=recipient_pk).delete()
        row = self.deliver()
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        thread = Thread.objects.get()
        self.assertEqual((thread.address, thread.contact_id), (ANNA_EMAIL, self.contact.pk))
        self.assertIsNone(thread.utskick_id)
        self.assertNotIn("other_address", row.meta)

    def test_a_reply_creates_no_contact(self):
        Recipient.objects.all().delete()
        row = self.deliver(raw_mail(frm="Okänd <okand@annan.example>"))
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        thread = Thread.objects.get()
        self.assertIsNone(thread.contact_id)
        self.assertEqual(thread.address, "okand@annan.example")
        self.assertFalse(self.account.utskick_contacts.filter(email="okand@annan.example").exists())

    def test_auto_replies_stay_out_of_the_inbox(self):
        cases = [
            raw_mail(headers={"Auto-Submitted": "auto-replied"}),
            raw_mail(headers={"X-Autoreply": "yes"}),
            raw_mail(headers={"Precedence": "bulk"}),
            raw_mail(subject="Frånvaro: Anna Lind"),
            raw_mail(subject="Automatiskt svar: Höstservice"),
            raw_mail(subject="Out of office"),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                row = self.deliver(raw)
                self.assertEqual(row.status, InboundMessage.Status.AUTOREPLY)
                self.assertEqual(row.meta["recipient"], self.recipient.pk)
                self.assertEqual((row.body, row.subject), ("", ""))
        self.assertFalse(Thread.objects.exists())
        self.recipient.refresh_from_db()
        self.assertIsNone(self.recipient.replied_at)

    def test_the_notification_headers_already_tell_an_auto_reply(self):
        row = self.deliver(headers=[("Auto-Submitted", "auto-generated")])
        self.assertEqual(row.status, InboundMessage.Status.AUTOREPLY)

    def test_a_delivery_report_is_not_a_reply(self):
        report = (
            b"From: postmaster@kund.example\r\nTo: s+x@svar.utskick.adx.se\r\n"
            b"Subject: Delivery Status Notification\r\nMIME-Version: 1.0\r\n"
            b'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nKunde inte levereras.\r\n--b--\r\n"
        )
        self.assertEqual(self.deliver(report).status, InboundMessage.Status.AUTOREPLY)

    def test_an_html_only_mail_becomes_text(self):
        html = (
            "<html><head><title>x</title><style>p{color:red}</style></head><body>"
            "<p>Hej &amp; tack,</p><p>tisdag passar.</p><script>alert(1)</script>"
            '<div class="gmail_quote">Den tis 8 okt. 2026 kl 09:00 skrev Exempelrör:'
            "<blockquote>Gammal text</blockquote></div></body></html>"
        )
        self.deliver(raw_mail(body=None, html=html))
        body = ThreadMessage.objects.get().body
        self.assertEqual(body, "Hej & tack,\ntisdag passar.")

    def test_plain_text_is_preferred_over_html(self):
        self.deliver(raw_mail(body="Texten", html="<p>HTML-versionen</p>"))
        self.assertEqual(ThreadMessage.objects.get().body, "Texten")

    def test_quoted_history_is_cut(self):
        raw = raw_mail(
            "Ja, tisdag 10.30 passar.\n\nDen tis 8 okt. 2026 kl 09:00 skrev Exempelrör "
            "<s+r1.2x0000000000@svar.utskick.adx.se>:\n> Höstservice av värmepumpen\n"
        )
        self.deliver(raw)
        self.assertEqual(ThreadMessage.objects.get().body, "Ja, tisdag 10.30 passar.")

    def test_attachments_are_listed_never_stored(self):
        raw = raw_mail("Se bilden.", attachments=[("offert.pdf", b"%PDF-1.4 " * 100)])
        row = self.deliver(raw)
        message = ThreadMessage.objects.get()
        self.assertEqual(message.attachments, [{"name": "offert.pdf", "size": 900}])
        self.assertEqual(row.meta["attachments"], 1)
        self.assertNotIn("offert", json.dumps(row.meta))
        rows = threads.email_rows(message.thread)
        self.assertEqual(rows[0].attachments, [{"name": "offert.pdf", "size": 900}])

    def test_a_mail_over_ten_megabytes_shows_metadata_only(self):
        note = self.notification()
        key = note["receipt"]["action"]["objectKey"]
        self.s3.sizes[key] = inbound_email.MAX_BYTES + 1
        row = self.receive(note, raw=raw_mail())
        self.process()
        row.refresh_from_db()
        message = ThreadMessage.objects.get()
        self.assertEqual(message.body, inbound_email.TOO_BIG_TEXT)
        self.assertEqual(message.subject, "Sv: Höstservice värmepump")
        self.assertTrue(row.meta["too_big"])
        self.assertEqual([b.reads for b in self.s3.bodies], [0], "inget lästes")

    def test_a_failed_fetch_waits_and_gives_up_after_five_tries(self):
        row = self.receive(self.notification(), raw=raw_mail())
        self.s3.fail = "InternalError"
        for attempt in range(1, inbound_email.MAX_ATTEMPTS):
            counts = self.process()
            row.refresh_from_db()
            self.assertEqual((counts["failed"], row.status), (1, "pending"))
            self.assertEqual(row.meta["attempts"], attempt)
        with self.captureOnCommitCallbacks(execute=True):
            self.process()
        row.refresh_from_db()
        self.assertEqual((row.status, row.meta["reason"]), ("ignored", "fetch"))
        self.assertEqual(len(django_mail.outbox), 1)
        self.assertFalse(Thread.objects.exists())

    def test_a_missing_object_is_ignored(self):
        row = self.receive(self.notification())
        self.process()
        row.refresh_from_db()
        self.assertEqual((row.status, row.meta["reason"]), ("ignored", "missing"))

    def test_at_most_twenty_per_tick(self):
        with mock.patch.object(inbound_email, "PER_REF_HOUR", 100):
            for _ in range(inbound_email.PER_TICK + 1):
                self.receive(self.notification(), raw=raw_mail())
        counts = self.process()
        self.assertEqual(counts["routed"], inbound_email.PER_TICK)
        self.assertEqual(
            InboundMessage.objects.filter(status=InboundMessage.Status.PENDING).count(), 1
        )

    def test_the_deadline_stops_the_fetching(self):
        self.receive(self.notification(), raw=raw_mail())
        counts = inbound_email.process_pending(timezone.now(), time.monotonic() - 1)
        self.assertEqual(counts["fetched"], 0)
        self.assertEqual(self.s3.gets, [])

    def test_a_reply_that_looks_like_an_unsubscribe_on_reklam(self):
        self.deliver(raw_mail("Sluta skicka mejl till mig, tack."))
        self.assertTrue(Thread.objects.get().looks_like_stop)

    def test_the_owner_notice_has_no_phone_for_an_email(self):
        self.deliver()
        message = ThreadMessage.objects.get()
        with mock.patch("apps.flamingo.sms.owner_reply_text", return_value="x") as text:
            threads._single_reply_text(message, "https://adx.se/x")
        self.assertEqual(text.call_args.kwargs["phone"], "")


class QuoteTests(TestCase):
    def test_the_common_clients(self):
        cases = {
            "Gmail på svenska": (
                "Ja tack\n\nDen tis 8 okt. 2026 kl 09:00 skrev Exempelrör <\n"
                "s+r1.2x0000000000@svar.utskick.adx.se>:\n\n> Gammalt",
                "Ja tack",
            ),
            "Gmail på engelska": (
                "Yes\n\nOn Tue, Oct 8, 2026 at 9:00 AM Exempelrör <x@svar.utskick.adx.se> wrote:"
                "\n> Old",
                "Yes",
            ),
            "Apple Mail": (
                "Bra\n\n8 okt. 2026 kl. 09:14 skrev Exempelrör <x@svar.utskick.adx.se>:\n\n> X",
                "Bra",
            ),
            "Outlook": (
                "Ok\n\n________________________________\nFrån: Exempelrör <x@y.example>\n"
                "Skickat: den 8 oktober 2026 09:00\nTill: Anna Lind\nÄmne: Höst\n\nGammalt",
                "Ok",
            ),
            "Original Message": ("Hej\n-----Original Message-----\nFrom: x", "Hej"),
            "citattecken": ("Svar\n> gammalt\n> mer", "Svar"),
            "svar längst ned": ("> gammalt\n> mer\n\nMitt svar under", "Mitt svar under"),
            "ingen historik": ("Bara text\n\nMed två stycken", "Bara text\n\nMed två stycken"),
            "en vanlig mening": (
                "Den 5 maj skrev jag till er om detta:\nTaket läcker.",
                "Den 5 maj skrev jag till er om detta:\nTaket läcker.",
            ),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(client=name):
                self.assertEqual(inbound_email.strip_quotes(text), expected)

    def test_the_length_is_capped(self):
        text = "a" * (ThreadMessage.MAX_BODY + 50)
        self.assertEqual(len(inbound_email.strip_quotes(text)), ThreadMessage.MAX_BODY)

    def test_addresses_in_headers(self):
        self.assertEqual(inbound_email.address_in("Anna <Anna@Kund.Example>"), ANNA_EMAIL)
        self.assertEqual(inbound_email.address_in(ANNA_EMAIL), ANNA_EMAIL)
        self.assertEqual(inbound_email.address_in("ingen adress"), "")


# ---------------------------------------------------------------------------
# Avregistrering med mejl (mailto i List-Unsubscribe)
# ---------------------------------------------------------------------------


class MailtoTests(InboundFixture, TestCase):
    def mailto(self, object_id=None, **kwargs):
        address = self.reply_to(tokens.MAILTO, object_id=object_id)
        note = self.notification(recipients=[address], subject="avregistrera", **kwargs)
        return self.receive(note, raw=raw_mail(subject="avregistrera"))

    def test_the_recipients_address_is_unsubscribed_without_reading_the_mail(self):
        row = self.mailto()
        self.assertEqual(row.status, InboundMessage.Status.STOP)
        self.assertEqual(row.meta["via"], "recipient")
        self.assertEqual((row.from_address, row.subject), ("", ""))
        value_hash = keys.value_hash(CHANNEL_EMAIL, ANNA_EMAIL)
        suppression = Suppression.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(
            (suppression.value_hash, suppression.reason, suppression.utskick_id),
            (value_hash, Suppression.Reason.LIST_UNSUB, self.utskick.pk),
        )
        consent = self.contact.consents.get(channel=CHANNEL_EMAIL)
        self.assertEqual(consent.status, Consent.Status.UNSUBSCRIBED)
        log = ConsentLog.objects.filter(contact=self.contact, channel=CHANNEL_EMAIL).first()
        self.assertEqual((log.source, log.source_detail), ("list_unsub", "Avregistrering med mejl"))
        self.recipient.refresh_from_db()
        self.assertIsNotNone(self.recipient.stopped_at)
        self.process()
        self.assertEqual(self.s3.gets, [])
        self.assertIn((BUCKET, row.meta["s3_key"]), self.s3.deleted)
        self.assertFalse(Thread.objects.exists())

    def test_after_the_recipient_row_is_gone_the_from_address_decides(self):
        pk = self.recipient.pk
        contacts.delete_contact(self.contact, suppress=False)
        Recipient.objects.filter(pk=pk).delete()
        row = self.mailto(object_id=pk, frm="Anna <Anna@Kund.Example>")
        self.assertEqual((row.status, row.meta["via"]), ("stop", "from"))
        suppression = Suppression.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(suppression.value_hash, keys.value_hash(CHANNEL_EMAIL, ANNA_EMAIL))
        log = ConsentLog.objects.get(
            value_hash=suppression.value_hash, contact=None, source="list_unsub"
        )
        self.assertEqual(log.new_status, Consent.Status.UNSUBSCRIBED)
        self.assertEqual(self.s3.gets, [])

    def test_a_blanked_recipient_falls_back_to_from(self):
        Recipient.objects.filter(pk=self.recipient.pk).update(address="", contact=None)
        row = self.mailto(frm="Anna <anna@kund.example>")
        self.assertEqual(row.meta["via"], "from")
        self.assertTrue(
            Suppression.objects.filter(
                account=self.account, value_hash=keys.value_hash(CHANNEL_EMAIL, ANNA_EMAIL)
            ).exists()
        )

    def test_the_unsubscribe_is_honoured_even_when_flagged_or_over_the_cap(self):
        with mock.patch.object(inbound_email, "PER_HOUR", 0):
            row = self.mailto(spam="FAIL")
        self.assertEqual(row.status, InboundMessage.Status.STOP)
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())

    def test_another_accounts_recipient_is_not_used(self):
        token = tokens.reply_address(tokens.MAILTO, self.other_account.pk, self.recipient.pk)
        note = self.notification(recipients=[token], frm="Bo <bo@annan.example>")
        row = self.receive(note, raw=raw_mail())
        self.assertEqual(row.meta["via"], "from")
        self.assertFalse(Suppression.objects.filter(account=self.account).exists())
        self.assertTrue(
            Suppression.objects.filter(
                account=self.other_account,
                value_hash=keys.value_hash(CHANNEL_EMAIL, "bo@annan.example"),
            ).exists()
        )

    def test_the_unsubscribe_works_when_utskick_is_off_for_the_account(self):
        self.settings.is_enabled = False
        self.settings.save(update_fields=["is_enabled"])
        self.assertEqual(self.mailto().status, InboundMessage.Status.STOP)


# ---------------------------------------------------------------------------
# Svepet
# ---------------------------------------------------------------------------


class SweepTests(InboundFixture, TestCase):
    def test_an_object_without_a_notification_is_taken_in(self):
        old = timezone.now() - timedelta(hours=2)
        raw = (
            f"Received: from mail.kund.example by inbound-smtp.eu-west-1.amazonaws.com "
            f"with SMTP id ses7777 for {self.reply_to()};\r\n"
            "X-SES-Spam-Verdict: PASS\r\nX-SES-Virus-Verdict: PASS\r\n"
        ).encode() + raw_mail(to="Exempelrör <hej@exempelror.example>")
        self.s3.put("in/ses7777", raw, modified=old)
        self.s3.put("in/ses8888", raw_mail(), modified=timezone.now())
        with self.captureOnCommitCallbacks(execute=True):
            counts = inbound_email.sweep_bucket()
        self.assertEqual((counts["orphans"], counts["listed"]), (1, 1))
        row = InboundMessage.objects.get(provider_id="ses7777")
        self.assertEqual(row.status, InboundMessage.Status.ROUTED)
        self.assertTrue(EventReceipt.objects.filter(key="in:ses7777").exists())
        self.assertEqual(Thread.objects.get().messages.count(), 1)
        self.assertIn("in/ses8888", self.s3.objects, "ett nytt objekt väntar på sin notis")

    def test_a_finished_object_is_removed(self):
        row = self.deliver()
        self.s3.put(row.meta["s3_key"], raw_mail())
        counts = inbound_email.sweep_bucket()
        self.assertEqual(counts["deleted"], 1)
        self.assertNotIn(row.meta["s3_key"], self.s3.objects)

    def test_spam_found_by_the_sweep_is_not_read_further(self):
        raw = (
            f"Received: from x by inbound-smtp.eu-west-1.amazonaws.com for {self.reply_to()};\r\n"
            "X-SES-Spam-Verdict: FAIL\r\n"
        ).encode() + raw_mail()
        self.s3.put("in/ses5555", raw, modified=timezone.now() - timedelta(hours=1))
        inbound_email.sweep_bucket()
        self.assertEqual(InboundMessage.objects.get().status, InboundMessage.Status.SPAM)
        self.assertFalse(Thread.objects.exists())

    @override_settings(UTSKICK_SQS_INBOUND_URL="")
    def test_off_without_the_queue(self):
        self.s3.put("in/ses1", raw_mail())
        self.assertEqual(inbound_email.sweep_bucket()["listed"], 0)
        self.assertEqual(self.s3.gets, [])


# ---------------------------------------------------------------------------
# Svar med mejl från Inkorgen (G.2)
# ---------------------------------------------------------------------------


class FakeDelivery:
    """sending.email.deliver och from_for i testerna (sändnings-byggarens)."""

    def __init__(self, result=None):
        self.result = result or transport.Sent(ok=True, message_id="ses-out-1", mode="ses")
        self.calls = []

    def deliver(self, account, mail, *, kind, utskick=None, recipient=None, now=None):
        self.calls.append({"account": account, "mail": mail, "kind": kind, "utskick": utskick})
        return self.result

    @staticmethod
    def from_for(account, utskick=None, sender_domain=None):
        return ("Exempelrör", "exempelror@utskick.adx.se")


@override_settings(UTSKICK_EMAIL_LIVE=True)
class EmailReplyTests(InboundFixture, TestCase):
    def setUp(self):
        super().setUp()
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK, defaults={"email_enabled": True}
        )
        self.fake = FakeDelivery()
        for name in ("deliver", "from_for"):
            patcher = mock.patch(f"apps.utskick.sending.email.{name}", getattr(self.fake, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.deliver()
        self.thread = Thread.objects.get()
        self.lead = self.thread.lead
        self.app = self.client_for(self.anna)

    def reply(self, text, client=None, **extra):
        url = reverse("flamingo:app_lead_reply", args=[self.lead.pk])
        return (client or self.app).post(url, {"text": text, **extra})

    def test_the_detail_page_shows_the_email_thread(self):
        html = self.app.get(reverse("flamingo:app_lead", args=[self.lead.pk])).content.decode()
        self.assertIn("E-postsvar", html)
        self.assertIn("Svara med mejl", html)
        self.assertIn(f"Till {ANNA_EMAIL}", html)
        self.assertIn("Ämne: Sv: Höstservice värmepump", html)
        self.assertIn("Avregistrera från e-post", html)
        self.assertNotIn("data-ut-sms", html, "ingen sms-räknare för mejl")
        self.assertIn("Har ni tid tisdag förmiddag?", html)

    def test_a_reply_by_email(self):
        response = self.reply("Tisdag 10.30 går bra.")
        self.assertRedirects(
            response,
            reverse("flamingo:app_lead", args=[self.lead.pk]) + "#svar",
            fetch_redirect_response=False,
        )
        self.assertEqual(len(self.fake.calls), 1)
        call = self.fake.calls[0]
        self.assertEqual((call["kind"], call["utskick"]), (transport.REPLY, self.utskick))
        sent = call["mail"]
        self.assertEqual((sent.to, sent.from_addr), (ANNA_EMAIL, "exempelror@utskick.adx.se"))
        self.assertEqual(sent.subject, "Sv: Höstservice värmepump")
        reply_to = tokens.read_reply_address(sent.headers["Reply-To"])
        self.assertEqual(
            (reply_to.kind, reply_to.account_id, reply_to.object_id),
            (tokens.THREAD, self.account.pk, self.thread.pk),
        )
        self.assertEqual(sent.headers["In-Reply-To"], "<CAabc123@mail.kund.example>")
        self.assertEqual(sent.headers["References"], "<CAabc123@mail.kund.example>")
        self.assertTrue(sent.text.startswith("Tisdag 10.30 går bra.\n\nDen "))
        self.assertIn("skrev du:\n> Har ni tid tisdag förmiddag?", sent.text)
        out = self.thread.messages.get(direction=ThreadMessage.Direction.OUT)
        self.assertEqual(
            (out.status, out.email_message_id, out.sent_by_id, out.sent_as_staff),
            ("sent", "ses-out-1", self.anna.pk, False),
        )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, Lead.STATUS_CONTACTED)

    def test_the_answer_to_the_answer_comes_back_to_the_thread(self):
        self.reply("Tisdag 10.30 går bra.")
        address = self.fake.calls[0]["mail"].headers["Reply-To"]
        self.deliver(raw_mail("Toppen, vi ses."), recipients=[address])
        self.assertEqual(Thread.objects.count(), 1)
        self.assertEqual(self.thread.messages.filter(direction="in").count(), 2)

    def test_a_double_click_sends_once(self):
        self.reply("Tisdag 10.30 går bra.")
        self.reply("Tisdag 10.30 går bra.")
        self.assertEqual(len(self.fake.calls), 1)

    def test_staff_must_tick_the_box(self):
        staff = self.client_for(self.staff)
        self.reply("Hej", client=staff)
        self.assertEqual(self.fake.calls, [])
        self.reply("Hej", client=staff, staff_ok="1")
        out = self.thread.messages.get(direction=ThreadMessage.Direction.OUT)
        self.assertEqual((out.sent_by_id, out.sent_as_staff), (self.staff.pk, True))

    def test_the_demo_never_sends(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        response = self.reply("Hej")
        self.assertEqual(self.fake.calls, [])
        self.assertContains(response, "Demokontot skickar aldrig.")

    def test_an_unsubscribed_address_gets_no_answer(self):
        threads.unsubscribe_email(self.thread, actor=mock.Mock(user=None, label="x", staff=False))
        response = self.reply("Hej")
        self.assertEqual(self.fake.calls, [])
        self.assertContains(response, "Adressen är avregistrerad från e-post.")

    def test_a_bounced_address_gets_no_answer(self):
        self.contact.email_state = "bounced"
        self.contact.save(update_fields=["email_state"])
        self.assertEqual(threads.email_reply_problem(self.thread), threads.EMAIL_BOUNCED_TEXT)
        self.reply("Hej")
        self.assertEqual(self.fake.calls, [])

    def test_while_email_is_off_the_page_says_so(self):
        Switchboard.objects.update(email_enabled=False)
        html = self.app.get(reverse("flamingo:app_lead", args=[self.lead.pk])).content.decode()
        self.assertIn("E-post är inte påslaget än.", html)
        self.assertNotIn("Svara med mejl", html)
        self.reply("Hej")
        self.assertEqual(self.fake.calls, [])

    def test_a_refused_mail_keeps_the_text(self):
        self.fake.result = transport.Sent(ok=False, error="MessageRejected")
        response = self.reply("Tisdag 10.30 går bra.")
        self.assertContains(response, threads.EMAIL_FAILED_TEXT)
        self.assertContains(response, "Tisdag 10.30 går bra.")
        self.assertFalse(self.thread.messages.filter(direction="out").exists())

    def test_a_throttled_mail_says_try_again(self):
        from .sending import email as email_sending

        self.fake.result = transport.Sent(ok=False, error="Throttling", retry=True)
        expected = email_sending.error_text(self.fake.result)
        if expected == email_sending.DEFAULT_ERROR_TEXT:
            expected = threads.EMAIL_BUSY_TEXT
        self.assertContains(self.reply("Hej"), expected)

    def test_the_adx_cap_text_comes_from_the_sending_side(self):
        from .sending import email as email_sending

        self.fake.result = transport.Sent(ok=False, error="adx_cap", stop=True)
        self.assertContains(self.reply("Hej"), email_sending.ERROR_TEXTS["adx_cap"])

    def test_an_unknown_outcome_is_kept_and_said(self):
        self.fake.result = transport.Sent(ok=False, error="timeout", unknown=True)
        self.reply("Hej")
        out = self.thread.messages.get(direction="out")
        self.assertEqual(out.status, ThreadMessage.Status.SENDING)
        rows = threads.email_rows(self.thread)
        self.assertEqual(rows[-1].note, threads.EMAIL_UNKNOWN_NOTE)

    def test_unsubscribe_from_email_in_the_inbox(self):
        url = reverse("flamingo:app_lead_unsubscribe", args=[self.lead.pk])
        self.app.post(url)
        suppression = Suppression.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(suppression.reason, Suppression.Reason.REPLY)
        log = ConsentLog.objects.filter(contact=self.contact, channel=CHANNEL_EMAIL).first()
        self.assertEqual((log.source, log.by_user_id), ("reply", self.anna.pk))
        html = self.app.get(reverse("flamingo:app_lead", args=[self.lead.pk])).content.decode()
        self.assertIn("Adressen är avregistrerad från e-post.", html)
        self.assertNotIn("Svara med mejl", html)

    def test_another_accounts_lead_is_404(self):
        other = self.client_for(self.anna)
        foreign = Lead.objects.create(account=self.other_account, source=Lead.SOURCE_REPLY)
        url = reverse("flamingo:app_lead_reply", args=[foreign.pk])
        self.assertEqual(other.post(url, {"text": "Hej"}).status_code, 404)
        self.assertEqual(self.fake.calls, [])


@override_settings(UTSKICK_EMAIL_LIVE=True)
class EmailReplySeamTests(InboundFixture, TestCase):
    """Svaret genom sändnings-byggarens riktiga deliver och transporten, till
    FakeSes: de råa huvudena som mejlprogrammen trådar på."""

    def setUp(self):
        super().setUp()
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK, defaults={"email_enabled": True}
        )
        self.deliver()
        self.thread = Thread.objects.get()

    def test_the_raw_mail_carries_the_threading_headers(self):
        with transport.FakeSes() as ses:
            result = threads.send_email_reply(
                self.thread,
                "Tisdag 10.30 går bra.",
                actor=mock.Mock(user=self.anna, label="Anna", staff=False),
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(ses.messages), 1)
        sent = ses.messages[0]
        self.assertEqual(sent["To"], ANNA_EMAIL)
        self.assertEqual(sent["In-Reply-To"], "<CAabc123@mail.kund.example>")
        reply_to = tokens.read_reply_address(str(sent["Reply-To"]))
        self.assertEqual((reply_to.kind, reply_to.object_id), (tokens.THREAD, self.thread.pk))
        self.assertEqual(str(sent["Subject"]), "Sv: Höstservice värmepump")
        self.assertIn("Tisdag 10.30 går bra.", sent.get_body(("plain",)).get_content())
        out = self.thread.messages.get(direction="out")
        self.assertEqual(out.email_message_id, "fake-0001")
