"""
Taken och takten för e-posten (README D.6, D.9, I.5, I.6, F.8, J S3
test_s3_caps): ADX-domänens 2 000 mejl per kund och månad (utskick,
testmejl och svar, förkontrollen före första mejlet och slingan under
kontots lås), att en egen domän inte räknas, dygnstaket de första 14
dagarna (en väntan, ingen paus), provet med de första 200 och en timmes
väntan, och takten mot SES MaxSendRate.
"""

from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from . import limits
from .access import Actor
from .email import transport
from .inbound import events
from .models import Recipient, Switchboard, Utskick, UtskickSettings
from .sending import email as email_loop
from .sending import health
from .test_s3_events import bounce, delivery
from .test_s3_transport import EmailFixture, outgoing

RS = Recipient.Status
ANNA = Actor(label="Anna Lindqvist")


class AdxCapTests(EmailFixture, TestCase):
    def test_the_month_count_and_what_is_left(self):
        u = self.sending(3)
        with transport.FakeSes():
            self.run_email()
            email_loop.deliver(self.account, outgoing(), kind=transport.REPLY)
            email_loop.send_test(
                self.utskick(status=Utskick.Status.DRAFT),
                address="anna@exempelror.example",
                actor=ANNA,
            )
        self.assertEqual(email_loop.adx_month_count(self.account), 5)
        self.assertEqual(email_loop.adx_cap_left(self.account), 1995)
        # Ett annat konto räknar sitt eget.
        self.assertEqual(email_loop.adx_month_count(self.other_account), 0)
        # Mejl från förra månaden räknas inte.
        start, _end = email_loop.month_bounds(timezone.now())
        u.recipients.update(sent_at=start - timedelta(minutes=1))
        self.assertEqual(email_loop.adx_month_count(self.account), 2)

    def test_the_counter_survives_the_nightly_purge(self):
        with transport.FakeSes():
            email_loop.deliver(self.account, outgoing(), kind=transport.REPLY)
        limits.purge(timezone.now() - limits.KEEP)
        self.assertEqual(email_loop.adx_month_count(self.account), 1)

    @override_settings(UTSKICK_ADX_MONTHLY_MAIL_CAP=3)
    def test_an_utskick_that_does_not_fit_pauses_before_the_first_mail(self):
        self.people(4)
        u = self.freeze(self.utskick())
        self.assertEqual(u.status, Utskick.Status.PAUSED_CAP)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.ADX_MAIL_CAP)
        verdict = u.stats.get("pause") if isinstance(u.stats, dict) else None
        self.assertIsNone(verdict, "förkontrollen sparar ingen byråanteckning")
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(ses.calls, [])
        text = email_loop.adx_cap_text(self.account, u)
        self.assertIn("0 av 3 mejl från ADX-domänen", text)
        self.assertIn("behöver 4 till", text)
        self.assertIn("Verifiera din egen domän under Inställningar", text)

    @override_settings(UTSKICK_ADX_MONTHLY_MAIL_CAP=3)
    def test_the_loop_stops_at_the_cap(self):
        u = self.sending(2)
        with transport.FakeSes():
            email_loop.deliver(self.account, outgoing(), kind=transport.REPLY)
            email_loop.deliver(self.account, outgoing(), kind=transport.REPLY)
        # Två svar och ett mejl ur utskicket når taket 3; det andra pausar.
        with transport.FakeSes() as ses:
            counts = self.run_email()
        self.assertEqual(len(ses.calls), 1)
        self.assertEqual(counts.get("adx_cap"), 1)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED_CAP)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.ADX_MAIL_CAP)
        self.assertIn("3 av 3 mejl från ADX-domänen", u.stats["pause"]["note"])
        self.assertIn("utskicket behöver 1 till", u.stats["pause"]["note"])
        self.assertEqual(self.statuses(u), [RS.QUEUED, RS.SENT])

    @override_settings(UTSKICK_ADX_MONTHLY_MAIL_CAP=3)
    def test_a_test_mail_counts_and_is_refused_at_the_cap(self):
        draft = self.utskick(status=Utskick.Status.DRAFT)
        with transport.FakeSes() as ses:
            results = [
                email_loop.send_test(draft, address="anna@exempelror.example", actor=ANNA)
                for _ in range(4)
            ]
        self.assertEqual([r.ok for r in results], [True, True, True, False])
        self.assertEqual(results[-1].error, "adx_cap")
        self.assertEqual(len(ses.calls), 3)
        self.assertIn("Taket för ADX-domänen", email_loop.error_text(results[-1]))

    @override_settings(UTSKICK_ADX_MONTHLY_MAIL_CAP=2)
    def test_an_own_domain_is_never_counted(self):
        domain = self.verified_domain()
        UtskickSettings.objects.filter(account=self.account).update(
            email_probe_passed_at=timezone.now()
        )
        u = self.sending(4, sender_domain=domain)
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(len(ses.calls), 4)
        self.assertEqual(ses.messages[0]["From"].addresses[0].addr_spec, "hej@exempelror.example")
        self.assertEqual(self.statuses(u), [RS.SENT] * 4)
        self.assertEqual(email_loop.adx_month_count(self.account), 0)

    def test_an_unverified_domain_pauses_before_and_during_sending(self):
        domain = self.verified_domain()
        u = self.sending(2, sender_domain=domain)
        domain.status = domain.Status.FAILED
        domain.save(update_fields=["status"])
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(ses.calls, [])
        u.refresh_from_db()
        self.assertEqual(u.pause_reason, Utskick.PauseReason.PROVIDER)
        later = self.freeze(self.utskick(sender_domain=domain))
        self.assertEqual(later.pause_reason, Utskick.PauseReason.PROVIDER)


class DailyCapTests(EmailFixture, TestCase):
    def test_new_accounts_send_2000_a_day_for_two_weeks(self):
        self.assertEqual(health.daily_cap(self.account), health.RAMP_DAILY)
        first = timezone.now() - timedelta(days=13)
        UtskickSettings.objects.filter(account=self.account).update(email_first_sent_at=first)
        self.assertEqual(health.daily_cap(self.account), health.RAMP_DAILY)
        first = timezone.now() - timedelta(days=15)
        UtskickSettings.objects.filter(account=self.account).update(email_first_sent_at=first)
        self.assertIsNone(health.daily_cap(self.account))
        self.assertIsNone(health.daily_cap_left(self.account))

    def test_over_the_cap_the_utskick_waits_for_tomorrow(self):
        UtskickSettings.objects.filter(account=self.account).update(email_daily_cap=2)
        u = self.sending(3)
        with transport.FakeSes() as ses:
            counts = self.run_email()
        self.assertEqual(len(ses.calls), 2)
        self.assertEqual(counts.get("daily_cap"), 1)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SENDING, "en väntan, ingen paus")
        waiting = u.recipients.get(status=RS.QUEUED)
        self.assertEqual(waiting.not_before, health.next_day(timezone.now()))
        self.assertEqual(health.daily_cap_left(self.account), 0)
        self.assertIn("Fortsätter i morgon", health.daily_wait_text(self.account))

    def test_next_day_is_midnight_in_stockholm(self):
        from apps.sms.pricing import STOCKHOLM

        now = timezone.make_aware(timezone.datetime(2026, 10, 24, 23, 30), STOCKHOLM)
        tomorrow = timezone.localtime(health.next_day(now), STOCKHOLM)
        self.assertEqual((tomorrow.day, tomorrow.hour, tomorrow.minute), (25, 0, 0))
        # Natten när sommartiden slutar.
        now = timezone.make_aware(timezone.datetime(2026, 10, 25, 12, 0), STOCKHOLM)
        tomorrow = timezone.localtime(health.next_day(now), STOCKHOLM)
        self.assertEqual((tomorrow.day, tomorrow.hour), (26, 0))


class ProbeTests(EmailFixture, TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(health, "PROBE_SIZE", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_first_mails_then_an_hour(self):
        u = self.sending(4)
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(len(ses.calls), 2)
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.SENDING)
        self.assertIsNotNone(u.hold_until)
        self.assertAlmostEqual((u.hold_until - timezone.now()).total_seconds(), 3600, delta=60)
        queued = u.recipients.filter(status=RS.QUEUED)
        self.assertEqual(set(queued.values_list("not_before", flat=True)), {u.hold_until})
        self.assertIn("Väntar på de första svaren", health.wait_probe_text(u.hold_until))
        # Efter timmen, med leveranser: provet är godkänt och resten går.
        for recipient in u.recipients.filter(status=RS.SENT):
            events.apply(delivery(recipient))
        past = timezone.now() - timedelta(minutes=1)
        Utskick.objects.filter(pk=u.pk).update(hold_until=past)
        u.recipients.filter(status=RS.QUEUED).update(not_before=past)
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(len(ses.calls), 2)
        row = UtskickSettings.objects.get(account=self.account)
        self.assertIsNotNone(row.email_probe_passed_at)
        u.refresh_from_db()
        self.assertIsNone(u.hold_until)

    def test_a_probe_with_bounces_pauses(self):
        u = self.sending(4)
        with transport.FakeSes():
            self.run_email()
        for recipient in u.recipients.filter(status=RS.SENT):
            events.apply(bounce(recipient))
        past = timezone.now() - timedelta(minutes=1)
        Utskick.objects.filter(pk=u.pk).update(hold_until=past)
        u.recipients.filter(status=RS.QUEUED).update(not_before=past)
        with transport.FakeSes() as ses:
            self.run_email()
        self.assertEqual(ses.calls, [])
        u.refresh_from_db()
        self.assertEqual(u.status, Utskick.Status.PAUSED_HEALTH)
        self.assertEqual(u.pause_reason, Utskick.PauseReason.BOUNCES)
        self.assertIsNone(UtskickSettings.objects.get(account=self.account).email_probe_passed_at)

    def test_a_passed_account_with_a_new_domain_probes_again(self):
        UtskickSettings.objects.filter(account=self.account).update(
            email_probe_passed_at=timezone.now()
        )
        plain = self.utskick(status=Utskick.Status.DRAFT)
        self.assertFalse(health.probe_needed(plain))
        domain = self.verified_domain(probe_passed_at=None)
        fresh = self.utskick(status=Utskick.Status.DRAFT, sender_domain=domain)
        self.assertTrue(health.probe_needed(fresh))


class RateTests(EmailFixture, TestCase):
    def test_eighty_percent_of_the_ses_rate(self):
        switch = Switchboard.get_solo()
        with override_settings(UTSKICK_EMAIL_PER_SECOND=10):
            switch.ses_max_rate = 0
            self.assertEqual(email_loop.rate_for(switch), 10)
            switch.ses_max_rate = 14
            self.assertEqual(email_loop.rate_for(switch), 10)
            switch.ses_max_rate = 5
            self.assertEqual(email_loop.rate_for(switch), 4)

    def test_the_pace_waits_between_mails(self):
        ctx = email_loop.Context(rate=4)
        with mock.patch("apps.utskick.sending.email.time.sleep") as sleep:
            ctx.pace()
            ctx.pace()
        self.assertEqual(sleep.call_count, 1)
        self.assertAlmostEqual(sleep.call_args[0][0], 0.25, delta=0.05)
