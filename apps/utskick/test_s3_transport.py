"""
E-posten ut (README D.6, D.5, D.8, F.8, J S3 test_s3_transport): rå MIME med
List-Unsubscribe och ettklicket, konfigurationssetet och taggarna,
sändningsklienten utan omförsök, broms från SES (tillbaka i kön),
läs-timeout och 5xx (unknown, skickas aldrig igen), SES som pausar kontot,
MAIL FROM, nekade mejl, adoptionen av ett oklart mejl, testmejlet, svar och
provmejl genom deliver, och demokontot som aldrig skickar.

Inget når nätet: SES är transport.FakeSes, och aws.client byts mot en
attrapp där något annat läses. Renderaren (byggare A) byts mot fake_render,
så att testerna här prövar slingan och inte mejlets utseende.

    EmailFixture        kontakter, utskick och slingan; används av test_s3_caps,
                        test_s3_events och test_s3_queues
    fake_render()       renderaren och blocken som en enkel attrapp
"""

import time
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount
from apps.projects.models import Customer

from . import aws, keys, links, tokens
from . import consent as consents
from .access import Actor
from .email import mime, transport
from .inbound import events
from .models import (
    CHANNEL_EMAIL,
    Consent,
    ContactList,
    Event,
    Recipient,
    SenderDomain,
    Switchboard,
    Utskick,
    UtskickSettings,
)
from .sending import email as email_loop
from .sending import freeze, recover, sms_wrapper
from .testing import UtskickFixture, enable_utskick, make_contact

EVENTS_URL = "https://sqs.eu-west-1.amazonaws.com/500841883756/adx-utskick-events"
INBOUND_URL = "https://sqs.eu-west-1.amazonaws.com/500841883756/adx-utskick-inbound"
LIVE = override_settings(
    UTSKICK_EMAIL_LIVE=True,
    UTSKICK_SQS_EVENTS_URL=EVENTS_URL,
    UTSKICK_EMAIL_PER_SECOND=1000,
    UTSKICK_ADX_MONTHLY_MAIL_CAP=2000,
    UTSKICK_ADX_MAIL_DOMAIN="utskick.adx.se",
    UTSKICK_REPLY_DOMAIN="svar.utskick.adx.se",
    UTSKICK_EMAIL_LINK_BASE="https://klick.adx.se",
    INQUIRY_NOTIFICATION_EMAIL="byran@adx.example",
)
STAFF = Actor(label="ADX (Byra)", staff=True)
RS = Recipient.Status


def fake_render():
    """Renderaren (email/render.py, email/text.py) och blocken som en enkel
    attrapp: varje mejl blir "<p>Hej</p>" och "Hej", ämnesraden är
    utskickets. Används som context manager."""
    stack = ExitStack()

    def context_for(utskick, *, mode, recipient=None, contact=None, test=False, snapshot=None):
        return SimpleNamespace(
            utskick=utskick, mode=mode, recipient=recipient, contact=contact, test=test
        )

    patches = {
        "apps.utskick.email.render.context_for": context_for,
        "apps.utskick.email.render.render_html": lambda u, ctx, mode=None: "<p>Hej</p>",
        "apps.utskick.email.render.subject_for": lambda u, ctx: u.subject or "Hej",
        "apps.utskick.email.render.collect_links": lambda u, doc=None, data=None: [],
        "apps.utskick.email.render.snapshot": lambda u, now=None: {"blocks": []},
        "apps.utskick.email.text.render_text": lambda u, ctx: "Hej",
        "apps.utskick.email.blocks.active_blocks": lambda u, doc=None: [],
        "apps.utskick.email.blocks.media_ids": lambda blocks: [],
        "apps.utskick.email.blocks.urls": lambda blocks: [],
    }
    for target, value in patches.items():
        stack.enter_context(mock.patch(target, side_effect=value))
    return stack


class EmailFixture(UtskickFixture):
    """En kund med e-post påslagen: Switchboard, listan Kunder, kontakter
    med samtycke för e-post och utskick som fryses av den riktiga
    frysningen. Klockan är den riktiga (taken räknas på sent_at)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.kunder = ContactList.objects.create(account=cls.account, name="Kunder")
        now = timezone.now()
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK,
            defaults={"email_enabled": True, "email_ready_at": now, "doi_ready_at": now},
        )

    def setUp(self):
        super().setUp()
        LIVE.enable()
        self.addCleanup(LIVE.disable)
        stack = fake_render()
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        self.n = 0

    def person(self, status=consents.YES, account=None, contact_list=None, **data):
        account = account or self.account
        self.n += 1
        data.setdefault("first_name", f"Person{self.n}")
        data.setdefault("email", f"person{self.n}@kund{account.pk}.example")
        kontakt = make_contact(account, **data)
        if status in (consents.YES, consents.EXISTING):
            consents.set_status(
                kontakt,
                CHANNEL_EMAIL,
                status,
                source=Consent.Source.MANUAL,
                evidence="kassan, 2024",
            )
        target = contact_list or (self.kunder if account == self.account else None)
        if target is not None:
            target.memberships.create(contact=kontakt)
        return kontakt

    def people(self, n, **kwargs):
        return [self.person(**kwargs) for _ in range(n)]

    def utskick(self, account=None, **kwargs):
        account = account or self.account
        kwargs.setdefault("audience", {"lists": [self.kunder.pk]})
        kwargs.setdefault("status", Utskick.Status.FREEZING)
        kwargs.setdefault("channel_mode", Utskick.ChannelMode.EMAIL_ONLY)
        kwargs.setdefault("subject", "Höstservice värmepump")
        kwargs.setdefault("scheduled_at", timezone.now() - timedelta(minutes=1))
        return Utskick.objects.create(account=account, name="Höstservice värmepump", **kwargs)

    def freeze(self, u):
        for _ in range(100):
            result = freeze.freeze_chunk(u, timezone.now())
            if result is None or result["done"]:
                break
        u.refresh_from_db()
        return u

    def sending(self, n=3, **kwargs):
        self.people(n)
        u = self.freeze(self.utskick(**kwargs))
        self.assertEqual(u.status, Utskick.Status.SENDING, u.pause_reason)
        return u

    def run_email(self, seconds=30, only=None):
        with mock.patch("apps.utskick.sending.email.time.sleep"):
            return email_loop.send_due(timezone.now(), time.monotonic() + seconds, only)

    def statuses(self, u):
        return sorted(u.recipients.values_list("status", flat=True))

    def verified_domain(self, account=None, domain="exempelror.example", **kwargs):
        account = account or self.account
        data = {
            "from_name": "Exempelrör",
            "status": SenderDomain.Status.VERIFIED,
            "verified_at": timezone.now(),
            "probe_passed_at": timezone.now(),
            "ses_created": True,
        }
        data.update(kwargs)
        return SenderDomain.objects.create(account=account, domain=domain, **data)


def outgoing(to="anna@kund.example", **kwargs):
    data = {
        "to": to,
        "from_name": "Exempelrör",
        "from_addr": "exempelror@utskick.adx.se",
        "subject": "Hej",
        "text": "Hej",
    }
    data.update(kwargs)
    return transport.OutgoingMail(**data)


# ---------------------------------------------------------------------------
# Mejlet och anropet
# ---------------------------------------------------------------------------


class MimeTests(EmailFixture, TestCase):
    def test_the_raw_mail_carries_the_headers(self):
        u = self.sending(1)
        recipient = u.recipients.get()
        with transport.FakeSes() as ses:
            counts = self.run_email()
        self.assertEqual(counts.get("sent"), 1)
        message = ses.messages[0]
        self.assertEqual(str(message["To"]), recipient.address)
        self.assertEqual(message["From"].addresses[0].addr_spec, "exempelror@utskick.adx.se")
        self.assertEqual(message["From"].addresses[0].display_name, "Exempelrör")
        self.assertEqual(str(message["Subject"]), "Höstservice värmepump")
        self.assertEqual(
            str(message["Reply-To"]),
            tokens.reply_address(tokens.REPLY, self.account.pk, recipient.pk),
        )
        unsubscribe = str(message["List-Unsubscribe"])
        https, mailto = [part.strip(" <>") for part in unsubscribe.split(",")]
        self.assertTrue(https.startswith("https://klick.adx.se/a/"))
        value_hash = keys.value_hash(CHANNEL_EMAIL, recipient.address)
        self.assertEqual(https, links.unsubscribe_url(self.account.pk, value_hash))
        self.assertEqual(mailto, links.mailto_unsubscribe(self.account.pk, recipient.pk))
        self.assertEqual(str(message["List-Unsubscribe-Post"]), mime.ONE_CLICK)
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        parts = [part.get_content_type() for part in message.iter_parts()]
        self.assertEqual(parts, ["text/plain", "text/html"])
        self.assertEqual(mime.text_part(message, "html").strip(), "<p>Hej</p>")

    def test_configuration_set_and_tags(self):
        u = self.sending(1)
        recipient = u.recipients.get()
        with transport.FakeSes() as ses:
            self.run_email()
        call = ses.sent[0]
        self.assertEqual(call["ConfigurationSetName"], "adx-utskick")
        self.assertEqual(
            ses.tags(),
            {
                "k": "utskick",
                "a": str(self.account.pk),
                "u": str(u.pk),
                "r": str(recipient.pk),
            },
        )
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.SENT)
        self.assertEqual(recipient.ses_message_id, "fake-0001")
        self.assertIsNotNone(recipient.sent_at)

    def test_no_configuration_set_before_the_event_queue_exists(self):
        with override_settings(UTSKICK_SQS_EVENTS_URL=""), transport.FakeSes() as ses:
            self.assertEqual(transport.configuration_set(), "")
            transport.send(outgoing(), kind=transport.PROBE)
        self.assertNotIn("ConfigurationSetName", ses.calls[0])
        self.assertEqual(ses.tags(), {"k": "probe"})

    def test_the_doi_mail_gets_the_set_and_its_tags(self):
        with transport.FakeSes() as ses:
            transport.send(outgoing(), kind=transport.DOI, account_id=self.account.pk)
        self.assertEqual(ses.calls[0]["ConfigurationSetName"], "adx-utskick")
        self.assertEqual(ses.tags(), {"k": "doi", "a": str(self.account.pk)})

    def test_the_send_client_never_retries(self):
        config = aws._config(send=True)
        self.assertEqual(config.retries, {"max_attempts": 1, "mode": "standard"})
        client = mock.Mock()
        client.send_email.return_value = {"MessageId": "ses-1"}
        with mock.patch("apps.utskick.aws.client", return_value=client) as made:
            result = transport.send(outgoing(), kind=transport.PROBE)
        made.assert_called_once_with("sesv2", send=True)
        self.assertTrue(result.ok)
        self.assertEqual(client.send_email.call_count, 1)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            transport.send(outgoing(), kind="nyhetsbrev")


# ---------------------------------------------------------------------------
# Felen från SES (D.6)
# ---------------------------------------------------------------------------


class ErrorTests(EmailFixture, TestCase):
    def test_throttling_requeues_and_halves_the_rate(self):
        u = self.sending(2)
        with transport.FakeSes(script=["TooManyRequestsException"]) as ses:
            with mock.patch.object(email_loop.Context, "throttled", autospec=True) as slow:
                counts = self.run_email()
        self.assertEqual(slow.call_count, 1)
        self.assertEqual(counts.get("throttled"), 1)
        self.assertEqual(counts.get("sent"), 2)
        self.assertEqual(len(ses.calls), 3)
        self.assertEqual(self.statuses(u), [RS.SENT, RS.SENT])
        self.assertEqual(sorted(u.recipients.values_list("attempts", flat=True)), [1, 1])

    def test_throttled_halves_the_rate_and_sleeps(self):
        ctx = email_loop.Context(rate=10)
        with mock.patch("apps.utskick.sending.email.time.sleep") as sleep:
            ctx.throttled()
        sleep.assert_called_once_with(email_loop.THROTTLE_SLEEP)
        self.assertEqual(ctx.rate, 5)

    def test_a_429_status_is_a_throttle_too(self):
        with transport.FakeSes(fail="SomethingElse", status=429):
            result = transport.send(outgoing(), kind=transport.PROBE)
        self.assertTrue(result.retry)
        self.assertFalse(result.stop)

    def test_a_read_timeout_is_unknown_and_never_resent(self):
        from botocore.exceptions import ReadTimeoutError

        u = self.sending(1)
        timeout = ReadTimeoutError(endpoint_url="https://email.eu-west-1.amazonaws.com")
        with transport.FakeSes(script=[timeout]) as ses:
            counts = self.run_email()
            again = self.run_email()
        self.assertEqual(counts.get("unknown"), 1)
        self.assertEqual(again, {})
        self.assertEqual(len(ses.calls), 1)
        recipient = u.recipients.get()
        self.assertEqual(recipient.status, RS.UNKNOWN)
        self.assertIsNotNone(recipient.sent_at)

    def test_a_5xx_is_unknown(self):
        u = self.sending(1)
        with transport.FakeSes(script=[("InternalFailure", 500)]):
            self.run_email()
        self.assertEqual(self.statuses(u), [RS.UNKNOWN])

    def test_a_connection_that_never_got_there_waits(self):
        from botocore.exceptions import EndpointConnectionError

        u = self.sending(2)
        error = EndpointConnectionError(endpoint_url="https://email.eu-west-1.amazonaws.com")
        with transport.FakeSes(script=[error]) as ses:
            counts = self.run_email()
        self.assertEqual(counts.get("stopped"), 1)
        self.assertEqual(len(ses.calls), 1)
        self.assertEqual(self.statuses(u), [RS.QUEUED, RS.QUEUED])

    def test_unknown_becomes_failed_after_24_hours(self):
        u = self.sending(1)
        old = timezone.now() - timedelta(hours=25)
        u.recipients.update(status=RS.UNKNOWN, sent_at=old, claimed_at=old)
        self.assertEqual(email_loop.stale_unknown(), 1)
        recipient = u.recipients.get()
        self.assertEqual(recipient.status, RS.FAILED)
        self.assertEqual(recipient.error, email_loop.UNKNOWN_TEXT)

    def test_ses_pausing_the_account_turns_email_off(self):
        u = self.sending(2)
        with transport.FakeSes(script=[("SendingPausedException", 400)]) as ses:
            counts = self.run_email()
        self.assertEqual(counts.get("ses_paused"), 1)
        self.assertEqual(len(ses.calls), 1)
        self.assertEqual(self.statuses(u), [RS.QUEUED, RS.QUEUED])
        self.assertFalse(Switchboard.get_solo().email_enabled)
        subjects = [m.subject for m in mail.outbox]
        self.assertIn("Utskick: SES har pausat e-posten", subjects)
        self.assertTrue(all(m.to == ["byran@adx.example"] for m in mail.outbox))
        # Utan e-post väntar utskicket (D.8), inget mer skickas.
        with transport.FakeSes() as ses:
            self.assertEqual(self.run_email(), {"email_off": 1})
        self.assertEqual(ses.calls, [])

    def test_mail_from_not_verified_pauses_the_utskick(self):
        domain = self.verified_domain()
        UtskickSettings.objects.filter(account=self.account).update(
            email_probe_passed_at=timezone.now()
        )
        u = self.sending(2, sender_domain=domain)
        with transport.FakeSes(script=[("MailFromDomainNotVerifiedException", 400)]):
            self.run_email()
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.PROVIDER)
        self.assertEqual(self.statuses(u), [RS.QUEUED, RS.QUEUED])

    def test_a_rejected_mail_fails_and_five_in_a_row_pause(self):
        u = self.sending(6)
        with transport.FakeSes(fail="MessageRejected") as ses:
            counts = self.run_email()
        self.assertEqual(counts.get("failed"), 5)
        self.assertEqual(len(ses.calls), 5)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.PROVIDER)
        failed = u.recipients.filter(status=RS.FAILED).first()
        self.assertIn("MessageRejected", failed.error)
        self.assertIsNone(failed.sent_at)

    def test_without_the_event_queue_production_waits_and_alerts(self):
        u = self.sending(1)
        with override_settings(UTSKICK_SQS_EVENTS_URL=""), transport.FakeSes() as ses:
            counts = self.run_email()
        self.assertEqual(counts, {"no_events": 1})
        self.assertEqual(ses.calls, [])
        self.assertEqual(self.statuses(u), [RS.QUEUED])
        self.assertIn("Utskick: e-postutskicken väntar på händelsekön", mail.outbox[0].subject)


# ---------------------------------------------------------------------------
# Återhämtningen och adoptionen (D.5)
# ---------------------------------------------------------------------------


class RecoveryTests(EmailFixture, TestCase):
    def test_a_stale_sending_mail_becomes_unknown_and_a_send_event_adopts_it(self):
        u = self.sending(1)
        recipient = u.recipients.get()
        old = timezone.now() - timedelta(minutes=10)
        Recipient.objects.filter(pk=recipient.pk).update(status=RS.SENDING, claimed_at=old)
        self.assertEqual(recover.recover_stale().get("unknown"), 1)
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(ses.calls, [], "ett oklart mejl skickas aldrig igen")
        event = {
            "eventType": "Send",
            "mail": {
                "messageId": "0102-ses-1",
                "timestamp": "2026-10-10T10:00:00.000Z",
                "tags": {
                    "a": [str(self.account.pk)],
                    "u": [str(u.pk)],
                    "r": [str(recipient.pk)],
                },
            },
        }
        self.assertEqual(events.apply(event), events.APPLIED)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.SENT)
        self.assertEqual(recipient.ses_message_id, "0102-ses-1")


# ---------------------------------------------------------------------------
# Avsändaren och svarsadressen
# ---------------------------------------------------------------------------


class SenderTests(EmailFixture, TestCase):
    def test_the_adx_domain_uses_the_public_slug(self):
        self.assertEqual(
            email_loop.from_for(self.account), ("Exempelrör", "exempelror@utskick.adx.se")
        )

    def test_a_verified_own_domain(self):
        domain = self.verified_domain(from_local="hej", from_name="Exempelrör AB")
        u = self.utskick(sender_domain=domain, from_name="Johan på Exempelrör")
        self.assertEqual(
            email_loop.from_for(self.account, u),
            ("Johan på Exempelrör", "hej@exempelror.example"),
        )
        self.assertEqual(
            email_loop.from_for(self.account, sender_domain=domain),
            ("Exempelrör AB", "hej@exempelror.example"),
        )

    def test_another_accounts_or_a_pending_domain_falls_back(self):
        foreign = self.verified_domain(account=self.other_account, domain="annan.example")
        pending = SenderDomain.objects.create(
            account=self.account, domain="ny.example", from_name="Exempelrör"
        )
        for domain in (foreign, pending):
            with self.subTest(domain=domain.domain):
                self.assertEqual(
                    email_loop.from_for(self.account, sender_domain=domain)[1],
                    "exempelror@utskick.adx.se",
                )

    def test_reply_to_the_inbox_or_a_confirmed_own_address(self):
        u = self.sending(1)
        recipient = u.recipients.get()
        token = tokens.reply_address(tokens.REPLY, self.account.pk, recipient.pk)
        self.assertEqual(email_loop.reply_to_for(self.account, recipient=recipient), token)
        UtskickSettings.objects.filter(account=self.account).update(
            email_reply_mode=UtskickSettings.REPLY_OWN, own_reply_to="anna@exempelror.se"
        )
        # Inte bekräftad och inte på en verifierad domän: Inkorgen.
        self.assertEqual(email_loop.reply_to_for(self.account, recipient=recipient), token)
        UtskickSettings.objects.filter(account=self.account).update(
            own_reply_to_confirmed_at=timezone.now()
        )
        self.assertEqual(
            email_loop.reply_to_for(self.account, recipient=recipient), "anna@exempelror.se"
        )
        thread = SimpleNamespace(pk=77)
        self.assertEqual(
            email_loop.reply_to_for(self.account, thread=thread),
            tokens.reply_address(tokens.THREAD, self.account.pk, 77),
        )

    def test_an_own_address_on_a_verified_domain_needs_no_link(self):
        self.verified_domain()
        UtskickSettings.objects.filter(account=self.account).update(
            email_reply_mode=UtskickSettings.REPLY_OWN, own_reply_to="hej@exempelror.example"
        )
        self.assertEqual(email_loop.reply_to_for(self.account), "hej@exempelror.example")


# ---------------------------------------------------------------------------
# Enstaka mejl: test, svar och prov (F.8, G.2, J S3 steg 7)
# ---------------------------------------------------------------------------


class DeliverTests(EmailFixture, TestCase):
    def test_the_test_mail(self):
        u = self.utskick(status=Utskick.Status.DRAFT)
        kontakt = self.person()
        actor = Actor(user=self.anna, label="Anna Lindqvist")
        with transport.FakeSes() as ses:
            sent = email_loop.send_test(
                u, address="Anna@Exempelror.example", contact=kontakt, actor=actor
            )
        self.assertTrue(sent.ok, sent.error)
        message = ses.messages[0]
        self.assertEqual(str(message["Subject"]), "Test: Höstservice värmepump")
        self.assertEqual(str(message["To"]), "anna@exempelror.example")
        self.assertEqual(
            str(message["Reply-To"]),
            tokens.reply_address(tokens.REPLY, self.account.pk, email_loop.NO_RECIPIENT),
        )
        # Ett tryck på Avsluta prenumerationen i ett test avregistrerar ingen.
        self.assertNotIn("List-Unsubscribe", message)
        self.assertEqual(ses.tags(), {"k": "test", "a": str(self.account.pk), "u": str(u.pk)})
        self.assertTrue(Event.objects.filter(contact=kontakt, kind="test_send").exists())
        self.assertEqual(email_loop.adx_month_count(self.account), 1)

    def test_ten_tests_a_day(self):
        u = self.utskick(status=Utskick.Status.DRAFT)
        actor = Actor(user=self.anna, label="Anna Lindqvist")
        with transport.FakeSes() as ses:
            results = [
                email_loop.send_test(u, address="anna@exempelror.example", actor=actor)
                for _ in range(11)
            ]
        self.assertEqual(len(ses.calls), 10)
        self.assertEqual(results[-1].error, "test_limit")
        self.assertIn("10 test i dag", email_loop.error_text(results[-1]))

    def test_the_test_mail_is_refused_when_it_should_be(self):
        u = self.utskick(status=Utskick.Status.DRAFT)
        actor = Actor(user=self.anna, label="Anna Lindqvist")
        from . import suppression

        suppression.add(
            self.account,
            CHANNEL_EMAIL,
            keys.value_hash(CHANNEL_EMAIL, "sparrad@exempelror.example"),
            "link",
        )
        with transport.FakeSes() as ses:
            self.assertEqual(
                email_loop.send_test(u, address="inte en adress", actor=actor).error, "address"
            )
            self.assertEqual(
                email_loop.send_test(u, address="sparrad@exempelror.example", actor=actor).error,
                "suppressed",
            )
            Switchboard.objects.update(email_enabled=False)
            self.assertEqual(
                email_loop.send_test(u, address="anna@exempelror.example", actor=actor).error,
                "email_off",
            )
        self.assertEqual(ses.calls, [])

    def test_a_reply_counts_against_the_adx_cap_and_a_probe_does_not(self):
        with transport.FakeSes() as ses:
            reply = email_loop.deliver(self.account, outgoing(), kind=transport.REPLY)
            probe = email_loop.deliver(self.account, outgoing(), kind=transport.PROBE)
        self.assertTrue(reply.ok and probe.ok)
        self.assertEqual(len(ses.calls), 2)
        self.assertEqual(email_loop.adx_month_count(self.account), 1)

    def test_a_failed_reply_is_not_counted(self):
        with transport.FakeSes(fail="MessageRejected"):
            sent = email_loop.deliver(self.account, outgoing(), kind=transport.REPLY)
        self.assertFalse(sent.ok)
        self.assertEqual(email_loop.adx_month_count(self.account), 0)

    def test_the_probe_works_before_email_is_on(self):
        Switchboard.objects.update(email_enabled=False)
        mail_out = email_loop.probe_mail(self.account, "byra@adx.example", staff_name="ADX (Byra)")
        with transport.FakeSes() as ses:
            sent = email_loop.deliver(self.account, mail_out, kind=transport.PROBE)
        self.assertTrue(sent.ok)
        message = ses.messages[0]
        self.assertIn("List-Unsubscribe-Post", message)
        self.assertEqual(ses.tags(), {"k": "probe", "a": str(self.account.pk)})

    def test_a_reply_waits_while_email_is_off(self):
        Switchboard.objects.update(email_enabled=False)
        with transport.FakeSes() as ses:
            sent = email_loop.deliver(self.account, outgoing(), kind=transport.REPLY)
        self.assertEqual(sent.error, "email_off")
        self.assertEqual(ses.calls, [])
        self.assertEqual(email_loop.error_text(sent), "E-post är inte påslaget än.")

    def test_deliver_never_sends_the_doi(self):
        with self.assertRaises(ValueError):
            email_loop.deliver(self.account, outgoing(), kind=transport.DOI)


# ---------------------------------------------------------------------------
# Demokontot (D12)
# ---------------------------------------------------------------------------


class DemoTests(EmailFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        customer = Customer.objects.create(name="Demo AB")
        cls.demo = FlamingoAccount.objects.create(customer=customer, is_enabled=True, is_demo=True)
        enable_utskick(cls.demo, "demo", "Demo")

    def test_the_transport_refuses_the_demo(self):
        with transport.FakeSes() as ses, self.assertRaises(sms_wrapper.DemoRefused):
            transport.send(outgoing(), kind=transport.UTSKICK, account_id=self.demo.pk)
        self.assertEqual(ses.calls, [])

    def test_every_path_refuses_or_simulates_the_demo(self):
        demo_list = ContactList.objects.create(account=self.demo, name="Demo")
        self.people(2, account=self.demo, contact_list=demo_list)
        u = self.freeze(self.utskick(account=self.demo, audience={"lists": [demo_list.pk]}))
        self.assertEqual(u.status, Utskick.Status.SENDING)
        with transport.FakeSes() as ses:
            counts = self.run_email()
            reply = email_loop.deliver(self.demo, outgoing(), kind=transport.REPLY)
            test = email_loop.send_test(
                u, address="anna@exempelror.example", actor=Actor(label="Demo")
            )
        self.assertEqual(ses.calls, [])
        self.assertEqual(counts.get("simulated"), 2)
        self.assertEqual(self.statuses(u), [RS.DELIVERED, RS.DELIVERED])
        self.assertTrue(all(u.recipients.values_list("simulated", flat=True)))
        self.assertEqual(reply.error, "demo")
        self.assertEqual(test.error, "demo")
        self.assertEqual(email_loop.error_text(test), "Demokontot skickar aldrig.")


# ---------------------------------------------------------------------------
# Frysningen av mejlet (D.3, F.4) och länkarna före den (E.8)
# ---------------------------------------------------------------------------


class FreezeEmailTests(EmailFixture, TestCase):
    BLOCK = "b_Ab12Cd34Ef56"

    def spots(self, *urls):
        from .email.render import LinkSpot

        return [LinkSpot(self.BLOCK, i, url, f"Länk {i}") for i, url in enumerate(urls)]

    def test_the_links_become_tracked_links_in_the_snapshot(self):
        from .models import TrackedLink

        spots = self.spots("https://exempelror.example/boka", "mailto:hej@exempelror.example")
        with mock.patch("apps.utskick.email.render.collect_links", return_value=spots):
            u = self.sending(2)
        link = TrackedLink.objects.get(utskick=u)
        self.assertEqual(link.kind, TrackedLink.Kind.EXTERNAL)
        self.assertEqual((link.block_id, link.position), (self.BLOCK, 0))
        self.assertEqual(link.destination, "https://exempelror.example/boka")
        self.assertEqual(u.email_snapshot["links"], {f"{self.BLOCK}:0": link.pk})
        self.assertEqual(u.email_snapshot["blocks"], [])

    def test_a_link_waiting_for_adx_pauses_with_content(self):
        spots = self.spots("https://ny-vard.example/erbjudande")
        self.people(1)
        with mock.patch("apps.utskick.email.render.collect_links", return_value=spots):
            u = self.freeze(self.utskick())
        self.assertEqual(u.status, Utskick.Status.PAUSED)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.CONTENT)

    def test_foreign_images_pause_with_content(self):
        self.people(1)
        with mock.patch("apps.utskick.email.blocks.media_ids", return_value=[999999]):
            u = self.freeze(self.utskick())
        self.assertEqual(u.pause_reason, Utskick.PauseReason.CONTENT)
        self.assertIn("bilder", u.stats["pause"]["note"])

    def test_a_renderer_error_pauses_instead_of_failing_every_tick(self):
        self.people(1)
        with mock.patch("apps.utskick.email.render.snapshot", side_effect=RuntimeError("trasigt")):
            u = self.freeze(self.utskick())
        self.assertEqual(u.pause_reason, Utskick.PauseReason.CONTENT)
        self.assertEqual(u.stats["pause"]["note"], email_loop.NOT_BUILT_TEXT)

    def test_the_mail_links_are_checked_before_the_freeze(self):
        from .links import PENDING_TEXT
        from .sending import state

        u = self.utskick(status=Utskick.Status.DRAFT, email_doc={"blocks": [{"id": "x"}]})
        self.assertEqual(state.content_problems(u), [])
        with mock.patch(
            "apps.utskick.email.blocks.urls",
            return_value=["https://ny-vard.example/x", "tel:+46701740601"],
        ):
            self.assertEqual(state.content_problems(u), [PENDING_TEXT])
        with mock.patch(
            "apps.utskick.email.blocks.urls", return_value=["https://exempelror.example/x"]
        ):
            self.assertEqual(state.content_problems(u), [])
