"""
SES-händelserna och leveranshälsan (README D.7, D.9, H.6, J S3
test_s3_events): leverans, permanent studs, tillfälliga studsar fem i rad,
klagomål, OnAccountSuppressionList, taggar som inte stämmer, mejl utan
mottagare (bekräftelsemejlen), gränserna per utskick och per konto, provets
väntan, spärren och "Släpp spärren", och sidan Leveranshälsa.

Händelserna är SES egna JSON-former (rå leverans från SNS till SQS).
"""

from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import consent as consents
from . import keys
from .access import Actor
from .email import transport
from .inbound import events
from .models import (
    CHANNEL_EMAIL,
    Consent,
    Contact,
    Recipient,
    Suppression,
    Utskick,
    UtskickSettings,
)
from .sending import health, state
from .test_s3_transport import EmailFixture

RS = Recipient.Status
STAFF = Actor(label="ADX (Byra)", staff=True)


def event(kind, recipient=None, *, account=None, message_id="", tags=None, **section):
    """En SES-händelse som konfigurationssetet publicerar den."""
    if tags is None:
        tags = {}
        if recipient is not None:
            tags = {
                "a": [str(account or recipient.utskick.account_id)],
                "u": [str(recipient.utskick_id)],
                "r": [str(recipient.pk)],
                "k": ["utskick"],
            }
    body = {
        "eventType": kind,
        "mail": {
            "timestamp": "2026-10-10T10:00:00.000Z",
            "messageId": message_id or (recipient.ses_message_id if recipient else "") or "m-1",
            "destination": [recipient.address] if recipient else [],
            "tags": tags,
        },
    }
    body.update(section)
    return body


def bounce(recipient, kind="Permanent", subtype="General", address=None, **kwargs):
    address = address or recipient.address
    return event(
        "Bounce",
        recipient,
        bounce={
            "bounceType": kind,
            "bounceSubType": subtype,
            "bouncedRecipients": [{"emailAddress": address}],
        },
        **kwargs,
    )


def complaint(recipient, subtype=None, **kwargs):
    section = {"complainedRecipients": [{"emailAddress": recipient.address}]}
    if subtype:
        section["complaintSubType"] = subtype
    return event("Complaint", recipient, complaint=section, **kwargs)


def delivery(recipient, **kwargs):
    return event(
        "Delivery",
        recipient,
        delivery={"timestamp": "2026-10-10T10:00:02.000Z", "recipients": [recipient.address]},
        **kwargs,
    )


class EventFixture(EmailFixture):
    def sent(self, n=1, **kwargs):
        """Ett utskick där n mejl skickats (FakeSes)."""
        u = self.sending(n, **kwargs)
        with transport.FakeSes():
            self.run_email()
        return u, list(u.recipients.order_by("pk"))


# ---------------------------------------------------------------------------
# Händelserna
# ---------------------------------------------------------------------------


class EventTests(EventFixture, TestCase):
    def test_delivery_moves_forward_and_resets_the_soft_counter(self):
        _u, (recipient,) = self.sent()
        Contact.objects.filter(pk=recipient.contact_id).update(email_soft_bounces=3)
        self.assertEqual(events.apply(delivery(recipient)), events.APPLIED)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.DELIVERED)
        self.assertIsNotNone(recipient.delivered_at)
        self.assertEqual(Contact.objects.get(pk=recipient.contact_id).email_soft_bounces, 0)

    def test_a_late_delivery_never_undoes_a_complaint(self):
        _u, (recipient,) = self.sent()
        events.apply(complaint(recipient))
        events.apply(delivery(recipient))
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.COMPLAINED)

    def test_a_delivery_before_the_loop_wrote_is_kept(self):
        u = self.sending(1)
        recipient = u.recipients.get()

        def deliver_first(**kwargs):
            # Händelsen hinner före slingans skrivning (D.5).
            fresh = Recipient.objects.get(pk=recipient.pk)
            fresh.ses_message_id = ""
            events.apply(delivery(fresh, message_id="fake-0001"))
            return {"MessageId": "fake-0001"}

        with transport.FakeSes() as ses:
            ses.send_email = deliver_first
            self.run_email()
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.DELIVERED)
        self.assertEqual(recipient.ses_message_id, "fake-0001")

    def test_a_hard_bounce(self):
        _u, (recipient,) = self.sent()
        self.assertEqual(events.apply(bounce(recipient)), events.APPLIED)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.BOUNCED)
        kontakt = Contact.objects.get(pk=recipient.contact_id)
        self.assertEqual(kontakt.email_state, Contact.EmailState.BOUNCED)
        self.assertIsNotNone(kontakt.email_bounced_at)
        row = Suppression.objects.get(
            account=self.account,
            channel=CHANNEL_EMAIL,
            value_hash=keys.value_hash(CHANNEL_EMAIL, recipient.address),
        )
        self.assertEqual(row.reason, Suppression.Reason.BOUNCE)
        self.assertEqual(row.utskick_id, recipient.utskick_id)
        # Samtycket står kvar: en studs är ingen avregistrering (H.6).
        self.assertEqual(kontakt.consents.get(channel=CHANNEL_EMAIL).status, consents.YES)

    def test_a_bounce_after_delivery_still_counts(self):
        _u, (recipient,) = self.sent()
        events.apply(delivery(recipient))
        events.apply(bounce(recipient))
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.BOUNCED)

    def test_on_account_suppression_list_only_fails_the_recipient(self):
        u, (recipient,) = self.sent()
        events.apply(bounce(recipient, subtype="OnAccountSuppressionList"))
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.FAILED)
        self.assertEqual(recipient.skip_reason, Recipient.SkipReason.SES_SUPPRESSED)
        kontakt = Contact.objects.get(pk=recipient.contact_id)
        self.assertEqual(kontakt.email_state, Contact.EmailState.OK)
        self.assertFalse(Suppression.objects.filter(account=self.account).exists())
        verdict = health.utskick_health(u)
        self.assertEqual((verdict.bounced, verdict.outcomes), (0, 0))

    def test_five_soft_bounces_in_a_row(self):
        kontakt = None
        for round_ in range(5):
            u = self.sending(1) if round_ == 0 else self.freeze(self.utskick())
            with transport.FakeSes():
                self.run_email()
            recipient = u.recipients.filter(contact__isnull=False).order_by("-pk").first()
            events.apply(bounce(recipient, kind="Transient", subtype="MailboxFull"))
            recipient.refresh_from_db()
            kontakt = Contact.objects.get(pk=recipient.contact_id)
            if round_ < 4:
                self.assertEqual(recipient.status, RS.FAILED)
                self.assertEqual(recipient.error, events.SOFT_TEXT)
                self.assertEqual(kontakt.email_soft_bounces, round_ + 1)
                self.assertEqual(kontakt.email_state, Contact.EmailState.OK)
            # Veckotaket (fyra mejl) får inte stoppa provet.
            Recipient.objects.filter(pk=recipient.pk).update(
                sent_at=timezone.now() - timedelta(days=8)
            )
        self.assertEqual(recipient.status, RS.BOUNCED)
        self.assertEqual(kontakt.email_state, Contact.EmailState.BOUNCED)
        self.assertTrue(
            Suppression.objects.filter(
                account=self.account, reason=Suppression.Reason.BOUNCE
            ).exists()
        )

    def test_a_complaint_unsubscribes(self):
        _u, (recipient,) = self.sent()
        events.apply(complaint(recipient))
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.COMPLAINED)
        kontakt = Contact.objects.get(pk=recipient.contact_id)
        row = kontakt.consents.get(channel=CHANNEL_EMAIL)
        self.assertEqual(row.status, consents.UNSUBSCRIBED)
        self.assertEqual(row.source, Consent.Source.COMPLAINT)
        suppression = Suppression.objects.get(account=self.account, channel=CHANNEL_EMAIL)
        self.assertEqual(suppression.reason, Suppression.Reason.COMPLAINT)

    def test_reject_and_rendering_failure(self):
        _u, (a, b) = self.sent(2)
        events.apply(event("Reject", a, reject={"reason": "Bad content"}))
        events.apply(event("Rendering Failure", b, failure={"errorMessage": "x"}))
        self.assertEqual(Recipient.objects.get(pk=a.pk).status, RS.FAILED)
        self.assertEqual(Recipient.objects.get(pk=b.pk).status, RS.FAILED)

    def test_delays_opens_and_clicks_change_nothing(self):
        _u, (recipient,) = self.sent()
        for kind in ("DeliveryDelay", "Open", "Click", "Subscription"):
            with self.subTest(kind=kind):
                self.assertEqual(events.apply(event(kind, recipient)), events.IGNORED)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.SENT)
        self.assertIsNone(recipient.opened_at)

    def test_tags_that_do_not_match_find_nobody(self):
        _u, (recipient,) = self.sent()
        wrong_account = bounce(recipient, account=self.other_account.pk)
        self.assertEqual(events.apply(wrong_account), events.UNKNOWN_RECIPIENT)
        wrong_id = bounce(recipient, message_id="another-message")
        self.assertEqual(events.apply(wrong_id), events.UNKNOWN_RECIPIENT)
        missing = event("Bounce", tags={"a": [str(self.account.pk)], "r": ["999999"]})
        self.assertEqual(events.apply(missing), events.UNKNOWN_RECIPIENT)
        recipient.refresh_from_db()
        self.assertEqual(recipient.status, RS.SENT)

    def test_a_bounced_confirmation_mail_marks_the_contact(self):
        kontakt = self.person(status=consents.MISSING, email="ny@kund.example")
        body = event(
            "Bounce",
            tags={"k": ["doi"], "a": [str(self.account.pk)]},
            bounce={
                "bounceType": "Permanent",
                "bounceSubType": "NoEmail",
                "bouncedRecipients": [{"emailAddress": "ny@kund.example"}],
            },
        )
        self.assertEqual(events.apply(body), events.APPLIED)
        kontakt.refresh_from_db()
        self.assertEqual(kontakt.email_state, Contact.EmailState.BOUNCED)
        self.assertTrue(Suppression.objects.filter(account=self.account).exists())

    def test_a_probe_without_a_recipient_touches_no_contact(self):
        kontakt = self.person(email="byra@adx.example")
        body = event(
            "Complaint",
            tags={"k": ["probe"], "a": [str(self.account.pk)]},
            complaint={"complainedRecipients": [{"emailAddress": "byra@adx.example"}]},
        )
        self.assertEqual(events.apply(body), events.IGNORED)
        self.assertEqual(kontakt.consents.get(channel=CHANNEL_EMAIL).status, consents.YES)

    def test_the_receipt_key(self):
        _u, (recipient,) = self.sent()
        self.assertEqual(
            events.receipt_key(delivery(recipient)), f"{recipient.ses_message_id}:Delivery"
        )


# ---------------------------------------------------------------------------
# Gränserna (D.9)
# ---------------------------------------------------------------------------


class ThresholdTests(EventFixture, TestCase):
    def outcomes(self, u, delivered=0, bounced=0, complained=0):
        """Mottagare med de utfallen direkt i databasen (snabbare än 200
        riktiga mejl); sent_at är nu."""
        now = timezone.now()
        rows = []
        for status, n in (
            (RS.DELIVERED, delivered),
            (RS.BOUNCED, bounced),
            (RS.COMPLAINED, complained),
        ):
            rows += [
                Recipient(
                    utskick=u,
                    channel=CHANNEL_EMAIL,
                    address=f"x{status}{i}@kund.example",
                    status=status,
                    sent_at=now,
                )
                for i in range(n)
            ]
        Recipient.objects.bulk_create(rows)

    def test_four_percent_bounces_of_200_pause_the_utskick(self):
        u = self.sending(1)
        self.outcomes(u, delivered=192, bounced=7)
        self.assertEqual(health.check_utskick(u), "")
        self.outcomes(u, bounced=1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(health.check_utskick(u), Utskick.PauseReason.BOUNCES)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED_HEALTH)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.BOUNCES)
        note = u.stats["pause"]["note"]
        self.assertIn("4\u00a0% av de första 200 mejlen studsade", note)
        self.assertIn("Vi pausar vid 4 %, före AWS gräns på 5 %.", note)
        self.assertEqual(mail.outbox[-1].to, ["byran@adx.example"])

    def test_under_200_outcomes_never_pause(self):
        u = self.sending(1)
        self.outcomes(u, delivered=100, bounced=50)
        self.assertEqual(health.check_utskick(u), "")

    def test_two_complaints_at_0_08_percent(self):
        u = self.sending(1)
        # 2 av 2 402 levererade är 0,083 %; ett enda klagomål pausar aldrig.
        self.outcomes(u, delivered=2400, complained=1)
        self.assertEqual(health.check_utskick(u), "")
        self.outcomes(u, complained=1)
        self.assertEqual(health.check_utskick(u), Utskick.PauseReason.COMPLAINTS)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED_HEALTH)
        # Klagomål släpper bara byrån (I.5).
        result = state.resume(u, actor=Actor(label="Anna Lindqvist"))
        self.assertFalse(result.ok)

    def test_a_bounce_event_runs_the_check(self):
        u, (recipient,) = self.sent()
        self.outcomes(u, delivered=195, bounced=8)
        events.apply(bounce(recipient))
        u.refresh_from_db()
        self.assertEqual(u.pause_reason, Utskick.PauseReason.BOUNCES)

    def test_a_resumed_utskick_is_judged_on_what_came_after(self):
        u = self.sending(1)
        self.outcomes(u, delivered=190, bounced=10)
        health.check_utskick(u)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED_HEALTH)
        later = timezone.now() + timedelta(seconds=1)
        result = state.resume(u, actor=STAFF, now=later)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(health.check_utskick(u), "")

    def test_the_account_is_blocked_after_500_and_released(self):
        u = self.sending(1)
        other = self.freeze(self.utskick())
        self.outcomes(u, delivered=480, bounced=21)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(health.check_account(self.account))
        self.assertTrue(health.is_blocked(self.account))
        row = UtskickSettings.objects.get(account=self.account)
        self.assertEqual(row.email_blocked_reason, Utskick.PauseReason.BOUNCES)
        other.refresh_from_db()
        self.assertEqual(other.status, Utskick.Status.PAUSED_HEALTH)
        self.assertEqual(other.pause_reason, Utskick.PauseReason.ACCOUNT_HEALTH)
        self.assertIn("Utskick: e-posten spärrad för en kund", [m.subject for m in mail.outbox])
        # Ett nytt utskick pausas redan i förkontrollerna.
        third = self.freeze(self.utskick())
        self.assertEqual(third.pause_reason, Utskick.PauseReason.ACCOUNT_HEALTH)
        # Släpp spärren: bara det som skickas efteråt räknas.
        self.assertTrue(health.release(self.account, actor=self.staff))
        row.refresh_from_db()
        self.assertIsNone(row.email_blocked_at)
        self.assertEqual(row.email_released_by, self.staff)
        self.assertFalse(health.check_account(self.account))

    def test_fewer_than_500_never_block(self):
        u = self.sending(1)
        self.outcomes(u, delivered=200, bounced=100)
        self.assertFalse(health.check_account(self.account))

    def test_the_loop_pauses_a_blocked_account(self):
        u = self.sending(2)
        UtskickSettings.objects.filter(account=self.account).update(
            email_blocked_at=timezone.now(), email_blocked_reason="bounces"
        )
        with transport.FakeSes() as ses:
            counts = self.run_email()
        self.assertEqual(ses.calls, [])
        self.assertEqual(counts.get("paused"), 1)
        u.refresh_from_db()
        self.assertEqual(u.pause_reason, Utskick.PauseReason.ACCOUNT_HEALTH)

    def test_adx_wide_alerts_and_reads_get_account(self):
        u = self.sending(1)
        self.outcomes(u, delivered=90, bounced=10)
        client = mock.Mock()
        client.get_account.return_value = {
            "ProductionAccessEnabled": True,
            "SendingEnabled": True,
            "EnforcementStatus": "HEALTHY",
            "SendQuota": {"Max24HourSend": 50000.0, "MaxSendRate": 14.0, "SentLast24Hours": 12.0},
        }
        with mock.patch("apps.utskick.aws.client", return_value=client):
            summary = health.adx_wide()
        self.assertTrue(summary["alert"])
        from .models import Switchboard

        switch = Switchboard.get_solo()
        self.assertEqual(switch.ses_max_rate, 14)
        self.assertEqual(switch.ses_daily_quota, 50000)
        self.assertEqual(switch.ses_account["EnforcementStatus"], "HEALTHY")
        self.assertIn("Utskick: e-posthälsan för hela ADX", [m.subject for m in mail.outbox])

    def test_adx_wide_without_aws(self):
        self.assertEqual(health.adx_wide()["ses"], "off")


# ---------------------------------------------------------------------------
# Leveranshälsa (I.11)
# ---------------------------------------------------------------------------


class HealthPageTests(EventFixture, TestCase):
    def test_the_page_shows_the_numbers_and_masks_addresses(self):
        u, (a, b, c) = self.sent(3)
        events.apply(bounce(a))
        events.apply(complaint(b))
        events.apply(bounce(c, kind="Transient", subtype="MailboxFull"))
        response = self.client_for(self.anna).get(reverse("flamingo:app_utskick_health"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Studs: finns inte", html)
        self.assertIn("Markerade som skräppost", html)
        self.assertIn("Tillfällig studs", html)
        self.assertNotIn(a.address, html)
        self.assertIn("Får aldrig mejl igen", html)
        self.assertIn("ADX-domänen", html)
        self.assertNotIn("style=", html.split("<main", 1)[-1])

    def test_a_blocked_account_and_a_paused_utskick(self):
        u = self.sending(1)
        UtskickSettings.objects.filter(account=self.account).update(email_blocked_at=timezone.now())
        state.pause(u, Utskick.PauseReason.BOUNCES, note="4,6 % av de första 500 mejlen studsade.")
        html = self.client_for(self.anna).get(reverse("flamingo:app_utskick_health")).content
        html = html.decode()
        self.assertIn(health.BLOCKED_TEXT, html)
        self.assertIn("4,6 % av de första 500 mejlen studsade.", html)

    def test_staff_and_other_accounts(self):
        staff = self.client_for(self.staff)
        self.assertEqual(staff.get(reverse("flamingo:app_utskick_health")).status_code, 200)
        UtskickSettings.objects.filter(account=self.account).update(is_enabled=False)
        anna = self.client_for(self.anna)
        self.assertEqual(anna.get(reverse("flamingo:app_utskick_health")).status_code, 404)
