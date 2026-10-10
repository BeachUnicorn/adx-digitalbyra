"""
Svaren direkt (apps/utskick/sending/kick.py, README G.1 punkt 3,
Giovannis beställning 2026-10-10): efter webbanropet för inkommande sms
skickas svaret på STOPP/START och ägarens sms direkt, med samma funktion
och samma spärrar som tickens fas 3, i stället för upp till en minut senare.

    SettingTests        UTSKICK_KICK är av i testerna och på som standard
    WebhookTests        webbanropet startar knuffen efter commit när något
                        köats, aldrig för demot, inte för en dubblett eller
                        avstämningen, och inte alls när inställningen är av
    RunTests            kick.run synkront: STOPP- och START-svaret, ägarens
                        sms (med väntan tills svaret får tas med), ett STOPP
                        under väntan besvaras direkt, nycklarna, demot, och
                        ticken skickar inget en gång till
    StartTests          tråden: kör run, tar ett varv till i stället för en
                        andra tråd, väcker en knuff som väntar, startar en ny
                        tråd när budgeten är slut, stänger sin anslutning
    ConcurrentTests     ticken (fas 3) och knuffen samtidigt: ett sms
"""

import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.flamingo.models import FlamingoAccount, SmsLog
from apps.sms.models import SmsMessage

from . import threads
from .inbound import elks
from .models import InboundMessage, ThreadMessage, UtskickSettings
from .sending import kick, sms_wrapper
from .test_s2_inbound import InboundFixture
from .testing import PHONE_ANNA, PHONE_BO

STOP_TEXT = "Du får inga fler sms från Exempelrör. Svara START om du ångrar dig."


def reset_state():
    with kick._lock:
        kick._state.update(running=False, again=False, notice_at=None)
        kick._wake.clear()


class SettingTests(SimpleTestCase):
    def test_off_in_the_tests_and_on_by_default(self):
        self.assertFalse(settings.UTSKICK_KICK)
        self.assertFalse(kick.enabled())
        source = (settings.BASE_DIR / "config" / "settings" / "base.py").read_text("utf-8")
        self.assertIn('UTSKICK_KICK = env.bool("UTSKICK_KICK", default=True)', source)
        with override_settings(UTSKICK_KICK=True):
            self.assertTrue(kick.enabled())


class KickFixture(InboundFixture):
    def setUp(self):
        super().setUp()
        reset_state()
        self.addCleanup(reset_state)
        type(self.account).objects.filter(pk=self.account.pk).update(notify_phone="0701740699")
        self.account.refresh_from_db()

    def sends(self):
        return [c for c in self.fake.calls if c.get("dryrun") != "yes"]

    def owner_sms(self):
        return list(SmsLog.objects.filter(kind=SmsLog.KIND_OWNER).order_by("pk"))


# ---------------------------------------------------------------------------
# Webbanropet startar knuffen
# ---------------------------------------------------------------------------


@override_settings(UTSKICK_KICK=True)
class WebhookTests(KickFixture, TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(kick, "start")
        self.start = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_stopp_kicks_after_commit_without_a_notice(self):
        self.send_out()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(self.url(), self.fields("STOPP"))
        self.assertEqual((response.status_code, response.content), (200, b""))
        # Inget i själva förfrågan: knuffen startar först efter commit.
        self.start.assert_not_called()
        for callback in callbacks:
            callback()
        self.start.assert_called_once_with(notice_at=None)
        self.assertEqual(self.sends(), [])

    def test_a_reply_kicks_with_the_time_the_notice_may_go(self):
        self.send_out()
        self.post("Har ni tid tisdag?")
        inbound = InboundMessage.objects.get()
        self.assertEqual(inbound.status, InboundMessage.Status.ROUTED)
        notice_at = self.start.call_args.kwargs["notice_at"]
        self.assertEqual(notice_at, inbound.created_at + threads.NOTICE_SETTLE + kick.SETTLE_MARGIN)

    def test_start_kicks_for_the_confirmation_link(self):
        self.send_out()
        self.post("STOPP")
        self.start.reset_mock()
        self.post("START")
        self.start.assert_called_once_with(notice_at=None)

    def test_a_duplicate_and_the_reconcile_never_kick(self):
        self.send_out()
        data = self.fields("STOPP")
        self.post("", data=data)
        self.start.reset_mock()
        self.post("", data=data)
        self.start.assert_not_called()
        # Avstämningen (ticken) anropar handle direkt: fas 3 kommer strax efter.
        self.handle("STOPP igen")
        self.start.assert_not_called()

    def test_nothing_queued_gives_no_kick(self):
        # Ett nummer utan kandidat: väntar på byrån, inget svar i kö.
        self.post("Hej", frm="+46701740699")
        self.assertEqual(InboundMessage.objects.get().status, InboundMessage.Status.UNROUTABLE)
        self.start.assert_not_called()

    def test_the_demo_is_never_kicked(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.send_out()
        self.post("STOPP")
        self.assertTrue(
            ThreadMessage.objects.filter(direction="out", status="sending").exists(),
            "svaret står i kö (ticken säger nej till demot)",
        )
        self.post("Har ni tid?")
        self.start.assert_not_called()

    @override_settings(UTSKICK_KICK=False)
    def test_off_means_only_the_tick(self):
        self.send_out()
        self.post("STOPP")
        self.start.assert_not_called()
        self.assertTrue(threads.answers_queued().exists())

    def test_a_failing_kick_never_fails_the_webhook(self):
        self.send_out()
        with (
            mock.patch.object(kick, "wanted", side_effect=RuntimeError("trasig")),
            self.assertLogs("apps.utskick.sending.kick", "ERROR"),
        ):
            response = self.post("STOPP")
        self.assertEqual((response.status_code, response.content), (200, b""))
        self.assertEqual(InboundMessage.objects.get().status, InboundMessage.Status.STOP)


# ---------------------------------------------------------------------------
# Knuffen, synkront
# ---------------------------------------------------------------------------


class RunTests(KickFixture, TestCase):
    def test_the_stopp_answer_goes_at_once_and_the_tick_sends_nothing_more(self):
        self.send_out()
        inbound = self.handle("STOPP")
        summary = kick.run()
        self.assertEqual((summary["status"], summary["answers"]), ("worked", 1))
        (send,) = self.sends()
        self.assertEqual((send["to"], send["message"]), ("+46701740601", STOP_TEXT))
        answer = ThreadMessage.objects.get(direction="out", inbound=inbound)
        self.assertEqual(answer.status, ThreadMessage.Status.SENT)
        self.assertEqual(answer.sms_message.reference, f"~x{inbound.pk}")
        self.assertEqual(
            InboundMessage.objects.get(pk=inbound.pk).meta["answers"],
            {str(self.account.pk): "sent"},
        )
        # Ticken (fas 3) efteråt: inget en gång till.
        self.assertFalse(threads.answers_queued().exists())
        threads.send_due(timezone.now())
        self.assertEqual(len(self.sends()), 1)

    def test_the_start_link_goes_at_once(self):
        self.send_out()
        self.handle("STOPP")
        kick.run()
        self.handle("START")
        kick.run()
        self.assertEqual(len(self.sends()), 2)
        self.assertIn("Klicka för att få sms från Exempelrör igen: ", self.sends()[1]["message"])

    def test_the_owner_notice_waits_until_the_reply_may_be_counted(self):
        self.send_out()
        start = timezone.now()
        inbound = self.handle("Har ni tid tisdag?", now=start)
        notice_at = inbound.created_at + threads.NOTICE_SETTLE + kick.SETTLE_MARGIN
        clock = {"offset": 0.0}
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            clock["offset"] += seconds

        fake_tz = SimpleNamespace(now=lambda: timezone.now() + timedelta(seconds=clock["offset"]))
        with (
            mock.patch.object(kick, "_sleep", side_effect=fake_sleep),
            mock.patch.object(kick, "timezone", fake_tz),
        ):
            summary = kick.run(notice_at=notice_at)
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 4.5)
        self.assertLessEqual(slept[0], 5.4)
        self.assertEqual(summary["notices"], 1)
        (row,) = self.owner_sms()
        self.assertTrue(row.body.startswith("Svar på utskick från Anna Lind, 070-174 06 01: "))
        self.assertIn("Har ni tid tisdag?", row.body)

    def test_a_stopp_during_the_wait_is_answered_at_once(self):
        """En knuff som kommer medan knuffen väntar in ägarsmset väcker den:
        STOPP-svaret går direkt, och ägarsmset när det får gå."""
        self.send_out()
        self.send_out(to=PHONE_BO, utskick=None)
        inbound = self.handle("Har ni tid tisdag?")
        notice_at = inbound.created_at + threads.NOTICE_SETTLE + kick.SETTLE_MARGIN
        clock = {"offset": 0.0}
        slept, sent_when_waiting_again = [], []

        def fake_sleep(seconds):
            slept.append(seconds)
            if len(slept) == 1:
                # Bo svarar STOPP efter en sekund; webbanropets knuff väcker.
                self.handle("STOPP", frm=PHONE_BO)
                clock["offset"] += 1
                return True
            sent_when_waiting_again.append([c["to"] for c in self.sends()])
            clock["offset"] += seconds
            return False

        fake_tz = SimpleNamespace(now=lambda: timezone.now() + timedelta(seconds=clock["offset"]))
        with (
            mock.patch.object(kick, "_sleep", side_effect=fake_sleep),
            mock.patch.object(kick, "timezone", fake_tz),
        ):
            summary = kick.run(notice_at=notice_at)
        self.assertEqual(len(slept), 2)
        self.assertEqual(sent_when_waiting_again, [["+46701740602"]])
        self.assertLess(slept[1], slept[0])
        self.assertEqual((summary["answers"], summary["notices"]), (1, 1))
        (row,) = self.owner_sms()
        self.assertIn("Har ni tid tisdag?", row.body)

    def test_an_old_enough_reply_needs_no_wait(self):
        self.send_out()
        start = timezone.now() - timedelta(seconds=30)
        self.handle("Har ni tid tisdag?", now=start)
        with mock.patch.object(kick, "_sleep") as sleep:
            summary = kick.run(notice_at=start + threads.NOTICE_SETTLE)
        sleep.assert_not_called()
        self.assertEqual(summary["notices"], 1)
        self.assertEqual(len(self.owner_sms()), 1)

    def test_no_wait_when_no_notice_is_due(self):
        UtskickSettings.objects.filter(account=self.account).update(notify_on_reply=False)
        self.send_out()
        inbound = self.handle("Har ni tid tisdag?")
        with mock.patch.object(kick, "_sleep") as sleep:
            summary = kick.run(notice_at=inbound.created_at + threads.NOTICE_SETTLE)
        sleep.assert_not_called()
        self.assertEqual(summary["notices"], 0)
        self.assertEqual(self.owner_sms(), [])

    def test_no_wait_past_the_budget(self):
        self.send_out()
        inbound = self.handle("Har ni tid tisdag?")
        with mock.patch.object(kick, "_sleep") as sleep:
            kick.run(
                notice_at=inbound.created_at + threads.NOTICE_SETTLE,
                deadline=time.monotonic() + 2,
            )
        sleep.assert_not_called()
        self.assertEqual(self.owner_sms(), [], "ticken tar ägarsmset")

    def test_wrong_keys_send_nothing(self):
        self.send_out()
        self.handle("STOPP")
        with mock.patch("apps.utskick.keys.check_fingerprints", return_value=False):
            self.assertEqual(kick.run(), {"status": "keys"})
        self.assertEqual(self.sends(), [])
        self.assertTrue(threads.answers_queued().exists())

    def test_the_demo_never_sends(self):
        FlamingoAccount.objects.filter(pk=self.account.pk).update(is_demo=True)
        self.send_out()
        inbound = self.handle("STOPP")
        kick.run()
        self.assertEqual(self.sends(), [])
        answer = ThreadMessage.objects.get(direction="out", inbound=inbound)
        self.assertEqual(answer.status, ThreadMessage.Status.FAILED)

    def test_a_claimed_answer_is_skipped(self):
        """Ett svar som en annan körning håller (_claimed) hoppas över."""
        self.send_out()
        self.handle("STOPP")
        answer = threads.answers_queued().get()
        with mock.patch.object(threads, "_claimed") as claimed:
            claimed.return_value.__enter__.return_value = False
            claimed.return_value.__exit__.return_value = False
            kick.run()
        claimed.assert_called_with(answer.pk)
        self.assertEqual(self.sends(), [])
        self.assertTrue(threads.answers_queued().exists())


# ---------------------------------------------------------------------------
# Tråden
# ---------------------------------------------------------------------------


class StartTests(SimpleTestCase):
    def setUp(self):
        reset_state()
        self.addCleanup(reset_state)
        patcher = mock.patch.object(kick, "connection")
        self.connection = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_thread_runs_the_kick_and_closes_its_connection(self):
        with mock.patch.object(kick, "run") as run:
            thread = kick.start(notice_at="vid")
            thread.join(5)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["notice_at"], "vid")
        self.connection.close.assert_called_once_with()
        self.assertFalse(kick._state["running"])
        self.assertTrue(thread.daemon)

    def test_a_second_kick_while_running_takes_one_more_round_not_a_thread(self):
        inside, release = threading.Event(), threading.Event()
        calls = []

        def slow_run(**kwargs):
            calls.append(kwargs)
            inside.set()
            release.wait(5)

        with mock.patch.object(kick, "run", side_effect=slow_run):
            thread = kick.start()
            self.assertTrue(inside.wait(5))
            self.assertIsNone(kick.start(notice_at="senare"))
            self.assertIsNone(kick.start())
            release.set()
            thread.join(5)
        self.assertEqual(
            calls,
            [{"notice_at": None, "deadline": mock.ANY}]
            + [{"notice_at": "senare", "deadline": mock.ANY}],
        )
        self.assertFalse(kick._state["running"])

    def test_a_kick_while_waiting_wakes_the_running_one(self):
        inside = threading.Event()
        woke = []

        def waiting_run(**kwargs):
            if not woke:
                inside.set()
                woke.append(kick._sleep(5))

        started = time.monotonic()
        with mock.patch.object(kick, "run", side_effect=waiting_run) as run:
            thread = kick.start(notice_at="vid")
            self.assertTrue(inside.wait(5))
            self.assertIsNone(kick.start())
            thread.join(5)
        self.assertEqual(woke, [True])
        self.assertLess(time.monotonic() - started, 4)
        # Och ett varv till för knuffen som väckte (again).
        self.assertEqual(run.call_count, 2)
        self.assertFalse(kick._state["running"])

    def test_a_kick_after_the_budget_gets_a_fresh_thread(self):
        """En knuff som kommer under det sista varvet när budgeten är slut
        lämnas inte åt ticken: en ny tråd med egen budget tar den."""
        seen = []

        def run_once(**kwargs):
            seen.append(threading.current_thread())
            if len(seen) == 1:
                kick.start(notice_at="senare")

        with (
            mock.patch.object(kick, "KICK_SECONDS", 0),
            mock.patch.object(kick, "run", side_effect=run_once) as run,
        ):
            kick.start().join(5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(seen) < 2:
                time.sleep(0.01)
            self.assertEqual(len(seen), 2)
            seen[1].join(5)
        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0], seen[1])
        self.assertEqual(run.call_args_list[1].kwargs["notice_at"], "senare")
        self.assertFalse(kick._state["running"])
        self.assertIsNone(kick._state["notice_at"])
        self.assertEqual(self.connection.close.call_count, 2)

    def test_a_crash_in_run_is_logged_and_the_next_kick_can_start(self):
        with mock.patch.object(kick, "run", side_effect=RuntimeError("trasig")):
            with self.assertLogs("apps.utskick.sending.kick", "ERROR"):
                kick.start().join(5)
        self.assertFalse(kick._state["running"])
        with mock.patch.object(kick, "run") as run:
            kick.start().join(5)
        run.assert_called_once()


# ---------------------------------------------------------------------------
# Ticken och knuffen samtidigt
# ---------------------------------------------------------------------------


class ConcurrentTests(KickFixture, TransactionTestCase):
    """Riktiga anslutningar i två trådar: ticken (fas 3, threads.send_due)
    och knuffen (kick.run) tar samma köade STOPP-svar. Den första som tar
    det håller det under sändningen; den andra hoppar över det."""

    def setUp(self):
        # TransactionTestCase kör inte setUpTestData.
        type(self).setUpTestData()
        super().setUp()
        self.send_out()
        self.inbound = self.handle("STOPP")
        self.real_send = sms_wrapper.send

    def handle(self, text, frm=PHONE_ANNA, now=None, **extra):
        """Utan testets transaktion: handle gör commit som i webbanropet."""
        message, _ = elks.handle(self.fields(text, frm, **extra), now=now)
        message.refresh_from_db()
        return message

    def race(self, first, second):
        inside, release = threading.Event(), threading.Event()
        calls = []
        errors = []

        def held_send(*args, **kwargs):
            calls.append(kwargs.get("reference"))
            if len(calls) == 1:
                inside.set()
                release.wait(10)
            return self.real_send(*args, **kwargs)

        def runner(func):
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - rapporteras av testet
                errors.append(exc)
            finally:
                connection.close()

        with mock.patch.object(sms_wrapper, "send", side_effect=held_send):
            one = threading.Thread(target=runner, args=(first,))
            one.start()
            self.assertTrue(inside.wait(10), "den första körningen kom aldrig fram till sms:et")
            two = threading.Thread(target=runner, args=(second,))
            two.start()
            two.join(10)
            release.set()
            one.join(10)
        self.assertEqual(errors, [])
        return calls

    def assertSentOnce(self, calls):
        self.assertEqual(calls, [f"x{self.inbound.pk}"])
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(SmsMessage.objects.filter(reference=f"~x{self.inbound.pk}").count(), 1)
        answer = ThreadMessage.objects.get(direction="out", inbound=self.inbound)
        self.assertEqual(answer.status, ThreadMessage.Status.SENT)
        self.assertFalse(threads.answers_queued().exists())

    def test_tick_first_then_kick(self):
        calls = self.race(lambda: threads.send_due(timezone.now()), kick.run)
        self.assertSentOnce(calls)

    def test_kick_first_then_tick(self):
        calls = self.race(kick.run, lambda: threads.send_due(timezone.now()))
        self.assertSentOnce(calls)
        # Och efteråt, en gång till var: fortfarande ett.
        kick.run()
        threads.send_due(timezone.now())
        self.assertEqual(len(self.sends()), 1)

    def test_the_webhook_kick_in_a_real_thread(self):
        """Webbanropet med knuffen på: tråden skickar STOPP-svaret och,
        när svaret fått vänta in NOTICE_SETTLE (kortad här), ägarens sms."""
        self.send_out(to=PHONE_BO, utskick=None)
        with (
            override_settings(UTSKICK_KICK=True),
            mock.patch.object(threads, "NOTICE_SETTLE", timedelta(milliseconds=400)),
        ):
            response = self.client.post(self.url(), self.fields("Har ni tid i morgon?", PHONE_BO))
            self.assertEqual((response.status_code, response.content), (200, b""))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and kick._state["running"]:
                time.sleep(0.05)
        self.assertFalse(kick._state["running"])
        # STOPP-svaret från setUp gick med samma knuff, och ägarsmset om Bos svar.
        self.assertEqual([c["message"] for c in self.sends()], [STOP_TEXT])
        self.assertFalse(threads.answers_queued().exists())
        (row,) = self.owner_sms()
        self.assertIn("Har ni tid i morgon?", row.body)
