"""Ticken och dygnsstädningen (README D.1, D.2, E.7, H.7, J S1
test_s1_tick): låset, nycklarna, hjärtslaget, faserna, monitor_check och
utskick_daily.

Inget når nätet: SES är FakeSes, och testkörningen sätter
UTSKICK_TICK_MAX_MB=0 så att kommandot inte sätter något minnestak på
testprocessen.
"""

import io
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.db import connections
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount

from . import consent as consents
from . import keys, limits, optin, retention
from .access import PERSON
from .email import transport
from .management.commands.utskick_tick import limit_memory
from .models import (
    CHANNEL_EMAIL,
    Consent,
    ConsentLog,
    Contact,
    Counter,
    Event,
    ExportLog,
    ImportJob,
    Switchboard,
)
from .sending import tick
from .testing import PHONE_ANNA, UtskickFixture, make_contact

AGENCY = {"INQUIRY_NOTIFICATION_EMAIL": "byran@adx.example"}
EMAIL = "ella.berg@hemma.example"


class TickFixture(UtskickFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Bekräftelsemejlen skickas bara när byrån klarmarkerat dem (optin.due).
        Switchboard.objects.update_or_create(
            pk=Switchboard.SOLO_PK, defaults={"doi_ready_at": timezone.now()}
        )

    def pending_email(self, account=None, address=EMAIL):
        kontakt = make_contact(account or self.account, first_name="Ella", email=address)
        consents.set_status(
            kontakt,
            CHANNEL_EMAIL,
            consents.PENDING,
            source=Consent.Source.SIGNUP,
            actor=PERSON,
            text_shown="Ja, jag vill få erbjudanden från Exempelrör via e-post.",
        )
        return kontakt

    def switch(self):
        return Switchboard.get_solo()


class TickTests(TickFixture, TestCase):
    def test_an_idle_tick_only_writes_the_heartbeat(self):
        summary = tick.run(budget=5)
        self.assertEqual(summary, {"status": "idle"})
        row = self.switch()
        self.assertIsNotNone(row.last_tick_at)
        self.assertEqual(row.last_tick_summary, {"status": "idle"})

    def test_a_second_tick_waits_for_the_lock(self):
        """Låset i Postgres (andra spärren bakom flock): en tick som redan
        kör, här en annan anslutning, gör att den här slutar direkt."""
        other = connections.create_connection("default")
        try:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [limits.TICK_LOCK])
            summary = tick.run(budget=5)
            self.assertEqual(summary, {"status": "locked"})
            self.assertIsNone(self.switch().last_tick_at)
        finally:
            with other.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [limits.TICK_LOCK])
            other.close()
        # Låset släpps efter en tick: nästa tick tar det igen.
        self.assertEqual(tick.run(budget=5)["status"], "idle")
        self.assertEqual(tick.run(budget=5)["status"], "idle")

    def test_the_doi_phase_sends_the_queue(self):
        self.pending_email()
        with transport.FakeSes() as ses:
            self.assertTrue(tick.work_exists())
            summary = tick.run(budget=10)
        self.assertEqual(summary["status"], "worked")
        self.assertEqual(summary["doi"]["sent"], 1)
        self.assertEqual(len(ses.calls), 1)
        row = self.switch()
        self.assertEqual(row.last_tick_summary["doi"]["sent"], 1)
        self.assertNotIn(EMAIL, str(row.last_tick_summary))

    def test_nothing_is_sent_without_a_transport(self):
        self.pending_email()
        self.assertFalse(transport.can_send())
        self.assertFalse(tick.work_exists())
        self.assertEqual(tick.run(budget=5)["status"], "idle")
        self.assertIsNone(Consent.objects.get(channel=CHANNEL_EMAIL).confirm_sent_at)

    def test_the_demo_never_sends(self):
        self.pending_email()
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        with transport.FakeSes() as ses:
            self.assertFalse(tick.work_exists())
            tick.run(budget=5)
        self.assertEqual(ses.calls, [])

    def test_the_import_phase_gets_the_time_that_is_left(self):
        with (
            mock.patch("apps.utskick.importer.work_exists", return_value=True),
            mock.patch(
                "apps.utskick.importer.import_chunk", return_value={"jobs": 1, "rows": 250}
            ) as chunk,
        ):
            summary = tick.run(budget=50)
        self.assertEqual(summary["import"], {"jobs": 1, "rows": 250})
        seconds = chunk.call_args.kwargs["seconds"]
        self.assertLessEqual(seconds, tick.IMPORT_SECONDS)
        self.assertGreater(seconds, tick.IMPORT_MIN_SECONDS)

    def test_no_import_phase_without_time_left(self):
        with (
            mock.patch("apps.utskick.importer.work_exists", return_value=True),
            mock.patch("apps.utskick.importer.import_chunk") as chunk,
            mock.patch("apps.utskick.sending.tick.time.monotonic", side_effect=[0, 0, 0, 100, 100]),
        ):
            summary = tick.run(budget=5)
        chunk.assert_not_called()
        self.assertNotIn("import", summary)

    def test_a_failing_phase_never_stops_the_next(self):
        with (
            mock.patch("apps.utskick.importer.work_exists", return_value=True),
            mock.patch("apps.utskick.optin.send_due", side_effect=RuntimeError("pang")),
            mock.patch(
                "apps.utskick.importer.import_chunk", return_value={"jobs": 1, "rows": 2}
            ) as chunk,
            self.assertLogs("apps.utskick.sending.tick", level="ERROR"),
        ):
            summary = tick.run(budget=30)
        chunk.assert_called_once()
        self.assertEqual(summary["failed"], ["doi"])
        self.assertIn("failed=doi", tick.summary_line(summary))
        self.assertIsNotNone(self.switch().last_tick_at)

    def test_an_import_left_by_a_request_is_taken_over(self):
        job = ImportJob.objects.create(
            account=self.account,
            original_name="kunder.csv",
            kind=ImportJob.Kind.CSV,
            status=ImportJob.Status.ANALYSING,
            in_request=True,
            started_at=timezone.now() - timedelta(minutes=30),
        )
        with mock.patch("apps.utskick.importer.import_chunk", return_value={"jobs": 0, "rows": 0}):
            summary = tick.run(budget=20)
        self.assertEqual(summary["recovered"], 1)
        job.refresh_from_db()
        self.assertFalse(job.in_request)


@override_settings(**AGENCY)
class KeyMismatchTests(TickFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.kontakt = self.pending_email()
        keys.forget_verified()
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(hash_fingerprint="0" * 64)
        mail.outbox = []

    def test_the_tick_refuses_to_send_and_alerts_the_agency(self):
        with transport.FakeSes() as ses:
            summary = tick.run(budget=5)
        self.assertEqual(summary, {"status": "keys"})
        self.assertEqual(ses.calls, [])
        self.assertIsNone(Consent.objects.get(channel=CHANNEL_EMAIL).confirm_sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["byran@adx.example"])
        self.assertIn(keys.MISMATCH_TEXT, mail.outbox[0].body)
        # Ingen tick räknas som gången: hjärtslaget skrivs inte.
        self.assertIsNone(self.switch().last_tick_at)

    def test_consent_and_suppression_writes_are_refused(self):
        logs = ConsentLog.objects.count()
        with self.assertRaises(keys.KeyMismatch):
            consents.set_status(
                self.kontakt, CHANNEL_EMAIL, consents.DECLINED, source=Consent.Source.MANUAL
            )
        self.assertEqual(ConsentLog.objects.count(), logs)


@override_settings(**AGENCY)
class HeartbeatTests(TickFixture, TestCase):
    def setUp(self):
        super().setUp()
        mail.outbox = []

    def test_a_stale_tick_with_work_alerts_once_an_hour(self):
        self.pending_email()
        Switchboard.get_solo()
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(
            last_tick_at=timezone.now() - timedelta(minutes=10)
        )
        with transport.FakeSes():
            self.assertTrue(tick.check_heartbeat())
            self.assertFalse(tick.check_heartbeat())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(tick.STALE_TEXT, mail.outbox[0].body)
        self.assertNotIn(EMAIL, mail.outbox[0].body)

    def test_a_tick_that_never_ran_counts_as_stale(self):
        self.pending_email()
        with transport.FakeSes():
            self.assertTrue(tick.check_heartbeat())

    def test_no_alert_without_work(self):
        Switchboard.get_solo()
        Switchboard.objects.filter(pk=Switchboard.SOLO_PK).update(
            last_tick_at=timezone.now() - timedelta(hours=3)
        )
        self.assertFalse(tick.check_heartbeat())
        self.assertEqual(mail.outbox, [])

    def test_no_alert_when_the_tick_is_alive(self):
        self.pending_email()
        with transport.FakeSes():
            tick.run(budget=5)
            self.pending_email(address="kim@hemma.example")
            self.assertFalse(tick.check_heartbeat())
        self.assertEqual(mail.outbox, [])

    def test_monitor_check_asks_about_the_tick(self):
        with mock.patch(
            "apps.utskick.sending.tick.check_heartbeat", return_value=False
        ) as heartbeat:
            call_command("monitor_check", stdout=io.StringIO())
        heartbeat.assert_called_once()
        with mock.patch("apps.utskick.sending.tick.check_heartbeat") as heartbeat:
            call_command("monitor_check", "--daily", "--skip-slow", stdout=io.StringIO())
        heartbeat.assert_not_called()

    def test_a_broken_heartbeat_never_breaks_monitoring(self):
        with mock.patch(
            "apps.utskick.sending.tick.check_heartbeat", side_effect=RuntimeError("pang")
        ):
            call_command("monitor_check", stdout=io.StringIO())


class CommandTests(TickFixture, TestCase):
    def test_a_line_only_when_the_tick_worked(self):
        out = io.StringIO()
        call_command("utskick_tick", stdout=out)
        self.assertEqual(out.getvalue(), "")
        self.pending_email()
        out = io.StringIO()
        with transport.FakeSes():
            call_command("utskick_tick", "--budget", "10", stdout=out)
        line = out.getvalue()
        self.assertIn("utskick_tick worked doi sent=1", line)
        self.assertNotIn(EMAIL, line)

    def test_verbose_always_writes_the_line(self):
        out = io.StringIO()
        call_command("utskick_tick", "--verbose", stdout=out)
        self.assertIn("utskick_tick idle", out.getvalue())

    def test_no_memory_cap_in_the_tests(self):
        self.assertFalse(limit_memory())


class DailyTests(TickFixture, TestCase):
    def test_retention_and_flags(self):
        now = timezone.now()
        old = now - timedelta(days=800)
        kontakt = make_contact(self.account, first_name="Bo", phone=PHONE_ANNA)
        Event.objects.create(account=self.account, contact=kontakt, kind=Event.SIGNUP, at=old)
        Event.objects.create(account=self.account, contact=kontakt, kind=Event.SIGNUP, at=now)
        ExportLog.objects.create(account=self.account, kind="contacts", rows=1, at=old)
        ExportLog.objects.create(account=self.account, kind="contacts", rows=1, at=now)
        Counter.objects.create(scope="signup_ip", key="x", window=now - timedelta(days=3))
        Counter.objects.create(scope="signup_ip", key="x", window=limits.hour_window(now))
        # Beviset för en borttagen kontakt: kvar i 36 månader, sedan bort.
        gone = make_contact(self.account, first_name="Cilla", email="cilla@hemma.example")
        consents.set_status(gone, CHANNEL_EMAIL, consents.DECLINED, source=Consent.Source.MANUAL)
        ConsentLog.objects.filter(contact=gone).update(at=now - timedelta(days=37 * 31))
        kept = ConsentLog.objects.filter(contact=kontakt).count()
        Contact.objects.filter(pk=gone.pk).delete()
        # Inaktiv: ingen grund och inte hörd av på två år.
        Contact.objects.filter(pk=kontakt.pk).update(created_at=old, last_activity_at=old)

        summary = retention.daily(now)

        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["exports"], 1)
        self.assertEqual(summary["counters"], 1)
        self.assertGreaterEqual(summary["consent_logs"], 1)
        self.assertFalse(ConsentLog.objects.filter(contact__isnull=True).exists())
        self.assertEqual(ConsentLog.objects.filter(contact=kontakt).count(), kept)
        self.assertEqual(summary["inactive"], {"flagged": 1, "cleared": 0})
        kontakt.refresh_from_db()
        self.assertIsNotNone(kontakt.inactive_flagged_at)
        self.assertIn("disk_free", summary)
        self.assertIn("contact", summary["tables_mb"])

    def test_a_contact_with_a_basis_is_never_flagged(self):
        old = timezone.now() - timedelta(days=800)
        kontakt = make_contact(self.account, first_name="Bo", phone=PHONE_ANNA)
        consents.set_status(
            kontakt,
            "sms",
            consents.EXISTING,
            source=Consent.Source.MANUAL,
            evidence="Kund sedan 2020",
        )
        Contact.objects.filter(pk=kontakt.pk).update(created_at=old, inactive_flagged_at=old)
        summary = retention.flag_inactive()
        self.assertEqual(summary, {"flagged": 0, "cleared": 1})

    @override_settings(**AGENCY)
    def test_low_disk_alerts_the_agency_once_a_day(self):
        mail.outbox = []
        usage = mock.Mock(total=100, used=90, free=10)
        with mock.patch("apps.utskick.retention.shutil.disk_usage", return_value=usage):
            self.assertEqual(retention.disk_check(), 0.1)
            retention.disk_check()
        self.assertEqual(len(mail.outbox), 1)

    def test_the_command_writes_counts_only(self):
        out = io.StringIO()
        call_command("utskick_daily", stdout=out)
        self.assertIn("utskick_daily", out.getvalue())
        self.assertIn('"imports"', out.getvalue())

    def test_the_months_are_calendar_months(self):
        now = timezone.make_aware(timezone.datetime(2026, 3, 31, 2, 45))
        self.assertEqual(retention.months_ago(now, 1).date().isoformat(), "2026-02-28")
        self.assertEqual(retention.months_ago(now, 25).date().isoformat(), "2024-02-29")

    def test_queue_counts_stay_without_addresses(self):
        self.pending_email()
        self.assertEqual(optin.due().count(), 1)
